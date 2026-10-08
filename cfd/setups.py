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

Round 2 (docs/cfd/PREREGISTRATION_R2.md) adds two more, with the same shape (bars in, signals out):
  IDX-DIP (H4)   `idx_dip`: long only, close > SMA200 and RSI(2) < 10 at the close of bar t; the stop,
                 fixed at the signal, is the close - 2.5 ATR.
  CARRY-FX (H5)  `carry_states`, `carry_holds` and `carry_fx`: the carry-gated trend of carry_strategy.py (its frozen
                 2.5 % band and 200-day average) with the rates of the two currencies given per bar.
                 The state at each close is LONG / SHORT / FLAT; a signal is a change from FLAT (or
                 from the opposite side) into LONG or SHORT, with the stop at the close -/+ 6 ATR.
                 The exits of H5 leave when the state stops being the trade's side, so the state
                 series is public.
BO-D is reused as it is for H3 (CR-BO) on other instruments.

Round 3 (docs/cfd/PREREGISTRATION_R3.md) adds one more, `fx_rev` (H6, FX-REV): range reversion in pairs of
linked economies. At the close of bar t, when ADX(14) < 20 (no trend), long when the close is under
SMA20 - 2.0 x SD20 and short when it is over SMA20 + 2.0 x SD20 (SD20 the population standard deviation of
the last 20 closes); the stop, fixed at the signal, is the close -/+ 2.0 ATR(14).
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
RSI_LEN = 2                  # IDX-DIP: RSI(2) ...
RSI_MAX = 10.0               # ... under this
DIP_STOP_ATR = 2.5           # IDX-DIP: stop = signal close - 2.5 ATR
CARRY_UPPER = 1.025          # CARRY-FX: long above 1.025 * SMA200 (carry_strategy.BAND_PCT = 2.5)
CARRY_LOWER = 0.975          # ... short below 0.975 * SMA200
CARRY_STOP_ATR = 6.0         # CARRY-FX: stop = signal close -/+ 6 ATR (carry_strategy.TRAIL_ATR)
CARRY_ATR_LEN = 20           # CARRY-FX: the original's ATR_LEN (amendment A2); H3 and H4 keep ATR(14)
LONG, SHORT, FLAT = "long", "short", "flat"      # the states of CARRY-FX
REV_SMA_LEN = 20             # FX-REV: the average the close is measured from ...
REV_SD_LEN = 20              # ... and the window of the standard deviation
REV_BAND_SD = 2.0            # FX-REV: signal beyond SMA20 -/+ this many SD20
REV_STOP_ATR = 2.0           # FX-REV: stop = signal close -/+ 2.0 ATR(14)
REV_ADX_MAX = 20.0           # FX-REV: only while ADX(14) is under this (no trend)


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


# ---------------------------------------------------------------- IDX-DIP (round 2, H4)
def idx_dip(bars: Sequence[Bar], *, min_bars: int = MIN_BARS_BEFORE_SIGNAL) -> list[Signal]:
    """Buying a sharp dip in a rising equity index, long only: at the close of bar t the close is
    above the 200-day average and Wilder's RSI(2) of the closes is under 10 (both strict). The stop,
    fixed at the signal, is the close - 2.5 ATR (R is measured from the actual entry, the next open)."""
    closes = ind.closes(bars)
    atr = ind.atr(bars, ATR_LEN)
    sma = ind.sma(closes, SMA_LEN)
    rsi = ind.rsi(closes, RSI_LEN)
    out: list[Signal] = []
    for i in range(max(min_bars, 1), len(bars)):
        a, avg, r = atr[i], sma[i], rsi[i]
        if a is None or avg is None or r is None:
            continue
        c = closes[i]
        if c > avg and r < RSI_MAX:
            out.append(Signal(i, "long", c - DIP_STOP_ATR * a, a))
    return out


# ---------------------------------------------------------------- CARRY-FX (round 2, H5)
def carry_states(bars: Sequence[Bar], base_rates: Sequence[float | None],
                 quote_rates: Sequence[float | None]) -> list[str]:
    """The state at the close of every bar: "long" when the close is above 1.025 * SMA200 and the
    base currency's rate is above the quote currency's; "short" when it is below 0.975 * SMA200 and
    the base rate is below the quote rate; otherwise "flat" (also while the average or either rate is
    missing, and when the rates are equal). `base_rates` and `quote_rates` hold, for each bar, the
    rate in force on its date (rates.Rates.rate_on), so they have the length of `bars`."""
    if not len(bars) == len(base_rates) == len(quote_rates):
        raise ValueError("bars, base_rates and quote_rates must have the same length")
    closes = ind.closes(bars)
    sma = ind.sma(closes, SMA_LEN)
    out: list[str] = []
    for c, avg, base, quote in zip(closes, sma, base_rates, quote_rates):
        if avg is None or base is None or quote is None:
            out.append(FLAT)
        elif c > avg * CARRY_UPPER and base > quote:
            out.append(LONG)
        elif c < avg * CARRY_LOWER and base < quote:
            out.append(SHORT)
        else:
            out.append(FLAT)
    return out


def carry_holds(bars: Sequence[Bar], base_rates: Sequence[float | None],
                quote_rates: Sequence[float | None]) -> list[str]:
    """The HOLD condition at the close of every bar (PREREGISTRATION_R2.md, amendment A1) -- the
    original's, carry_strategy.py's hold_l / hold_s: "long" while the close is above SMA200 and the
    base currency's rate is above the quote currency's; "short" while it is below SMA200 and the base
    rate is below the quote rate; otherwise "flat". No band: the 2.5 % of `carry_states` applies to
    entries only, so a trade is not thrown out by a fall back inside it. Inputs as in `carry_states`."""
    if not len(bars) == len(base_rates) == len(quote_rates):
        raise ValueError("bars, base_rates and quote_rates must have the same length")
    closes = ind.closes(bars)
    sma = ind.sma(closes, SMA_LEN)
    out: list[str] = []
    for c, avg, base, quote in zip(closes, sma, base_rates, quote_rates):
        if avg is None or base is None or quote is None:
            out.append(FLAT)
        elif c > avg and base > quote:
            out.append(LONG)
        elif c < avg and base < quote:
            out.append(SHORT)
        else:
            out.append(FLAT)
    return out


def carry_fx(bars: Sequence[Bar], base_rates: Sequence[float | None],
             quote_rates: Sequence[float | None], *,
             min_bars: int = MIN_BARS_BEFORE_SIGNAL) -> list[Signal]:
    """The entries of the carry-gated trend: a signal at the close of bar t when the state
    (`carry_states`) changes from flat, or from the opposite side, into long or short. The stop,
    fixed at the signal, is the close -/+ 6 ATR(20) (the original's ATR_LEN, amendment A2); R is
    measured from the actual entry, the next open. What a trade is held on is `carry_holds` of the
    same arguments, not this entry state."""
    states = carry_states(bars, base_rates, quote_rates)
    closes = ind.closes(bars)
    atr = ind.atr(bars, CARRY_ATR_LEN)
    out: list[Signal] = []
    for i in range(max(min_bars, 1), len(bars)):
        state, a = states[i], atr[i]
        if state == FLAT or state == states[i - 1] or a is None:
            continue
        stop = closes[i] - CARRY_STOP_ATR * a if state == LONG else closes[i] + CARRY_STOP_ATR * a
        out.append(Signal(i, state, stop, a))
    return out


# ---------------------------------------------------------------- FX-REV (round 3, H6)
def fx_rev(bars: Sequence[Bar], *, min_bars: int = MIN_BARS_BEFORE_SIGNAL) -> list[Signal]:
    """Range reversion, both directions: at the close of bar t, when ADX(14) is under 20, a long if the
    close is under SMA20 - 2.0 x SD20 and a short if it is over SMA20 + 2.0 x SD20 (all strict; SD20 is
    the population standard deviation of the last 20 closes). The stop, fixed at the signal, is the close
    -/+ 2.0 ATR(14); R is measured from the actual entry, the next open. What the trade is held on
    (SMA20, 16 bars) belongs to the backtest."""
    closes = ind.closes(bars)
    atr = ind.atr(bars, ATR_LEN)
    adx = ind.adx(bars, ADX_LEN)
    avg = ind.sma(closes, REV_SMA_LEN)
    sd = ind.stdev(closes, REV_SD_LEN)
    out: list[Signal] = []
    for i in range(max(min_bars, 1), len(bars)):
        a, x, m, s = atr[i], adx[i], avg[i], sd[i]
        if a is None or x is None or m is None or s is None or x >= REV_ADX_MAX:
            continue
        c = closes[i]
        if c < m - REV_BAND_SD * s:
            out.append(Signal(i, "long", c - REV_STOP_ATR * a, a))
        elif c > m + REV_BAND_SD * s:
            out.append(Signal(i, "short", c + REV_STOP_ATR * a, a))
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
