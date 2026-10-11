"""cfd/research_edge.py: EURUSD, a search across other kinds of entry (docs/cfd/PREREGISTRATION_EDGE_EURUSD.md)
-- five families, 53 candidates, on hourly bars; chosen on 2012-2019, judged once on 2020-2026."""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

from cfd import indicators as ind
from cfd import intraday as day
from cfd import research_day as rd
from cfd.data import Bar

DESIGN = (dt.date(2012, 1, 1), dt.date(2019, 12, 31))
HOLDOUT_FROM = dt.date(2020, 1, 1)
MIN_TRADES, MIN_YEARS, MIN_T = 80, 6, 2.5
THIRDS = [(2.0, 1 / 3), (3.0, 1 / 3), (4.0, 1 / 3)]
COST_ABS: float | None = None        # the round trip in price units (gold: dollars an ounce); None: rd.COST_PCT
NIGHT_PCT: float | None = None       # financing a night, % of the price; None: rd.NIGHT_PCT


def trade(bars: list[Bar], j: int, side: str, r: float, *, levels=(), trail=False,
          until: dt.datetime | None = None, leave=None):
    """A trade entered at the open of bar j with its stop `r` away: (result in R after costs, exit index).
    `levels` are (R, share) take-profits; `until` a time to be out by; `leave(k)` says, at bar k's close,
    to be out at the next open. None when the data ends first."""
    sign = 1 if side == "long" else -1
    entry, first = bars[j].open, j
    stop_r, left, got, k, out_next = -1.0, 1.0, 0.0, 0, False
    while j < len(bars):
        b = bars[j]
        open_r = sign * (b.open - entry) / r
        if out_next or (until is not None and b.ts >= until) or (j > first and open_r <= stop_r):
            got += left * open_r
            break
        if sign * ((b.low if sign > 0 else b.high) - entry) / r <= stop_r:
            got += left * stop_r
            break
        high_r = sign * ((b.high if sign > 0 else b.low) - entry) / r
        while k < len(levels) and high_r >= levels[k][0]:
            got += levels[k][1] * max(levels[k][0], open_r if j > first else levels[k][0])
            left -= levels[k][1]
            k += 1
            if trail:
                stop_r = 0.0 if k == 1 else levels[k - 2][0]
        if left <= 1e-9:
            break
        out_next = bool(leave and leave(j))
        j += 1
    else:
        return None
    nights = (bars[j].ts.date() - bars[first].ts.date()).days
    night = rd.NIGHT_PCT if NIGHT_PCT is None else NIGHT_PCT
    trip = rd.COST_PCT / 100.0 * entry if COST_ABS is None else COST_ABS
    return got - (trip + nights * night / 100.0 * entry) / r, j


@dataclass(frozen=True)
class Result:
    name: str
    n: int
    total: float
    mean: float
    t: float
    win: float
    by_year: dict

    @property
    def years_up(self) -> int:
        return sum(1 for v in self.by_year.values() if v > 0)


def result(name: str, rows: list[tuple[int, float]]) -> Result:
    n = len(rows)
    values = [r for _y, r in rows]
    mean = sum(values) / n if n else 0.0
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1)) if n > 1 else 0.0
    by_year: dict[int, float] = {}
    for y, r in rows:
        by_year[y] = by_year.get(y, 0.0) + r
    return Result(name, n, sum(values), mean, mean / (sd / math.sqrt(n)) if sd else 0.0,
                  sum(1 for v in values if v > 0) / n if n else 0.0, by_year)


def run(period: str, only: list[str] | None = None, log=print, *, bars: list[Bar] | None = None,
        design=DESIGN, holdout_from=HOLDOUT_FROM, sides: tuple = ("long", "short")) -> list[Result]:
    bars = bars or rd.hourly()
    first, last = design if period == "design" else (holdout_from, bars[-1].ts.date())
    log(f"{period}: {first} .. {last}")
    closes = [b.close for b in bars]
    mean20, sd20 = ind.sma(closes, 20), ind.stdev(closes, 20)
    atr, rsi, trend = ind.atr(bars, 24), ind.rsi(closes, 14), ind.ema(closes, 480)
    adr = rd.daily_range(bars)
    hi = {n: ind.highest([b.high for b in bars], n) for n in (60, 120, 240, 480)}
    lo = {n: ind.lowest([b.low for b in bars], n) for n in (60, 120, 240, 480)}
    local = [day.local(b.ts) for b in bars]
    ends = [day.local(b.ts + rd.HOUR) for b in bars]
    span = [i for i in range(500, len(bars) - 1) if first <= ends[i].date() <= last and trend[i] is not None]
    watched = [i for i in span if ends[i].weekday() < 5 and day.ENTRY_FROM <= ends[i].time() <= day.ENTRY_UNTIL]
    out: list[Result] = []

    def play(name: str, signals, make):
        """`signals` are (i, side) in order; `make(i, side)` gives the trade's keyword arguments with `r`,
        or None. One trade at a time."""
        if only and name not in only:
            return
        rows, free = [], 0
        for i, side in signals:
            if i < free or side not in sides:
                continue
            kw = make(i, side)
            if not kw or not kw["r"] or kw["r"] <= 0:
                continue
            got = trade(bars, i + 1, side, **kw)
            if got is None:
                continue
            rows.append((ends[i].year, got[0]))
            free = got[1]
        out.append(result(name, rows))

    def with_trend(i, side):
        return (side == "long") == (closes[i] > trend[i])

    # FADE
    for k in (2.0, 2.5, 3.0):
        sig = []
        for i in watched:
            if mean20[i] is None or not sd20[i]:
                continue
            z = (closes[i] - mean20[i]) / sd20[i]
            if abs(z) >= k:
                sig.append((i, "short" if z > 0 else "long"))
        for only_trend in (False, True):
            for s in (1.5, 3.0):
                def make(i, side, s=s):
                    sign = 1 if side == "long" else -1
                    return {"r": s * atr[i] if atr[i] else None, "until": bars[i + 1].ts + dt.timedelta(hours=48),
                            "leave": lambda j: sign * (closes[j] - mean20[j]) >= 0}
                play(f"FADE bb{k} {'trend ' if only_trend else ''}stop{s}atr",
                     [x for x in sig if not only_trend or with_trend(*x)], make)

    # PULLBACK
    for low_, high_ in ((30, 70), (25, 75)):
        sig = []
        for i in watched:
            if rsi[i] is None or rsi[i - 1] is None:
                continue
            if rsi[i] < low_ <= rsi[i - 1] and closes[i] > trend[i]:
                sig.append((i, "long"))
            elif rsi[i] > high_ >= rsi[i - 1] and closes[i] < trend[i]:
                sig.append((i, "short"))
        for s in (0.5, 1.0):
            for target in ("X2", "X3", "X234"):
                for hold in (7, 14):
                    def make(i, side, s=s, target=target, hold=hold):
                        a = adr.get(bars[i].ts.date())
                        levels = THIRDS if target == "X234" else [(float(target[1:]), 1.0)]
                        return {"r": s * a if a else None, "levels": levels, "trail": target == "X234",
                                "until": bars[i + 1].ts + dt.timedelta(days=hold)}
                    play(f"PULLBACK rsi{low_} stop{s}adr {target} {hold}d", sig, make)

    # DONCHIAN
    for n in (120, 240, 480):
        sig = []
        for i in watched:
            if hi[n][i - 1] is None:
                continue
            if closes[i] > hi[n][i - 1] and closes[i - 1] <= hi[n][i - 1]:
                sig.append((i, "long"))
            elif closes[i] < lo[n][i - 1] and closes[i - 1] >= lo[n][i - 1]:
                sig.append((i, "short"))
        for exit_ in ("X3", "channel"):
            def make(i, side, n=n, exit_=exit_):
                a = adr.get(bars[i].ts.date())
                half = n // 2
                kw = {"r": a, "until": bars[i + 1].ts + dt.timedelta(days=28)}
                if exit_ == "X3":
                    kw["levels"] = [(3.0, 1.0)]
                else:
                    kw["leave"] = (lambda j: closes[j] < lo[half][j - 1]) if side == "long" \
                        else (lambda j: closes[j] > hi[half][j - 1])
                return kw
            play(f"DONCHIAN {n // 24}d {exit_}", sig, make)

    # SESSION
    def at_hour(h):                                   # the bar before the one that starts at h:00 Riga
        return [i for i in span if local[i + 1].weekday() < 5 and local[i + 1].hour == h and local[i + 1].minute == 0]

    for h in (9, 10, 11):
        for out_h in (17, 18):
            def make(i, side, out_h=out_h):
                return {"r": adr.get(bars[i].ts.date()), "until": day._at(local[i + 1].date(), dt.time(out_h))}
            play(f"SESSION short {h}:00-{out_h}:00", [(i, "short") for i in at_hour(h)], make)

    def night(i, side):
        nxt = local[i + 1].date() + dt.timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += dt.timedelta(days=1)
        return {"r": adr.get(bars[i].ts.date()), "until": day._at(nxt, dt.time(9))}
    play("SESSION long 18:00-09:00", [(i, "long") for i in at_hour(18)], night)

    # WEEK
    mondays = [i for i in at_hour(9) if local[i + 1].weekday() == 0]
    for mode in ("with", "against"):
        sig = []
        for a, b in zip(mondays, mondays[1:]):
            move = closes[b] - closes[a]
            if move:
                sig.append((b, "long" if (move > 0) == (mode == "with") else "short"))
        for s in (1.0, 2.0):
            def make(i, side, s=s):
                a = adr.get(bars[i].ts.date())
                friday = local[i + 1].date() + dt.timedelta(days=4)
                return {"r": s * a if a else None, "until": day._at(friday, dt.time(17))}
            play(f"WEEK {mode} stop{s}adr", sig, make)

    return sorted(out, key=lambda x: -x.t)


def qualifies(x: Result) -> bool:
    return x.n >= MIN_TRADES and x.years_up >= MIN_YEARS and x.t >= MIN_T


def table(results: list[Result]) -> str:
    years = sorted({y for x in results for y in x.by_year})
    lines = [f"{'candidate':<34}{'n':>5}{'sum R':>8}{'mean':>7}{'t':>6}{'win':>5} up " + "".join(f"{y % 100:>6}" for y in years)]
    for x in results:
        lines.append(f"{x.name:<34}{x.n:>5}{x.total:>+8.1f}{x.mean:>+7.3f}{x.t:>+6.2f}{x.win:>5.0%} {x.years_up:>2} "
                     + "".join(f"{x.by_year.get(y, 0.0):>+6.1f}" for y in years))
    return "\n".join(lines)


# ================================================================ the session rule (PREREGISTRATION_SESSION.md)
SESSION_COST_PIPS = {"EURUSD": 0.8, "GBPUSD": 1.1}


def session(bars: list[Bar], cost_pips: float, first: dt.date, last: dt.date, *, enter: int = 11, leave: int = 18,
            side: str = "short") -> Result:
    """Every weekday of `first`..`last`: `side` at `enter`:00 Europe/Riga, out at `leave`:00, stop 1 ADR."""
    adr = rd.daily_range(bars)
    sign = 1 if side == "long" else -1
    rows = []
    for j in range(1, len(bars)):
        at = day.local(bars[j].ts)
        if at.weekday() >= 5 or at.hour != enter or at.minute or not first <= at.date() <= last:
            continue
        r = adr.get(bars[j - 1].ts.date())
        if not r:
            continue
        got = trade(bars, j, side, r, until=day._at(at.date(), dt.time(leave)))
        if got is None:
            continue
        entry = bars[j].open
        gross = got[0] + rd.COST_PCT / 100.0 * entry / r          # trade() took the default cost: put it back
        rows.append((at.year, gross - cost_pips * 1e-4 / r))
    return result(f"{side} {enter}:00-{leave}:00", rows)
