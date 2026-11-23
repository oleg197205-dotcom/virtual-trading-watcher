"""Second batch of backtests (virtual money, learning only).

S4 "Трендовая линия 1:3" (video INVESTCOIN «Трейдинг с нуля за 12 минут»): a rising trend line
   drawn through swing lows and confirmed by a third touch. Buy when price comes back to the line,
   stop 1% below the line, take profit 3% above (risk/reward 1:3). 1h and 4h candles.
S5 "Цена закрывает дыры (FVG)": claim from the same video that price tends to return and fill
   gaps after sharp moves. Measured as a statistic against a fair baseline, no trading.
S6 "Ротация по силе" (own idea, documented in research on time-series and cross-sectional
   momentum): once a week hold the strongest coins of the last N days, only while their own
   N-day trend is positive; otherwise cash. Compared with buying and holding all 7 coins.

Long only. Uses the cached Coinbase 1h candles from run_backtests.py (3 years).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_backtests as rb  # noqa: E402

COINS = rb.COINS
FEES = rb.FEES


def load(coin):
    return rb.candles(coin, 3600)


def resample(bars, hours):
    out = []
    for b in bars:
        key = b[0] // (hours * 3600)
        if out and out[-1][0] // (hours * 3600) == key:
            t, o, h, l, c, v = out[-1]
            out[-1] = (t, o, max(h, b[2]), min(l, b[3]), b[4], v + b[5])
        else:
            out.append((key * hours * 3600, *b[1:]))
    return out


# ---------------------------------------------------------------- S4 trend line touch
def pivots_low(bars, k):
    res = []
    for i in range(k, len(bars) - k):
        lo = bars[i][3]
        if all(lo < bars[j][3] for j in range(i - k, i)) and all(lo <= bars[j][3] for j in range(i + 1, i + k + 1)):
            res.append(i)
    return res


def s4(bars, k, tol, max_hold, trend_filter=False):
    """Trend line through pivot lows a,b (rising), confirmed by a third pivot p near the line."""
    piv = pivots_low(bars, k)
    closes = [b[4] for b in bars]
    trades = []
    busy_until = -1
    used_lines = set()
    for pi in range(2, len(piv)):
        p = piv[pi]
        start = p + k  # pivot p is only known k bars later
        if start <= busy_until:
            continue
        found = None
        for bi in range(pi - 1, max(0, pi - 6), -1):
            b = piv[bi]
            for ai in range(bi - 1, max(-1, bi - 6), -1):
                a = piv[ai]
                la, lb = bars[a][3], bars[b][3]
                if lb <= la:
                    continue
                slope = (lb - la) / (b - a)
                line_p = la + slope * (p - a)
                if abs(bars[p][3] - line_p) / line_p > tol:
                    continue
                # no close clearly below the line between a and p
                if any(closes[j] < (la + slope * (j - a)) * (1 - tol) for j in range(a, p + 1)):
                    continue
                found = (a, la, slope)
                break
            if found:
                break
        if not found or found[0] in used_lines:
            continue
        a, la, slope = found
        for i in range(start + 1, min(start + 120, len(bars))):
            line = la + slope * (i - a)
            _, o, h, l, c, _ = bars[i]
            if closes[i - 1] < line * (1 - 0.01):
                break  # line broken before we got a touch
            if o > line and l <= line:
                if trend_filter and i >= 200:
                    sma = statistics.fmean(closes[i - 200:i])
                    if closes[i - 1] < sma:
                        break
                entry = line
                stop = line * 0.99
                target = entry * 1.03
                exit_p, j, why = rb.run_exit(bars, i, entry, stop, target, max_hold)
                trades.append({"t": bars[i][0], "entry": entry, "stop": stop, "exit": exit_p, "why": why,
                               **{f"r{int(f*1000)}": rb.trade_result(entry, stop, exit_p, f) for f in FEES}})
                busy_until = j
                used_lines.add(a)
                break
    return trades


# ---------------------------------------------------------------- S5 FVG fill statistic
def s5(bars, horizon):
    """Bullish gaps (high[i-2] < low[i]) of at least 0.3%: does price come back to high[i-2]
    within `horizon` bars? Baseline: the same distance below a random bar's close."""
    gaps, base = [], []
    n = len(bars)
    for i in range(2, n - horizon):
        top = bars[i - 2][2]
        lo = bars[i][3]
        if lo <= top * 1.003:
            continue
        dist = (bars[i][4] - top) / bars[i][4]
        filled = any(bars[j][3] <= top for j in range(i + 1, i + 1 + horizon))
        gaps.append(filled)
        # baseline: same relative distance, bar 37 hours later (deterministic, not overlapping)
        jb = (i + 37) % (n - horizon - 1)
        lvl = bars[jb][4] * (1 - dist)
        base.append(any(bars[j][3] <= lvl for j in range(jb + 1, jb + 1 + horizon)))
    return {"gaps": len(gaps), "fillRate": round(100 * sum(gaps) / max(1, len(gaps)), 1),
            "baselineRate": round(100 * sum(base) / max(1, len(base)), 1)}


# ---------------------------------------------------------------- S6 momentum rotation
def daily_closes():
    out = {}
    for c in COINS:
        d = {}
        for b in load(c):
            d[b[0] // 86400] = b[4]
        out[c] = d
    days = sorted(set.intersection(*(set(v) for v in out.values())))
    return days, out


def s6(days, cl, lookback, top_n, fee, rebalance_every=7):
    cash, hold = 10_000.0, {}
    eq_curve = []
    for di, d in enumerate(days):
        value = cash + sum(q * cl[c][d] for c, q in hold.items())
        eq_curve.append((d, value))
        if di < lookback or di % rebalance_every:
            continue
        past = days[di - lookback]
        mom = {c: cl[c][d] / cl[c][past] - 1 for c in COINS}
        pick = [c for c in sorted(mom, key=mom.get, reverse=True)[:top_n] if mom[c] > 0]
        target = {c: value / top_n for c in pick}
        # sell what is not wanted or over target, then buy
        for c in list(hold):
            want = target.get(c, 0.0)
            cur = hold[c] * cl[c][d]
            if cur > want * 1.2 or want == 0:
                sell_val = cur - want
                cash += sell_val * (1 - fee)
                hold[c] -= sell_val / cl[c][d]
                if hold[c] * cl[c][d] < 1:
                    del hold[c]
        for c, want in target.items():
            cur = hold.get(c, 0.0) * cl[c][d]
            if cur < want * 0.8:
                buy_val = min(want - cur, cash)
                if buy_val > 1:
                    cash -= buy_val
                    hold[c] = hold.get(c, 0.0) + buy_val * (1 - fee) / cl[c][d]
    return eq_curve


def curve_stats(curve):
    vals = [v for _, v in curve]
    peak, dd = vals[0], 0.0
    for v in vals:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    by_year = {}
    for d, v in curve:
        y = dt.datetime.fromtimestamp(d * 86400, dt.timezone.utc).year
        by_year.setdefault(y, [v, v])[1] = v
    return {"final": round(vals[-1], 0), "returnPct": round((vals[-1] / vals[0] - 1) * 100, 1),
            "maxDrawdownPct": round(dd * 100, 1),
            "byYearPct": {y: round((b / a - 1) * 100, 1) for y, (a, b) in by_year.items()}}


def main():
    res = {"S4": {}, "S5": {}, "S6": {}}
    for tf_name, hours, k, hold in (("1h", 1, 5, 240), ("4h", 4, 3, 60)):
        for filt in (False, True):
            allt = []
            for c in COINS:
                bars = load(c)
                if hours > 1:
                    bars = resample(bars, hours)
                allt += s4(bars, k=k, tol=0.004, max_hold=hold, trend_filter=filt)
            allt.sort(key=lambda t: t["t"])
            key = f"{tf_name}{'_trendfilter' if filt else ''}"
            res["S4"][key] = {f"fee{f*100:g}%": rb.summarize(allt, f"r{int(f*1000)}") for f in FEES}
            res["S4"][key]["exits"] = {w: sum(1 for t in allt if t["why"] == w) for w in ("stop", "target", "time")}
            by_year = {}
            for t in allt:
                y = dt.datetime.fromtimestamp(t["t"], dt.timezone.utc).year
                by_year[y] = by_year.get(y, 0) + t["r1"]
            res["S4"][key]["byYearR_fee0.1%"] = {y: round(v, 1) for y, v in by_year.items()}
    for c in COINS:
        res["S5"][c] = s5(load(c), 48)
    days, cl = daily_closes()
    hold_all = []
    units = {c: (10_000 / len(COINS)) * (1 - 0.004) / cl[c][days[0]] for c in COINS}
    for d in days:
        hold_all.append((d, sum(units[c] * cl[c][d] for c in COINS)))
    res["S6"]["buyHoldAll7"] = curve_stats(hold_all)
    btc_units = 10_000 * (1 - 0.004) / cl["BTC"][days[0]]
    res["S6"]["buyHoldBTC"] = curve_stats([(d, btc_units * cl["BTC"][d]) for d in days])
    for lb in (14, 28, 56):
        for top in (1, 2, 3):
            res["S6"][f"lb{lb}_top{top}"] = curve_stats(s6(days, cl, lb, top, 0.004))
    print(json.dumps(res, indent=1, ensure_ascii=False))
    with open(os.path.join(rb.HERE, "results2.json"), "w") as f:
        json.dump(res, f, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
