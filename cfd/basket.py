"""cfd/basket.py: H7 (CARRY-BASKET) of docs/cfd/PREREGISTRATION_R3.md -- the three highest-yielding of
USD, EUR, GBP, JPY, AUD, CAD, CHF and NZD against the three lowest, rebalanced on the first daily bar of
each calendar month. Everything here is a pure function of prices and rates; the loading (Yahoo bars,
FRED rates) and the gate belong to cfd/research3.py.

The rules, as written:
  ranking     on the first daily bar of the month, by the rate in force that day (the publication rule
              of cfd/rates.py); a currency with no rate is left out, and at least six are needed or
              there is no basket that month. Long the top three, short the bottom three, equal weights.
              (A currency with no price against USD that day is left out the same way: it cannot trade.
              Ties are broken by the order of the currency list, so a run is reproducible.)
  holding     to the first daily bar of the next month. A month is a complete month: the one still
              running when the data ends is not a month yet.
  leg return  its spot change against USD over the month -- the close of XXXUSD=X, or the inverse of
              USDXXX=X; USD itself is 0 -- plus (its rate - the USD rate) / 12 / 100, the rate being in
              percent a year and the one in force at the rebalance.
  month       mean(long legs) - mean(short legs), less the costs below.
  costs       0.015 % x 2 for every leg that enters or leaves the basket, weighted 1/3 (a leg that changes
              side leaves one and enters the other: two); a financing markup of 0.004 % a night on each of
              the six legs, weighted 1/3 -- 0.008 % x the nights in the month.
  statistics  on monthly returns: profit factor = sum of positive months / |sum of negative months|; t on
              the mean month (sample standard deviation, n - 1); the worst peak-to-trough in %.

Prices of a currency in USD are lists of (date, value); the value on a day is that of the latest bar on or
before it, so a day one pair lacks does not drop the currency. Returns are fractions (0.01 = 1 %).
"""
from __future__ import annotations

import bisect
import datetime as dt
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from cfd.data import Bar
from cfd.instruments import H7_CURRENCIES

CURRENCIES = H7_CURRENCIES
USD = "USD"
LEGS_PER_SIDE = 3
MIN_CURRENCIES = 6                  # a basket needs this many currencies with a rate (and a price)
LEG_WEIGHT = 1.0 / 3.0              # the weight of a leg in each cost, and in each side's mean
TURNOVER_COST_PCT = 0.015 * 2       # per leg that enters or leaves the basket, in % of the leg's notional
FINANCING_MARKUP_PCT = 0.004        # per night per leg, in %
MONTHS_PER_YEAR = 12

RateOn = Callable[[str, dt.date], "float | None"]
Series = Sequence[tuple[dt.date, float]]


@dataclass(frozen=True)
class Basket:
    """The three longs (highest rate first) and the three shorts (lowest rate first)."""
    longs: tuple[str, ...]
    shorts: tuple[str, ...]

    @property
    def legs(self) -> dict[str, int]:
        """Currency -> +1 (long) or -1 (short)."""
        return {**{c: 1 for c in self.longs}, **{c: -1 for c in self.shorts}}


@dataclass(frozen=True)
class Month:
    """One month of the basket: the day it was set up and the day it was closed (the next month's), what
    it held, and the return before costs, the costs and the return after them."""
    start: dt.date
    end: dt.date
    basket: Basket
    nights: int
    events: int          # legs that entered or left the basket at this rebalance
    gross: float
    cost: float
    net: float


@dataclass(frozen=True)
class MonthStats:
    n: int
    win_rate: float
    profit_factor: float
    mean: float          # the mean month, a fraction
    t_stat: float
    total: float         # the sum of the months, a fraction
    max_drawdown_pct: float


# ---------------------------------------------------------------- the basket
def select(rates: Mapping[str, float | None]) -> Basket | None:
    """Rank the currencies that have a rate (highest first; a tie goes to the earlier in `CURRENCIES`)
    and take the top three long and the bottom three short. None when fewer than six have one."""
    ranked = sorted((c for c in CURRENCIES if rates.get(c) is not None),
                    key=lambda c: (-rates[c], CURRENCIES.index(c)))
    if len(ranked) < MIN_CURRENCIES:
        return None
    return Basket(tuple(ranked[:LEGS_PER_SIDE]), tuple(reversed(ranked[-LEGS_PER_SIDE:])))


def turnover_events(previous: Basket | None, new: Basket) -> int:
    """How many legs enter or leave the basket at a rebalance: a leg that was in and is not (or is on the
    other side now) leaves, a leg that is in and was not (or was on the other side) enters, so a currency
    that changes side counts twice. The first basket has six entries."""
    before = previous.legs if previous is not None else {}
    after = new.legs
    leaves = sum(1 for c, side in before.items() if after.get(c) != side)
    enters = sum(1 for c, side in after.items() if before.get(c) != side)
    return leaves + enters


def leg_return(spot_start: float, spot_end: float, rate: float, usd_rate: float) -> float:
    """A leg's return against USD over a month, as if long the currency: its price in USD at the end over
    the start, less one, plus the carry (rate - USD rate) / 12 / 100."""
    return spot_end / spot_start - 1.0 + (rate - usd_rate) / MONTHS_PER_YEAR / 100.0


def basket_return(basket: Basket, leg_returns: Mapping[str, float]) -> float:
    """mean(long legs) - mean(short legs): equal weights, six legs."""
    long_mean = sum(leg_returns[c] for c in basket.longs) / len(basket.longs)
    short_mean = sum(leg_returns[c] for c in basket.shorts) / len(basket.shorts)
    return long_mean - short_mean


def month_cost(events: int, nights: int) -> float:
    """The costs of a month, as a fraction: 0.015 % x 2 for each leg that entered or left, weighted 1/3,
    plus 0.004 % a night on each of the six legs, weighted 1/3 (0.008 % a night)."""
    turnover = events * TURNOVER_COST_PCT / 100.0 * LEG_WEIGHT
    financing = nights * FINANCING_MARKUP_PCT / 100.0 * 2 * LEGS_PER_SIDE * LEG_WEIGHT
    return turnover + financing


# ---------------------------------------------------------------- the calendar and the prices
def rebalance_dates(dates: Sequence[dt.date]) -> list[dt.date]:
    """The first of the given days in each calendar month, in order."""
    first: dict[tuple[int, int], dt.date] = {}
    for d in sorted(set(dates)):
        first.setdefault((d.year, d.month), d)
    return sorted(first.values())


def usd_values(bars: Sequence[Bar], inverted: bool) -> list[tuple[dt.date, float]]:
    """The daily closes of a currency's pair as its price in USD: XXXUSD=X as it is, USDXXX=X inverted."""
    return [(b.ts.date(), 1.0 / b.close if inverted else b.close) for b in bars]


def value_on(series: Series, day: dt.date) -> float | None:
    """The value of the latest bar on or before `day`, None before the first."""
    k = bisect.bisect_right([d for d, _ in series], day) - 1
    return None if k < 0 else series[k][1]


# ---------------------------------------------------------------- the months
def run_months(values: Mapping[str, Series], rate_on: RateOn) -> list[Month]:
    """Every complete month of the basket. `values` holds, for each currency but USD, its price in USD
    (see `usd_values`); `rate_on(currency, day)` is the rate in force on a day (rates.Rates.rate_on).
    The rebalance days are the first day of each month among the days of all the series. A month has
    no basket -- and is not a month of the result -- when USD has no rate (no carry can be measured) or
    fewer than six currencies have a rate and a price covering the whole month; the next basket is then
    compared with the last one held, so every entry and exit is charged once."""
    days = sorted({d for series in values.values() for d, _ in series})
    starts = rebalance_dates(days)
    index = {c: [d for d, _ in values[c]] for c in values}
    months: list[Month] = []
    held: Basket | None = None
    for start, end in zip(starts, starts[1:]):
        usd_rate = rate_on(USD, start)
        if usd_rate is None:
            continue
        rates: dict[str, float | None] = {USD: usd_rate}
        spots: dict[str, tuple[float, float]] = {USD: (1.0, 1.0)}
        for c in CURRENCIES:
            if c == USD:
                continue
            series = values.get(c)
            first, last = (value_on(series, start), value_on(series, end)) if series else (None, None)
            covered = bool(series) and index[c][-1] >= end
            rate = rate_on(c, start)
            if first is None or last is None or not covered or rate is None:
                rates[c] = None
                continue
            rates[c] = rate
            spots[c] = (first, last)
        basket = select(rates)
        if basket is None:
            continue
        legs = {c: leg_return(*spots[c], rates[c], usd_rate) for c in basket.legs}
        events = turnover_events(held, basket)
        nights = (end - start).days
        gross = basket_return(basket, legs)
        cost = month_cost(events, nights)
        months.append(Month(start, end, basket, nights, events, gross, cost, gross - cost))
        held = basket
    return months


# ---------------------------------------------------------------- the statistics
def month_stats(returns: Sequence[float]) -> MonthStats:
    """Months, share of positive months, profit factor (sum of the positive months over the absolute sum
    of the negative ones; infinite with no losing month, 0 with no winning one), the mean month, the
    t-statistic of the mean (mean / (sample sd / sqrt n); 0 with fewer than two months or no spread), the
    sum, and the worst peak-to-trough of the compounded equity curve, in % (from 1, months in order)."""
    n = len(returns)
    if n == 0:
        return MonthStats(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    gross_win = sum(x for x in returns if x > 0)
    gross_loss = -sum(x for x in returns if x < 0)
    pf = gross_win / gross_loss if gross_loss > 0 else (math.inf if gross_win > 0 else 0.0)
    mean = sum(returns) / n
    t = 0.0
    if n >= 2:
        sd = math.sqrt(sum((x - mean) ** 2 for x in returns) / (n - 1))
        t = mean / (sd / math.sqrt(n)) if sd > 0 else 0.0
    equity = peak = 1.0
    worst = 0.0
    for x in returns:
        equity *= 1.0 + x
        peak = max(peak, equity)
        worst = max(worst, (peak - equity) / peak)
    return MonthStats(n, sum(1 for x in returns if x > 0) / n, pf, mean, t, sum(returns), worst * 100.0)


def stats_of(months: Sequence[Month]) -> MonthStats:
    """`month_stats` of the net returns of the months, in the order given."""
    return month_stats([m.net for m in months])


def by_year(months: Sequence[Month]) -> list[tuple[int, MonthStats]]:
    """Statistics per calendar year of the month's start (for information, not a gate)."""
    years: dict[int, list[Month]] = {}
    for m in months:
        years.setdefault(m.start.year, []).append(m)
    return [(y, stats_of(years[y])) for y in sorted(years)]
