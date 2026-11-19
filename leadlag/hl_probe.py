"""Order round-trip probe for Hyperliquid xyz:ANT.

    python -m leadlag.hl_probe            # shows the plan, places nothing
    python -m leadlag.hl_probe --yes      # runs it

Places one tiny post-only buy order 3% BELOW the market, waits for the exchange to confirm it,
cancels it, and repeats. Post-only means it can never take; 3% away means it should never fill.
It measures how long an order takes to reach Hyperliquid's book from this server, which is the
delay the shadow test has to assume.

It tries two ways of sending (a fresh HTTPS request, and one open websocket) and reports both.
The account is looked up from the API wallet itself, so no address file is needed.

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


class WsPost:
    """Sends signed actions over Hyperliquid's websocket instead of a new HTTPS request each time."""

    def __init__(self, url: str):
        import websocket
        self.ws = websocket.create_connection(url.replace("https://", "wss://") + "/ws", timeout=10)
        self.id = 0

    def __call__(self, url_path: str, payload):
        import json
        self.id += 1
        self.ws.send(json.dumps({"method": "post", "id": self.id, "request": {"type": "action", "payload": payload}}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("channel") == "post" and m["data"]["id"] == self.id:
                r = m["data"]["response"]
                return r["payload"] if r.get("type") == "action" else {"status": "err", "response": r.get("payload")}
            if m.get("channel") == "error":
                return {"status": "err", "response": m.get("data")}


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
    ap.add_argument("--transport", choices=("both", "http", "ws"), default="both")
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
    wallet = Account.from_key(read_secret(a.key_file))
    role = info.post("/info", {"type": "userRole", "user": wallet.address})
    if role.get("role") == "agent":            # an API wallet: orders and balances belong to its owner
        address = role["data"]["user"]
    elif role.get("role") in ("user", "subAccount"):
        address = wallet.address
    else:
        sys.exit("Hyperliquid does not know this key (role: %s). Is the API wallet approved? Nothing was sent." % role.get("role"))
    print("account found for this API wallet. Open orders and position are checked on that account.")

    def position() -> float:
        for p in info.user_state(address, DEX).get("assetPositions", []):
            if p["position"]["coin"] == COIN:
                return float(p["position"]["szi"])
        return 0.0

    pos0 = position()
    res, live = {}, []
    modes = ["http", "ws"] if a.transport == "both" else [a.transport]
    try:
        for mode in modes:
            ex = Exchange(wallet, url, account_address=address, perp_dexs=["", DEX])
            if mode == "ws":
                ex.post = WsPost(url)
            place_rt, to_book, cancel_rt, cancel_book = [], [], [], []
            res[mode] = (place_rt, to_book, cancel_rt, cancel_book)
            print("\nvia %s" % ("a fresh HTTPS request each time" if mode == "http" else "one open websocket"))
            for i in range(n if len(modes) == 1 else max(1, n // 2)):
                t0 = time.time()
                r = ex.order(COIN, True, sz, px, {"limit": {"tif": "Alo"}})
                t1 = time.time()
                try:
                    stt = r["response"]["data"]["statuses"][0]
                    oid = stt["resting"]["oid"]
                except (KeyError, TypeError, IndexError):
                    sys.exit("order was not accepted: %s" % str(r)[:300])
                live.append(oid)
                c0 = time.time()
                ex.cancel(COIN, oid)
                c1 = time.time()
                live.remove(oid)
                time.sleep(1.0)
                o = info.query_order_by_oid(address, oid).get("order", {})
                ts, cts = o.get("order", {}).get("timestamp"), o.get("statusTimestamp")   # block times
                place_rt.append((t1 - t0) * 1000)
                cancel_rt.append((c1 - c0) * 1000)
                got_c = bool(cts and o.get("status") == "canceled")
                if ts:
                    to_book.append(ts - t0 * 1000)
                if got_c:
                    cancel_book.append(cts - c0 * 1000)
                # "?" = Hyperliquid's order lookup had no answer for this round; it is left out of the result
                print("  round %2d: order in the book %s ms after sending, confirmed after %4.0f ms; cancel took effect after %s ms" % (
                    i + 1, "%4.0f" % to_book[-1] if ts else "   ?", place_rt[-1], "%4.0f" % cancel_book[-1] if got_c else "   ?"))
                time.sleep(1.0)
    finally:
        ex = Exchange(wallet, url, account_address=address, perp_dexs=["", DEX])
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
    for mode, (place_rt, to_book, cancel_rt, cancel_book) in res.items():
        print(" via %s" % mode)
        line("send -> order in Hyperliquid's book", to_book)
        line("send -> cancel takes effect", cancel_book)
        line("send -> confirmation back at this server", place_rt)
    print("  'order in the book' is the delay to compare with the shadow test's 100 / 250 / 500 ms columns.")
    print("  It compares this server's clock with Hyperliquid's block time, so it is only as good as the clock sync.")


if __name__ == "__main__":
    main()
