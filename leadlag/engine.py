"""Shadow engine: "leader moves, take the follower".

Pure and event-driven: feed it best-bid/offer updates and a clock, it writes one
row per signal and parameter combination. The same code runs live (inside the
recorder) and in replay. It cannot place orders; it has no network access at all.
"""
from __future__ import annotations

import csv
import heapq
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from . import VERSION
from .hours import hours_tag

COLUMNS = [
    "version", "pair", "leader", "follower", "clock",
    "signal_ts_ms", "signal_utc", "dir", "x_bps", "w_ms", "lag_ms", "hold_s",
    "leader_move_bps", "leader_mid", "follower_mid_at_signal", "follower_premove_bps", "gap_bps",
    "entry_ts_ms", "entry_px", "entry_disp_sz", "entry_quote_age_ms", "entry_spread_bps", "size_usd",
    "exit_px", "exit_disp_sz", "exit_quote_age_ms",
    "fee_bps_side", "gross_bps", "net_bps", "net_usd",
    "hours_tag", "conv_tag", "status",
]


@dataclass
class Params:
    xs_bps: tuple = (6.0, 8.0, 12.0)   # leader move thresholds
    w_ms: float = 500.0                # leader move must happen within this window
    lags_ms: tuple = (100.0, 250.0, 500.0)
    holds_s: tuple = (2.0, 5.0)
    cooldown_ms: float = 3000.0        # at most one signal per threshold in this time
    max_usd: float = 3000.0
    stale_ms: float = 30000.0          # follower quote older than this is not used
    premove_ms: float = 500.0


@dataclass
class PairCfg:
    name: str
    leader: str       # feed id, e.g. "okx:ANTHROPIC-USDT-SWAP"
    follower: str     # feed id, e.g. "hl:xyz:ANT"
    fee_bps: float    # follower taker fee per side


@dataclass
class _Quote:
    t: float
    bid: Optional[float]
    bsz: float
    ask: Optional[float]
    asz: float

    @property
    def mid(self) -> Optional[float]:
        if self.bid and self.ask:
            return (self.bid + self.ask) / 2.0
        return None


@dataclass(order=True)
class _Pending:
    due: float
    seq: int
    kind: str = field(compare=False)
    row: dict = field(compare=False)


class PairEngine:
    def __init__(self, cfg: PairCfg, params: Params, sink: Callable[[dict], None],
                 conv_tag: Callable[[float], str] = lambda t: "unknown", clock: str = "recv"):
        self.cfg, self.p, self.sink, self.conv_tag, self.clock = cfg, params, sink, conv_tag, clock
        self.now = 0.0
        self.lwin: deque = deque()              # (t, leader mid)
        self.fq: Optional[_Quote] = None
        self.fhist: deque = deque()             # (t, follower mid)
        self.last_sig = {x: -1e18 for x in params.xs_bps}
        self.pending: list = []
        self._seq = 0
        self.n_signals = 0
        self.n_rows = 0

    # -- feed state ---------------------------------------------------------
    def reset_feed(self, feed: str) -> None:
        """Call when a feed disconnects or reconnects: its last quote is no longer trusted."""
        if feed == self.cfg.leader:
            self.lwin.clear()
        if feed == self.cfg.follower:
            self.fq = None
            self.fhist.clear()

    def on_bbo(self, feed: str, t: float, bid, bsz, ask, asz) -> None:
        """t is in milliseconds on the engine clock."""
        if feed != self.cfg.leader and feed != self.cfg.follower:
            return
        if t < self.now:
            t = self.now
        self.advance(t)
        bid = bid if bid and bid > 0 else None
        ask = ask if ask and ask > 0 else None
        if bid and ask and bid > ask:      # crossed book: ignore the update
            return
        if feed == self.cfg.follower:
            self.fq = _Quote(t, bid, bsz or 0.0, ask, asz or 0.0)
            m = self.fq.mid
            if m:
                h = self.fhist
                h.append((t, m))
                while len(h) >= 2 and h[1][0] <= t - self.p.premove_ms:
                    h.popleft()
        if feed == self.cfg.leader and bid and ask:
            self._leader((bid + ask) / 2.0, t)

    def advance(self, t: float) -> None:
        """Move the clock to t and resolve every shadow entry/exit due before t."""
        pend = self.pending
        while pend and pend[0].due < t:
            ev = heapq.heappop(pend)
            if ev.kind == "entry":
                self._entry(ev)
            else:
                self._exit(ev)
        if t > self.now:
            self.now = t

    # -- signal -------------------------------------------------------------
    def _leader(self, mid: float, t: float) -> None:
        win = self.lwin
        win.append((t, mid))
        # keep one point at or before the window start: it is the price the window opened at
        while len(win) >= 2 and win[1][0] <= t - self.p.w_ms:
            win.popleft()
        lo = min(m for _, m in win)
        hi = max(m for _, m in win)
        up = (mid / lo - 1.0) * 1e4
        dn = (mid / hi - 1.0) * 1e4
        move = up if up >= -dn else dn
        a = abs(move)
        for x in self.p.xs_bps:
            if a >= x and t - self.last_sig[x] >= self.p.cooldown_ms:
                self.last_sig[x] = t
                self._fire(x, t, move, mid)

    def _fresh_quote(self, t: float) -> Optional[_Quote]:
        q = self.fq
        if q is None or t - q.t > self.p.stale_ms:
            return None
        return q

    def _fire(self, x: float, t: float, move: float, lmid: float) -> None:
        self.n_signals += 1
        d = 1 if move > 0 else -1
        q = self._fresh_quote(t)
        fmid = q.mid if q else None
        pre = None
        if fmid and self.fhist and self.fhist[0][0] <= t - self.p.premove_ms:
            pre = (fmid / self.fhist[0][1] - 1.0) * 1e4
        base = {
            "version": VERSION, "pair": self.cfg.name, "leader": self.cfg.leader,
            "follower": self.cfg.follower, "clock": self.clock,
            "signal_ts_ms": int(t),
            "signal_utc": datetime.fromtimestamp(t / 1000.0, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "dir": d, "x_bps": x, "w_ms": self.p.w_ms,
            "leader_move_bps": round(move, 3), "leader_mid": round(lmid, 6),
            "follower_mid_at_signal": round(fmid, 6) if fmid else "",
            "follower_premove_bps": round(pre, 3) if pre is not None else "",
            "gap_bps": round((lmid / fmid - 1.0) * 1e4, 3) if fmid else "",   # leader over follower
            "fee_bps_side": self.cfg.fee_bps,
            "hours_tag": hours_tag(t), "conv_tag": self.conv_tag(t),
        }
        for lag in self.p.lags_ms:
            for hold in self.p.holds_s:
                row = dict(base, lag_ms=lag, hold_s=hold)
                self._seq += 1
                heapq.heappush(self.pending, _Pending(t + lag, self._seq, "entry", row))

    # -- shadow fills -------------------------------------------------------
    def _entry(self, ev: _Pending) -> None:
        row, q = ev.row, self._fresh_quote(ev.due)
        px, sz = (q.ask, q.asz) if q and row["dir"] > 0 else (q.bid, q.bsz) if q else (None, 0.0)
        if not px or sz <= 0:
            row["status"] = "no_entry_quote"
            self._write(row)
            return
        row.update(entry_ts_ms=int(ev.due), entry_px=px, entry_disp_sz=sz,
                   entry_quote_age_ms=int(ev.due - q.t),
                   entry_spread_bps=round((q.ask / q.bid - 1.0) * 1e4, 3) if q.bid and q.ask else "",
                   size_usd=round(min(sz * px, self.p.max_usd), 2))
        self._seq += 1
        heapq.heappush(self.pending, _Pending(ev.due + row["hold_s"] * 1000.0, self._seq, "exit", row))

    def _exit(self, ev: _Pending) -> None:
        row, q = ev.row, self._fresh_quote(ev.due)
        px, sz = (q.bid, q.bsz) if q and row["dir"] > 0 else (q.ask, q.asz) if q else (None, 0.0)
        if not px:
            row["status"] = "no_exit_quote"
            self._write(row)
            return
        gross = row["dir"] * (px / row["entry_px"] - 1.0) * 1e4
        net = gross - 2.0 * self.cfg.fee_bps
        row.update(exit_px=px, exit_disp_sz=sz, exit_quote_age_ms=int(ev.due - q.t),
                   gross_bps=round(gross, 3), net_bps=round(net, 3),
                   net_usd=round(row["size_usd"] * net / 1e4, 4), status="ok")
        self._write(row)

    def _write(self, row: dict) -> None:
        self.n_rows += 1
        self.sink(row)


class CsvSink:
    """Appends rows to shadow_leadlag.csv. An old file with different columns is moved aside."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path, newline="") as f:
                head = next(csv.reader(f), [])
            if head != COLUMNS:
                os.replace(path, path + ".old-%d" % int(time.time()))
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        self.f = open(path, "a", newline="")
        self.w = csv.DictWriter(self.f, fieldnames=COLUMNS, restval="")
        if new:
            self.w.writeheader()
            self.f.flush()

    def __call__(self, row: dict) -> None:
        self.w.writerow(row)
        self.f.flush()

    def close(self) -> None:
        self.f.close()
