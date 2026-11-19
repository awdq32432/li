"""Edge map: where, if anywhere, does the shadow trade pay?

    python -m leadlag.edgemap                      # okx>hl, 250 ms delay, 2 s hold, fair view
    python -m leadlag.edgemap --lag 100 --pair "okx>hl"

Splits every signal by the conditions at the moment it fired and shows net bps for each bucket,
separately for the earlier 60% of the data ("find") and the later 40% ("check"). A bucket only
counts if it is positive in both halves. This reads a CSV; it changes nothing about the bot.
"""
from __future__ import annotations

import argparse
import math
import statistics as st

from . import VERSION, config
from .report import load_rows


def _f(r, k):
    try:
        return float(r[k])
    except (TypeError, ValueError, KeyError):
        return None


def _bucket(v, edges, fmt="%g"):
    if v is None:
        return "(unknown)"
    lo = None
    for e in edges:
        if v < e:
            return ("< " + fmt % e) if lo is None else (fmt + " to " + fmt) % (lo, e)
        lo = e
    return (">= " + fmt) % lo


def features(rows: list) -> list:
    """One dict of bucket labels per row. `rows` must be one pair, in time order."""
    out, gaps = [], []
    for r in rows:
        d = float(r["dir"])
        move, pre, gap = abs(_f(r, "leader_move_bps") or 0.0), _f(r, "follower_premove_bps"), _f(r, "gap_bps")
        # how far the follower is behind the leader, against the usual gap over the last 50 signals
        behind = d * (gap - st.median(gaps[-50:])) if gap is not None and len(gaps) >= 10 else None
        if gap is not None:
            gaps.append(gap)
        px, sz = _f(r, "entry_px"), _f(r, "entry_disp_sz")
        out.append({
            "size of the leader's move (bps)": _bucket(move, [8, 12, 20]),
            "follower already moved the same way (bps)": _bucket(d * pre if pre is not None else None, [0, 2, 5]),
            "follower behind the leader vs usual gap (bps)": _bucket(behind, [4, 8, 12]),
            "follower spread at entry (bps)": _bucket(_f(r, "entry_spread_bps"), [1, 2, 4]),
            "size shown at entry ($)": _bucket(px * sz if px and sz else None, [1000, 3000, 10000]),
            "follower quote age at entry (ms)": _bucket(_f(r, "entry_quote_age_ms"), [50, 200, 1000]),
            "direction": "leader up (buy)" if d > 0 else "leader down (sell)",
            "market hours": r["hours_tag"],
            "conversion": r["conv_tag"],
        })
    return out


def _stat(v: list) -> tuple:
    n = len(v)
    if not n:
        return 0, 0.0, 0.0
    m = sum(v) / n
    sd = st.stdev(v) if n > 1 else 0.0
    return n, m, (m / (sd / math.sqrt(n)) if sd > 0 else 0.0)


def build(rows: list, pair: str, x: float, hold: float, lag: float, min_n: int = 30) -> str:
    rows = sorted((r for r in rows if r["pair"] == pair and r["status"] == "ok" and r["x_bps"] == x
                   and r["hold_s"] == hold and r["lag_ms"] == lag), key=lambda r: r["signal_ts_ms"])
    order = lambda b: (0 if b.startswith("<") else 2 if b.startswith(">=") else 3 if b.startswith("(") else 1,
                       float(b.split()[0]) if b[:1].isdigit() else 0.0, b)
    out = ["OKX bot edge map  %s   pair %s   signals of %g bps or more, %g ms delay, %g s hold" % (VERSION, pair, x, lag, hold)]
    if len(rows) < 2 * min_n:
        return "\n".join(out + ["  only %d trades: too few for an edge map (need at least %d)" % (len(rows), 2 * min_n)])
    cut = int(len(rows) * 0.6)
    feats = features(rows)
    net = [r["net_bps"] for r in rows]
    days = max((rows[-1]["signal_ts_ms"] - rows[0]["signal_ts_ms"]) / 86400000.0, 1e-9)
    n1, m1, t1 = _stat(net[:cut])
    n2, m2, t2 = _stat(net[cut:])
    out += ["  %d trades over %.2f days. All trades: find %+.2f bps (t %+.1f), check %+.2f bps (t %+.1f)" % (len(rows), days, m1, t1, m2, t2),
            "  find = earlier 60% of the data, check = later 40%. net bps is after fees.", ""]
    keep, tested = [], 0
    for name in feats[0]:
        out.append("  %s" % name)
        out.append("    %-22s | %6s %8s %6s | %6s %8s %6s |" % ("bucket", "find n", "net_bps", "t", "chk n", "net_bps", "t"))
        for b in sorted({f[name] for f in feats}, key=order):
            idx = [i for i, f in enumerate(feats) if f[name] == b]
            a, c = _stat([net[i] for i in idx if i < cut]), _stat([net[i] for i in idx if i >= cut])
            tested += 1
            ok = a[0] >= min_n and c[0] >= min_n and a[1] > 0 and c[1] > 0
            if ok:
                usd = sum(float(rows[i]["net_usd"]) for i in idx) / days
                keep.append("%s = %s: %+.2f then %+.2f bps, %d trades, about $%.0f a day at shadow size" % (name, b, a[1], c[1], len(idx), usd))
            out.append("    %-22s | %6d %+8.2f %+6.1f | %6d %+8.2f %+6.1f | %s" % (b, a[0], a[1], a[2], c[0], c[1], c[2], "HOLDS" if ok else ""))
        out.append("")
    out.append("BUCKETS POSITIVE IN BOTH HALVES (at least %d trades in each):" % min_n)
    out += ["  " + k for k in keep] or ["  none"]
    out += ["", "  Caution: %d buckets were tested, so one or two can look good by luck alone. A bucket is a lead" % tested,
            "  to test on new days, not a rule to trade. Trades in the same burst are not independent."]
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--csv", help="default: the fair-view CSV")
    ap.add_argument("--pair", default="okx>hl")
    ap.add_argument("--x", type=float, default=6, help="6 covers every signal; the move size is one of the splits")
    ap.add_argument("--hold", type=float, default=2)
    ap.add_argument("--lag", type=float, default=250)
    a = ap.parse_args()
    cfg = config.load(a.config)
    if not a.csv:
        from .fair import update_fair
        update_fair(cfg, verbose=True)
    keep = lambda r: r["pair"] == a.pair and float(r["x_bps"]) == a.x and float(r["hold_s"]) == a.hold and float(r["lag_ms"]) == a.lag
    print(build(load_rows(a.csv or cfg["fair_csv"], keep), a.pair, a.x, a.hold, a.lag))


if __name__ == "__main__":
    main()
