"""Order round-trip probe for Hyperliquid xyz:ANT.

    python -m leadlag.hl_probe            # shows the plan, places nothing
    python -m leadlag.hl_probe --yes      # runs it

Places one tiny post-only buy order 3% BELOW the market, waits for the exchange to confirm it,
cancels it, and repeats. Post-only means it can never take; 3% away means it should never fill.
It measures how long an order takes to reach Hyperliquid's book from this server, which is the
delay the shadow test has to assume.

The API wallet key is read from a file on this server (default ~/.hl_api_key, owner-only
permissions). It is never printed, logged or sent anywhere except as a signature to Hyperliquid.
"""
from __future__ import annotations

import argparse
import math
import os
import stat
import statistics as st
import sys
import time

COIN, DEX = "xyz:ANT", "xyz"
AWAY = 0.03            # order price is this far below the best bid
MAX_USD = 15.0         # hard cap on the order's value
MAX_N = 30


def read_secret(path: str) -> str:
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        sys.exit("key file %s not found (see README: 'Order round-trip probe')" % path)
    if stat.S_IMODE(os.stat(path).st_mode) & 0o077:
        sys.exit("%s is readable by other users. Run: chmod 600 %s" % (path, path))
    k = open(path).read().strip().replace("0x", "").replace("0X", "")
    if len(k) == 128 and k[:64] == k[64:]:      # this terminal pastes everything twice
        k = k[:64]
    if len(k) != 64 or any(c not in "0123456789abcdefABCDEF" for c in k):
        sys.exit("%s does not hold a 64-character private key (found %d characters). Nothing was sent." % (path, len(k)))
    return k


def read_address(path: str) -> str:
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        sys.exit("address file %s not found (see README: 'Order round-trip probe')" % path)
    a = open(path).read().strip()
    if len(a) == 84 and a[:42] == a[42:]:
        a = a[:42]
    if len(a) != 42 or not a.startswith("0x"):
        sys.exit("%s does not hold a 42-character account address starting with 0x" % path)
    return a


def sig5(px: float, max_dec: int) -> float:
    """Hyperliquid prices: at most 5 significant figures and max_dec decimals."""
    d = max(0, min(max_dec, 4 - int(math.floor(math.log10(px)))))
    return round(px, d)


def pct(v: list, q: float) -> float:
    v = sorted(v)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="actually place and cancel the orders")
    ap.add_argument("--n", type=int, default=20, help="how many place/cancel rounds (max %d)" % MAX_N)
    ap.add_argument("--key-file", default="~/.hl_api_key")
    ap.add_argument("--address-file", default="~/.hl_account_address")
    a = ap.parse_args()
    n = max(1, min(a.n, MAX_N))

    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    url = constants.MAINNET_API_URL
    info = Info(url, skip_ws=True, perp_dexs=["", DEX])
    meta = next(u for u in info.post("/info", {"type": "meta", "dex": DEX})["universe"] if u["name"] == COIN)
    szd = int(meta["szDecimals"])
    book = info.l2_snapshot(COIN)["levels"]
    bid, ask = float(book[0][0]["px"]), float(book[1][0]["px"])
    px = sig5(bid * (1 - AWAY), 6 - szd)
    sz = math.ceil(11.0 / px * 10 ** szd) / 10 ** szd          # just above the $10 minimum
    usd = px * sz
    print("Hyperliquid %s: best bid %s, best ask %s" % (COIN, bid, ask))
    print("Plan: %d rounds of [post-only BUY %s at %s (%.1f%% below the bid, worth $%.2f) -> confirm -> cancel]" % (
        n, sz, px, AWAY * 100, usd))
    print("Worst case: the price falls %.0f%% while an order rests (about 1 second each) and it fills: you would" % (AWAY * 100))
    print("then hold $%.2f of ANT. The probe checks for that at the end and tells you." % usd)
    if usd > MAX_USD or px >= bid * (1 - AWAY / 2):
        sys.exit("safety check failed (order value or distance from market). Nothing was sent.")
    if not a.yes:
        print("\nNothing was sent. Add --yes to run it.")
        return

    from eth_account import Account
    from hyperliquid.exchange import Exchange
    address = read_address(a.address_file)
    wallet = Account.from_key(read_secret(a.key_file))
    ex = Exchange(wallet, url, account_address=address, perp_dexs=["", DEX])

    def position() -> float:
        for p in info.user_state(address, DEX).get("assetPositions", []):
            if p["position"]["coin"] == COIN:
                return float(p["position"]["szi"])
        return 0.0

    pos0 = position()
    place_rt, to_book, cancel_rt, live = [], [], [], []
    try:
        for i in range(n):
            t0 = time.time()
            r = ex.order(COIN, True, sz, px, {"limit": {"tif": "Alo"}})
            t1 = time.time()
            try:
                stt = r["response"]["data"]["statuses"][0]
            except (KeyError, TypeError, IndexError):
                sys.exit("order was not accepted: %s" % str(r)[:300])
            if "resting" not in stt:
                sys.exit("order was not accepted: %s" % str(stt)[:300])
            oid = stt["resting"]["oid"]
            live.append(oid)
            o = info.query_order_by_oid(address, oid)
            ts = o.get("order", {}).get("order", {}).get("timestamp")     # the block that took the order
            c0 = time.time()
            ex.cancel(COIN, oid)
            c1 = time.time()
            live.remove(oid)
            place_rt.append((t1 - t0) * 1000)
            cancel_rt.append((c1 - c0) * 1000)
            if ts:
                to_book.append(ts - t0 * 1000)
            print("  round %2d: confirmed in %4.0f ms, reached the book %s ms after sending, cancel %4.0f ms" % (
                i + 1, place_rt[-1], "%4.0f" % to_book[-1] if ts else "   ?", cancel_rt[-1]))
            time.sleep(2.0)
    finally:
        for oid in live:                      # never leave an order behind
            try:
                ex.cancel(COIN, oid)
            except Exception as e:
                print("could not cancel order %s: %s. Cancel it in the Hyperliquid app." % (oid, e))
        left = [o for o in info.open_orders(address, DEX) if o.get("coin") == COIN]
        pos1 = position()
        print("\nopen %s orders left: %d   position before %s, after %s%s" % (
            COIN, len(left), pos0, pos1, "" if pos1 == pos0 else "   <-- AN ORDER FILLED: close it in the app"))

    def line(name, v):
        if v:
            print("  %-46s median %4.0f ms   90%% under %4.0f ms   worst %4.0f ms   (n=%d)" % (name, st.median(v), pct(v, 0.9), max(v), len(v)))
    print("\nRESULT")
    line("send -> order in Hyperliquid's book", to_book)
    line("send -> confirmation back at this server", place_rt)
    line("cancel -> confirmation", cancel_rt)
    print("  The first line is the delay to compare with the shadow test's 100 / 250 / 500 ms columns.")
    print("  It uses this server's clock against Hyperliquid's block time, so it is good to a few ms only if the clock is synced.")


if __name__ == "__main__":
    main()
