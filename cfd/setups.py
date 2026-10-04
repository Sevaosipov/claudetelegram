"""cfd/setups.py: the three setups of spec 2026-10-05-cfd-signals.md §1.2 -- PB-D (the user's
Model B pullback on daily bars), BO-D (a 55-bar Donchian breakout on daily bars) and PB-H1-GOLD
(the gold config, a 1:1 port of Model B from eurusd_daytrader.pine on hourly gold). Pure: bars in,
signals out, nothing else.

Each function returns the signal of every bar that satisfies the rules, both directions, in bar
order. A `Signal` is read at the close of bar `index`; what to do with it -- the entry price, the
exit, one open trade per instrument -- belongs to exits.py and research.py. An instrument needs
`min_bars` (300, §1.1) bars before its first signal, so a signal at an earlier index is not
produced.

PB-D and PB-H1-GOLD share one trigger and differ in the trend filter (a daily EMA50 rising over
five bars vs the 4-hour EMA50 of the previous completed 4-hour bar) and in the session:
  dip:      the lowest low of the last 5 bars <= the highest high of the last 10 bars - 1.0 ATR
            (windows include the current bar, as Pine's ta.highest / ta.lowest do)
  trigger:  close > the previous bar's high and close >= open
  filter:   ADX(14) >= 20
  stop:     the lowest low of the last 5 bars - 0.5 ATR
and the short side is the mirror image.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from cfd import indicators as ind
from cfd.data import LONDON_SESSION, MIN_BARS_BEFORE_SIGNAL, Bar, Session

ATR_LEN = 14
ADX_LEN = 14
ADX_MIN = 20.0
EMA_LEN = 50                 # PB-D trend: close > EMA50 and EMA50 > EMA50 five bars earlier
EMA_LAG = 5
SWING_BARS = 10              # the swing high / low the dip is measured from
PULLBACK_BARS = 5            # the window of the pullback low / high
PULLBACK_ATR = 1.0           # the dip depth, in ATR
PULLBACK_STOP_ATR = 0.5      # the stop sits this far beyond the pullback extreme
DONCHIAN_BARS = 55           # BO-D: the previous 55 bars, the signal bar excluded
SMA_LEN = 200                # BO-D: the trend side
BREAKOUT_STOP_ATR = 2.5
HTF_HOURS = 4                # PB-H1-GOLD: the higher timeframe is 4 hours ...
HTF_EMA_LEN = 50             # ... and its EMA has this length


@dataclass(frozen=True)
class Signal:
    index: int      # the bar whose close makes the signal
    side: str       # "long" or "short"
    stop: float     # the stop level at the signal
    atr: float      # ATR(14) at the signal bar


# ---------------------------------------------------------------- the shared Model B trigger
def _pullback(bars: Sequence[Bar], up: Sequence[bool], down: Sequence[bool],
              allowed: Sequence[bool] | None, min_bars: int) -> list[Signal]:
    hi, lo = ind.highs(bars), ind.lows(bars)
    atr = ind.atr(bars, ATR_LEN)
    adx = ind.adx(bars, ADX_LEN)
    sw_hi, sw_lo = ind.highest(hi, SWING_BARS), ind.lowest(lo, SWING_BARS)
    pb_hi, pb_lo = ind.highest(hi, PULLBACK_BARS), ind.lowest(lo, PULLBACK_BARS)
    out: list[Signal] = []
    for i in range(max(min_bars, 1), len(bars)):
        a, x = atr[i], adx[i]
        swing_hi, swing_lo, pull_hi, pull_lo = sw_hi[i], sw_lo[i], pb_hi[i], pb_lo[i]
        if None in (a, x, swing_hi, swing_lo, pull_hi, pull_lo) or x < ADX_MIN \
                or (allowed is not None and not allowed[i]):
            continue
        b, prev = bars[i], bars[i - 1]
        if up[i] and pull_lo <= swing_hi - PULLBACK_ATR * a and b.close > prev.high \
                and b.close >= b.open:
            out.append(Signal(i, "long", pull_lo - PULLBACK_STOP_ATR * a, a))
        elif down[i] and pull_hi >= swing_lo + PULLBACK_ATR * a and b.close < prev.low \
                and b.close <= b.open:
            out.append(Signal(i, "short", pull_hi + PULLBACK_STOP_ATR * a, a))
    return out


# ---------------------------------------------------------------- PB-D
def pb_d(bars: Sequence[Bar], *, min_bars: int = MIN_BARS_BEFORE_SIGNAL) -> list[Signal]:
    """Trend pullback on daily bars: trend up is close > EMA50 and EMA50 > EMA50 five bars
    earlier (down is the mirror); then the shared dip / trigger / ADX rules. The signal is read at
    the close of bar t; the entry is the open of bar t+1."""
    closes = ind.closes(bars)
    ema = ind.ema(closes, EMA_LEN)
    up = [False] * len(bars)
    down = [False] * len(bars)
    for i in range(EMA_LAG, len(bars)):
        e, e_prev = ema[i], ema[i - EMA_LAG]
        if e is None or e_prev is None:
            continue
        up[i] = closes[i] > e and e > e_prev
        down[i] = closes[i] < e and e < e_prev
    return _pullback(bars, up, down, None, min_bars)


# ---------------------------------------------------------------- BO-D
def bo_d(bars: Sequence[Bar], *, min_bars: int = MIN_BARS_BEFORE_SIGNAL) -> list[Signal]:
    """Donchian breakout on daily bars: long when the close is above the highest high of the
    previous 55 bars (the signal bar excluded) and above the 200-day average; short is the mirror.
    The stop, fixed at the signal, is close - 2.5 ATR (long) or close + 2.5 ATR (short)."""
    closes = ind.closes(bars)
    atr = ind.atr(bars, ATR_LEN)
    hh = ind.highest(ind.highs(bars), DONCHIAN_BARS)
    ll = ind.lowest(ind.lows(bars), DONCHIAN_BARS)
    sma = ind.sma(closes, SMA_LEN)
    out: list[Signal] = []
    for i in range(max(min_bars, 1), len(bars)):
        a, avg, prev_hh, prev_ll = atr[i], sma[i], hh[i - 1], ll[i - 1]
        if a is None or avg is None or prev_hh is None or prev_ll is None:
            continue
        c = closes[i]
        if c > prev_hh and c > avg:
            out.append(Signal(i, "long", c - BREAKOUT_STOP_ATR * a, a))
        elif c < prev_ll and c < avg:
            out.append(Signal(i, "short", c + BREAKOUT_STOP_ATR * a, a))
    return out


# ---------------------------------------------------------------- PB-H1-GOLD
def htf_emas(bars: Sequence[Bar], length: int = HTF_EMA_LEN,
             hours: int = HTF_HOURS) -> tuple[list[float | None], list[float | None]]:
    """For every hourly bar, the EMA of the 4-hour closes as of the previous completed 4-hour bar,
    and as of the bar before that (Pine's htfEma and htfEmaOld).

    The 4-hour bars are the UTC-aligned blocks (00-04, 04-08, ...) that hold at least one hourly
    bar, in order; a block's close is the close of its last hourly bar. For a bar inside block k
    the values are EMA[k-1] and EMA[k-2]: nothing from block k itself, which is still forming."""
    seconds = hours * 3600
    block_closes: list[float] = []
    block_keys: list[int] = []
    ordinal: list[int] = []
    for b in bars:
        key = int(b.ts.timestamp()) // seconds
        if not block_keys or block_keys[-1] != key:
            block_keys.append(key)
            block_closes.append(b.close)
        else:
            block_closes[-1] = b.close
        ordinal.append(len(block_keys) - 1)
    ema = ind.ema(block_closes, length)
    htf = [ema[k - 1] if k >= 1 else None for k in ordinal]
    htf_old = [ema[k - 2] if k >= 2 else None for k in ordinal]
    return htf, htf_old


def pb_h1_gold(bars_1h: Sequence[Bar], *, session: Session = LONDON_SESSION,
               min_bars: int = MIN_BARS_BEFORE_SIGNAL) -> list[Signal]:
    """The gold config on hourly bars, Model B ported from eurusd_daytrader.pine: trend up is
    close > htfEma and htfEma > htfEmaOld (see htf_emas; down is the mirror), then the shared
    dip / trigger / ADX rules, only for bars whose start time is inside the session (07:00-16:00
    Europe/London). The entry is the signal bar's close."""
    htf, htf_old = htf_emas(bars_1h)
    up = [h is not None and o is not None and b.close > h and h > o
          for b, h, o in zip(bars_1h, htf, htf_old)]
    down = [h is not None and o is not None and b.close < h and h < o
            for b, h, o in zip(bars_1h, htf, htf_old)]
    allowed = [session.contains(b.ts) for b in bars_1h]
    return _pullback(bars_1h, up, down, allowed, min_bars)
