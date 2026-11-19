"""Public market-data connections. Subscribe-only: nothing here can send an order."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request

import websockets

log = logging.getLogger("leadlag")


def http_json(url: str, body: dict | None = None, timeout: float = 10.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": "Mozilla/5.0 (leadlag-recorder)", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode()


async def aget(url: str, body: dict | None = None) -> str:
    return await asyncio.to_thread(http_json, url, body)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class WsVenue:
    """Reconnecting websocket reader. Subclasses subscribe and parse."""
    stream = "x"
    url = ""
    ping_every = 15.0
    idle_timeout = 60.0

    def __init__(self, hub):
        self.hub = hub
        self.ws = None
        self.last_rx = 0.0
        self._app_ping_t = None
        self._url_i = 0

    def _next_url(self) -> str:
        """`url` may be a list of alternatives (OKX: port 8443, then 443)."""
        urls = self.url if isinstance(self.url, list) else [self.url]
        return urls[self._url_i % len(urls)]

    async def on_open(self, ws) -> None: ...
    def on_message(self, msg: str, r: int) -> None: ...
    async def app_ping(self, ws) -> None: ...

    def emit(self, r: int, norm: list, raw: str | None) -> None:
        self.hub.emit(self.stream, r, norm, raw)

    def app_pong(self, r: int) -> None:
        if self._app_ping_t is not None:
            self.emit(r, [{"c": "rtt", "k": "app", "ms": round((time.perf_counter() - self._app_ping_t) * 1000, 2)}], None)
            self._app_ping_t = None

    async def _keepalive(self, ws) -> None:
        while True:
            await asyncio.sleep(self.ping_every)
            if time.monotonic() - self.last_rx > self.idle_timeout:
                log.warning("%s: no data for %.0fs, reconnecting", self.stream, self.idle_timeout)
                await ws.close()
                return
            try:
                self._app_ping_t = time.perf_counter()
                await self.app_ping(ws)
                waiter = await ws.ping()
                rtt = await asyncio.wait_for(waiter, 10)
                self.emit(time.time_ns(), [{"c": "rtt", "k": "ws", "ms": round(rtt * 1000, 2)}], None)
            except asyncio.TimeoutError:
                pass   # some servers ignore protocol pings; the idle check above covers liveness
            except websockets.ConnectionClosed:
                return

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                got = False
                async with websockets.connect(self._next_url(), ping_interval=None, max_size=1 << 23,
                                              open_timeout=20, close_timeout=3) as ws:
                    self.ws = ws
                    self.last_rx = time.monotonic()
                    await self.on_open(ws)
                    self.hub.conn(self.stream, "up")
                    log.info("%s: connected", self.stream)
                    ka = asyncio.create_task(self._keepalive(ws))
                    try:
                        async for msg in ws:
                            r = time.time_ns()
                            self.last_rx = time.monotonic()
                            backoff, got = 1.0, True
                            try:
                                self.on_message(msg, r)
                            except Exception:
                                log.exception("%s: could not parse %.300s", self.stream, msg)
                                self.emit(r, [{"c": "unparsed"}], msg if isinstance(msg, str) and msg[:1] in "{[" else None)
                    finally:
                        ka.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("%s: connection lost (%s): %s: %s", self.stream, self._next_url(), type(e).__name__, e)
                if not got:
                    self._url_i += 1   # never got data on this address: try the next one
            self.ws = None
            self.hub.conn(self.stream, "down")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


# --------------------------------------------------------------------------- OKX
class Okx(WsVenue):
    """One OKX public connection. `mult` maps instId -> base units per contract."""

    def __init__(self, hub, cfg: dict, stream: str = "okx"):
        super().__init__(hub)
        self.stream, self.url, self.rest = stream, cfg["ws"], cfg["rest"]
        self.subs: list = []
        self.mult: dict = {}

    async def subscribe(self, args: list, op: str = "subscribe") -> None:
        if op == "subscribe":
            self.subs += [a for a in args if a not in self.subs]
        else:
            self.subs = [a for a in self.subs if a not in args]
        if self.ws is not None and args:
            await self._send(self.ws, args, op)

    async def _send(self, ws, args: list, op: str = "subscribe") -> None:
        for i in range(0, len(args), 20):
            await ws.send(json.dumps({"op": op, "args": args[i:i + 20]}))

    async def on_open(self, ws) -> None:
        await self._send(ws, list(self.subs))

    async def app_ping(self, ws) -> None:
        await ws.send("ping")

    async def load_mult(self, inst_type: str, **q) -> list:
        qs = "&".join("%s=%s" % kv for kv in q.items())
        j = json.loads(await aget("%s/api/v5/public/instruments?instType=%s&%s" % (self.rest, inst_type, qs)))
        for d in j.get("data", []):
            self.mult[d["instId"]] = 1.0 if inst_type == "SPOT" else (_f(d.get("ctVal")) or 1.0) * (_f(d.get("ctMult")) or 1.0)
        return j.get("data", [])

    def on_message(self, msg: str, r: int) -> None:
        if msg == "pong":
            self.app_pong(r)
            return
        j = json.loads(msg)
        if "event" in j:
            if j["event"] == "error":
                log.warning("%s: %s", self.stream, msg)
            self.emit(r, [{"c": "event", "ev": j["event"], "arg": j.get("arg"), "msg": j.get("msg")}], msg)
            return
        ch, norm = j["arg"]["channel"], []
        for d in j.get("data", []):
            inst, e = d.get("instId") or j["arg"].get("instId"), _f(d.get("ts"))
            if ch == "bbo-tbt":
                m = self.mult.get(inst, 1.0)
                b = d["bids"][0] if d.get("bids") else None
                a = d["asks"][0] if d.get("asks") else None
                norm.append({"c": "bbo", "i": inst, "e": e,
                             "b": _f(b[0]) if b else None, "B": _f(b[1]) * m if b else 0.0,
                             "a": _f(a[0]) if a else None, "A": _f(a[1]) * m if a else 0.0})
            elif ch == "trades":
                norm.append({"c": "trade", "i": inst, "e": e, "p": _f(d["px"]),
                             "q": _f(d["sz"]) * self.mult.get(inst, 1.0), "sd": d["side"][:1]})
            elif ch == "index-tickers":
                norm.append({"c": "index", "i": inst, "e": e, "px": _f(d["idxPx"])})
            elif ch == "mark-price":
                norm.append({"c": "mark", "i": inst, "e": e, "px": _f(d["markPx"])})
            elif ch == "funding-rate":
                norm.append({"c": "funding", "i": inst, "e": e, "rate": _f(d.get("fundingRate")),
                             "next": _f(d.get("fundingTime"))})
            elif ch == "opt-summary":
                # high volume: the full message is kept in "d"; only a marker goes in "n"
                norm.append({"c": "optsum", "i": inst, "e": e, "iv": _f(d.get("markVol"))})
            else:
                norm.append({"c": ch, "i": inst, "e": e})
        self.emit(r, norm, msg)


# --------------------------------------------------------------------------- Hyperliquid
class Hyperliquid(WsVenue):
    stream = "hl"
    ping_every = 20.0

    def __init__(self, hub, cfg: dict):
        super().__init__(hub)
        self.url, self.coins = cfg["ws"], cfg["coins"]

    async def on_open(self, ws) -> None:
        for coin in self.coins:
            for typ in ("bbo", "trades"):
                await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": typ, "coin": coin}}))

    async def app_ping(self, ws) -> None:
        await ws.send('{"method":"ping"}')

    def on_message(self, msg: str, r: int) -> None:
        j = json.loads(msg)
        ch, d = j.get("channel"), j.get("data")
        if ch == "pong":
            self.app_pong(r)
            return
        norm = []
        if ch == "bbo":
            b, a = d["bbo"]
            norm.append({"c": "bbo", "i": d["coin"], "e": d["time"],
                         "b": _f(b["px"]) if b else None, "B": _f(b["sz"]) if b else 0.0,
                         "a": _f(a["px"]) if a else None, "A": _f(a["sz"]) if a else 0.0})
        elif ch == "trades":
            for t in d:
                norm.append({"c": "trade", "i": t["coin"], "e": t["time"], "p": _f(t["px"]),
                             "q": _f(t["sz"]), "sd": "b" if t["side"] == "B" else "s"})
        else:
            norm.append({"c": "event", "ev": ch})
            if ch == "error":
                log.warning("hl: %s", msg)
        self.emit(r, norm, msg)


# --------------------------------------------------------------------------- Binance USDT-M futures
class Binance(WsVenue):
    stream = "binance"

    def __init__(self, hub, cfg: dict, symbol: str):
        super().__init__(hub)
        s = symbol.lower()
        self.url = "%s?streams=%s@bookTicker/%s@aggTrade" % (cfg["ws"], s, s)

    def on_message(self, msg: str, r: int) -> None:
        d = json.loads(msg).get("data") or {}
        ev, norm = d.get("e"), []
        if ev == "bookTicker":
            norm.append({"c": "bbo", "i": d["s"], "e": d.get("T") or d.get("E"),
                         "b": _f(d["b"]), "B": _f(d["B"]), "a": _f(d["a"]), "A": _f(d["A"])})
        elif ev == "aggTrade":
            norm.append({"c": "trade", "i": d["s"], "e": d["T"], "p": _f(d["p"]), "q": _f(d["q"]),
                         "sd": "s" if d["m"] else "b"})
        else:
            norm.append({"c": "event", "ev": ev})
        self.emit(r, norm, msg)


# --------------------------------------------------------------------------- Bybit linear
class Bybit(WsVenue):
    stream = "bybit"
    ping_every = 20.0

    def __init__(self, hub, cfg: dict, symbol: str):
        super().__init__(hub)
        self.url, self.symbol = cfg["ws"], symbol
        self.bid = self.ask = None   # level-1 book arrives as snapshot + deltas

    async def on_open(self, ws) -> None:
        self.bid = self.ask = None
        await ws.send(json.dumps({"op": "subscribe", "args": ["orderbook.1." + self.symbol, "publicTrade." + self.symbol]}))

    async def app_ping(self, ws) -> None:
        await ws.send('{"op":"ping"}')

    @staticmethod
    def _side(levels, old):
        live = [l for l in levels if _f(l[1])]
        if live:
            return (_f(live[0][0]), _f(live[0][1]))
        return None if levels else old   # only deletions: side is empty; no entries: unchanged

    def on_message(self, msg: str, r: int) -> None:
        j = json.loads(msg)
        topic, norm = j.get("topic", ""), []
        if topic.startswith("orderbook."):
            d = j["data"]
            if j.get("type") == "snapshot":
                self.bid = self.ask = None
            self.bid, self.ask = self._side(d.get("b", []), self.bid), self._side(d.get("a", []), self.ask)
            norm.append({"c": "bbo", "i": d["s"], "e": j.get("cts") or j.get("ts"),
                         "b": self.bid[0] if self.bid else None, "B": self.bid[1] if self.bid else 0.0,
                         "a": self.ask[0] if self.ask else None, "A": self.ask[1] if self.ask else 0.0})
        elif topic.startswith("publicTrade."):
            for t in j["data"]:
                norm.append({"c": "trade", "i": t["s"], "e": t["T"], "p": _f(t["p"]), "q": _f(t["v"]),
                             "sd": "b" if t["S"] == "Buy" else "s"})
        else:
            if j.get("op") == "ping" or j.get("ret_msg") == "pong":
                self.app_pong(r)
                return
            norm.append({"c": "event", "ev": j.get("op"), "ok": j.get("success")})
        self.emit(r, norm, msg)


# --------------------------------------------------------------------------- Gate USDT futures
class Gate(WsVenue):
    stream = "gate"

    def __init__(self, hub, cfg: dict, symbol: str, mult: float):
        super().__init__(hub)
        self.url, self.symbol, self.mult = cfg["ws"], symbol, mult   # mult: shares per contract

    async def on_open(self, ws) -> None:
        for ch in ("futures.book_ticker", "futures.trades"):
            await ws.send(json.dumps({"time": int(time.time()), "channel": ch, "event": "subscribe", "payload": [self.symbol]}))

    async def app_ping(self, ws) -> None:
        await ws.send(json.dumps({"time": int(time.time()), "channel": "futures.ping"}))

    def on_message(self, msg: str, r: int) -> None:
        j = json.loads(msg)
        ch, ev, d, norm = j.get("channel"), j.get("event"), j.get("result"), []
        if ch == "futures.pong":
            self.app_pong(r)
            return
        if ch == "futures.book_ticker" and ev == "update":
            norm.append({"c": "bbo", "i": d["s"], "e": d["t"],
                         "b": _f(d["b"]) or None, "B": (_f(d["B"]) or 0.0) * self.mult,
                         "a": _f(d["a"]) or None, "A": (_f(d["A"]) or 0.0) * self.mult})
        elif ch == "futures.trades" and ev == "update":
            for t in d:
                sz = _f(t["size"])
                norm.append({"c": "trade", "i": t["contract"], "e": t.get("create_time_ms") or t["create_time"] * 1000,
                             "p": _f(t["price"]), "q": abs(sz) * self.mult, "sd": "b" if sz > 0 else "s"})
        else:
            norm.append({"c": "event", "ev": ev, "ch": ch})
            if j.get("error"):
                log.warning("gate: %s", msg)
        self.emit(r, norm, msg)


# --------------------------------------------------------------------------- symbol discovery
async def resolve_symbol(venue: str, cfg: dict, ref_px: float, tol: float):
    """Returns (symbol, shares_per_contract) for the first candidate priced within tol of OKX, else None.
    Raises if the venue cannot be reached."""
    reached, last_err = False, None
    for sym in cfg["symbols"]:
        try:
            mult = 1.0
            if venue == "binance":
                px = _f(json.loads(await aget("%s/fapi/v1/ticker/price?symbol=%s" % (cfg["rest"], sym))).get("price"))
            elif venue == "bybit":
                lst = json.loads(await aget("%s/v5/market/tickers?category=linear&symbol=%s" % (cfg["rest"], sym)))["result"].get("list") or []
                px = _f(lst[0]["lastPrice"]) if lst else None
            else:
                j = json.loads(await aget("%s/api/v4/futures/usdt/contracts/%s" % (cfg["rest"], sym)))
                px, mult = _f(j.get("last_price")), _f(j.get("quanto_multiplier")) or 1.0
            reached = True
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):      # unknown symbol on a reachable venue
                reached = True
                continue
            last_err = e
            continue
        except Exception as e:
            last_err = e
            continue
        if px and abs(px / ref_px - 1.0) <= tol:
            return sym, mult
        log.warning("%s: %s priced %s vs OKX %.2f, not the same contract unit; skipped", venue, sym, px, ref_px)
    if not reached and last_err is not None:
        raise last_err
    return None
