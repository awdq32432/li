"""Defaults for the recorder and shadow engine. Override any of it with a JSON file (--config)."""
import copy
import json

DEFAULT = {
    "data_dir": "data/raw/okx_ant",
    "shadow_csv": "data/shadow/shadow_leadlag.csv",      # live view: the server's receive times
    "fair_csv": "data/shadow/shadow_fair.csv",           # fair view: exchange timestamps, hour by hour
    "conversion_log": "data/shadow/conversion_log.jsonl",
    "okx": {
        "ws": ["wss://ws.okx.com:8443/ws/v5/public", "wss://ws.okx.com/ws/v5/public"],
        "rest": "https://www.okx.com",
        "ant_swap": "ANTHROPIC-USDT-SWAP",
        "hype_swap": "HYPE-USDT-SWAP",
        "ant_spot": "xANT-USDT",            # subscribed as soon as OKX lists it
        "ant_index": "ANTHROPIC-USDT",
        "hype_index": "HYPE-USD",
        "opt_family": "HYPE-USD_UM",
        "opt_moneyness": 0.10,              # options within 10% of the money
        "opt_expiries": 3,                  # nearest three expiries
    },
    "hl": {
        "ws": "wss://api.hyperliquid.xyz/ws",
        "rest": "https://api.hyperliquid.xyz/info",
        "ant": "xyz:ANT",
        "coins": ["xyz:ANT", "HYPE"],
        "book_coins": ["xyz:ANT"],          # order-book depth snapshots
    },
    # Other Anthropic perpetuals. A candidate symbol is used only if its price is within
    # price_tol of OKX's, which rejects wrong tickers and contracts with a different share count.
    "binance": {"enabled": True, "symbols": ["ANTHROPICUSDT", "ANTUSDT"],
                "ws": "wss://fstream.binance.com/stream", "rest": "https://fapi.binance.com"},
    "bybit": {"enabled": True, "symbols": ["ANTHROPICUSDT", "ANTUSDT"],
              "ws": "wss://stream.bybit.com/v5/public/linear", "rest": "https://api.bybit.com"},
    "gate": {"enabled": True, "symbols": ["ANTHROPIC_USDT", "ANT_USDT"],
             "ws": "wss://fx-ws.gateio.ws/v4/ws/usdt", "rest": "https://api.gateio.ws"},
    "price_tol": 0.03,
    # Taker fee per side in bps. hl assumes the growth-mode HIP-3 rate; the others are the
    # venues' regular published tiers. None of these were read from an account.
    "fees_bps": {"hl": 0.9, "okx": 5.0, "binance": 5.0, "bybit": 5.5, "gate": 5.0},
    "engine": {
        "xs_bps": [6, 8, 12], "w_ms": 500, "lags_ms": [100, 250, 500], "holds_s": [2, 5],
        "cooldown_ms": 3000, "max_usd": 3000, "stale_ms": 30000,
    },
}


def _merge(a: dict, b: dict) -> dict:
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            _merge(a[k], v)
        else:
            a[k] = v
    return a


def load(path=None) -> dict:
    cfg = copy.deepcopy(DEFAULT)
    if path:
        with open(path) as f:
            _merge(cfg, json.load(f))
    return cfg


def engine_params(cfg: dict):
    from .engine import Params
    e = cfg["engine"]
    return Params(xs_bps=tuple(float(x) for x in e["xs_bps"]), w_ms=float(e["w_ms"]),
                  lags_ms=tuple(float(x) for x in e["lags_ms"]),
                  holds_s=tuple(float(x) for x in e["holds_s"]),
                  cooldown_ms=float(e["cooldown_ms"]), max_usd=float(e["max_usd"]),
                  stale_ms=float(e["stale_ms"]))
