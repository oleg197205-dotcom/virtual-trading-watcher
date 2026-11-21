"""Программа-сторож для виртуального счёта Олега (paper trading).

Каждые 5-10 минут (GitHub Actions) берёт свежие цены и торгует ВИРТУАЛЬНЫМИ
деньгами строго по правилам из state/state.json -> rules.params.
Настоящих денег, бирж и кошельков здесь нет: только чтение публичных цен.

Три счёта:
  trend  - BTC, ETH, SOL по 20-дневному тренду
  hype   - монеты из топ-200, которые взлетели за сутки, со стопом и следящим стопом
  stocks - крупные акции США по тому же трендовому правилу (торгуем только когда биржа открыта)
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) paper-trading-watcher/1.0"}
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state")
STATE_PATH = os.environ.get("STATE_PATH", os.path.join(ROOT, "state.json"))
RULES_PATH = os.environ.get("RULES_PATH", os.path.join(ROOT, "rules.json"))
COMMANDS_PATH = os.environ.get("COMMANDS_PATH", os.path.join(ROOT, "commands.json"))
MAX_EVENTS = 400
MAX_TRADES = 2000

STABLE_WORDS = ("USD", "DAI", "FRAX", "GHO", "TUSD", "USDE", "PYUSD", "RLUSD", "EUR")
WRAPPED_SYMS = {"WBTC", "WETH", "WBETH", "STETH", "CBETH", "BNSOL", "WBNB", "SUSDE", "WSTETH", "RETH", "WEETH", "JITOSOL", "MSOL", "XAUT", "PAXG"}
WRAPPED_WORDS = ("wrapped", "staked", "bridged", "gold")


# ----------------------------------------------------------------- data access
class Data:
    """Real network access. Tests replace this with a fake."""

    def get_json(self, url: str, tries: int = 2):
        last = None
        for i in range(tries):
            try:
                req = urllib.request.Request(url, headers=UA)
                with urllib.request.urlopen(req, timeout=25) as r:
                    return json.loads(r.read().decode("utf-8"))
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(2 + 3 * i)
        raise RuntimeError(f"{url}: {last}")

    # Kraken: BTC, ETH, SOL
    KRAKEN = {"BTC": "XXBTZUSD", "ETH": "XETHZUSD", "SOL": "SOLUSD"}
    KRAKEN_REQ = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD"}

    def crypto_prices(self) -> dict:
        j = self.get_json("https://api.kraken.com/0/public/Ticker?pair=XBTUSD,ETHUSD,SOLUSD")
        if j.get("error"):
            raise RuntimeError(f"kraken: {j['error']}")
        res = j["result"]
        out = {}
        for sym, key in self.KRAKEN.items():
            t = res[key]
            out[sym] = {"price": float(t["c"][0]), "low24": float(t["l"][1]), "high24": float(t["h"][1])}
        return out

    def crypto_daily(self, sym: str) -> list:
        j = self.get_json(f"https://api.kraken.com/0/public/OHLC?pair={self.KRAKEN_REQ[sym]}&interval=1440")
        if j.get("error"):
            raise RuntimeError(f"kraken ohlc: {j['error']}")
        rows = j["result"][self.KRAKEN[sym]]
        return [{"d": dt.datetime.fromtimestamp(int(r[0]), dt.timezone.utc).strftime("%Y-%m-%d"), "c": float(r[4])} for r in rows]

    # CoinLore: everything else
    def coins(self) -> list:
        out = []
        for start in (0, 100):
            j = self.get_json(f"https://api.coinlore.net/api/tickers/?start={start}&limit=100")
            for c in j["data"]:
                out.append(_coin(c))
        return out

    def coin(self, coin_id: str) -> dict | None:
        j = self.get_json(f"https://api.coinlore.net/api/ticker/?id={coin_id}")
        return _coin(j[0]) if j else None

    # Yahoo: stocks
    def stock(self, sym: str) -> dict:
        j = self.get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=6mo&interval=1d")
        r = j["chart"]["result"][0]
        meta = r["meta"]
        closes = []
        for t, c in zip(r.get("timestamp", []), r["indicators"]["quote"][0].get("close", [])):
            if c is None:
                continue
            d = dt.datetime.fromtimestamp(int(t), ZoneInfo(meta.get("exchangeTimezoneName", "America/New_York"))).strftime("%Y-%m-%d")
            closes.append({"d": d, "c": float(c)})
        period = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
        return {
            "price": float(meta["regularMarketPrice"]),
            "time": int(meta.get("regularMarketTime", 0)),
            "open": int(period.get("start", 0)),
            "close": int(period.get("end", 0)),
            "closes": closes,
        }


def _coin(c: dict) -> dict:
    def f(k):
        try:
            return float(c.get(k) or 0)
        except (TypeError, ValueError):
            return 0.0
    return {"id": str(c.get("id")), "sym": str(c.get("symbol", "")).upper(), "name": c.get("name", ""),
            "nameid": c.get("nameid", ""), "price": f("price_usd"), "ch1h": f("percent_change_1h"),
            "ch24": f("percent_change_24h"), "ch7d": f("percent_change_7d"), "vol": f("volume24"),
            "rank": int(c.get("rank") or 9999)}


# ----------------------------------------------------------------- helpers
def r2(x: float) -> float:
    return round(x + 0.0, 2)


def rp(x: float) -> float:
    """Round a price: 2 decimals from $1000, otherwise 6 significant digits."""
    if x >= 1000:
        return round(x, 2)
    if x <= 0:
        return 0.0
    return float(f"{x:.6g}")


def floor_to(x: float, decimals: int) -> float:
    f = 10 ** decimals
    return math.floor(x * f) / f


def floor_sig(x: float, sig: int) -> float:
    if x <= 0:
        return 0.0
    d = sig - int(math.floor(math.log10(x))) - 1
    return floor_to(x, max(d, 0)) if d >= 0 else math.floor(x / 10 ** -d) * 10 ** -d


def sma(vals: list, n: int):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def money(x: float) -> str:
    s = f"{abs(x):,.2f}".replace(",", " ").replace(".", ",")
    return ("−" if x < 0 else "") + "$" + s


def px(x: float) -> str:
    if x >= 1:
        return money(x)
    return "$" + f"{x:.6g}".replace(".", ",")


class Run:
    def __init__(self, state: dict, rules: dict, data: Data, now: dt.datetime, commands: list | None = None):
        self.s = state
        self.rules = rules
        self.commands = commands or []
        self.data = data
        self.now = now.astimezone(dt.timezone.utc)
        self.local = self.now.astimezone(BERLIN)
        self.today = self.local.strftime("%Y-%m-%d")
        self.hhmm = self.local.strftime("%H:%M")
        self.p = rules["params"]
        self.health = state.setdefault("health", {})
        self.health["errors"] = []
        self.changed_books = False

    # --------------------------------------------------- bookkeeping
    def event(self, text: str, book: str | None = None, kind: str = "info"):
        ev = {"t": self.now.isoformat(timespec="seconds"), "date": self.today, "time": self.hhmm, "book": book, "kind": kind, "text": text}
        self.s.setdefault("events", []).append(ev)
        self.s["events"] = self.s["events"][-MAX_EVENTS:]

    def err(self, where: str, e: Exception):
        msg = f"{where}: {e}"[:300]
        self.health["errors"].append(msg)
        print("ERROR", msg, file=sys.stderr)

    def trade(self, book: str, asset: str, side: str, qty: float, price: float, fee: float, reason: str, **extra):
        tid = f"{self.today}-{book}-{asset}-{side}-{self.local.strftime('%H%M')}"
        t = {"id": tid, "ts": self.now.isoformat(timespec="seconds"), "date": self.today, "book": book, "asset": asset,
             "side": side, "qty": qty, "price": rp(price), "fee": r2(fee), "reason": reason}
        t.update(extra)
        self.s.setdefault("trades", []).append(t)
        self.s["trades"] = self.s["trades"][-MAX_TRADES:]
        b = self.s["books"][book]
        b["tradeCount"] = b.get("tradeCount", 0) + 1
        self.changed_books = True
        return t

    def sell(self, book: str, asset: str, price: float, exit_kind: str, reason: str):
        b = self.s["books"][book]
        pos = b["positions"].pop(asset)
        fee_rate = self.p[book]["feePct"] / 100
        gross = pos["qty"] * price
        proceeds = gross * (1 - fee_rate)
        pnl = proceeds - pos["cost"]
        b["cash"] = r2(b["cash"] + proceeds)
        self.trade(book, asset, "sell", pos["qty"], price, gross * fee_rate, reason, exit=exit_kind,
                   pnl=r2(pnl), pnlPct=round(pnl / pos["cost"] * 100, 2), cost=r2(pos["cost"]),
                   entry=pos["entry"], openedAt=pos["openedAt"])
        sign = "+" if pnl >= 0 else ""
        pct = f"{pnl / pos['cost'] * 100:+.1f}".replace(".", ",").replace("-", "−")
        self.event(f"Продали {asset} по {px(price)}: {reason} Итог {sign}{money(pnl)} ({pct}%).", book, "sell")
        return pnl

    def buy(self, book: str, asset: str, price: float, qty: float, pos_extra: dict, reason: str):
        b = self.s["books"][book]
        fee_rate = self.p[book]["feePct"] / 100
        cost = qty * price * (1 + fee_rate)
        b["cash"] = r2(b["cash"] - cost)
        pos = {"qty": qty, "entry": rp(price), "cost": r2(cost), "openedAt": self.today,
               "openedTs": self.now.isoformat(timespec="seconds"), "reason": reason}
        pos.update(pos_extra)
        b["positions"][asset] = pos
        self.trade(book, asset, "buy", qty, price, qty * price * fee_rate, reason, cost=r2(cost))
        self.event(f"Купили {asset} на {money(cost)} по {px(price)}. {reason}", book, "buy")

    # --------------------------------------------------- crypto trend book
    def crypto(self):
        try:
            q = self.data.crypto_prices()
        except Exception as e:  # noqa: BLE001
            self.err("kraken", e)
            return
        m = self.s.setdefault("market", {})
        prev = m.get("prices", {})
        for sym, v in q.items():
            old = prev.get(sym)
            if old and abs(v["price"] / old - 1) > 0.4:
                self.err("kraken", RuntimeError(f"{sym} price jump {old}->{v['price']}, skipped run"))
                return
        m["prices"] = {k: v["price"] for k, v in q.items()}
        m["low24"] = {k: v["low24"] for k, v in q.items()}
        m["high24"] = {k: v["high24"] for k, v in q.items()}
        m["updatedAt"] = self.now.isoformat(timespec="seconds")
        m["source"] = "Kraken"
        self.health["kraken"] = m["updatedAt"]

        closes = m.setdefault("closes", {})
        utc_day = self.now.strftime("%Y-%m-%d")
        if m.get("historyDay") != utc_day:
            try:
                for sym in q:
                    closes[sym] = self.data.crypto_daily(sym)[-90:]
                m["historyDay"] = utc_day
            except Exception as e:  # noqa: BLE001
                self.err("kraken ohlc", e)
        for sym, v in q.items():
            cs = closes.setdefault(sym, [])
            if cs and cs[-1]["d"] == utc_day:
                cs[-1]["c"] = v["price"]
            else:
                cs.append({"d": utc_day, "c": v["price"]})
            closes[sym] = cs[-90:]
        self.trend_book("trend", m["prices"], closes, self.p["trend"])

    # --------------------------------------------------- generic trend logic (crypto + stocks)
    def indicators(self, cs: list, P: dict):
        vals = [x["c"] for x in cs]
        n, lag = P["smaDays"], P["slopeLagDays"]
        s_now = sma(vals, n)
        s_prev = sum(vals[-(n + lag):-lag]) / n if len(vals) >= n + lag else None
        look = P["stopLookbackDays"]
        low_n = min(vals[-(look + 1):-1]) if len(vals) > look else None
        ch30 = vals[-1] / vals[-31] - 1 if len(vals) >= 31 else None
        return s_now, s_prev, low_n, ch30

    def trend_book(self, book: str, prices: dict, closes: dict, P: dict):
        b = self.s["books"][book]
        fee = P["feePct"] / 100
        # exits
        for sym in list(b["positions"].keys()):
            pos = b["positions"][sym]
            price = prices.get(sym)
            if price is None:
                continue
            pos["last"] = price
            s_now, _, _, _ = self.indicators(closes.get(sym, []), P)
            if price <= pos["stop"]:
                self.sell(book, sym, price, "stop", f"цена дошла до стопа {px(pos['stop'])}.")
            elif price >= pos["target"]:
                self.sell(book, sym, price, "target", f"цена дошла до цели {px(pos['target'])}.")
            elif s_now and price < s_now and pos["openedAt"] != self.today:
                self.sell(book, sym, price, "trend", f"цена ушла ниже средней за {P['smaDays']} дней ({px(s_now)}).")
        # entry: at most maxEntriesPerDay per Berlin day
        bought_today = sum(1 for t in self.s.get("trades", []) if t["book"] == book and t["side"] == "buy" and t["date"] == self.today)
        sold_today = {t["asset"] for t in self.s.get("trades", []) if t["book"] == book and t["side"] == "sell" and t["date"] == self.today}
        if bought_today >= P["maxEntriesPerDay"] or len(b["positions"]) >= P.get("maxPositions", 99):
            return
        cands = []
        for sym, price in prices.items():
            if sym in b["positions"] or sym in sold_today:
                continue
            s_now, s_prev, low_n, ch30 = self.indicators(closes.get(sym, []), P)
            if None in (s_now, s_prev, low_n, ch30):
                continue
            if price > s_now and s_now > s_prev:
                cands.append((ch30, sym, price, s_now, low_n))
        if not cands:
            return
        ch30, sym, price, s_now, low_n = max(cands)
        equity = b["cash"] + sum(p["qty"] * prices.get(k, p.get("last", p["entry"])) for k, p in b["positions"].items())
        stop = low_n * (1 - P["stopBufferPct"] / 100)
        dist = (price - stop) / price
        dist = min(max(dist, P["stopMinPct"] / 100), P["stopMaxPct"] / 100)
        stop = price * (1 - dist)
        target = price + P["targetR"] * (price - stop)
        qty = min(P["riskPct"] / 100 * equity / (price - stop), P["maxPositionPct"] / 100 * equity / price,
                  0.98 * b["cash"] / (price * (1 + fee)))
        qty = floor_to(qty, P.get("qtyDecimals", 6))
        if qty * price < P.get("minOrderUsd", 100):
            return
        reason = (f"Цена {px(price)} выше средней за {P['smaDays']} дней ({px(s_now)}), и средняя растёт; "
                  f"за 30 дней {ch30 * 100:+.1f}%. Стоп {px(stop)}, цель {px(target)}.")
        self.buy(book, sym, price, qty, {"stop": rp(stop), "target": rp(target), "last": price}, reason)

    # --------------------------------------------------- hype book
    def universe_ok(self, c: dict, P: dict) -> bool:
        sym, name = c["sym"], (c["name"] or "") + " " + (c["nameid"] or "")
        if c["rank"] > P["universeTop"] or sym in ("BTC", "ETH", "SOL") or sym in WRAPPED_SYMS:
            return False
        if any(w in sym for w in STABLE_WORDS) or any(w.lower() in name.lower() for w in WRAPPED_WORDS):
            return False
        if 0.97 <= c["price"] <= 1.03 and abs(c["ch24"]) < 1:
            return False
        return c["price"] > 0

    def hype(self):
        P = self.p["hype"]
        b = self.s["books"]["hype"]
        try:
            coins = self.data.coins()
        except Exception as e:  # noqa: BLE001
            self.err("coinlore", e)
            return
        self.health["coinlore"] = self.now.isoformat(timespec="seconds")
        by_id = {c["id"]: c for c in coins}
        fee = P["feePct"] / 100

        # update and exit held coins
        for sym in list(b["positions"].keys()):
            pos = b["positions"][sym]
            c = by_id.get(pos["id"])
            if c is None:
                try:
                    c = self.data.coin(pos["id"])
                except Exception as e:  # noqa: BLE001
                    self.err(f"coinlore {sym}", e)
                    continue
            if not c or c["price"] <= 0:
                continue
            price = c["price"]
            last = pos.get("last", pos["entry"])
            if abs(price / last - 1) > 0.4:  # big jump: confirm with the single-coin endpoint
                try:
                    c2 = self.data.coin(pos["id"])
                except Exception as e:  # noqa: BLE001
                    self.err(f"coinlore confirm {sym}", e)
                    continue
                if not c2 or abs(c2["price"] / price - 1) > 0.05:
                    self.err(f"coinlore {sym}", RuntimeError(f"unconfirmed jump {last}->{price}"))
                    continue
            pos["last"] = price
            pos["lastTs"] = self.now.isoformat(timespec="seconds")
            if price > pos["high"]:
                pos["high"] = price
            if pos["high"] >= pos["entry"] * (1 + P["trailStartPct"] / 100):
                new_stop = pos["high"] * (1 - P["trailPct"] / 100)
                if new_stop > pos["stop"] * 1.0001:
                    pos["stop"] = rp(new_stop)
                    pos["trailing"] = True
            held_days = (dt.date.fromisoformat(self.today) - dt.date.fromisoformat(pos["openedAt"])).days
            if price <= pos["stop"]:
                kind = "trail" if pos.get("trailing") else "stop"
                why = (f"цена упала ниже следящего стопа {px(pos['stop'])} (на {P['trailPct']}% ниже максимума {px(pos['high'])})."
                       if kind == "trail" else f"цена упала ниже стопа {px(pos['stop'])}.")
                self.sell("hype", sym, price, kind, why)
                b.setdefault("cooldown", {})[sym] = self.today
            elif held_days >= P["maxHoldDays"]:
                self.sell("hype", sym, price, "time", f"прошло {P['maxHoldDays']} дней, продаём по правилу.")
                b.setdefault("cooldown", {})[sym] = self.today

        # scan and entry
        cooldown = b.setdefault("cooldown", {})
        for sym, d in list(cooldown.items()):
            if (dt.date.fromisoformat(self.today) - dt.date.fromisoformat(d)).days >= P["cooldownDays"]:
                cooldown.pop(sym)
        uni = [c for c in coins if self.universe_ok(c, P)]
        uni.sort(key=lambda c: c["ch24"], reverse=True)
        equity = b["cash"] + sum(p["qty"] * p.get("last", p["entry"]) for p in b["positions"].values())
        entries = 0
        scan = []
        for c in uni[:12]:
            verdict, ok = None, False
            if c["ch24"] < P["minChange24hPct"]:
                verdict = "мало роста"
            elif c["vol"] < P["minVolume24hUsd"]:
                verdict = "мал объём"
            elif c["ch1h"] <= P["minChange1hPct"]:
                verdict = "рост остановился"
            elif c["sym"] in b["positions"]:
                verdict = "уже держим"
            elif c["sym"] in cooldown:
                verdict = "пауза после продажи"
            elif len(b["positions"]) >= P["maxPositions"]:
                verdict = "лимит позиций"
            elif entries >= P["maxEntriesPerRun"]:
                verdict = "один вход за проверку"
            else:
                size = min(P["positionPct"] / 100 * equity, 0.98 * b["cash"])
                qty = floor_sig(size / (c["price"] * (1 + fee)), 8)
                if qty * c["price"] < P.get("minOrderUsd", 100):
                    verdict = "мало денег"
                else:
                    reason = (f"{c['name']} ({c['sym']}): за сутки {c['ch24']:+.1f}%, за час {c['ch1h']:+.1f}%, "
                              f"объём {c['vol'] / 1e6:.0f} млн $. Новости ещё не проверены: Claude проверит при ближайшем разборе.")
                    self.buy("hype", c["sym"], c["price"], qty,
                             {"id": c["id"], "name": c["name"], "stop": rp(c["price"] * (1 - P["stopPct"] / 100)),
                              "high": c["price"], "last": c["price"], "newsChecked": False}, reason)
                    entries += 1
                    verdict, ok = "куплено", True
            scan.append({"sym": c["sym"], "name": c["name"], "price": rp(c["price"]), "ch24": round(c["ch24"], 2),
                         "ch1h": round(c["ch1h"], 2), "vol": round(c["vol"]), "ok": ok, "verdict": verdict})
        self.s["scan"] = {"updatedAt": self.now.isoformat(timespec="seconds"),
                          "updatedText": self.local.strftime("%d.%m.%Y, %H:%M (Берлин)"),
                          "source": f"CoinLore top {P['universeTop']}", "candidates": scan[:6]}

    # --------------------------------------------------- stocks book
    def stocks(self):
        P = self.p.get("stocks")
        if not P or "stocks" not in self.s["books"]:
            return
        sm = self.s.setdefault("stockMarket", {"prices": {}, "closes": {}})
        need_history = sm.get("historyDay") != self.today
        quotes, open_now = {}, False
        syms = list(dict.fromkeys(P["universe"] + [P["benchmark"]]))
        # cheap probe first: is the US market open now?
        try:
            probe = self.data.stock(P["benchmark"])
        except Exception as e:  # noqa: BLE001
            self.err("yahoo", e)
            return
        now_ts = self.now.timestamp()
        open_now = probe["open"] <= now_ts < probe["close"] and now_ts - probe["time"] < 30 * 60
        sm["marketOpen"] = open_now
        if not open_now and not need_history:
            return
        quotes[P["benchmark"]] = probe
        for sym in syms:
            if sym in quotes:
                continue
            try:
                quotes[sym] = self.data.stock(sym)
            except Exception as e:  # noqa: BLE001
                self.err(f"yahoo {sym}", e)
        for sym, q in quotes.items():
            old = sm["prices"].get(sym)
            if old and abs(q["price"] / old - 1) > 0.4:
                self.err("yahoo", RuntimeError(f"{sym} jump {old}->{q['price']}"))
                continue
            sm["prices"][sym] = q["price"]
            if q["closes"]:
                sm["closes"][sym] = q["closes"][-130:]
        sm["updatedAt"] = self.now.isoformat(timespec="seconds")
        sm["historyDay"] = self.today
        self.health["yahoo"] = sm["updatedAt"]
        bm = self.s.setdefault("stocksBenchmark", {})
        if open_now and not bm.get("units") and P["benchmark"] in sm["prices"]:
            p0 = sm["prices"][P["benchmark"]]
            bm.update({"symbol": P["benchmark"], "units": 10000 * (1 - P["feePct"] / 100) / p0, "startPrice": p0, "startDate": self.today})
            self.event(f"Счёт на акциях запущен. Для сравнения «купил и держу» купили фонд {P['benchmark']} (500 крупнейших компаний США) на $10 000 по {px(p0)}.", "stocks")
        if open_now:
            prices = {k: v for k, v in sm["prices"].items() if k in P["universe"]}
            self.trend_book("stocks", prices, sm["closes"], P)

    # --------------------------------------------------- totals
    def totals(self):
        s = self.s
        m = s.get("market", {}).get("prices", {})
        sm = s.get("stockMarket", {}).get("prices", {})

        def book_val(name, prices):
            b = s["books"][name]
            return b["cash"] + sum(p["qty"] * prices.get(k, p.get("last", p["entry"])) for k, p in b["positions"].items())

        snap = {"date": self.today, "t": self.now.isoformat(timespec="seconds"),
                "equity": r2(book_val("trend", m)),
                "hype": r2(book_val("hype", {}))}
        units = s.get("benchmark", {}).get("units", {})
        if units and all(k in m for k in units):
            snap["benchmark"] = r2(sum(units[k] * m[k] for k in units))
        if "stocks" in s["books"]:
            snap["stocks"] = r2(book_val("stocks", sm))
            sb = s.get("stocksBenchmark", {})
            if sb.get("units") and sb.get("symbol") in sm:
                snap["spy"] = r2(sb["units"] * sm[sb["symbol"]])
        prev = s.setdefault("equityDaily", {}).get(self.today, {})
        for k in ("benchmark", "spy"):
            if k not in snap and k in prev:
                snap[k] = prev[k]
        s["equityDaily"][self.today] = snap
        s["lastRun"] = snap["t"]
        s["lastRunText"] = self.local.strftime("%d.%m.%Y, %H:%M (Берлин)")

    def apply_commands(self):
        """Claude's decisions from commands.json (e.g. sell a coin after a bad news check)."""
        done = set(self.s.setdefault("commandsDone", []))
        for c in self.commands:
            cid = c.get("id")
            if not cid or cid in done:
                continue
            done.add(cid)
            book, asset = c.get("book"), c.get("asset")
            b = self.s["books"].get(book or "")
            if c.get("action") == "sell" and b and asset in b["positions"]:
                pos = b["positions"][asset]
                price = pos.get("last", pos["entry"])
                self.sell(book, asset, price, "claude", c.get("reason", "решение Claude после проверки новостей."))
                if book == "hype":
                    b.setdefault("cooldown", {})[asset] = self.today
            elif c.get("action") == "newsChecked" and b and asset in b["positions"]:
                b["positions"][asset]["newsChecked"] = True
                if c.get("note"):
                    b["positions"][asset]["newsNote"] = c["note"][:400]
        self.s["commandsDone"] = sorted(done)[-500:]

    def go(self):
        for step in (self.crypto, self.hype, self.apply_commands, self.stocks):
            try:
                step()
            except Exception as e:  # noqa: BLE001
                self.err(step.__name__, e)
        self.totals()
        self.health["lastRun"] = self.now.isoformat(timespec="seconds")
        self.health["runs"] = self.health.get("runs", 0) + 1


def main():
    with open(STATE_PATH, encoding="utf-8") as f:
        state = json.load(f)
    with open(RULES_PATH, encoding="utf-8") as f:
        rules = json.load(f)
    commands = []
    if os.path.exists(COMMANDS_PATH):
        with open(COMMANDS_PATH, encoding="utf-8") as f:
            commands = json.load(f).get("commands", [])
    if rules.get("paused"):
        print("paused")
        return
    state["rulesVersion"] = rules.get("version")
    Run(state, rules, Data(), dt.datetime.now(dt.timezone.utc), commands).go()
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_PATH)
    errs = state.get("health", {}).get("errors", [])
    print(f"ok {state['lastRunText']} errors={len(errs)}")


if __name__ == "__main__":
    main()
