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
           stop_at: float, thirds: bool, look: int = LOOK, stop_atr: float | None = None,
           targets: tuple | None = None, trend: list | None = None, cost: float = COST,
           night_pct: float = NIGHT_PCT, break_k: int = 0, c_target: str | None = None,
           cci: list | None = None, stop_fixed: float | None = None,
           log: list | None = None) -> list[tuple[int, float]]:
    """(year, result in R after costs) of one side's trades; `sign` +1 long, -1 short (prices mirrored).
    `stop_at` is the retracement the stop sits at: 1.0 is point 0, more is beyond it; with `stop_atr` the
    stop is that many ATR from the entry instead. `look` bars make «an absolute low». `targets` (round 3) are
    the exits as multiples of 0-A from point 0, an equal share at each; `trend` an average the close must be
    above (mirrored) for a setup to count. With `log` (cfd/sk_live.py) what happens is appended to it as
    it happens -- ("order", bar, bar of 0, bar of A, entry, stop, target), ("cancel", bar), ("void", bar),
    ("fill", bar, price), ("exit", bar, result in R) -- prices mirrored as here; and the replay stops at a
    trade the bars leave open."""
    hi = [sign * (b.high if sign > 0 else b.low) for b in bars]
    lo = [sign * (b.low if sign > 0 else b.high) for b in bars]
    op = [sign * b.open for b in bars]
    out = []
    last_swing = [None] * len(bars)          # the last swing high confirmed by each bar (`break_k` bars a side)
    if break_k:
        cur_swing = None
        for j in range(len(bars)):
            p = j - break_k
            if p >= break_k and hi[p] >= max(hi[p - break_k:p + break_k + 1]):
                cur_swing = hi[p]
            last_swing[j] = cur_swing
    barrier = None
    zero_i = top_i = 0
    zero = top = None
    order = None                       # (entry, stop, [levels]) waiting for a fill, set at bar `since`
    since = -1
    i = look
    n = len(bars)
    while i < n:
        d = day.local(bars[i].ts).date()
        if d > last:
            break
        fills = bool(order) and i > since and lo[i] <= order[0]
        if not fills and lo[i] <= min(lo[i - look + 1:i + 1]):   # a new extreme: a new point 0
            if order and log is not None:
                log.append(("cancel", i))
            zero, top, order = lo[i], hi[i], None
            zero_i = top_i = i
            barrier = last_swing[i]                              # the lower high a structure shift must break
            i += 1
            continue
        if zero is None:
            i += 1
            continue
        # the fill comes before anything else in the bar: a bar that falls through the entry and through
        # point 0 is a filled trade and a stop, not a setup that quietly lapsed
        if fills and d < first:
            order = None
        elif fills:
            entry, stop, levels = order
            entry = min(entry, op[i])                            # a gap under the limit: the open
            order = None
            r = entry - stop
            if log is not None:
                log.append(("fill", i, entry) if r > 0 and cost / r <= MAX_COST_R else ("void", i))
            if r > 0 and cost / r <= MAX_COST_R:
                got, j = _run(bars, hi, lo, op, i, entry, stop, levels, thirds,
                              even=targets is not None or c_target is not None)
                if got is not None:
                    nights = (bars[j].ts.date() - bars[i].ts.date()).days
                    out.append((day.local(bars[i].ts).year,
                                got - (cost + nights * night_pct / 100.0 * abs(entry)) / r))
                    if log is not None:
                        log.append(("exit", j, out[-1][1]))
                    zero = top = None                            # the sequence is spent
                    i = j + 1
                    continue
                if log is not None:
                    break                                        # the trade is still open: nothing after it yet
            zero = top = None                                    # filled, or refused for its cost: spent
            i += 1
            continue
        if hi[i] > top:
            if order and log is not None:
                log.append(("cancel", i))
            top, order, top_i = hi[i], None, i                   # a new A is forming
        size = top - zero
        with_trend = trend is None or (trend[i] is not None and sign * bars[i].close > sign * trend[i])
        shifted = not break_k or (barrier is not None and top > barrier)
        if cci is not None:      # round 3: CCI beyond 100 on point 0's side at 0, on the other side at A
            c0, ca = cci[zero_i], cci[top_i]
            shifted = shifted and c0 is not None and ca is not None and sign * c0 < -100 and sign * ca > 100
        if order is None and atr[i] and size >= m * atr[i] and lo[i] <= top - 0.382 * size and with_trend \
                and shifted:
            entry = top - e * size
            if lo[i] > entry:                                    # not already through the entry
                stop = entry - stop_atr * atr[i] if stop_atr else top - stop_at * size
                if stop_fixed:                                   # round 4: a fixed distance in price
                    stop = entry - stop_fixed
                levels = [zero + k * size for k in targets] if targets else [top, entry + size, entry + 1.618 * size]
                if c_target:                                     # round 2: A, C (the entry plus 0-A), or both
                    levels = {"A": [top], "C": [entry + size], "A+C": [top, entry + size],
                              "C161": [entry + 1.618 * size]}[c_target]
                order = (entry, stop, levels)
                since = i
                if log is not None:
                    log.append(("order", i, zero_i, top_i, entry, stop, levels[0]))
        i += 1
    return out


def _run(bars, hi, lo, op, i, entry, stop, levels, thirds, even=False):
    """The trade filled at bar i: (result in R before costs, exit index), or (None, i) when the data ends."""
    r = entry - stop
    plan = [(p, 1 / len(levels)) for p in levels] if even else \
        [(p, 1 / 3) for p in levels] if thirds else [(levels[0], 1.0)]
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


# ---------------------------------------------------------------- round 2: four-hour bars, other stops
H4_DESIGN = (dt.date(2012, 1, 1), dt.date(2019, 12, 31))
H4_HOLDOUT_FROM = dt.date(2020, 1, 1)
STOPS = {"78.6": (0.786, None), "0": (1.0, None), "0+25%": (1.25, None), "0+50%": (1.5, None), "2atr": (1.0, 2.0)}


def to_h4(hours: list[Bar]) -> list[Bar]:
    out, cur = [], None
    for b in hours:
        start = b.ts.replace(hour=b.ts.hour - b.ts.hour % 4)
        if cur is None or cur[0] != start:
            if cur:
                out.append(Bar(*cur))
            cur = [start, b.open, b.high, b.low, b.close]
        else:
            cur[2], cur[3], cur[4] = max(cur[2], b.high), min(cur[3], b.low), b.close
    if cur:
        out.append(Bar(*cur))
    return out


def run_h4(period: str, only: list[str] | None = None, log=print) -> list[edge.Result]:
    bars = to_h4(rd.hourly())
    first, last = H4_DESIGN if period == "design" else (H4_HOLDOUT_FROM, bars[-1].ts.date())
    start = next(k for k, b in enumerate(bars) if b.ts.date() >= first - dt.timedelta(days=30))
    bars = bars[max(start, 0):]
    atr = ind.atr(bars, 14)
    log(f"{period}: {first} .. {last}")
    out = []
    for m, e, stop, thirds in product((4.0, 8.0), (0.5, 0.618), STOPS, (False, True)):
        name = f"ABC-H4 M{m:g} E{e:g} stop {stop} {'thirds' if thirds else 'A'}"
        if only and name not in only:
            continue
        at, k = STOPS[stop]
        rows = []
        for sign in (1, -1):
            rows += trades(bars, atr, first, last, sign, m=m, e=e, stop_at=at, thirds=thirds, look=30, stop_atr=k)
        out.append(edge.result(name, rows))
    return sorted(out, key=lambda x: -x.t)


# ---------------------------------------------------------------- TPR across eight markets (PREREGISTRATION_TPR.md)
TPR_DESIGN = (dt.date(2012, 1, 1), dt.date(2019, 12, 31))
TPR_HOLDOUT_FROM = dt.date(2020, 1, 1)
PIP = {"EURUSD": 1e-4, "GBPUSD": 1e-4, "AUDUSD": 1e-4, "USDJPY": 1e-2, "EURGBP": 1e-4, "EURJPY": 1e-2, "GBPJPY": 1e-2}
COST_PIPS = {"EURUSD": 0.8, "GBPUSD": 1.1, "AUDUSD": 0.9, "USDJPY": 0.9, "EURGBP": 1.2, "EURJPY": 1.3, "GBPJPY": 1.8}
TARGETS = {"A": (1.0,), "EXT": (1.618,), "A+EXT": (1.0, 1.618)}
_HOURS: dict[str, list[Bar]] = {}


def market_hours(name: str) -> list[Bar]:
    if name not in _HOURS:
        from cfd import research4 as r4
        from cfd import research_gold
        _HOURS[name] = (rd.hourly() if name == "EURUSD" else research_gold.load_gold() if name == "XAUUSD"
                        else r4.load_fxcm_hourly(name, 2012, 2024))
    return _HOURS[name]


def tpr_names():
    for frame, e, target, m, trend in product(("H1", "H4"), (0.5, 0.618), TARGETS, (0.0, 8.0), (False, True)):
        yield (f"TPR {frame} E{e:g} {target}{' M8' if m else ''}{' trend' if trend else ''}", frame, e, target, m, trend)


def run_tpr(period: str, only: list[str] | None = None, log=print):
    """{candidate: {market: rows}} on the "design" or the "holdout" period."""
    out: dict[str, dict[str, list]] = {}
    for market in [*PIP, "XAUUSD"]:
        hours = market_hours(market)
        cost = 0.30 if market == "XAUUSD" else COST_PIPS[market] * PIP[market]
        night = 0.020 if market == "XAUUSD" else 0.010
        for frame in ("H1", "H4"):
            bars = hours if frame == "H1" else to_h4(hours)
            first, last = TPR_DESIGN if period == "design" else (TPR_HOLDOUT_FROM, bars[-1].ts.date())
            start = next(k for k, b in enumerate(bars) if b.ts.date() >= first - dt.timedelta(days=120))
            bars = bars[start:]
            atr = ind.atr(bars, 14)
            ema = ind.ema([b.close for b in bars], 200)
            for name, fr, e, target, m, trend in tpr_names():
                if fr != frame or (only and name not in only):
                    continue
                rows = []
                for sign in (1, -1):
                    rows += trades(bars, atr, first, last, sign, m=m, e=e, stop_at=1.0, thirds=False, look=300,
                                   targets=TARGETS[target], trend=ema if trend else None, cost=cost, night_pct=night)
                out.setdefault(name, {})[market] = rows
        log(f"  {market} done")
    return out


def run_sk(period: str, log=print):
    """Round 2: {candidate: {market: rows}} with the structure shift."""
    out: dict[str, dict[str, list]] = {}
    for market in [*PIP, "XAUUSD"]:
        hours = market_hours(market)
        cost = 0.30 if market == "XAUUSD" else COST_PIPS[market] * PIP[market]
        night = 0.020 if market == "XAUUSD" else 0.010
        for frame in ("H1", "H4"):
            bars = hours if frame == "H1" else to_h4(hours)
            first, last = TPR_DESIGN if period == "design" else (TPR_HOLDOUT_FROM, bars[-1].ts.date())
            start = next(k for k, b in enumerate(bars) if b.ts.date() >= first - dt.timedelta(days=120))
            bars = bars[start:]
            atr = ind.atr(bars, 14)
            for e, target in product((0.5, 0.618), ("A", "C", "A+C")):
                rows = []
                for sign in (1, -1):
                    rows += trades(bars, atr, first, last, sign, m=0.0, e=e, stop_at=1.0, thirds=False, look=300,
                                   cost=cost, night_pct=night, break_k=3, c_target=target)
                out.setdefault(f"SK {frame} E{e:g} {target}", {})[market] = rows
        log(f"  {market} done")
    return out


def cci_of(bars: list[Bar], n: int = 20) -> list[float | None]:
    """The Commodity Channel Index: the typical price against its average, in units of 0.015 mean deviations."""
    tp = [(b.high + b.low + b.close) / 3 for b in bars]
    out: list[float | None] = [None] * len(bars)
    for i in range(n - 1, len(bars)):
        window = tp[i - n + 1:i + 1]
        mean = sum(window) / n
        dev = sum(abs(x - mean) for x in window) / n
        out[i] = (tp[i] - mean) / (0.015 * dev) if dev else None
    return out


SK3 = [(0.5, 0.667), (0.559, 0.618), (0.618, 0.667)]


def run_sk3(period: str, log=print):
    """Round 3: {candidate: {market: rows}} -- the CCI check and the in-zone stops."""
    out: dict[str, dict[str, list]] = {}
    for market in [*PIP, "XAUUSD"]:
        hours = market_hours(market)
        cost = 0.30 if market == "XAUUSD" else COST_PIPS[market] * PIP[market]
        night = 0.020 if market == "XAUUSD" else 0.010
        for frame in ("H1", "H4"):
            bars = hours if frame == "H1" else to_h4(hours)
            first, last = TPR_DESIGN if period == "design" else (TPR_HOLDOUT_FROM, bars[-1].ts.date())
            start = next(k for k, b in enumerate(bars) if b.ts.date() >= first - dt.timedelta(days=120))
            bars = bars[start:]
            atr, cci = ind.atr(bars, 14), cci_of(bars)
            plans = [(e, stop, target, use) for (e, stop), target, use in product(SK3, ("A", "C"), (False, True))]
            plans += [(0.618, 1.0, "A", True)]
            for e, stop, target, use in plans:
                rows = []
                for sign in (1, -1):
                    rows += trades(bars, atr, first, last, sign, m=0.0, e=e, stop_at=stop, thirds=False, look=300,
                                   cost=cost, night_pct=night, c_target=target, cci=cci if use else None)
                name = f"SK3 {frame} E{e:g} stop{'0' if stop == 1.0 else f'{stop:g}'} {target}{' CCI' if use else ''}"
                out.setdefault(name, {})[market] = rows
        log(f"  {market} done")
    return out


def run_sk4(period: str, log=print):
    """Round 4: the Tradebook's fixed 20-pip stop at each of the four entries, out at C (161.8 %) or at A."""
    out: dict[str, dict[str, list]] = {}
    for market in [*PIP, "XAUUSD"]:
        bars = market_hours(market)
        cost = 0.30 if market == "XAUUSD" else COST_PIPS[market] * PIP[market]
        night = 0.020 if market == "XAUUSD" else 0.010
        twenty = 2.0 if market == "XAUUSD" else 20 * PIP[market]
        first, last = TPR_DESIGN if period == "design" else (TPR_HOLDOUT_FROM, bars[-1].ts.date())
        start = next(k for k, b in enumerate(bars) if b.ts.date() >= first - dt.timedelta(days=120))
        bars = bars[start:]
        atr = ind.atr(bars, 14)
        for e, target in product((0.5, 0.559, 0.618, 0.667), ("C161", "A")):
            rows = []
            for sign in (1, -1):
                rows += trades(bars, atr, first, last, sign, m=0.0, e=e, stop_at=1.0, thirds=False, look=300,
                               cost=cost, night_pct=night, c_target=target, stop_fixed=twenty)
            out.setdefault(f"SK4 H1 E{e:g} 20pips {target}", {})[market] = rows
        log(f"  {market} done")
    return out
