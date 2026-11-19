"""OKX bot health: the hourly report.

    python -m leadlag.report                 # last 60 minutes, printed
    python -m leadlag.report --loop          # print and append to data/reports/leadlag_health.log every hour
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import statistics as st
import time
from datetime import datetime, timezone

from . import VERSION, config
from .store import list_streams, read_events

GRID_MS = 50


def _utc(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------- shadow results
NUM = ("signal_ts_ms", "x_bps", "lag_ms", "hold_s", "net_bps", "net_usd", "size_usd")


def load_rows(path: str, keep=None) -> list:
    """Rows of a shadow CSV as dicts. `keep` filters on the raw text row, to save memory."""
    if not os.path.exists(path):
        return []
    out = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if keep is not None and not keep(r):
                continue
            for k in NUM:
                r[k] = float(r[k]) if r.get(k) not in ("", None) else None
            out.append(r)
    return out


class Acc:
    """Running count, mean, t-statistic and share of winners, without keeping the rows."""
    __slots__ = ("n", "s", "ss", "w")

    def __init__(self):
        self.n, self.s, self.ss, self.w = 0, 0.0, 0.0, 0

    def add(self, v: float) -> None:
        self.n += 1
        self.s += v
        self.ss += v * v
        self.w += v > 0

    def __str__(self) -> str:
        n = self.n
        if n == 0:
            return "%5d %8s %6s %5s" % (0, "-", "-", "-")
        m = self.s / n
        var = (self.ss - n * m * m) / (n - 1) if n > 1 else 0.0
        t = "%+6.1f" % (m / math.sqrt(var / n)) if var > 1e-12 and n >= 5 else "%6s" % "-"   # too few for a t-statistic
        return "%5d %+8.2f %s %4.0f%%" % (n, m, t, 100.0 * self.w / n)


def aggregate(path: str, t0=None, t1=None) -> dict:
    """Reads a shadow CSV once and keeps only totals. Scopes: all, win(dow), hours tag, conversion tag."""
    A: dict = {}
    if not os.path.exists(path):
        return A
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            P = A.get(r["pair"])
            if P is None:
                P = A[r["pair"]] = {"follower": r["follower"], "fee": r["fee_bps_side"], "stats": {}, "sig": {}, "combos": set()}
            ts, x = float(r["signal_ts_ms"]), float(r["x_bps"])
            combo = (x, float(r["hold_s"]), float(r["lag_ms"]))
            P["combos"].add(combo)
            inwin = t0 is not None and t0 <= ts <= t1
            if inwin:
                P["sig"].setdefault(x, set()).add(ts)
            if r["status"] != "ok":
                continue
            v = float(r["net_bps"])
            for scope in ("all", "win" if inwin else None, ("hours_tag", r["hours_tag"]), ("conv_tag", r["conv_tag"])):
                if scope is not None:
                    key = combo + (scope,)
                    acc = P["stats"].get(key)
                    if acc is None:
                        acc = P["stats"][key] = Acc()
                    acc.add(v)
    return A


def shadow_section(fair: dict, live: dict, t0, t1, full: bool = False) -> list:
    """Per pair: signals per hour, then net bps (after fees), t-statistic and winners per combination.
    FAIR = replayed on exchange timestamps (the honest view). LIVE = the server's receive times."""
    if not fair and not live:
        return ["  no shadow rows yet"]
    out = []
    hdr = "%5s %8s %6s %5s" % ("n", "net_bps", "t", "win")
    none = Acc()
    for pair in sorted(set(fair) | set(live), key=lambda p: (p != "okx>hl", p)):
        pf, pl = fair.get(pair), live.get(pair)
        base = pf or pl
        out += ["", "PAIR %s   follower %s   fee %s bps a side" % (pair, base["follower"], base["fee"])]
        if t0 is not None:
            hrs = max((t1 - t0) / 3600000.0, 1e-9)
            out.append("  signals per hour (window): " + ("   ".join(
                "X>=%g: %.0f" % (x, len(v) / hrs) for x, v in sorted(base["sig"].items())) or "none"))
        combos = sorted(base["combos"])
        if not (full or pair == "okx>hl"):      # the 5 bps venues: headline rows only
            combos = [c for c in combos if c[0] == 8 and c[1] == 2]
        cols = []
        if pf and t0 is not None:
            cols.append(("FAIR window", pf, "win"))
        if pf:
            cols.append(("FAIR all", pf, "all"))
        if pl:
            cols.append(("LIVE all", pl, "all"))
        out.append("  %4s %5s %6s | " % ("X", "hold", "delay") + " | ".join("%-12s%s" % (name, hdr) for name, _, _ in cols))
        for c in combos:
            out.append("  %4g %4gs %4gms | " % c + " | ".join("%-12s%s" % ("", P["stats"].get(c + (sc,), none)) for _, P, sc in cols))
        if pf and (full or pair == "okx>hl"):
            lags = sorted({c[2] for c in combos})
            for col, label in (("hours_tag", "market hours"), ("conv_tag", "conversion")):
                out.append("  FAIR split by %s (X=8, hold 2 s, all time):" % label)
                for tag in sorted({k[3][1] for k in pf["stats"] if isinstance(k[3], tuple) and k[3][0] == col}):
                    out.append("    %-16s %s" % (tag, " | ".join(
                        "%gms %s" % (lag, pf["stats"].get((8.0, 2.0, lag, (col, tag)), none)) for lag in lags)))
    return out


# ---------------------------------------------------------------- raw data sections
def scan(root: str, t0: float, t1: float, clock: str) -> dict:
    """One pass over the window's raw files."""
    S: dict = {"streams": {}, "mids": {}, "opt": {"trades": 0, "qty": 0.0, "hs": [], "sz": [], "insts": set()},
               "rule": None, "growth": None}
    for s in list_streams(root):
        d = S["streams"].setdefault(s, {"msgs": 0, "bbo": 0, "trade": 0, "last": 0.0, "down": 0, "rtt_ws": [], "rtt_app": [], "lat": []})
        for r, n in read_events(root, s, t0, t1):
            c = n["c"]
            d["msgs"] += 1
            d["last"] = r
            if c == "bbo":
                d["bbo"] += 1
                if n.get("e"):
                    d["lat"].append(r - n["e"])
                b, a = n["b"], n["a"]
                if s == "okx_opt":
                    o = S["opt"]
                    o["insts"].add(n["i"])
                    if b and a:
                        o["hs"].append((a - b) / (a + b) * 100.0)   # half-spread as % of mid
                        o["sz"] += [n["B"], n["A"]]
                elif b and a and "ANT" in n["i"].upper():
                    t = n["e"] if clock == "exch" and n.get("e") else r
                    S["mids"].setdefault(s + ":" + n["i"], []).append((t, (a + b) / 2.0))
            elif c == "trade":
                d["trade"] += 1
                if s == "okx_opt":
                    S["opt"]["trades"] += 1
                    S["opt"]["qty"] += n["q"]
            elif c == "conn" and n["st"] == "down":
                d["down"] += 1
            elif c == "rtt":
                d["rtt_" + n["k"]].append(n["ms"])
            elif c == "instruments":
                S["rule"] = n["f"].get("ruleType")
            elif c == "meta":
                S["growth"] = n["f"].get("growthMode") or "off"
    return S


def _grid(series: list, t0: float, n: int) -> list:
    """Last mid at each grid point (None before the first quote)."""
    out, j, last = [None] * n, 0, None
    series.sort()
    for i in range(n):
        t = t0 + (i + 1) * GRID_MS
        while j < len(series) and series[j][0] <= t:
            last = series[j][1]
            j += 1
        out[i] = last
    return out


def _rets(g: list) -> dict:
    return {i: math.log(g[i] / g[i - 1]) for i in range(1, len(g)) if g[i] and g[i - 1] and g[i] != g[i - 1]}


def xcorr_peak(ra: dict, rb: dict, max_lag: int = 20):
    """Lag k (in grid steps) that maximises corr(a[t], b[t+k]). k > 0: a leads b."""
    va, vb = sum(v * v for v in ra.values()), sum(v * v for v in rb.values())
    if not va or not vb:
        return None
    best = (0, -2.0)
    for k in range(-max_lag, max_lag + 1):
        c = sum(v * rb.get(i + k, 0.0) for i, v in ra.items()) / math.sqrt(va * vb)
        if c > best[1]:
            best = (k, c)
    return best


def raw_sections(S: dict, t0: float, t1: float, okx_feed: str, hl_feed: str) -> list:
    out = ["", "FEEDS", "  %-8s %8s %8s %7s %9s %6s %12s %12s %14s" % (
        "stream", "msgs", "bbo", "trades", "last_msg", "drops", "ws_ping_ms", "app_ping_ms", "recv-exch_ms")]
    med = lambda v: "%.1f" % st.median(v) if v else "-"
    for s, d in S["streams"].items():
        age = "%.0fs ago" % ((t1 - d["last"]) / 1000) if d["last"] else "never"
        out.append("  %-8s %8d %8d %7d %9s %6d %12s %12s %14s" % (
            s, d["msgs"], d["bbo"], d["trade"], age, d["down"], med(d["rtt_ws"]), med(d["rtt_app"]), med(d["lat"])))
    out.append("  ping = market-data round trip from this server (half of it is one way). It is not an order round trip.")
    conv = "unknown" if S["rule"] is None else ("pre_conversion" if S["rule"] == "pre_market" else "post_conversion")
    seen = lambda v: v if v is not None else "not polled in this window"   # polled every 1 and 5 minutes
    out += ["", "STATE  OKX ruleType=%s (%s)   Hyperliquid growthMode=%s" % (seen(S["rule"]), conv, seen(S["growth"]))]

    n = int((t1 - t0) // GRID_MS)
    grids = {f: _grid(v, t0, n) for f, v in S["mids"].items() if len(v) > 100}
    rets = {f: _rets(g) for f, g in grids.items()}
    out += ["", "LEAD-LAG  lag at which the correlation of %d ms returns peaks (search +-1 s)" % GRID_MS]
    feeds = sorted(rets)
    for i, a in enumerate(feeds):
        for b in feeds[i + 1:]:
            pk = xcorr_peak(rets[a], rets[b])
            if pk is None:
                continue
            k, c = pk
            who = "moves together" if k == 0 else "%s leads by %d ms" % (a if k > 0 else b, abs(k) * GRID_MS)
            out.append("  %-28s vs %-22s %-40s corr %.2f" % (a, b, who, c))
    if len(feeds) < 2:
        out.append("  not enough data")
    if okx_feed in grids and hl_feed in grids:
        step = 1000 // GRID_MS
        gaps = [(grids[okx_feed][i] / grids[hl_feed][i] - 1.0) * 1e4 for i in range(0, n, step)
                if grids[okx_feed][i] and grids[hl_feed][i]]
        if gaps:
            out += ["", "PRICE GAP  OKX over Hyperliquid: median %+.1f bps, range %+.1f to %+.1f bps" % (
                st.median(gaps), min(gaps), max(gaps))]
    o, hrs = S["opt"], max((t1 - t0) / 3600000.0, 1e-9)
    out += ["", "HYPE OPTIONS (OKX)  %d contracts watched" % len(o["insts"])]
    out.append("  trades per hour: %.1f   traded: %.1f HYPE in the window" % (o["trades"] / hrs, o["qty"]))
    if o["hs"]:
        out.append("  median half-spread: %.1f%% of the option price   quoter's typical size: %.1f HYPE a side" % (
            st.median(o["hs"]), st.median(o["sz"])))
    else:
        out.append("  no two-sided quotes recorded")
    return out


def build(cfg: dict, minutes: float, clock: str, now_ms: float | None = None, full: bool = False) -> str:
    t1 = now_ms or time.time() * 1000
    t0 = t1 - minutes * 60000
    out = ["=" * 100, "OKX bot health  %s   %s   window: last %g min   clock: %s" % (VERSION, _utc(t1), minutes, clock),
           "Shadow results only. No orders were placed."]
    out += ["", "SHADOW TEST  (net_bps is after fees; t = t-statistic; window = the last %g min)" % minutes,
            "  FAIR = both venues lined up on their own timestamps, finished hours only. Judge the trade by this.",
            "  LIVE = this server's receive times. Hyperliquid's quotes arrive ~300 ms old, so LIVE flatters it."]
    try:
        from .fair import update_fair
        update_fair(cfg, t1)
    except Exception as e:
        out.append("  fair view could not be updated: %s: %s" % (type(e).__name__, e))
    out += shadow_section(aggregate(cfg["fair_csv"], t0, t1), aggregate(cfg["shadow_csv"], t0, t1), t0, t1, full)
    S = scan(cfg["data_dir"], t0, t1, clock)
    out += raw_sections(S, t0, t1, "okx:" + cfg["okx"]["ant_swap"], "hl:" + cfg["hl"]["ant"])
    log = cfg["conversion_log"]
    if os.path.exists(log):
        lines = open(log).read().splitlines()
        out += ["", "CHANGES LOGGED (%d, latest 5)" % len(lines)] + ["  " + l[:300] for l in lines[-5:]]
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--clock", choices=("recv", "exch"), default="recv")
    ap.add_argument("--full", action="store_true", help="every combination for every pair")
    ap.add_argument("--loop", action="store_true", help="run at the top of every hour and append to the log")
    ap.add_argument("--log", default="data/reports/leadlag_health.log")
    a = ap.parse_args()
    cfg = config.load(a.config)
    if not a.loop:
        print(build(cfg, a.minutes, a.clock, full=a.full))
        return
    os.makedirs(os.path.dirname(a.log) or ".", exist_ok=True)
    while True:
        time.sleep(3600 - time.time() % 3600 + 5)
        try:
            txt = build(cfg, a.minutes, a.clock, full=a.full)
        except Exception as e:
            txt = "leadlag_health failed: %s: %s" % (type(e).__name__, e)
        print(txt, flush=True)
        with open(a.log, "a") as f:
            f.write(txt + "\n")


if __name__ == "__main__":
    main()
