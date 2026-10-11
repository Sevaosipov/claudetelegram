"""cfd/research_day.py: the EURUSD day-trading research (docs/cfd/PREREGISTRATION_DAY_EURUSD.md) -- the day
league's ideas and their variants on 15-minute bars built from FXCM's public one-minute candles, chosen on a
design period and judged once on a hold-out period."""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import io
import time
from pathlib import Path

from cfd import research4 as r4
from cfd.data import Bar

UTC = dt.timezone.utc
M1_DIR = r4.CACHE_DIR / "fxcm_m1"
FIRST_YEAR = 2021


def load_m1(pair: str = "EURUSD", first_year: int = FIRST_YEAR, last: dt.date | None = None,
            cache_dir: Path = M1_DIR, pause: float = 0.4, log=None) -> list[tuple]:
    """FXCM's one-minute candles of `pair`: [(ts UTC, bid open, high, low, close, ask open)], oldest first.
    A file per week of the year, kept once fetched (the running week is not asked for)."""
    last = last or dt.date.today()
    rows: dict[dt.datetime, tuple] = {}
    for year in range(first_year, last.year + 1):
        for week in range(1, 54):
            if year == last.year and week >= last.isocalendar()[1]:
                break
            path = cache_dir / f"{pair}_{year}_{week:02d}.csv.gz"
            if not path.exists():
                body = r4._get(f"https://candledata.fxcorporate.com/m1/{pair}/{year}/{week}.csv.gz")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body or b"")
                if log:
                    log(f"{path.name} {len(body or b'')}")
                time.sleep(pause)
            body = path.read_bytes()
            if not body:
                continue
            text = gzip.decompress(body).decode("utf-8", errors="replace").lstrip("﻿")
            for row in csv.reader(io.StringIO(text)):
                if len(row) < 6 or not row[0][:1].isdigit():
                    continue
                try:
                    ts = dt.datetime.strptime(row[0].split(".")[0], "%m/%d/%Y %H:%M:%S").replace(tzinfo=UTC)
                    o, h, low, c, ask = (float(x) for x in row[1:6])
                except ValueError:
                    continue
                if low > 0 and h >= max(o, c) and low <= min(o, c):
                    rows[ts] = (ts, o, h, low, c, ask)
    return [rows[ts] for ts in sorted(rows)]


def to_m15(m1: list[tuple]) -> tuple[list[Bar], dict[dt.datetime, float]]:
    """15-minute bid bars from one-minute rows, and each bar's spread at its open (ask - bid)."""
    bars: list[Bar] = []
    spread: dict[dt.datetime, float] = {}
    cur = None
    for ts, o, h, low, c, ask in m1:
        start = ts.replace(minute=ts.minute - ts.minute % 15, second=0, microsecond=0)
        if cur is None or cur[0] != start:
            if cur:
                bars.append(Bar(*cur))
            cur = [start, o, h, low, c]
            spread[start] = ask - o
        else:
            cur[2], cur[3], cur[4] = max(cur[2], h), min(cur[3], low), c
    if cur:
        bars.append(Bar(*cur))
    return bars, spread


# ================================================================ the calendar (F-NEWS)
CAL_DIR = r4.CACHE_DIR / "calendar"
CAL_URL = "https://economic-calendar.tradingview.com/events"


def load_events(first: dt.date, last: dt.date, cache_dir: Path = CAL_DIR, pause: float = 0.5) -> list[dt.datetime]:
    """The times (UTC) of the US and euro-area events TradingView's economic calendar marks as of high
    importance, month by month; a past month is kept once fetched."""
    import json

    import requests
    out = []
    month = first.replace(day=1)
    while month <= last:
        nxt = (month.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        path = cache_dir / f"{month:%Y-%m}.json"
        if path.exists():
            times = json.loads(path.read_text())
        else:
            resp = requests.get(CAL_URL, timeout=30, headers={**r4._AGENT, "Origin": "https://www.tradingview.com"},
                                params={"from": f"{month.isoformat()}T00:00:00.000Z",
                                        "to": f"{nxt.isoformat()}T00:00:00.000Z", "countries": "US,EU"})
            resp.raise_for_status()
            body = resp.json()
            times = sorted({e["date"] for e in (body.get("result", body) if isinstance(body, dict) else body)
                            if e.get("importance") == 1})
            if nxt <= dt.date.today():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(times))
            time.sleep(pause)
        out += [dt.datetime.fromisoformat(t.replace("Z", "+00:00")) for t in times]
        month = nxt
    return sorted(out)


# ================================================================ the candidates
from bisect import bisect_left, bisect_right                     # noqa: E402
from dataclasses import dataclass                                # noqa: E402
from itertools import product                                    # noqa: E402

from cfd import indicators as ind                                # noqa: E402
from cfd import intraday as day                                  # noqa: E402

PAIR = "EURUSD"
COST_PCT = 0.015
DESIGN = (dt.date(2021, 1, 1), dt.date(2024, 12, 31))
HOLDOUT_FROM = dt.date(2025, 1, 1)
IDEAS = ("OPEN-RANGE/mid", "OPEN-RANGE/far", "BRK-15", "ASIA-SWEEP")
FILTERS = ("F-TREND", "F-NEWS", "F-COST")
EXITS = ("X1", "X2", "X3", "X3TP", "X3TP-HOLD")
NEWS_GAP = dt.timedelta(minutes=30)
MIN_TRADES, MIN_YEARS_UP = 150, 3
LOOKBACK = 120                       # bars an idea is shown


@dataclass(frozen=True)
class Setup:
    """A signal an idea made, with what a filter or an exit needs."""
    idea: str
    i: int                           # index of the signal bar
    side: str
    stop: float
    own_target: float | None
    atr: float
    trend_up: bool | None


def ema(values: list[float], n: int) -> list[float | None]:
    out, k, cur = [], 2.0 / (n + 1), None
    for j, v in enumerate(values):
        cur = v if cur is None else cur + k * (v - cur)
        out.append(cur if j >= n - 1 else None)
    return out


def setups(bars: list[Bar], first: dt.date, last: dt.date, log=None) -> list[Setup]:
    """Every signal of the four ideas on the local days `first`..`last`: the first of an idea in a day (of a
    side, for ASIA-SWEEP), as the day league makes them."""
    trend = ema([b.close for b in bars], 200)
    out, said = [], set()
    start = bisect_left([b.ts for b in bars], day._at(first, dt.time(0)))
    for i in range(max(start, LOOKBACK), len(bars)):
        end = day.local(bars[i].ts + day.BAR)
        if end.date() > last:
            break
        if end.weekday() >= 5 or not day.ENTRY_FROM <= end.time() <= day.ENTRY_UNTIL \
                or day.local(bars[i].ts).date() != end.date():
            continue
        window = bars[i - LOOKBACK + 1:i + 1]
        atr = ind.atr(window, 14)[-1]
        if not atr or trend[i] is None:
            continue
        up = bars[i].close > trend[i]
        d = end.date()
        found = day.london_break(window)
        if found and ("OR", d) not in said:
            said.add(("OR", d))
            side, high, low = found
            out.append(Setup("OPEN-RANGE/mid", i, side, (high + low) / 2, None, atr, up))
            out.append(Setup("OPEN-RANGE/far", i, side, low if side == "long" else high, None, atr, up))
        if ("BRK", d) not in said:
            for s in day.brk_15(PAIR, window):
                said.add(("BRK", d))
                out.append(Setup("BRK-15", i, s.side, s.stop_price, None, atr, up))
        for s in day.asia_sweep(PAIR, window):
            if ("SWEEP", d, s.side) not in said:
                said.add(("SWEEP", d, s.side))
                out.append(Setup("ASIA-SWEEP", i, s.side, s.stop_price, s.target_price, atr, up))
        if log and i % 20000 == 0:
            log(f"  {bars[i].ts.date()} {len(out)} setups")
    return out


def simulate(bars: list[Bar], s: Setup, exit_: str, max_cost_r: float) -> float | None:
    """The result in R, after the cost, of a setup under an exit; None when it is not a trade."""
    sign = 1 if s.side == "long" else -1
    j = s.i + 1
    if j >= len(bars):
        return None
    flat = day._at(day.local(bars[s.i].ts + day.BAR).date(), day.FLAT_AT)
    entry = bars[j].open
    r = sign * (entry - s.stop)
    if bars[j].ts >= flat or r <= 0 or r > day.MAX_R_ATR * s.atr:
        return None
    cost = COST_PCT / 100.0 * entry / r
    if cost > max_cost_r:
        return None
    if exit_ == "XOWN":
        if sign * (s.own_target - entry) < r:
            return None
        levels = [(sign * (s.own_target - entry) / r, 1.0)]
    elif exit_ == "XT":                                           # round 2: no target
        levels = []
    elif exit_ == "X234":                                         # round 2: thirds at 2R, 3R, 4R
        levels = [(2.0, 1 / 3), (3.0, 1 / 3), (4.0, 1 / 3)]
    elif exit_[1:].isdigit():
        levels = [(float(exit_[1:]), 1.0)]
    else:
        levels = [(1.0, 1 / 3), (2.0, 1 / 3), (3.0, 1 / 3)]
    trail = exit_ in ("X3TP", "X234")
    stop_r, left, got, k = -1.0, 1.0, 0.0, 0
    while j < len(bars) and left > 1e-9:
        b = bars[j]
        if b.ts >= flat:
            got += left * sign * (b.open - entry) / r
            left = 0.0
            break
        open_r = sign * (b.open - entry) / r
        if j > s.i + 1 and open_r <= stop_r:                      # a gap through the stop
            got += left * open_r
            left = 0.0
            break
        low_r = sign * ((b.low if sign > 0 else b.high) - entry) / r
        high_r = sign * ((b.high if sign > 0 else b.low) - entry) / r
        if low_r <= stop_r:                                       # the stop comes first inside a bar
            got += left * stop_r
            left = 0.0
            break
        while k < len(levels) and high_r >= levels[k][0]:
            got += levels[k][1] * max(levels[k][0], open_r if j > s.i + 1 else levels[k][0])
            left -= levels[k][1]
            k += 1
            if trail:
                stop_r = 0.0 if k == 1 else levels[k - 2][0]
        j += 1
    if left > 1e-9:                                               # the data ended
        return None
    return got - cost


@dataclass(frozen=True)
class Score:
    name: str
    n: int
    total: float
    mean: float
    win: float
    by_year: dict


def candidates():
    for idea, mask, exit_ in product(IDEAS, product((False, True), repeat=len(FILTERS)),
                                     EXITS + ("XOWN",)):
        if exit_ == "XOWN" and idea != "ASIA-SWEEP":
            continue
        yield idea, tuple(f for f, on in zip(FILTERS, mask) if on), exit_


def name_of(idea, filters, exit_) -> str:
    return " ".join([idea, *filters, exit_])


def score(bars, made: list[Setup], events: list[dt.datetime], idea, filters, exit_) -> Score:
    results = []
    for s in made:
        if s.idea != idea:
            continue
        if "F-TREND" in filters and (s.side == "long") != s.trend_up:
            continue
        at = bars[s.i].ts + day.BAR
        if "F-NEWS" in filters and bisect_right(events, at + NEWS_GAP) > bisect_left(events, at - NEWS_GAP):
            continue
        r = simulate(bars, s, exit_, 0.15 if "F-COST" in filters else day.MAX_COST_R)
        if r is not None:
            results.append((day.local(at).year, r))
    by_year: dict[int, float] = {}
    for year, r in results:
        by_year[year] = by_year.get(year, 0.0) + r
    n = len(results)
    total = sum(r for _y, r in results)
    return Score(name_of(idea, filters, exit_), n, total, total / n if n else 0.0,
                 sum(1 for _y, r in results if r > 0) / n if n else 0.0, by_year)


def run(period: str, only: list[str] | None = None, log=print) -> list[Score]:
    """Every candidate (or those named in `only`) on the "design" or the "holdout" period, best mean first."""
    m1 = load_m1()
    bars, _spread = to_m15(m1)
    last_day = bars[-1].ts.date()
    first, last = DESIGN if period == "design" else (HOLDOUT_FROM, last_day)
    log(f"{period}: {first} .. {last}, {len(bars)} bars in all")
    events = load_events(first, last)
    made = setups(bars, first, last, log=log)
    log(f"{len(made)} setups, {len(events)} high-importance events")
    scores = [score(bars, made, events, *c) for c in candidates() if not only or name_of(*c) in only]
    return sorted(scores, key=lambda s: -s.mean)


def qualifies(s: Score) -> bool:
    return s.n >= MIN_TRADES and sum(1 for v in s.by_year.values() if v > 0) >= MIN_YEARS_UP


def table(scores: list[Score]) -> str:
    years = sorted({y for s in scores for y in s.by_year})
    lines = [f"{'candidate':<44}{'n':>5}{'sum R':>8}{'mean':>7}{'win':>5}" + "".join(f"{y:>8}" for y in years)]
    for s in scores:
        lines.append(f"{s.name:<44}{s.n:>5}{s.total:>+8.1f}{s.mean:>+7.3f}{s.win:>5.0%}"
                     + "".join(f"{s.by_year.get(y, 0.0):>+8.1f}" for y in years))
    return "\n".join(lines)


# ================================================================ round 2: fewer trades, further targets
COST_CAPS = (0.25, 0.10, 0.07)
EXITS_2 = ("X3", "X4", "X5", "XT", "X234")
MIN_TRADES_2 = 100


def run2(period: str, only: list[str] | None = None, log=print) -> list[Score]:
    bars, _spread = to_m15(load_m1())
    first, last = DESIGN if period == "design" else (HOLDOUT_FROM, bars[-1].ts.date())
    log(f"{period}: {first} .. {last}")
    made = setups(bars, first, last)
    scores = []
    for idea, cap, trend, exit_ in product(IDEAS, COST_CAPS, (False, True), EXITS_2):
        name = " ".join([idea, f"C{cap:.2f}", *(["F-TREND"] if trend else []), exit_])
        if only and name not in only:
            continue
        results = []
        for s in made:
            if s.idea != idea or (trend and (s.side == "long") != s.trend_up):
                continue
            r = simulate(bars, s, exit_, cap)
            if r is not None:
                results.append((day.local(bars[s.i].ts + day.BAR).year, r))
        by_year: dict[int, float] = {}
        for year, r in results:
            by_year[year] = by_year.get(year, 0.0) + r
        n, total = len(results), sum(r for _y, r in results)
        scores.append(Score(name, n, total, total / n if n else 0.0,
                            sum(1 for _y, r in results if r > 0) / n if n else 0.0, by_year))
    return sorted(scores, key=lambda s: -s.mean)


def qualifies2(s: Score) -> bool:
    return s.n >= MIN_TRADES_2 and sum(1 for v in s.by_year.values() if v > 0) >= MIN_YEARS_UP


# ================================================================ swing: the same entries, held hours to weeks
# docs/cfd/PREREGISTRATION_SWING_EURUSD.md
from cfd import league                                            # noqa: E402

HOUR = dt.timedelta(hours=1)
SWING_DESIGN = (dt.date(2012, 1, 1), dt.date(2019, 12, 31))
SWING_HOLDOUT_FROM = dt.date(2020, 1, 1)
NIGHT_PCT = 0.010
SWING_ENTRIES = ("OPEN-RANGE/mid", "OPEN-RANGE/far", "BRK", "ASIA-SWEEP")
SWING_TARGETS = ("X3", "X5", "X8", "XT", "X234")
SWING_HOLDS = (7, 14, 28)
SWING_MIN_TRADES, SWING_MIN_YEARS = 100, 6


def to_hours(m1: list[tuple]) -> list[Bar]:
    bars, cur = [], None
    for ts, o, h, low, c, _ask in m1:
        start = ts.replace(minute=0, second=0, microsecond=0)
        if cur is None or cur[0] != start:
            if cur:
                bars.append(Bar(*cur))
            cur = [start, o, h, low, c]
        else:
            cur[2], cur[3], cur[4] = max(cur[2], h), min(cur[3], low), c
    if cur:
        bars.append(Bar(*cur))
    return bars


def hourly() -> list[Bar]:
    """2012-2020 from FXCM's hourly archive, then hours built from its one-minute candles."""
    old = [b for b in r4.load_fxcm_hourly(PAIR, 2012, 2020) if b.ts.year <= 2020]
    new = [b for b in to_hours(load_m1()) if b.ts.year >= 2021]
    return old + new


def daily_range(bars: list[Bar]) -> dict[dt.date, float]:
    """UTC date -> the mean high-low of the 14 days before it."""
    days: dict[dt.date, list[float]] = {}
    for b in bars:
        hl = days.setdefault(b.ts.date(), [b.high, b.low])
        hl[0], hl[1] = max(hl[0], b.high), min(hl[1], b.low)
    dates = sorted(d for d in days if d.weekday() < 5)
    out = {}
    for k in range(14, len(dates)):
        out[dates[k]] = sum(days[d][0] - days[d][1] for d in dates[k - 14:k]) / 14
    return out


def swing_setups(bars: list[Bar], first: dt.date, last: dt.date) -> list[Setup]:
    """Every hourly signal of the entries on the local days `first`..`last` (one trade at a time is the
    simulation's business, not this list's)."""
    trend = ema([b.close for b in bars], 200)
    out, said = [], set()
    start = bisect_left([b.ts for b in bars], day._at(first, dt.time(0)))
    for i in range(max(start, 250), len(bars)):
        end = day.local(bars[i].ts + HOUR)
        if end.date() > last:
            break
        if end.weekday() >= 5 or not day.ENTRY_FROM <= end.time() <= day.ENTRY_UNTIL or trend[i] is None:
            continue
        d = end.date()
        up = bars[i].close > trend[i]
        nine = day._at(d, day.ENTRY_FROM)
        today = [b for b in bars[i - 12:i + 1] if b.ts >= nine]
        # OPEN-RANGE: the hour of London's open
        london = day._at(bars[i].ts.astimezone(day.LONDON).date(), dt.time(8), day.LONDON)
        first_hour = next((b for b in bars[i - 12:i + 1] if b.ts == london), None)
        later = [b for b in bars[i - 12:i + 1] if b.ts > london]
        if first_hour and later and ("OR", d) not in said:
            hi, lo = first_hour.high, first_hour.low
            if not any(b.close > hi or b.close < lo for b in later[:-1]) and not lo <= later[-1].close <= hi:
                said.add(("OR", d))
                side = "long" if later[-1].close > hi else "short"
                out.append(Setup("OPEN-RANGE/mid", i, side, (hi + lo) / 2, None, 0.0, up))
                out.append(Setup("OPEN-RANGE/far", i, side, lo if side == "long" else hi, None, 0.0, up))
        # BRK: the league's rule on these bars
        for s in league.brk_retest({PAIR: bars[i - 79:i + 1]}):
            out.append(Setup("BRK", i, s.side, s.stop_price, None, 0.0, up))
        # ASIA-SWEEP: the six hours before 09:00
        asia = [b for b in bars[i - 20:i + 1] if nine - 6 * HOUR <= b.ts < nine]
        if len(asia) >= 4 and today:
            c = len(today) - 1
            for side, sign in (("long", 1), ("short", -1)):
                floor = min(sign * (b.low if sign > 0 else b.high) for b in asia)
                lo_ = (lambda b: sign * (b.low if sign > 0 else b.high))
                for b in range(max(c - 2, 0), c + 1):
                    if lo_(today[b]) < floor and sign * today[c].close > floor \
                            and all(sign * x.close <= floor for x in today[b:c]):
                        if ("SWEEP", d, side) not in said:
                            said.add(("SWEEP", d, side))
                            out.append(Setup("ASIA-SWEEP", i, side, sign * min(lo_(x) for x in today[b:]), None, 0.0, up))
                        break
    return out


def swing_trade(bars: list[Bar], s: Setup, wide: float | None, target: str, hold_days: int):
    """(result in R after costs, index of the exit bar), or None when it is not a trade. `wide` is the least
    distance of the stop (half the average daily range) or None for the entry's own stop."""
    sign = 1 if s.side == "long" else -1
    j = s.i + 1
    if j >= len(bars):
        return None
    entry = bars[j].open
    r = sign * (entry - s.stop)
    if r <= 0:
        return None
    if wide is not None:
        r = max(r, wide)
    if COST_PCT / 100.0 * entry / r > day.MAX_COST_R:
        return None
    levels = {"XT": [], "X234": [(2.0, 1 / 3), (3.0, 1 / 3), (4.0, 1 / 3)]}.get(target) or \
        ([] if target == "XT" else [(float(target[1:]), 1.0)])
    trail = target == "X234"
    until = bars[j].ts + dt.timedelta(days=hold_days)
    stop_r, left, got, k, first = -1.0, 1.0, 0.0, 0, j
    while j < len(bars):
        b = bars[j]
        open_r = sign * (b.open - entry) / r
        if b.ts >= until or (j > first and open_r <= stop_r):
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
        j += 1
    else:
        return None                                                # the data ended
    nights = (bars[j].ts.date() - bars[first].ts.date()).days
    return got - (COST_PCT + nights * NIGHT_PCT) / 100.0 * entry / r, j


def run_swing(period: str, only: list[str] | None = None, log=print) -> list[Score]:
    bars = hourly()
    first, last = SWING_DESIGN if period == "design" else (SWING_HOLDOUT_FROM, bars[-1].ts.date())
    log(f"{period}: {first} .. {last}, {len(bars)} hourly bars")
    made = swing_setups(bars, first, last)
    ranges = daily_range(bars)
    log(f"{len(made)} setups")
    scores = []
    for entry, wide, trend, target, hold in product(SWING_ENTRIES, (False, True), (False, True),
                                                    SWING_TARGETS, SWING_HOLDS):
        name = " ".join([entry, "wide" if wide else "own", *(["F-TREND"] if trend else []), target, f"{hold}d"])
        if only and name not in only:
            continue
        results, free_from = [], 0
        for s in made:
            if s.idea != entry or s.i < free_from or (trend and (s.side == "long") != s.trend_up):
                continue
            half = ranges.get(bars[s.i].ts.date())
            if wide and half is None:
                continue
            got = swing_trade(bars, s, half / 2 if wide else None, target, hold)
            if got is None:
                continue
            results.append((day.local(bars[s.i].ts).year, got[0]))
            free_from = got[1]
        by_year: dict[int, float] = {}
        for year, r in results:
            by_year[year] = by_year.get(year, 0.0) + r
        n, total = len(results), sum(r for _y, r in results)
        scores.append(Score(name, n, total, total / n if n else 0.0,
                            sum(1 for _y, r in results if r > 0) / n if n else 0.0, by_year))
    return sorted(scores, key=lambda s: -s.mean)


def qualifies_swing(s: Score) -> bool:
    return s.n >= SWING_MIN_TRADES and sum(1 for v in s.by_year.values() if v > 0) >= SWING_MIN_YEARS
