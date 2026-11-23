"""Backtests of three strategies taken from videos Oleg sent (virtual money, learning only).

S1 "Скрытый разворот" (15 min, KryptoBosh): previous UTC day's low and the nearest older low below it
   are buy lines. Candle 1 pierces a line with its wick but closes back above it, candle 2 is green,
   entry when price trades above candle 2's high. Stop under candle 1's wick, target = previous day's high.
S2 "Отскок -> проторговка -> пробой" (1 h): long fall, abnormally large green bar, sideways base,
   a big bar closes above the base, entry when the next bar trades above the breakout bar's high.
S3 "Смена 4-часовых свечей 13:00 / 17:00 Berlin": what happens after the 4h candles that close at
   13:00 and 17:00 Berlin time (European / US session), compared with all other 4h candles.

Long only (our rules forbid short selling). Results are measured in R (1R = the planned risk of a
trade) and in dollars with $100 risk per trade (1% of a $10,000 book), net of fees on both sides.

Data: Coinbase Exchange public candles (cached under backtest/data/).
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import json
import math
import os
import statistics
import sys
import time
import urllib.request
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
UA = {"User-Agent": "Mozilla/5.0 paper-trading-backtest/1.0"}
COINS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "AVAX", "LINK"]
DAYS = int(os.environ.get("BT_DAYS", "365"))
FEES = [0.001, 0.004]  # per side: cheap exchange / our current rule
RISK_USD = 100.0
BERLIN = ZoneInfo("Europe/Berlin")


# ---------------------------------------------------------------- data
def fetch(url):
    for i in range(5):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            print("retry", url, e, file=sys.stderr)
            time.sleep(2 + 3 * i)
    raise RuntimeError(url)


def candles(coin: str, gran: int) -> list[tuple]:
    """[(ts, open, high, low, close, volume)] ascending, gran in seconds (900 or 3600)."""
    os.makedirs(DATA, exist_ok=True)
    path = os.path.join(DATA, f"{coin}-{gran}.csv.gz")
    end = int(time.time()) // gran * gran
    start = end - DAYS * 86400
    rows = {}
    if os.path.exists(path):
        with gzip.open(path, "rt") as f:
            for r in csv.reader(f):
                rows[int(r[0])] = tuple(float(x) for x in r[1:])
    have_to = max(rows) if rows else start
    t = max(start, have_to)
    while t < end:
        t2 = min(t + 300 * gran, end)
        url = (f"https://api.exchange.coinbase.com/products/{coin}-USD/candles?granularity={gran}"
               f"&start={dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat()}"
               f"&end={dt.datetime.fromtimestamp(t2, dt.timezone.utc).isoformat()}")
        for c in fetch(url):  # [time, low, high, open, close, volume]
            rows[int(c[0])] = (float(c[3]), float(c[2]), float(c[1]), float(c[4]), float(c[5]))
        t = t2
        time.sleep(0.15)
    keep = sorted(k for k in rows if k >= start)
    with gzip.open(path, "wt") as f:
        w = csv.writer(f)
        for k in keep:
            w.writerow([k, *rows[k]])
    return [(k, *rows[k]) for k in keep]


# ---------------------------------------------------------------- trade bookkeeping
def trade_result(entry, stop, exit_price, fee):
    """R multiple net of fees on both sides (risk = entry - stop)."""
    risk = entry - stop
    gross = (exit_price - entry) / risk
    fee_r = fee * (entry + exit_price) / risk
    return gross - fee_r


def run_exit(bars, i0, entry, stop, target, max_bars):
    """Walk forward from bar i0 (the entry bar, already entered). Stop first if both touched."""
    for j in range(i0, min(i0 + max_bars, len(bars))):
        _, o, h, l, c, _ = bars[j]
        if j > i0 or True:
            if l <= stop:
                return stop if o > stop else o, j, "stop"
            if target is not None and h >= target:
                return (target if o < target else o), j, "target"
    j = min(i0 + max_bars, len(bars)) - 1
    return bars[j][4], j, "time"


def summarize(trades, fee_key):
    rs = [t[fee_key] for t in trades]
    if not rs:
        return {"trades": 0}
    wins = [r for r in rs if r > 0]
    eq, peak, dd = 0.0, 0.0, 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return {"trades": len(rs), "winRate": round(len(wins) / len(rs) * 100, 1),
            "avgR": round(statistics.mean(rs), 3), "totalR": round(sum(rs), 2),
            "usd": round(sum(rs) * RISK_USD, 2), "maxDrawdownR": round(dd, 2),
            "best": round(max(rs), 2), "worst": round(min(rs), 2)}


# ---------------------------------------------------------------- S1 hidden reversal (15m)
def s1(bars, coin, variant):
    """variant: dict(bigMult, maxWait, stopBufPct, needR)"""
    by_day = {}
    for b in bars:
        d = dt.datetime.fromtimestamp(b[0], dt.timezone.utc).strftime("%Y-%m-%d")
        by_day.setdefault(d, []).append(b)
    days = sorted(by_day)
    day_hi = {d: max(b[2] for b in by_day[d]) for d in days}
    day_lo = {d: min(b[3] for b in by_day[d]) for d in days}
    idx = {b[0]: i for i, b in enumerate(bars)}
    trades = []
    busy_until = -1
    for k in range(2, len(days)):
        d, pd = days[k], days[k - 1]
        hi, lo = day_hi[pd], day_lo[pd]
        # nearest older low below the previous day's low
        local = None
        for back in range(k - 2, max(-1, k - 40), -1):
            if day_lo[days[back]] < lo:
                # nearest point in time: walk that day's bars from the end
                for b in reversed(by_day[days[back]]):
                    if b[3] < lo:
                        local = b[3]
                        break
                break
        lines = [x for x in (lo, local) if x is not None]
        today = by_day[d]
        for n in range(0, len(today) - 3):
            i = idx[today[n][0]]
            if i <= busy_until or i < 21:
                continue
            _, o1, h1, l1, c1, _ = bars[i]
            body = o1 - c1
            med = statistics.median(abs(b[4] - b[1]) for b in bars[i - 20:i]) or 1e-12
            if body <= 0 or body < variant["bigMult"] * med:
                continue
            line = next((x for x in lines if l1 < x < c1), None)
            if line is None:
                continue
            _, o2, h2, l2, c2, _ = bars[i + 1]
            if c2 <= o2:
                continue
            # entry: a later bar trades above candle 2's high
            for j in range(i + 2, min(i + 2 + variant["maxWait"], len(bars))):
                _, o, h, l, c, _ = bars[j]
                if l <= l1:  # setup failed before entry
                    break
                if h > h2:
                    entry = max(h2, o)
                    stop = min(l1, l2) * (1 - variant["stopBufPct"] / 100)
                    target = hi
                    if entry <= stop or target <= entry:
                        break
                    if (target - entry) / (entry - stop) < variant["needR"]:
                        break
                    ex, jx, why = run_exit(bars, j, entry, stop, target, 96 * 3)
                    t = {"coin": coin, "time": dt.datetime.fromtimestamp(bars[j][0], dt.timezone.utc).isoformat(),
                         "entry": entry, "stop": stop, "target": target, "exit": ex, "why": why,
                         "stopPct": round((entry - stop) / entry * 100, 3), "plannedR": round((target - entry) / (entry - stop), 2)}
                    for fee in FEES:
                        t[f"r{fee}"] = trade_result(entry, stop, ex, fee)
                    trades.append(t)
                    busy_until = jx
                    break
            if i <= busy_until:
                continue
    return trades


# ---------------------------------------------------------------- S2 bounce -> base -> breakout (1h)
def s2(bars, coin, v):
    trades = []
    busy_until = -1
    n = len(bars)
    i = 150
    while i < n - 2:
        _, o, h, l, c, _ = bars[i]
        avgb = statistics.mean(abs(b[4] - b[1]) for b in bars[i - 24:i]) or 1e-12
        win = bars[i - v["fallBars"]:i]
        top = max(b[2] for b in win)
        low = min(b[3] for b in bars[i - 6:i + 1])
        is_rev = (c > o and (c - o) >= v["revMult"] * avgb and (top - low) / top >= v["fallPct"] / 100)
        if not is_rev or i <= busy_until:
            i += 1
            continue
        rev_i, rev_open = i, o
        done = False
        for k in range(rev_i + 1 + v["minBase"], min(rev_i + 1 + v["maxBase"], n - 2)):
            base = bars[rev_i + 1:k]
            ch_hi = max(b[2] for b in base)
            ch_lo = min(b[3] for b in base)
            if ch_lo < rev_open:  # the bounce was fully given back: no base
                break
            _, bo, bh, bl, bc, _ = bars[k]
            if bc > ch_hi and (bc - bo) >= v["brkMult"] * avgb:
                for j in range(k + 1, min(k + 4, n)):
                    _, jo, jh, jl, jc, _ = bars[j]
                    if jh > bh:
                        entry = max(bh, jo)
                        stop = (ch_lo if v["stop"] == "channel" else bl) * 0.999
                        if entry <= stop:
                            break
                        target = entry + v["targetR"] * (entry - stop) if v["targetR"] else None
                        ex, jx, why = run_exit(bars, j, entry, stop, target, v["maxHold"])
                        t = {"coin": coin, "time": dt.datetime.fromtimestamp(bars[j][0], dt.timezone.utc).isoformat(),
                             "entry": entry, "stop": stop, "exit": ex, "why": why,
                             "stopPct": round((entry - stop) / entry * 100, 3)}
                        for fee in FEES:
                            t[f"r{fee}"] = trade_result(entry, stop, ex, fee)
                        trades.append(t)
                        busy_until = jx
                        done = True
                        break
                break
        i = max(i + 1, busy_until + 1) if done else i + 1
    return trades


# ---------------------------------------------------------------- S3 4h candles closing 13:00 / 17:00 Berlin
def s3(bars1h, coin):
    """Builds UTC-aligned 4h candles from 1h ones, classifies them by Berlin close hour, and measures
    the next 4h return after a red or a green candle (long only: 'buy after red' = fade the crowd)."""
    four = []
    for i in range(0, len(bars1h) - 3):
        t = bars1h[i][0]
        if t % 14400 != 0:
            continue
        grp = bars1h[i:i + 4]
        if len(grp) < 4 or grp[-1][0] - t != 10800:
            continue
        four.append((t, grp[0][1], max(b[2] for b in grp), min(b[3] for b in grp), grp[-1][4]))
    out = {}
    for a, b in zip(four, four[1:]):
        if b[0] - a[0] != 14400:
            continue
        close_hour = dt.datetime.fromtimestamp(a[0] + 14400, BERLIN).hour
        group = "13/17" if close_hour in (13, 17) else "other"
        color = "red" if a[4] < a[1] else "green"
        nxt = b[4] / b[1] - 1
        out.setdefault((group, color), []).append(nxt)
    res = {}
    for (g, col), xs in out.items():
        res[f"{g} after {col}"] = {"n": len(xs), "avgNextPct": round(statistics.mean(xs) * 100, 4),
                                   "upShare": round(sum(1 for x in xs if x > 0) / len(xs) * 100, 1)}
    return res


# ---------------------------------------------------------------- main
S1_VARIANTS = {
    "как в видео": {"bigMult": 1.5, "maxWait": 4, "stopBufPct": 0.05, "needR": 0.0},
    "только если цель ≥ 2R": {"bigMult": 1.5, "maxWait": 4, "stopBufPct": 0.05, "needR": 2.0},
}
S2_VARIANTS = {
    "стоп под каналом, цель 3R": {"fallBars": 120, "fallPct": 8, "revMult": 3, "minBase": 6, "maxBase": 72, "brkMult": 1.5, "stop": "channel", "targetR": 3, "maxHold": 240},
    "стоп под каналом, цель 10R": {"fallBars": 120, "fallPct": 8, "revMult": 3, "minBase": 6, "maxBase": 72, "brkMult": 1.5, "stop": "channel", "targetR": 10, "maxHold": 240},
    "стоп под свечой пробоя, цель 3R": {"fallBars": 120, "fallPct": 8, "revMult": 3, "minBase": 6, "maxBase": 72, "brkMult": 1.5, "stop": "bar", "targetR": 3, "maxHold": 240},
}


def main():
    report = {"generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "days": DAYS,
              "coins": COINS, "fees": FEES, "riskUsd": RISK_USD, "s1": {}, "s2": {}, "s3": {}, "buyHold": {}, "data": {}}
    data15, data60 = {}, {}
    for coin in COINS:
        try:
            data15[coin] = candles(coin, 900)
            data60[coin] = candles(coin, 3600)
        except Exception as e:  # noqa: BLE001
            print("skip", coin, e, file=sys.stderr)
            continue
        b = data60[coin]
        report["data"][coin] = {"bars15": len(data15[coin]), "bars60": len(b),
                                "from": dt.datetime.fromtimestamp(b[0][0], dt.timezone.utc).strftime("%Y-%m-%d"),
                                "to": dt.datetime.fromtimestamp(b[-1][0], dt.timezone.utc).strftime("%Y-%m-%d")}
        report["buyHold"][coin] = round((b[-1][4] / b[0][1] - 1) * 100, 1)
    all_trades = {}
    for name, v in S1_VARIANTS.items():
        tr = [t for coin in data15 for t in s1(data15[coin], coin, v)]
        all_trades["s1|" + name] = tr
        report["s1"][name] = {f"fee {f * 100:g}%": summarize(tr, f"r{f}") for f in FEES}
        report["s1"][name]["byCoin"] = {c: summarize([t for t in tr if t["coin"] == c], "r0.001") for c in data15}
        report["s1"][name]["medianStopPct"] = round(statistics.median([t["stopPct"] for t in tr]), 3) if tr else None
    for name, v in S2_VARIANTS.items():
        tr = [t for coin in data60 for t in s2(data60[coin], coin, v)]
        all_trades["s2|" + name] = tr
        report["s2"][name] = {f"fee {f * 100:g}%": summarize(tr, f"r{f}") for f in FEES}
        report["s2"][name]["byCoin"] = {c: summarize([t for t in tr if t["coin"] == c], "r0.001") for c in data60}
        report["s2"][name]["medianStopPct"] = round(statistics.median([t["stopPct"] for t in tr]), 3) if tr else None
    for coin in data60:
        report["s3"][coin] = s3(data60[coin], coin)
    with open(os.path.join(HERE, "results.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    with open(os.path.join(HERE, "trades.json"), "w", encoding="utf-8") as f:
        json.dump(all_trades, f, ensure_ascii=False)
    print(json.dumps({k: report[k] for k in ("data", "buyHold")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
