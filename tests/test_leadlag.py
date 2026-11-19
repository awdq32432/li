import json
import os
import tempfile
import unittest

from leadlag import config
from leadlag.engine import COLUMNS, CsvSink, PairCfg, PairEngine, Params
from leadlag.hours import hours_tag
from leadlag.report import _grid, _rets, load_rows, xcorr_peak
from leadlag.store import RawWriter, read_events
from leadlag.venues import Binance, Bybit, Gate, Hyperliquid, Okx

L, F = "okx:L", "hl:F"
T0 = 1795100000000.0   # 19 Nov 2026, US regular hours


def engine(rows, **kw):
    p = Params(**kw)
    return PairEngine(PairCfg("t", L, F, 0.9), p, rows.append, lambda t: "pre_conversion")


def q(e, feed, t, mid, half=0.005, sz=100.0):
    e.on_bbo(feed, T0 + t, mid - half, sz, mid + half, sz)


class EngineTest(unittest.TestCase):
    def test_up_move_entry_exit_and_pnl(self):
        rows = []
        e = engine(rows)
        q(e, F, 0, 100.00)
        q(e, L, 0, 100.00)
        q(e, L, 200, 100.10)             # +10 bps in 200 ms: fires X=6 and X=8, not 12
        q(e, F, 320, 100.05)             # follower reprices 120 ms after the signal
        q(e, F, 2000, 100.10)
        e.advance(T0 + 10000)
        self.assertEqual(e.n_signals, 2)
        self.assertEqual(len(rows), 2 * 3 * 2)
        self.assertEqual({r["x_bps"] for r in rows}, {6.0, 8.0})
        r = next(r for r in rows if r["x_bps"] == 8 and r["lag_ms"] == 100 and r["hold_s"] == 2)
        self.assertEqual(r["dir"], 1)
        self.assertAlmostEqual(r["entry_px"], 100.005)          # stale ask, taken at signal + 100 ms
        self.assertAlmostEqual(r["exit_px"], 100.095)           # bid 2 s after entry
        gross = (100.095 / 100.005 - 1) * 1e4
        self.assertAlmostEqual(r["gross_bps"], gross, places=2)
        self.assertAlmostEqual(r["net_bps"], gross - 1.8, places=2)
        self.assertEqual(r["size_usd"], 3000.0)                 # capped: 100 shares shown is $10,000
        self.assertAlmostEqual(r["net_usd"], 3000 * (gross - 1.8) / 1e4, places=3)
        self.assertEqual((r["hours_tag"], r["conv_tag"], r["status"]), ("us_regular", "pre_conversion", "ok"))
        late = next(r for r in rows if r["x_bps"] == 8 and r["lag_ms"] == 500 and r["hold_s"] == 2)
        self.assertAlmostEqual(late["entry_px"], 100.055)       # by 500 ms the follower has repriced
        self.assertEqual(late["entry_quote_age_ms"], 380)
        self.assertEqual(r["follower_mid_at_signal"], 100.0)

    def test_quote_arriving_exactly_at_delay_is_used_later_one_is_not(self):
        rows = []
        e = engine(rows, xs_bps=(8.0,), lags_ms=(100.0,), holds_s=(2.0,))
        q(e, F, 0, 100.0)
        q(e, L, 0, 100.0)
        q(e, L, 100, 100.10)
        q(e, F, 200, 100.02)             # exactly signal + 100 ms: visible
        q(e, F, 201, 100.50)             # 1 ms too late
        e.advance(T0 + 9000)
        self.assertAlmostEqual(rows[0]["entry_px"], 100.025)

    def test_down_move_sells_bid_and_buys_back_ask(self):
        rows = []
        e = engine(rows, xs_bps=(8.0,), lags_ms=(100.0,), holds_s=(2.0,))
        q(e, F, 0, 100.0)
        q(e, L, 0, 100.0)
        q(e, L, 100, 99.90)
        q(e, F, 1000, 99.90)
        e.advance(T0 + 9000)
        r = rows[0]
        self.assertEqual(r["dir"], -1)
        self.assertAlmostEqual(r["entry_px"], 99.995)
        self.assertAlmostEqual(r["exit_px"], 99.905)
        self.assertGreater(r["net_bps"], 0)

    def test_slow_move_does_not_fire(self):
        rows = []
        e = engine(rows)
        q(e, F, 0, 100.0)
        for i in range(21):              # 10 bps spread over 2 s
            q(e, L, i * 100, 100.0 + 0.005 * i)
        self.assertEqual(e.n_signals, 0)

    def test_cooldown_three_seconds_per_threshold(self):
        rows = []
        e = engine(rows, xs_bps=(8.0,))
        q(e, F, 0, 100.0)
        q(e, L, 0, 100.0)
        q(e, L, 100, 100.10)
        q(e, L, 1000, 100.10)
        q(e, L, 1100, 100.20)            # second jump 1 s later: suppressed
        self.assertEqual(e.n_signals, 1)
        q(e, L, 3200, 100.20)
        q(e, L, 3300, 100.30)            # 3.2 s after the first: allowed
        self.assertEqual(e.n_signals, 2)

    def test_small_displayed_size_and_missing_quote(self):
        rows = []
        e = engine(rows, xs_bps=(8.0,), lags_ms=(100.0,), holds_s=(2.0,))
        q(e, F, 0, 100.0, sz=5.0)
        q(e, L, 0, 100.0)
        q(e, L, 100, 100.10)
        e.advance(T0 + 9000)
        self.assertAlmostEqual(rows[0]["size_usd"], 500.03, places=1)   # 5 shares shown
        e.reset_feed(F)                  # follower disconnected: no quote to take
        q(e, L, 20000, 100.10)
        q(e, L, 20100, 100.20)
        e.advance(T0 + 30000)
        self.assertEqual(rows[1]["status"], "no_entry_quote")

    def test_stale_follower_and_leader_reset(self):
        rows = []
        e = engine(rows, xs_bps=(8.0,), lags_ms=(100.0,), holds_s=(2.0,), stale_ms=5000.0)
        q(e, F, 0, 100.0)
        q(e, L, 10000, 100.0)
        q(e, L, 10100, 100.10)
        e.advance(T0 + 20000)
        self.assertEqual(rows[0]["status"], "no_entry_quote")   # follower quote is 10 s old
        e.reset_feed(L)                  # leader reconnects at a new price: not a signal
        q(e, L, 30000, 101.0)
        self.assertEqual(e.n_signals, 1)

    def test_csv_has_every_column(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.csv")
            sink = CsvSink(path)
            e = PairEngine(PairCfg("t", L, F, 0.9), Params(), sink)
            q(e, F, 0, 100.0)
            q(e, L, 0, 100.0)
            q(e, L, 100, 100.10)
            e.advance(T0 + 9000)
            sink.close()
            rows = load_rows(path)
            self.assertEqual(len(rows), 12)               # 10 bps fires X=6 and X=8
            self.assertEqual(list(rows[0].keys()), COLUMNS)
            self.assertTrue(all(r["status"] == "ok" and r["net_bps"] is not None for r in rows))


class HoursTest(unittest.TestCase):
    def ms(self, s):
        from datetime import datetime
        return datetime.fromisoformat(s).timestamp() * 1000

    def test_tags(self):
        self.assertEqual(hours_tag(self.ms("2026-11-19T15:00:00+00:00")), "us_regular")   # 10:00 ET
        self.assertEqual(hours_tag(self.ms("2026-11-19T14:29:00+00:00")), "us_pre")
        self.assertEqual(hours_tag(self.ms("2026-11-19T21:00:00+00:00")), "us_post")      # 16:00 ET
        self.assertEqual(hours_tag(self.ms("2026-11-20T03:00:00+00:00")), "overnight")
        self.assertEqual(hours_tag(self.ms("2026-11-21T15:00:00+00:00")), "weekend")
        self.assertEqual(hours_tag(self.ms("2026-11-21T02:00:00+00:00")), "weekend")      # Fri 21:00 ET
        self.assertEqual(hours_tag(self.ms("2026-11-23T02:00:00+00:00")), "overnight")    # Sun 21:00 ET
        self.assertEqual(hours_tag(self.ms("2026-11-26T15:00:00+00:00")), "holiday")      # Thanksgiving
        self.assertEqual(hours_tag(self.ms("2026-07-01T13:45:00+00:00")), "us_regular")   # summer time


class Capture:
    def __init__(self):
        self.out = []

    def emit(self, stream, r, norm, raw):
        self.out += norm

    def conn(self, *a): ...


class ParserTest(unittest.TestCase):
    def test_okx(self):
        h = Capture()
        v = Okx(h, config.DEFAULT["okx"])
        v.mult["HYPE-USD_UM-261120-68-C"] = 0.1
        v.on_message('{"arg":{"channel":"bbo-tbt","instId":"ANTHROPIC-USDT-SWAP"},"data":[{"asks":[["104.2","5.91","0","3"]],"bids":[["104.19","11.69","0","7"]],"ts":"1795103311203","seqId":1}]}', 1)
        v.on_message('{"arg":{"channel":"bbo-tbt","instId":"HYPE-USD_UM-261120-68-C"},"data":[{"asks":[["1.5","100","0","1"]],"bids":[],"ts":"1795103311203","seqId":1}]}', 1)
        v.on_message('{"arg":{"channel":"trades","instId":"ANTHROPIC-USDT-SWAP"},"data":[{"instId":"ANTHROPIC-USDT-SWAP","tradeId":"1","px":"104.19","sz":"0.1","side":"sell","ts":"1795103311500"}]}', 1)
        v.on_message('{"arg":{"channel":"index-tickers","instId":"ANTHROPIC-USDT"},"data":[{"instId":"ANTHROPIC-USDT","idxPx":"104.164","ts":"1795103311033"}]}', 1)
        a, b, t, i = h.out
        self.assertEqual((a["b"], a["B"], a["a"], a["A"], a["e"]), (104.19, 11.69, 104.2, 5.91, 1795103311203.0))
        self.assertEqual((b["b"], b["B"], b["a"]), (None, 0.0, 1.5))
        self.assertAlmostEqual(b["A"], 10.0)                     # 100 contracts of 0.1 HYPE
        self.assertEqual((t["p"], t["q"], t["sd"]), (104.19, 0.1, "s"))
        self.assertEqual(i["px"], 104.164)

    def test_hyperliquid(self):
        h = Capture()
        v = Hyperliquid(h, config.DEFAULT["hl"])
        v.on_message('{"channel":"bbo","data":{"coin":"xyz:ANT","time":1795103311000,"bbo":[{"px":"104.0","sz":"60.5","n":2},null]}}', 1)
        v.on_message('{"channel":"trades","data":[{"coin":"xyz:ANT","side":"B","px":"104.1","sz":"2","time":1795103311001,"hash":"0x","tid":1}]}', 1)
        self.assertEqual((h.out[0]["b"], h.out[0]["B"], h.out[0]["a"], h.out[0]["A"]), (104.0, 60.5, None, 0.0))
        self.assertEqual((h.out[1]["p"], h.out[1]["sd"]), (104.1, "b"))

    def test_gate_sizes_are_converted_to_shares(self):
        h = Capture()
        v = Gate(h, config.DEFAULT["gate"], "ANT_USDT", 0.01)
        v.on_message('{"time":1,"channel":"futures.book_ticker","event":"update","result":{"t":1795103311000,"u":1,"s":"ANT_USDT","b":"104.1","B":2500,"a":"104.2","A":100}}', 1)
        v.on_message('{"time":1,"channel":"futures.trades","event":"update","result":[{"size":-300,"id":1,"create_time":1795103311,"create_time_ms":1795103311500,"price":"104.1","contract":"ANT_USDT"}]}', 1)
        self.assertEqual((h.out[0]["B"], h.out[0]["A"]), (25.0, 1.0))
        self.assertEqual((h.out[1]["q"], h.out[1]["sd"]), (3.0, "s"))

    def test_binance_quotes_and_trades(self):
        h = Capture()
        v = Binance(h, config.DEFAULT["binance"], "ANTUSDT")
        self.assertTrue(v.url.endswith("?streams=antusdt@bookTicker/antusdt@trade"))
        v.on_message('{"stream":"antusdt@bookTicker","data":{"e":"bookTicker","u":1,"E":5,"T":4,"s":"ANTUSDT","b":"107.10","B":"3.5","a":"107.12","A":"1.2"}}', 1)
        v.on_message('{"stream":"antusdt@trade","data":{"e":"trade","E":1795104957121,"T":1795104957121,"s":"ANTUSDT","t":2772107,"p":"107.10000","q":"0.15","X":"MARKET","m":true,"st":1}}', 1)
        v.on_message('{"stream":"antusdt@trade","data":{"e":"trade","E":1,"T":1,"s":"ANTUSDT","t":1,"p":"107.1","q":"9","X":"INSURANCE_FUND","m":false}}', 1)
        self.assertEqual((h.out[0]["b"], h.out[0]["A"], h.out[0]["e"]), (107.10, 1.2, 4))
        self.assertEqual((h.out[1]["c"], h.out[1]["p"], h.out[1]["q"], h.out[1]["sd"]), ("trade", 107.1, 0.15, "s"))
        self.assertEqual(h.out[2]["c"], "event")

    def test_bybit_level1_deltas(self):
        h = Capture()
        v = Bybit(h, config.DEFAULT["bybit"], "ANTUSDT")
        v.on_message('{"topic":"orderbook.1.ANTUSDT","type":"snapshot","ts":5,"cts":4,"data":{"s":"ANTUSDT","b":[["104.1","3"]],"a":[["104.2","4"]],"u":1,"seq":1}}', 1)
        v.on_message('{"topic":"orderbook.1.ANTUSDT","type":"delta","ts":6,"cts":5,"data":{"s":"ANTUSDT","b":[],"a":[["104.2","0"],["104.3","7"]],"u":2,"seq":2}}', 1)
        self.assertEqual((h.out[1]["b"], h.out[1]["B"], h.out[1]["a"], h.out[1]["A"]), (104.1, 3.0, 104.3, 7.0))


class StoreAndStatsTest(unittest.TestCase):
    def test_round_trip_keeps_both_timestamps_and_raw(self):
        with tempfile.TemporaryDirectory() as d:
            w = RawWriter(d)
            r = int(T0 * 1e6)
            w.write("okx", r, [{"c": "bbo", "i": "X", "e": T0 - 5, "b": 1.0, "B": 2.0, "a": 1.1, "A": 3.0}], '{"raw":1}')
            w.close()
            ev = list(read_events(d, "okx", T0 - 1000, T0 + 1000))
            self.assertEqual(ev[0][0], T0)
            self.assertEqual(ev[0][1]["e"], T0 - 5)
            path = [os.path.join(dp, f) for dp, _, fs in os.walk(d) for f in fs][0]
            with open(path) as f:
                self.assertEqual(json.loads(f.readline())["d"], {"raw": 1})

    def test_xcorr_finds_a_known_lag(self):
        import random
        random.seed(1)
        a, px = [], 100.0
        for i in range(4000):
            if random.random() < 0.2:
                px *= 1 + random.gauss(0, 1e-4)
            a.append((i * 50.0, px))
        b = [(t + 250.0, p) for t, p in a]        # b repeats a 250 ms later
        ga, gb = _grid(a, 0.0, 4000), _grid(b, 0.0, 4000)
        k, c = xcorr_peak(_rets(ga), _rets(gb))
        self.assertEqual(k * 50, 250)
        self.assertGreater(c, 0.9)


if __name__ == "__main__":
    unittest.main()
