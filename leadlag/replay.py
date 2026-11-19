"""Re-run the shadow engine over a recording.

    python -m leadlag.replay --out data/shadow/replay.csv [--clock exch] [--lags-ms 100,250,500,180]

Use --lags-ms to add the server's measured order round trip to Hyperliquid as a delay.
--clock recv uses the server's receive times (what a bot on this server would see);
--clock exch uses the exchanges' own timestamps.
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone

from . import config
from .engine import CsvSink, PairCfg, PairEngine
from .store import data_span, hour_files, list_streams, read_file


def _first_inst(root: str, stream: str, t0: float, t1: float):
    for p in hour_files(root, stream, t0, t1):
        for _, n in read_file(p, only=("bbo",)):
            return n["i"]
    return None


def build_pairs(cfg: dict, root: str, t0: float, t1: float) -> list:
    okx, hl = "okx:" + cfg["okx"]["ant_swap"], "hl:" + cfg["hl"]["ant"]
    fees = cfg["fees_bps"]
    pairs = [PairCfg("okx>hl", okx, hl, fees["hl"]), PairCfg("hl>okx", hl, okx, fees["okx"])]
    for v in ("binance", "bybit", "gate"):
        if v in list_streams(root):
            inst = _first_inst(root, v, t0, t1)
            if inst:
                pairs.append(PairCfg("okx>" + v, okx, "%s:%s" % (v, inst), fees[v]))
    return pairs


def run(cfg: dict, root: str, out: str, clock: str = "recv", t0: float = 0.0, t1: float = 0.0, pairs=None,
        append: bool = False, rule0=None, sig_lt: float | None = None) -> tuple:
    """Replays [t0, t1]. Returns (engines, last ruleType seen).
    append: add to `out` (otherwise it is replaced). rule0: ruleType known before t0.
    sig_lt: keep only rows whose signal is before this time (used to cut whole hours cleanly)."""
    span = data_span(root)
    t0, t1 = t0 or span[0], t1 or span[1]
    pairs = pairs or build_pairs(cfg, root, t0, t1)
    state = {"rule": rule0}

    def conv_tag(_t):
        if state["rule"] is None:
            return "unknown"
        return "pre_conversion" if state["rule"] == "pre_market" else "post_conversion"

    if os.path.exists(out) and not append:
        os.remove(out)
    csv_sink = CsvSink(out)
    sink = csv_sink if sig_lt is None else (lambda row: csv_sink(row) if row["signal_ts_ms"] < sig_lt else None)
    params = config.engine_params(cfg)
    engines = [PairEngine(p, params, sink, conv_tag, clock) for p in pairs]
    by_feed: dict = {}
    for e in engines:
        for f in (e.cfg.leader, e.cfg.follower):
            by_feed.setdefault(f, []).append(e)
    streams = sorted({f.split(":", 1)[0] for f in by_feed})
    h = int(t0 // 3600000)
    while h * 3600000 < t1:
        h0 = h * 3600000.0
        evs = []
        for s in streams:
            for p in hour_files(root, s, h0, h0):
                for r, n in read_file(p, only=("bbo", "conn", "instruments")):
                    c = n["c"]
                    if c == "bbo":
                        feed = s + ":" + n["i"]
                        if feed in by_feed:
                            t = n["e"] if clock == "exch" and n.get("e") else r
                            evs.append((t, r, 1, feed, n))
                    elif c == "conn":
                        evs.append((r, r, 0, s, n))
                    elif c == "instruments":
                        evs.append((r, r, 0, s, n))
        evs.sort(key=lambda x: (x[0], x[1]))
        for t, r, kind, key, n in evs:
            if not (t0 <= r <= t1):
                continue
            if kind == 1:
                for e in by_feed[key]:
                    e.on_bbo(key, t, n["b"], n["B"], n["a"], n["A"])
            elif n["c"] == "conn":
                for feed, engs in by_feed.items():
                    if feed.startswith(key + ":"):
                        for e in engs:
                            e.reset_feed(feed)
            else:
                state["rule"] = n["f"].get("ruleType")
        h += 1
    for e in engines:          # everything up to t1 has been seen: settle what is due by then
        e.advance(t1)
    csv_sink.close()
    return engines, state["rule"]


def _ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp() * 1000 if s else 0.0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--data-dir")
    ap.add_argument("--out", required=True)
    ap.add_argument("--clock", choices=("recv", "exch"), default="recv")
    ap.add_argument("--start", default="", help="UTC, e.g. 2026-11-19T14:00:00Z")
    ap.add_argument("--end", default="")
    ap.add_argument("--lags-ms", help="comma list, replaces the configured delays")
    a = ap.parse_args()
    cfg = config.load(a.config)
    if a.lags_ms:
        cfg["engine"]["lags_ms"] = [float(x) for x in a.lags_ms.split(",")]
    engines, _ = run(cfg, a.data_dir or cfg["data_dir"], a.out, a.clock, _ts(a.start), _ts(a.end))
    for e in engines:
        print("%-10s signals %5d  rows %6d  unfinished at end %d" % (e.cfg.name, e.n_signals, e.n_rows, len(e.pending)))
    from .report import aggregate, shadow_section
    print("\n".join(shadow_section(aggregate(a.out), {}, None, None, full=True)))


if __name__ == "__main__":
    main()
