"""Raw recording: one JSON line per message, one file per stream per UTC hour.

Line: {"r": receive time (ns since epoch, server clock), "s": stream,
       "n": [normalised events], "d": the exchange's message, untouched}
Normalised events carry the exchange's own timestamp in "e" (ms), so both
timestamps are kept for every message. Sizes in "n" are in base units (shares, HYPE).

Path: <root>/<stream>/<YYYYMMDD>/<stream>_<YYYYMMDD>_<HH>.jsonl  (gzipped once the hour ends)
"""
from __future__ import annotations

import glob
import gzip
import json
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from typing import Iterator


def _hour_key(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, timezone.utc).strftime("%Y%m%d_%H")


def _gzip_file(path: str) -> None:
    try:
        with open(path, "rb") as src, gzip.open(path + ".gz.tmp", "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        os.replace(path + ".gz.tmp", path + ".gz")
        os.remove(path)
    except Exception as e:  # leave the plain file in place; nothing is lost
        print("gzip failed for %s: %s" % (path, e), flush=True)


class RawWriter:
    def __init__(self, root: str):
        self.root = root
        self.files: dict = {}   # stream -> (hour_key, file, path)
        self.counts: dict = {}
        os.makedirs(root, exist_ok=True)
        cur = _hour_key(time.time_ns())
        for p in glob.glob(os.path.join(root, "*", "*", "*.jsonl")):   # left over from a crash
            if not p.endswith("_%s.jsonl" % cur):
                threading.Thread(target=_gzip_file, args=(p,), daemon=True).start()

    def write(self, stream: str, recv_ns: int, norm: list, raw: str | None) -> None:
        hk = _hour_key(recv_ns)
        cur = self.files.get(stream)
        if cur is None or cur[0] != hk:
            if cur is not None:
                cur[1].close()
                threading.Thread(target=_gzip_file, args=(cur[2],), daemon=True).start()
            d = os.path.join(self.root, stream, hk[:8])
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, "%s_%s.jsonl" % (stream, hk))
            cur = (hk, open(path, "a", buffering=1 << 16), path)
            self.files[stream] = cur
        cur[1].write('{"r":%d,"s":"%s","n":%s,"d":%s}\n' % (
            recv_ns, stream, json.dumps(norm, separators=(",", ":")), raw if raw else "null"))
        self.counts[stream] = self.counts.get(stream, 0) + 1

    def flush(self) -> None:
        for _, f, _ in self.files.values():
            f.flush()

    def close(self) -> None:
        for _, f, _ in self.files.values():
            f.close()
        self.files.clear()


def hour_files(root: str, stream: str, t0_ms: float, t1_ms: float) -> list:
    """Files of one stream that overlap [t0, t1], oldest first."""
    out = []
    h = int(t0_ms // 3600000)
    while h * 3600000 <= t1_ms:
        hk = _hour_key(h * 3600000 * 1000000)
        base = os.path.join(root, stream, hk[:8], "%s_%s.jsonl" % (stream, hk))
        if os.path.exists(base + ".gz"):       # the .gz appears only once it is complete
            out.append(base + ".gz")
        elif os.path.exists(base):
            out.append(base)
        h += 1
    return out


def read_file(path: str, with_raw: bool = False, only: tuple | None = None) -> Iterator[tuple]:
    """Yields (recv_ms, normalised event[, raw]) for every event in a file.
    `only` limits it to some event types, e.g. ("bbo", "conn"), and skips the other lines unparsed."""
    op = gzip.open if path.endswith(".gz") else open
    marks = tuple('"c":"%s"' % c for c in only) if only else None
    try:
        with op(path, "rt") as f:
            for line in f:
                try:
                    cut = line.index(',"d":')            # the normalised part ends here
                    head = line[:cut]
                    if marks and not any(m in head for m in marks):
                        continue
                    if with_raw:
                        j = json.loads(line)
                        r, norm, raw = j["r"] / 1e6, j["n"], j["d"]
                    else:                                # do not parse the exchange's message at all
                        j = json.loads(head + "}")
                        r, norm, raw = j["r"] / 1e6, j["n"], None
                except ValueError:
                    continue   # a torn last line after a crash
                for n in norm:
                    if only and n["c"] not in only:
                        continue
                    yield (r, n, raw) if with_raw else (r, n)
    except (EOFError, OSError):
        return


def scan_heads(root: str, stream: str, t0_ms: float, t1_ms: float) -> Iterator[tuple]:
    """Yields (recv_ms, normalised part as text) per line, unparsed, so callers can skip cheaply."""
    for p in hour_files(root, stream, t0_ms, t1_ms):
        op = gzip.open if p.endswith(".gz") else open
        try:
            with op(p, "rt") as f:
                for line in f:
                    try:
                        r = int(line[5:line.index(",", 5)]) / 1e6
                        head = line[:line.index(',"d":')]
                    except ValueError:
                        continue
                    if t0_ms <= r <= t1_ms:
                        yield r, head
        except (EOFError, OSError):
            continue


def read_events(root: str, stream: str, t0_ms: float, t1_ms: float) -> Iterator[tuple]:
    for p in hour_files(root, stream, t0_ms, t1_ms):
        for r, n in read_file(p):
            if t0_ms <= r <= t1_ms:
                yield r, n


def list_streams(root: str) -> list:
    return sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))) if os.path.isdir(root) else []


def data_span(root: str) -> tuple:
    """(first_hour_ms, last_hour_end_ms) across all streams, from file names."""
    keys = []
    for p in glob.glob(os.path.join(root, "*", "*", "*.jsonl*")):
        b = os.path.basename(p).split(".jsonl")[0]
        keys.append(b[-11:])
    if not keys:
        return (0.0, 0.0)
    f = lambda k: datetime.strptime(k, "%Y%m%d_%H").replace(tzinfo=timezone.utc).timestamp() * 1000
    return (f(min(keys)), f(max(keys)) + 3600000)
