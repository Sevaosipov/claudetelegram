"""cfd/research_robust.py: two checks asked for on 2026-10-12 (docs/cfd/REPORT_ROBUST.md).

A. The live crypto breakout (BO-D with the trailing exit E0, 13 coins) looked at harder. Every bar of its
   history was already used to find it, so there is no unseen data: this is not a new test, it is four
   questions about the old one -- does it rest on a few trades, on the longs of a rising market, on one
   setting, and how sure is it once the coins' moving together is allowed for.
B. Stock indices: is being long only above the 200-day average better than simply holding, as a CFD that
   pays financing every night?"""
from __future__ import annotations

import datetime as dt
import math
import time

from cfd import data, exits, research, setups
from cfd import indicators as ind
from cfd import instruments as ins

COINS = tuple(i for i in ins.UNIVERSE if i.klass == ins.CRYPTO) + ins.ALT_COINS
OOS_FROM = dt.date(2020, 1, 1)


def daily(symbol: str, crypto: bool, pause: float = 3.0):
    fetch = data.cached_fetch(data.yahoo_fetch)
    path = data._cache_path(data.CACHE_DIR, symbol, data.DAILY)
    fresh = not path.exists()
    bars = data.daily_bars(symbol, fetch=fetch, max_wick=data.MAX_WICK_CRYPTO if crypto else None)
    if fresh:
        time.sleep(pause)                    # Yahoo slows a client that asks for many histories at once
    return bars


def stats(values: list[float]) -> dict:
    n = len(values)
    if not n:
        return {"n": 0, "sum": 0.0, "mean": 0.0, "t": 0.0, "pf": 0.0, "win": 0.0}
    mean = sum(values) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1)) if n > 1 else 0.0
    wins, losses = sum(v for v in values if v > 0), -sum(v for v in values if v < 0)
    return {"n": n, "sum": sum(values), "mean": mean, "t": mean / (sd / math.sqrt(n)) if sd else 0.0,
            "pf": wins / losses if losses else float("inf"), "win": sum(1 for v in values if v > 0) / n}


def breakout_trades(channel: int = setups.DONCHIAN_BARS, trail: float | None = None) -> list[tuple]:
    """(coin, side, signal date, exit date, result in R) of BO-D under E0 on the 13 coins."""
    out = []
    old = setups.DONCHIAN_BARS
    setups.DONCHIAN_BARS = channel
    try:
        for coin in COINS:
            bars = daily(coin.symbol, True)
            if len(bars) < 320:
                continue
            atr = ind.atr(bars, setups.ATR_LEN)
            kw = {"trail_atr": trail} if trail else {}
            trades, _counts = research.take_trades(bars, setups.bo_d(bars), exits.E0, costs=coin.costs,
                                                   entry="next_open", atr=atr, **kw)
            for t in trades:
                out.append((coin.name, t.side, bars[t.signal_index].ts.date(), t.exit_ts.date(), t.result_r))
    finally:
        setups.DONCHIAN_BARS = old
    return out


def crypto_report(log=print) -> None:
    trades = breakout_trades()
    oos = [t for t in trades if t[2] >= OOS_FROM]
    line = lambda name, rows: log(  # noqa: E731
        f"{name:<34} n {len(rows):>4}  sum {sum(r[4] for r in rows):>+7.1f}R  mean {stats([r[4] for r in rows])['mean']:+.3f}"
        f"  t {stats([r[4] for r in rows])['t']:+.2f}  PF {stats([r[4] for r in rows])['pf']:.2f}  win {stats([r[4] for r in rows])['win']:.0%}")
    line("all trades, all years", trades)
    line("2020 on", oos)
    best = sorted(oos, key=lambda r: -r[4])
    line("2020 on, without the best 5", best[5:])
    line("2020 on, without the best 10", best[10:])
    line("2020 on, longs", [t for t in oos if t[1] == "long"])
    line("2020 on, shorts", [t for t in oos if t[1] == "short"])
    for year in sorted({t[2].year for t in oos}):
        line(f"  {year}", [t for t in oos if t[2].year == year])
    months: dict = {}
    for t in oos:
        months[(t[3].year, t[3].month)] = months.get((t[3].year, t[3].month), 0.0) + t[4]
    m = stats(list(months.values()))
    log(f"by month of exit (the coins move together): {m['n']} months, mean {m['mean']:+.2f}R, t {m['t']:+.2f}, "
        f"{sum(1 for v in months.values() if v > 0)} up")
    log("neighbouring settings, 2020 on:")
    for channel in (40, 55, 70):
        for trail in (2.5, 3.0, 3.5):
            rows = [t for t in breakout_trades(channel, trail) if t[2] >= OOS_FROM]
            line(f"  {channel}-day high, trail {trail} ATR", rows)
    top = sorted({t[0] for t in oos})
    log("by coin, 2020 on: " + "  ".join(f"{c} {sum(t[4] for t in oos if t[0] == c):+.1f}" for c in top))


# ---------------------------------------------------------------- B: indices, the 200-day average against holding
INDICES = ("US500", "US100", "DE40")
INDEX_COST_PCT, INDEX_NIGHT_PCT = 0.030, 0.020


def index_paths(name: str, first: dt.date, last: dt.date) -> dict:
    """Growth of 1 from `first` to `last`, after CFD costs: held all the time, and held only while the last
    close is above its 200-day average (decided at a close, acted on at the next open)."""
    bars = daily(ins.by_name(name).symbol, False)
    sma = ind.sma(ind.closes(bars), 200)
    hold = timed = 1.0
    peak_h = peak_t = 1.0
    dd_h = dd_t = 0.0
    inside, switches, days_in, days = False, 0, 0, 0
    for i in range(201, len(bars)):
        d = bars[i].ts.date()
        if not first <= d <= last:
            continue
        nights = (d - bars[i - 1].ts.date()).days
        want = sma[i - 1] is not None and bars[i - 1].close > sma[i - 1]
        day_ret = bars[i].close / bars[i - 1].close
        fin = 1 - nights * INDEX_NIGHT_PCT / 100.0
        hold *= day_ret * fin
        if want != inside:                                       # act at this bar's open
            switches += 1
            timed *= 1 - INDEX_COST_PCT / 200.0
            if want:
                timed *= bars[i].close / bars[i].open            # in from the open
            else:
                timed *= (bars[i].open / bars[i - 1].close) * fin    # out at the open, the night paid
            inside = want
        elif inside:
            timed *= day_ret * fin
        days += 1
        days_in += inside
        peak_h, peak_t = max(peak_h, hold), max(peak_t, timed)
        dd_h, dd_t = max(dd_h, 1 - hold / peak_h), max(dd_t, 1 - timed / peak_t)
    years = max((last - first).days / 365.25, 1e-9)
    return {"hold": hold, "timed": timed, "hold_cagr": hold ** (1 / years) - 1, "timed_cagr": timed ** (1 / years) - 1,
            "hold_dd": dd_h, "timed_dd": dd_t, "switches": switches, "share_in": days_in / days if days else 0.0}


def index_report(log=print) -> None:
    for label, first, last in (("2000-2015", dt.date(2000, 1, 1), dt.date(2015, 12, 31)),
                               ("2016-2026", dt.date(2016, 1, 1), dt.date(2026, 10, 9))):
        log(label)
        for name in INDICES:
            p = index_paths(name, first, last)
            log(f"  {name:<6} hold {p['hold_cagr']:+.1%} a year, worst fall {p['hold_dd']:.0%} | above the average only "
                f"{p['timed_cagr']:+.1%} a year, worst fall {p['timed_dd']:.0%}, in the market {p['share_in']:.0%} of days, "
                f"{p['switches']} switches")


if __name__ == "__main__":
    crypto_report()
    index_report()
