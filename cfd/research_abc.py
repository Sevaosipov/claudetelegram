"""cfd/research_abc.py: the 0-A-B-C «sequence» of the sheet the user sent (docs/cfd/PREREGISTRATION_ABC.md) on
EURUSD's 15-minute bars: a retracement entry into a move from a day's extreme, targets at A and its extensions."""
from __future__ import annotations

import datetime as dt
from itertools import product

from cfd import indicators as ind
from cfd import intraday as day
from cfd import research_day as rd
from cfd import research_edge as edge
from cfd.data import Bar

COST, NIGHT_PCT, MAX_COST_R = 0.8e-4, 0.010, 0.25
LOOK = 96


def trades(bars: list[Bar], atr: list, first: dt.date, last: dt.date, sign: int, *, m: float, e: float,
           stop_at: float, thirds: bool) -> list[tuple[int, float]]:
    """(year, result in R after costs) of one side's trades; `sign` +1 long, -1 short (prices mirrored).
    `stop_at` is the retracement the stop sits at: 1.0 is point 0."""
    hi = [sign * (b.high if sign > 0 else b.low) for b in bars]
    lo = [sign * (b.low if sign > 0 else b.high) for b in bars]
    op = [sign * b.open for b in bars]
    out = []
    zero = top = None
    order = None                       # (entry, stop, [levels]) waiting for a fill, set at bar `since`
    since = -1
    i = LOOK
    n = len(bars)
    while i < n:
        d = day.local(bars[i].ts).date()
        if d > last:
            break
        if lo[i] <= min(lo[i - LOOK + 1:i + 1]):                 # a day's low: a new point 0
            zero, top, order = lo[i], hi[i], None
            i += 1
            continue
        if zero is None:
            i += 1
            continue
        if order and i > since and lo[i] <= order[0] and d >= first:
            entry, stop, levels = order
            entry = min(entry, op[i])                            # a gap under the limit: the open
            order = None
            r = entry - stop
            if r > 0 and COST / r <= MAX_COST_R:
                got, j = _run(bars, hi, lo, op, i, entry, stop, levels, thirds)
                if got is not None:
                    nights = (bars[j].ts.date() - bars[i].ts.date()).days
                    out.append((day.local(bars[i].ts).year,
                                got - (COST + nights * NIGHT_PCT / 100.0 * abs(entry)) / r))
                    zero = top = None                            # the sequence is spent
                    i = j + 1
                    continue
        if hi[i] > top:
            top, order = hi[i], None                             # a new A is forming
        size = top - zero
        if order is None and atr[i] and size >= m * atr[i] and lo[i] <= top - 0.382 * size:
            entry = top - e * size
            if lo[i] > entry:                                    # not already through the entry
                order = (entry, top - stop_at * size, [top, entry + size, entry + 1.618 * size])
                since = i
        i += 1
    return out


def _run(bars, hi, lo, op, i, entry, stop, levels, thirds):
    """The trade filled at bar i: (result in R before costs, exit index), or (None, i) when the data ends."""
    r = entry - stop
    plan = [(p, 1 / 3) for p in levels] if thirds else [(levels[0], 1.0)]
    left, got, k, cur = 1.0, 0.0, 0, stop
    j = i
    while j < len(bars):
        if j > i and op[j] <= cur:
            return got + left * (op[j] - entry) / r, j
        if lo[j] <= cur:
            return got + left * (cur - entry) / r, j
        if j > i:                                                # no target in the bar of the fill
            while k < len(plan) and hi[j] >= plan[k][0]:
                price = max(plan[k][0], op[j])
                got += plan[k][1] * (price - entry) / r
                left -= plan[k][1]
                k += 1
                cur = entry if k == 1 else plan[k - 2][0]
            if left <= 1e-9:
                return got, j
        j += 1
    return None, i


def run(period: str, only: list[str] | None = None, log=print) -> list[edge.Result]:
    bars, _ = rd.to_m15(rd.load_m1())
    first, last = rd.DESIGN if period == "design" else (rd.HOLDOUT_FROM, bars[-1].ts.date())
    start = next(k for k, b in enumerate(bars) if day.local(b.ts).date() >= first - dt.timedelta(days=5))
    bars = bars[start:]
    atr = ind.atr(bars, 14)
    log(f"{period}: {first} .. {last}")
    out = []
    for m, e, stop_at, thirds in product((4.0, 8.0), (0.5, 0.618), (1.0, 0.786), (False, True)):
        name = f"ABC M{m:g} E{e:g} stop{'0' if stop_at == 1.0 else '78.6'} {'thirds' if thirds else 'A'}"
        if only and name not in only:
            continue
        rows = []
        for sign in (1, -1):
            rows += trades(bars, atr, first, last, sign, m=m, e=e, stop_at=stop_at, thirds=thirds)
        out.append(edge.result(name, rows))
    return sorted(out, key=lambda x: -x.t)
