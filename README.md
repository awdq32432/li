# The OKX bot (`leadlag`): OKX ANT recorder and shadow test

Records public market data for the Anthropic perpetuals and runs a shadow test of
"OKX moves, take the follower". **It places no orders, reads no keys and touches no account.**
Version line: `leadlag-0.2.1` (separate from the Omnibook version history). The owner calls this "the OKX bot".

## Start it on the server

```bash
git clone https://github.com/awdq32432/li ~/li && cd ~/li && git checkout claude/youthful-mendel-l5c0ny
sudo bash deploy/install.sh
```

That creates `.venv`, runs the tests, and starts two services that restart on failure and on reboot:

| Service | What it does |
|---|---|
| `leadlag-recorder` | records every feed and runs the shadow engine live |
| `leadlag-report` | prints the hourly report and appends it to `data/reports/leadlag_health.log` |

Useful commands:

```bash
.venv/bin/python -m leadlag.report --minutes 60        # the report, now
journalctl -u leadlag-recorder -f                      # recorder log
cat data/shadow/conversion_log.jsonl                   # every rule change OKX or Hyperliquid made
.venv/bin/python -m leadlag.replay --out data/shadow/replay.csv --lags-ms 100,250,500,180
```

Without systemd: `python -m leadlag.recorder` and `python -m leadlag.report --loop`.

## What is recorded

Under `data/raw/okx_ant/<stream>/<YYYYMMDD>/<stream>_<YYYYMMDD>_<HH>.jsonl` (gzipped when the hour ends):

| Stream | Contents |
|---|---|
| `okx` | `bbo-tbt` and `trades` for `ANTHROPIC-USDT-SWAP`, `HYPE-USDT-SWAP` and `xANT-USDT` spot once OKX lists it; `index-tickers` for `ANTHROPIC-USDT` and `HYPE-USD`; `mark-price` and `funding-rate`; the instrument rules and price limits, re-pulled every minute |
| `okx_opt` | `bbo-tbt` and `trades` for every `HYPE-USD_UM` option within 10% of the money for the nearest three expiries (re-chosen every 5 minutes), plus `opt-summary` |
| `hl` | `bbo` and `trades` for `xyz:ANT` and `HYPE`; order-book depth snapshots for `xyz:ANT` (Hyperliquid sends one about every 12 s); the `xyz:ANT` fee mode (`growthMode`) every 5 minutes |
| `binance`, `bybit`, `gate` | best bid/offer and trades for each venue's Anthropic perpetual |

One line per message:

```json
{"r": 1795103311203456789, "s": "okx", "n": [{"c":"bbo","i":"ANTHROPIC-USDT-SWAP","e":1795103311103,"b":104.19,"B":11.69,"a":104.2,"A":5.91}], "d": {...the exchange's message, untouched...}}
```

- `r` is the server's receive time in nanoseconds; `e` is the exchange's own timestamp in milliseconds. Both are kept for every message.
- `n` is a normalised copy: `bbo` (b/a prices, B/A sizes), `trade` (p, q, sd = b or s), `index`, `mark`, `funding`, `instruments`, `conn` (up/down), `rtt` (ping round trip). **Sizes are in shares or HYPE, not contracts.**
- This is the recorder's own format. It is not the Omnibook format, so the Omnibook replay tools will not read it; use `leadlag.replay`.

**The conversion moment.** Any change to the OKX instrument (`ruleType`, `tickSz`, `lever`, price limits, anything else) is written to `data/shadow/conversion_log.jsonl` with a timestamp. Shadow rows are tagged `pre_conversion` while `ruleType` is `pre_market` and `post_conversion` after.

**Other venues.** Each venue has a list of candidate tickers. One is used only if its price is within 3% of OKX's, so a wrong ticker or a contract with a different share count is rejected and logged. A venue that cannot be reached is retried every 10 minutes.

## The shadow engine

- Signal: the leader's mid price moved at least `X` bps within the last `W` = 500 ms (measured against the highest and lowest mid in that window). At most one signal every 3 s per `X`.
- Entry: at signal + `L` ms, buy the follower's best offer (leader went up) or sell its best bid (leader went down), using the last follower quote received by that time. Size = the smaller of the displayed size and $3,000.
- Exit: `H` seconds after entry, by taking the other side.
- Combinations, all from the same recording: `X` = 6, 8, 12 bps; `L` = 100, 250, 500 ms; `H` = 2, 5 s.
- Pairs: `okx>hl` (fee 0.9 bps a side), `hl>okx` (5 bps, the control), `okx>binance`, `okx>bybit`, `okx>gate` (5, 5.5, 5 bps).
- Live, the clock is the server's receive time, which is what a bot on this server would see.

`data/shadow/shadow_leadlag.csv` has one row per signal and combination:

| Column | Meaning |
|---|---|
| `signal_ts_ms`, `signal_utc`, `dir` | when, and +1 (leader up, shadow buy) or -1 |
| `x_bps`, `w_ms`, `lag_ms`, `hold_s` | the combination |
| `leader_move_bps`, `leader_mid` | the move that fired the signal |
| `follower_mid_at_signal`, `follower_premove_bps` | follower mid, and how far it had already moved since 500 ms before the signal |
| `entry_px`, `entry_disp_sz`, `entry_quote_age_ms`, `size_usd` | the shadow fill; displayed size is in shares |
| `exit_px`, `exit_disp_sz`, `exit_quote_age_ms` | the shadow exit |
| `fee_bps_side`, `gross_bps`, `net_bps`, `net_usd` | profit before and after two fees |
| `hours_tag` | `us_regular`, `us_pre`, `us_post`, `overnight`, `weekend`, `holiday` (New York time) |
| `conv_tag` | `pre_conversion`, `post_conversion`, `unknown` |
| `status` | `ok`, or `no_entry_quote` / `no_exit_quote` when the follower had no usable quote |

## Fair view and live view

The shadow test is reported two ways, side by side:

- **FAIR** replays each finished hour with both venues lined up on their own timestamps
  (`data/shadow/shadow_fair.csv`). **Judge the trade by this.**
- **LIVE** uses the server's receive times (`data/shadow/shadow_leadlag.csv`). Hyperliquid's quotes
  arrive about 300 ms old, so LIVE "fills" at prices a real order would no longer get.

FAIR is updated whenever the report runs. It treats Hyperliquid's timestamp as the moment a quote
became takeable; that is believed to be the block time but has not been verified with Hyperliquid.

## The hourly report

Per pair: signals per hour; average net bps, t-statistic and share of winners, FAIR for the last
hour, FAIR for all time and LIVE for all time. `okx>hl` shows every combination and the split by
market hours and by pre/post conversion; the 5 bps venues show the headline rows (`--full` shows
everything). Then feed health and ping round trips, disk use and days of space left, the lag at which cross-correlation peaks for
each pair of venues, the OKX to Hyperliquid price gap, and for HYPE options trades per hour, HYPE
traded, median half-spread in % of price and the quoter's typical size.

## The edge map

```bash
.venv/bin/python -m leadlag.edgemap                 # okx>hl, 250 ms delay, 2 s hold, fair view
.venv/bin/python -m leadlag.edgemap --lag 100
.venv/bin/python -m leadlag.edgemap --lag 100 --conv post_conversion    # one regime only
```

Splits every signal by the conditions when it fired: size of the leader's move, how far the
follower had already moved, how far it sat behind the leader, its spread, the size shown, quote age,
direction, market hours, conversion. Each bucket shows net bps for the earlier 60% of the data and
for the later 40%. Only buckets positive in both, with at least 30 trades in each, are listed at
the end. With about 30 buckets tested, one or two will look good by luck: a bucket is a lead to
test on new days, not a rule to trade. Two shadow columns feed it: `gap_bps` (leader over follower
at the signal) and `entry_spread_bps`.

## Settings

Defaults are in `leadlag/config.py`. Override any of them with a JSON file: `python -m leadlag.recorder --config my.json`, for example `{"fees_bps": {"hl": 9.0}}` or `{"binance": {"symbols": ["XYZUSDT"]}}`.

## What the shadow test does not tell you

- It assumes a fill at the displayed price. A faster trader may take that quote first.
- The exit ignores the displayed size on the exit side (it is recorded in `exit_disp_sz`).
- The fees are assumptions, not account rates. Hyperliquid's 0.9 bps depends on `growthMode` staying enabled; a change is logged.
- The ping figures are market-data round trips. An order round trip to Hyperliquid can only be measured with an account.
- Early market closes (1 pm New York) are tagged as normal days.
