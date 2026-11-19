"""Finds which Binance futures websocket address delivers trades for a symbol. Public data only.

    .venv/bin/python scripts/check_binance_trades.py ANTUSDT
"""
import asyncio
import sys

import websockets

SYM = (sys.argv[1] if len(sys.argv) > 1 else "ANTUSDT").lower()
BASE = "wss://fstream.binance.com"
URLS = [p % (ch,) for ch in (SYM + "@aggTrade", SYM + "@trade")
        for p in (BASE + "/ws/%s", BASE + "/stream?streams=%s", BASE + "/market/ws/%s",
                  BASE + "/market/stream?streams=%s", BASE + "/public/ws/%s")]


async def check(url: str) -> None:
    n, first = 0, ""
    try:
        async with websockets.connect(url, open_timeout=8) as ws:
            try:
                async with asyncio.timeout(12):
                    async for m in ws:
                        n += 1
                        first = first or m[:160]
            except TimeoutError:
                pass
        print("%-62s %4d messages  %s" % (url, n, first))
    except Exception as e:
        print("%-62s FAILED %s: %s" % (url, type(e).__name__, str(e)[:80]))


async def main() -> None:
    await asyncio.gather(*(check(u) for u in URLS))

asyncio.run(main())
