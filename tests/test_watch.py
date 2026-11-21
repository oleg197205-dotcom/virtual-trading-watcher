import copy
import datetime as dt
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "watcher"))
import watch  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..", "state")
RULES = json.load(open(os.path.join(ROOT, "rules.json"), encoding="utf-8"))
STATE = json.load(open(os.path.join(os.path.dirname(__file__), "fixture_state.json"), encoding="utf-8"))


def closes(start, step, n=60, end="2026-11-21"):
    d0 = dt.date.fromisoformat(end) - dt.timedelta(days=n - 1)
    return [{"d": (d0 + dt.timedelta(days=i)).isoformat(), "c": start + step * i} for i in range(n)]


class Fake(watch.Data):
    def __init__(self):
        self.cp = {"BTC": 55000.0, "ETH": 1550.0, "SOL": 65.0}
        self.hist = {"BTC": closes(80000, -400), "ETH": closes(2500, -15), "SOL": closes(100, -0.6)}
        self.coin_list = []
        self.single = {}
        self.stock_open = False
        self.stock_px = {}
        self.fail = set()

    def crypto_prices(self):
        if "kraken" in self.fail:
            raise RuntimeError("down")
        return {k: {"price": v, "low24": v * 0.97, "high24": v * 1.03} for k, v in self.cp.items()}

    def crypto_daily(self, sym):
        return copy.deepcopy(self.hist[sym])

    def coins(self):
        return copy.deepcopy(self.coin_list)

    def coin(self, cid):
        return copy.deepcopy(self.single.get(cid))

    def stock(self, sym):
        now = dt.datetime(2026, 11, 23, 16, 0, tzinfo=dt.timezone.utc).timestamp()
        o, c = (now - 3600, now + 3600) if self.stock_open else (now + 3600, now + 7200)
        p = self.stock_px.get(sym, 100.0)
        return {"price": p, "time": now - 60, "open": o, "close": c, "closes": closes(p * 0.8, p * 0.2 / 59)}


def coin(sym, price, ch24, ch1h=1.0, vol=5e7, cid=None, rank=50):
    return {"id": cid or sym.lower(), "sym": sym, "name": sym.title(), "nameid": sym.lower(), "price": price,
            "ch1h": ch1h, "ch24": ch24, "ch7d": 0, "vol": vol, "rank": rank}


T = dt.datetime(2026, 11, 21, 19, 0, tzinfo=dt.timezone.utc)


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.s = copy.deepcopy(STATE)
        self.d = Fake()

    def run_at(self, t=T, cmds=None):
        watch.Run(self.s, RULES, self.d, t, cmds).go()

    def test_icnt_below_stop_is_sold(self):
        self.d.coin_list = [coin("ICNT", 0.351697, -12.2, cid="161661", rank=180)]
        self.run_at()
        self.assertNotIn("ICNT", self.s["books"]["hype"]["positions"])
        sell = self.s["trades"][-1]
        self.assertEqual((sell["asset"], sell["side"], sell["exit"]), ("ICNT", "sell", "trail"))
        self.assertAlmostEqual(sell["pnl"], 4175.3569 * 0.351697 * 0.994 - 1808.58, places=1)
        self.assertEqual(self.s["books"]["hype"]["cooldown"]["ICNT"], "2026-11-21")
        self.assertEqual(self.s["health"]["errors"], [])

    def test_downtrend_no_trend_buy_and_totals(self):
        self.d.coin_list = [coin("ICNT", 0.45, 1, cid="161661")]
        self.run_at()
        self.assertEqual(self.s["books"]["trend"]["positions"], {})
        snap = self.s["equityDaily"]["2026-11-21"]
        self.assertAlmostEqual(snap["benchmark"], 0.04490947 * 55000 + 1.61289539 * 1550 + 39.74144123 * 65, places=1)
        self.assertAlmostEqual(snap["hype"], 9616.94 + 4175.3569 * 0.45, places=1)

    def test_uptrend_buy_then_stop(self):
        self.d.hist["SOL"] = closes(60, 0.5)  # rising: last close 89.5
        self.d.cp["SOL"] = 90.0
        self.d.coin_list = [coin("ICNT", 0.45, 1, cid="161661")]
        self.run_at()
        pos = self.s["books"]["trend"]["positions"]["SOL"]
        self.assertLess(pos["stop"], 90)
        self.assertAlmostEqual(pos["target"], 90 + 2 * (90 - pos["stop"]), places=1)
        risk = pos["qty"] * (90 - pos["stop"])
        self.assertLessEqual(risk, 100.5)  # 1% of $10 000
        self.assertLessEqual(pos["qty"] * 90, 3000.5)  # max 30%
        # second buy the same day is not allowed
        self.d.hist["BTC"] = closes(50000, 200)
        self.d.cp["BTC"] = 62000.0
        self.run_at(T + dt.timedelta(minutes=5))
        self.assertNotIn("BTC", self.s["books"]["trend"]["positions"])
        # next day price crashes to the stop
        self.d.cp["SOL"] = pos["stop"] - 1
        self.run_at(T + dt.timedelta(days=1))
        self.assertNotIn("SOL", self.s["books"]["trend"]["positions"])
        sol_sell = [x for x in self.s["trades"] if x["asset"] == "SOL" and x["side"] == "sell"][-1]
        self.assertEqual(sol_sell["exit"], "stop")
        self.assertLess(sol_sell["pnl"], 0)

    def test_hype_entry_trail_and_exit(self):
        self.d.coin_list = [coin("ICNT", 0.45, 1, cid="161661"), coin("PUMP", 1.0, 40, 2, 9e7),
                            coin("SMALL", 2.0, 60, 2, 1e5), coin("USDX", 1.0, 0.1)]
        self.run_at()
        b = self.s["books"]["hype"]
        self.assertIn("PUMP", b["positions"])
        self.assertNotIn("SMALL", b["positions"])
        p = b["positions"]["PUMP"]
        self.assertAlmostEqual(p["stop"], 0.88, places=6)
        self.assertFalse(p["newsChecked"])
        verdicts = {c["sym"]: c["verdict"] for c in self.s["scan"]["candidates"]}
        self.assertEqual(verdicts["SMALL"], "мал объём")
        self.assertNotIn("USDX", verdicts)
        # rises 30%: trailing stop moves to 15% under the high
        self.d.coin_list = [coin("ICNT", 0.45, 1, cid="161661"), coin("PUMP", 1.3, 5, 1, 9e7)]
        self.run_at(T + dt.timedelta(minutes=5))
        self.assertAlmostEqual(b["positions"]["PUMP"]["stop"], 1.105, places=6)
        # falls to 1.10: sold on the trailing stop with a profit
        self.d.coin_list = [coin("ICNT", 0.45, 1, cid="161661"), coin("PUMP", 1.10, -10, -2, 9e7)]
        self.run_at(T + dt.timedelta(minutes=10))
        self.assertNotIn("PUMP", b["positions"])
        last = self.s["trades"][-1]
        self.assertEqual(last["exit"], "trail")
        self.assertGreater(last["pnl"], 0)
        # cooldown: a new jump the same day is not bought
        self.d.coin_list = [coin("ICNT", 0.45, 1, cid="161661"), coin("PUMP", 1.5, 50, 3, 9e7)]
        self.run_at(T + dt.timedelta(minutes=15))
        self.assertNotIn("PUMP", b["positions"])

    def test_unconfirmed_jump_is_ignored(self):
        self.d.coin_list = [coin("ICNT", 0.2, -50, cid="161661")]
        self.d.single["161661"] = coin("ICNT", 0.45, 1, cid="161661")
        self.run_at()
        self.assertIn("ICNT", self.s["books"]["hype"]["positions"])
        self.assertTrue(any("unconfirmed" in e for e in self.s["health"]["errors"]))

    def test_time_exit(self):
        self.d.coin_list = [coin("ICNT", 0.45, 1, cid="161661")]
        self.run_at(dt.datetime(2026, 11, 26, 19, 0, tzinfo=dt.timezone.utc))
        self.assertNotIn("ICNT", self.s["books"]["hype"]["positions"])
        self.assertEqual(self.s["trades"][-1]["exit"], "time")

    def test_command_sell(self):
        self.d.coin_list = [coin("ICNT", 0.45, 1, cid="161661")]
        cmds = [{"id": "c1", "action": "sell", "book": "hype", "asset": "ICNT", "reason": "плохие новости."}]
        self.run_at(cmds=cmds)
        self.assertNotIn("ICNT", self.s["books"]["hype"]["positions"])
        self.assertEqual(self.s["trades"][-1]["exit"], "claude")
        n = len(self.s["trades"])
        self.run_at(T + dt.timedelta(minutes=5), cmds)  # same id is applied once
        self.assertEqual(len(self.s["trades"]), n)

    def test_stocks_closed_then_open(self):
        self.d.coin_list = [coin("ICNT", 0.45, 1, cid="161661")]
        self.d.stock_px = {"NVDA": 200.0}
        t = dt.datetime(2026, 11, 23, 16, 0, tzinfo=dt.timezone.utc)
        self.run_at(t)
        self.assertFalse(self.s["stockMarket"]["marketOpen"])
        self.assertEqual(self.s["books"]["stocks"]["positions"], {})
        self.assertFalse(self.s["stocksBenchmark"].get("units"))
        self.d.stock_open = True
        self.run_at(t + dt.timedelta(minutes=5))
        self.assertTrue(self.s["stocksBenchmark"]["units"])
        pos = self.s["books"]["stocks"]["positions"]
        self.assertEqual(len(pos), 1)  # one entry per day
        snap = self.s["equityDaily"]["2026-11-23"]
        self.assertIn("spy", snap)
        self.assertIn("stocks", snap)
        self.assertEqual(self.s["health"]["errors"], [])

    def test_kraken_down_keeps_going(self):
        self.d.fail.add("kraken")
        self.d.coin_list = [coin("ICNT", 0.45, 1, cid="161661")]
        self.run_at()
        self.assertTrue(any("kraken" in e for e in self.s["health"]["errors"]))
        self.assertIn("2026-11-21", self.s["equityDaily"])


if __name__ == "__main__":
    unittest.main()
