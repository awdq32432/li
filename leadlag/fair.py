"""Fair view: the shadow test replayed on the exchanges' own timestamps, one finished hour at a time.

The live shadow table uses the server's receive times. Hyperliquid's quotes arrive about 300 ms
old, so the live table "fills" at prices a real order would no longer get. Lining both venues up on
their own timestamps removes that. Rows are appended to shadow_fair.csv.
"""
from __future__ import annotations

import json
import os
import time

from .replay import run
from .store import data_span

HOUR = 3600000.0


def update_fair(cfg: dict, now_ms: float | None = None, verbose: bool = False) -> int:
    """Replays every finished hour not yet done. Returns the number of hours processed."""
    now_ms = now_ms or time.time() * 1000
    out = cfg["fair_csv"]
    state_path = out + ".state.json"
    state = {}
    if os.path.exists(state_path) and os.path.exists(out):
        with open(state_path) as f:
            state = json.load(f)
    span = data_span(cfg["data_dir"])
    if not span[1]:
        return 0
    h = state.get("next_hour") or int(span[0] // HOUR)
    last_done = int(now_ms // HOUR) - 1          # the latest hour that has fully ended
    rule, n = state.get("rule"), 0
    while h <= last_done:
        h0 = h * HOUR
        # 10 s of overlap lets positions opened late in the hour reach their exit
        _, rule = run(cfg, cfg["data_dir"], out, "exch", h0, h0 + HOUR + 10000, append=True,
                      rule0=rule, sig_lt=h0 + HOUR)
        h += 1
        n += 1
        with open(state_path, "w") as f:
            json.dump({"next_hour": h, "rule": rule}, f)
        if verbose:
            print("fair view: replayed hour starting %s UTC" % time.strftime("%Y-%m-%d %H:00", time.gmtime(h0 / 1000)), flush=True)
    return n
