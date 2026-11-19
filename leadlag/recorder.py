"""Recorder + live shadow engine.

    python -m leadlag.recorder [--config cfg.json] [--no-shadow]

Public market data only: no keys, no orders, no account access.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import time

from . import VERSION, config
from .engine import CsvSink, PairCfg, PairEngine
from .store import RawWriter
from .venues import Binance, Bybit, Gate, Hyperliquid, Okx, _f, aget, resolve_symbol

log = logging.getLogger("leadlag")
WATCHED = ("ruleType", "tickSz", "lever", "state", "initPxLmtPct", "floatPxLmtPct", "maxPxLmtPct")


class Hub:
    """Receives every message: writes it to disk, then feeds the shadow engines."""

    def __init__(self, cfg: dict, shadow: bool = True):
        self.cfg = cfg
        self.writer = RawWriter(cfg["data_dir"])
        self.sink = CsvSink(cfg["shadow_csv"]) if shadow else None
        self.params = config.engine_params(cfg)
        self.by_feed: dict = {}
        self.engines: list = []
        self.rule_type = None

    def conv_tag(self, t: float) -> str:
        if self.rule_type is None:
            return "unknown"
        return "pre_conversion" if self.rule_type == "pre_market" else "post_conversion"

    def add_pair(self, name: str, leader: str, follower: str, fee_bps: float) -> None:
        if self.sink is None:
            return
        eng = PairEngine(PairCfg(name, leader, follower, fee_bps), self.params, self.sink, self.conv_tag, "recv")
        self.engines.append(eng)
        for feed in (leader, follower):
            self.by_feed.setdefault(feed, []).append(eng)
        log.info("shadow pair %s: leader %s, follower %s, fee %.2f bps a side", name, leader, follower, fee_bps)

    def emit(self, stream: str, r: int, norm: list, raw) -> None:
        self.writer.write(stream, r, norm, raw)
        for n in norm:
            if n["c"] == "bbo":
                feed = stream + ":" + n["i"]
                engs = self.by_feed.get(feed)
                if engs:
                    for eng in engs:
                        try:
                            eng.on_bbo(feed, r / 1e6, n["b"], n["B"], n["a"], n["A"])
                        except Exception:   # the engine must never stop the recording
                            log.exception("shadow engine error on %s", feed)

    def conn(self, stream: str, state: str) -> None:
        self.writer.write(stream, time.time_ns(), [{"c": "conn", "st": state}], None)
        for feed, engs in self.by_feed.items():
            if feed.startswith(stream + ":"):
                for eng in engs:
                    eng.reset_feed(feed)

    def change(self, kind: str, inst: str, diffs: dict) -> None:
        os.makedirs(os.path.dirname(self.cfg["conversion_log"]) or ".", exist_ok=True)
        with open(self.cfg["conversion_log"], "a") as f:
            f.write(json.dumps({"ts_ms": int(time.time() * 1000),
                                "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                "kind": kind, "inst": inst, "changes": diffs}) + "\n")
        log.warning("CHANGE %s %s: %s", kind, inst, diffs)


async def every(period: float, fn, *a) -> None:
    while True:
        try:
            await fn(*a)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("%s failed: %s: %s", fn.__name__, type(e).__name__, e)
        await asyncio.sleep(period - time.time() % period)


def _diff(old: dict, new: dict) -> dict:
    return {k: [old.get(k), new.get(k)] for k in set(old) | set(new) if old.get(k) != new.get(k)}


async def poll_instrument(hub: Hub, okx: Okx, state: dict) -> None:
    """Every minute: the ANT swap's rules. A change to ruleType marks the conversion moment."""
    o = hub.cfg["okx"]
    inst = o["ant_swap"]
    raw = await aget("%s/api/v5/public/instruments?instType=SWAP&instId=%s" % (o["rest"], inst))
    d = json.loads(raw)["data"][0]
    okx.mult[inst] = (_f(d.get("ctVal")) or 1.0) * (_f(d.get("ctMult")) or 1.0)
    hub.emit("okx", time.time_ns(), [{"c": "instruments", "i": inst, "f": {k: d.get(k) for k in WATCHED}}], raw)
    hub.rule_type = d.get("ruleType")
    if state.get("inst") is not None:
        diffs = _diff(state["inst"], d)
        if diffs:
            hub.change("okx_instrument", inst, diffs)
    else:
        log.info("OKX %s: %s", inst, {k: d.get(k) for k in WATCHED})
    state["inst"] = d
    raw = await aget("%s/api/v5/public/price-limit?instId=%s" % (o["rest"], inst))
    p = json.loads(raw)["data"][0]
    hub.emit("okx", time.time_ns(), [{"c": "pxlimit", "i": inst, "e": _f(p.get("ts")),
                                      "buy": _f(p.get("buyLmt")), "sell": _f(p.get("sellLmt"))}], raw)


async def watch_spot(hub: Hub, okx: Okx) -> None:
    """xANT spot is not listed yet: check every 5 minutes and subscribe when it appears."""
    inst = hub.cfg["okx"]["ant_spot"]
    while True:
        try:
            if await okx.load_mult("SPOT", instId=inst):
                await okx.subscribe([{"channel": c, "instId": inst} for c in ("bbo-tbt", "trades")])
                hub.change("okx_spot_listed", inst, {})
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            pass   # OKX answers with an error until the instrument exists
        await asyncio.sleep(300)


async def refresh_options(hub: Hub, opt: Okx) -> None:
    """HYPE options within 10% of the money for the nearest three expiries."""
    o = hub.cfg["okx"]
    insts = await opt.load_mult("OPTION", instFamily=o["opt_family"])
    idx = _f(json.loads(await aget("%s/api/v5/market/index-tickers?instId=%s" % (o["rest"], o["hype_index"])))["data"][0]["idxPx"])
    now = time.time() * 1000
    live = {d["instId"]: d for d in insts if d.get("state") == "live" and _f(d["expTime"]) > now}
    exps = sorted({_f(d["expTime"]) for d in live.values()})[: o["opt_expiries"]]
    near = lambda d, tol: abs(_f(d["stk"]) / idx - 1.0) <= tol
    want = {i for i, d in live.items() if _f(d["expTime"]) in exps and near(d, o["opt_moneyness"])}
    have = {a["instId"] for a in opt.subs if a["channel"] == "bbo-tbt"}
    keep = {i for i in have if i in live and near(live[i], o["opt_moneyness"] * 1.5)}   # avoid flapping at the edge
    args = lambda ids: [{"channel": c, "instId": i} for i in sorted(ids) for c in ("bbo-tbt", "trades")]
    if want - have:
        await opt.subscribe(args(want - have))
    if have - want - keep:
        await opt.subscribe(args(have - want - keep), "unsubscribe")
    hub.emit("okx_opt", time.time_ns(), [{"c": "optset", "idx": idx, "n": len(want | keep),
                                          "insts": sorted(want | keep)}], None)


async def poll_hl_meta(hub: Hub, state: dict) -> None:
    """Hyperliquid's fee mode for xyz:ANT. The 0.9 bps fee assumption depends on growthMode."""
    h = hub.cfg["hl"]
    dex = h["ant"].split(":")[0]
    meta = json.loads(await aget(h["rest"], {"type": "meta", "dex": dex}))
    u = next((x for x in meta["universe"] if x["name"] == h["ant"]), None)
    if u is None:
        return
    hub.emit("hl", time.time_ns(), [{"c": "meta", "i": h["ant"], "f": u}], json.dumps(u))
    if state.get("hl") is not None and _diff(state["hl"], u):
        hub.change("hl_meta", h["ant"], _diff(state["hl"], u))
    elif state.get("hl") is None:
        log.info("Hyperliquid %s: %s", h["ant"], u)
    state["hl"] = u


async def other_venue(hub: Hub, name: str, leader: str) -> None:
    """Binance / Bybit / Gate: find the Anthropic contract, then record it and shadow it."""
    cfg, o = hub.cfg[name], hub.cfg["okx"]
    while True:
        try:
            ref = _f(json.loads(await aget("%s/api/v5/market/ticker?instId=%s" % (o["rest"], o["ant_swap"])))["data"][0]["last"])
            found = await resolve_symbol(name, cfg, ref, hub.cfg["price_tol"])
            if found:
                sym, mult = found
                log.info("%s: recording %s (%.4g shares per contract)", name, sym, mult)
                hub.writer.write(name, time.time_ns(), [{"c": "symbol", "i": sym, "mult": mult}], None)
                venue = {"binance": lambda: Binance(hub, cfg, sym), "bybit": lambda: Bybit(hub, cfg, sym),
                         "gate": lambda: Gate(hub, cfg, sym, mult)}[name]()
                hub.add_pair("okx>" + name, leader, "%s:%s" % (name, sym), hub.cfg["fees_bps"][name])
                await venue.run()
            log.warning("%s: no Anthropic contract matched %s; retry in 10 min", name, cfg["symbols"])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("%s: unreachable (%s: %s); retry in 10 min", name, type(e).__name__, e)
        await asyncio.sleep(600)


async def housekeeping(hub: Hub) -> None:
    last, n = time.time(), 0
    while True:
        await asyncio.sleep(0.02)
        now = time.time_ns() / 1e6
        for eng in hub.engines:
            try:
                eng.advance(now)
            except Exception:
                log.exception("shadow engine error")
        n += 1
        if n % 50 == 0:
            hub.writer.flush()
        if time.time() - last >= 60:
            last = time.time()
            log.info("msgs %s | shadow signals %s | %s", dict(sorted(hub.writer.counts.items())),
                     {e.cfg.name: e.n_signals for e in hub.engines}, hub.conv_tag(now))


async def main_async(cfg: dict, shadow: bool) -> None:
    hub = Hub(cfg, shadow)
    o, h = cfg["okx"], cfg["hl"]
    okx, opt = Okx(hub, o, "okx"), Okx(hub, o, "okx_opt")
    for inst in (o["ant_swap"], o["hype_swap"]):
        try:
            await okx.load_mult("SWAP", instId=inst)
        except Exception as e:
            log.warning("could not load %s contract size: %s", inst, e)
    okx.subs = ([{"channel": c, "instId": i} for i in (o["ant_swap"], o["hype_swap"]) for c in ("bbo-tbt", "trades")]
                + [{"channel": "index-tickers", "instId": i} for i in (o["ant_index"], o["hype_index"])]
                + [{"channel": c, "instId": o["ant_swap"]} for c in ("mark-price", "funding-rate")])
    opt.subs = [{"channel": "opt-summary", "instFamily": o["opt_family"]}]
    okx_feed, hl_feed = "okx:" + o["ant_swap"], "hl:" + h["ant"]
    hub.add_pair("okx>hl", okx_feed, hl_feed, cfg["fees_bps"]["hl"])
    hub.add_pair("hl>okx", hl_feed, okx_feed, cfg["fees_bps"]["okx"])   # control, expected to lose
    state: dict = {}
    jobs = [okx.run(), opt.run(), Hyperliquid(hub, h).run(), housekeeping(hub), watch_spot(hub, okx),
            every(60, poll_instrument, hub, okx, state), every(300, refresh_options, hub, opt),
            every(300, poll_hl_meta, hub, state)]
    jobs += [other_venue(hub, v, okx_feed) for v in ("binance", "bybit", "gate") if cfg[v].get("enabled")]
    tasks = [asyncio.create_task(j) for j in jobs]
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, stop.set)
    log.info("%s recording to %s", VERSION, cfg["data_dir"])
    await stop.wait()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    hub.writer.close()
    if hub.sink:
        hub.sink.close()
    log.info("stopped cleanly")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config")
    ap.add_argument("--no-shadow", action="store_true", help="record only")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("websockets").setLevel(logging.WARNING)
    asyncio.run(main_async(config.load(a.config), not a.no_shadow))


if __name__ == "__main__":
    main()
