"""cfd/setups.py, round 2: IDX-DIP (`idx_dip`, H4) and CARRY-FX (`carry_states`, `carry_fx` and, after
amendment A1 of PREREGISTRATION_R2.md, `carry_holds`, H5), each on crafted series with hand-computed values
(worked in the comments). Where a single comparison needs its boundary tested the indicator is stubbed to a
number worked out by hand; the indicators themselves are tested in test_cfd_indicators.py and
test_cfd_r2_indicators.py.

H5 has two conditions that must not be confused: the ENTRY state (`carry_states`: the close beyond the 2.5 % band
of SMA200 with the rates agreeing) and the HOLD condition (`carry_holds`: the original's, close above / below
SMA200 itself with the rates agreeing, no band). The stop and the signal's ATR are ATR(20) (amendment A2)."""
from __future__ import annotations

import datetime as dt
import math
import random

import pandas as pd
import pytest

from cfd import indicators as ind
from cfd import setups
from cfd.data import Bar

UTC = dt.timezone.utc
DAY = dt.timedelta(days=1)
D0 = dt.datetime(2024, 1, 1, tzinfo=UTC)


def mk(rows, start=D0):
    """Bars from (open, high, low, close) rows."""
    return [Bar(start + i * DAY, o, h, low, c) for i, (o, h, low, c) in enumerate(rows)]


def mirror(rows, k):
    return [(k - o, k - low, k - h, k - c) for (o, h, low, c) in rows]


def indices(signals):
    return [s.index for s in signals]


def climb(n, start=100.0, step=0.5, wick=0.25):
    """n bars, each opening at the previous close and closing `step` higher, with wicks of `wick`
    on both sides: every true range is step + 2 * wick (1.0 by default), so ATR is exactly that."""
    rows, c = [], start
    for _ in range(n):
        o, c = c, c + step
        rows.append((o, c + wick, o - wick, c))
    return rows


def flat(n, price=100.0, half_range=0.5):
    return [(price, price + half_range, price - half_range, price)] * n


def test_the_round_two_parameters_are_the_preregistered_ones():
    assert setups.RSI_LEN == 2 and setups.RSI_MAX == 10.0
    assert setups.DIP_STOP_ATR == 2.5
    assert setups.CARRY_UPPER == 1.025 and setups.CARRY_LOWER == 0.975      # carry_strategy.BAND_PCT 2.5 %
    assert setups.CARRY_STOP_ATR == 6.0
    assert setups.CARRY_ATR_LEN == 20                                       # amendment A2: the original's ATR_LEN
    assert setups.SMA_LEN == 200 and setups.ATR_LEN == 14                  # H3 and H4 keep ATR(14)
    assert (setups.LONG, setups.SHORT, setups.FLAT) == ("long", "short", "flat")


# ================================================================== IDX-DIP (H4)
# 300 bars climbing 0.5 a bar (closes 100.5 ... 250, every true range 1.0), then two drops of 3:
#   bar 300: open 250, close 247, high 250.25, low 246.75 -> TR 3.5
#   bar 301: open 247, close 244 -> TR 3.5
# RSI(2): bar 299 = 100 (only gains); bar 300: avg gain (0.5 + 0) / 2 = 0.25, avg loss (0 + 3) / 2 = 1.5
#   -> 100 * 0.25 / 1.75 = 14.29 (not under 10); bar 301: avg gain 0.125, avg loss 2.25 -> 5.263 (under 10)
#   (with RSI(3) the same bars read 25.0 and 11.76: never under 10)
# ATR(14) at bar 301 = ((1 * 13 + 3.5) / 14 * 13 + 3.5) / 14 = 527/392 = 1.3443878
# SMA(200) at bar 301 = mean(closes of bars 102..301) = 201.1975, far under the close of 244
DIP_ROWS = climb(300) + [(250.0, 250.25, 246.75, 247.0), (247.0, 247.25, 243.75, 244.0)]
DIP_ATR = ((1.0 * 13 + 3.5) / 14 * 13 + 3.5) / 14
K = 500.0


def test_idx_dip_signals_a_second_sharp_drop_above_the_200_day_average():
    sigs = setups.idx_dip(mk(DIP_ROWS))
    assert indices(sigs) == [301]                    # bar 300 reads RSI(2) 14.29: not yet
    s = sigs[0]
    assert s.side == "long"
    assert DIP_ATR == pytest.approx(527 / 392) and s.atr == pytest.approx(1.3443877551)
    # stop = signal close - 2.5 ATR
    assert s.stop == pytest.approx(244.0 - 2.5 * DIP_ATR)
    assert s.stop == pytest.approx(240.6390306122)


def test_idx_dip_is_long_only():
    # the mirrored series reads RSI(2) ~ 0 on every bar of its long decline, yet it is below its
    # 200-day average all the way: no long, and there is no short side to fall back on
    mirrored = mk(mirror(DIP_ROWS, K))
    assert ind.rsi(ind.closes(mirrored), 2)[150] < 10          # the premise
    assert setups.idx_dip(mirrored, min_bars=0) == []


def test_idx_dip_is_silent_through_a_long_calm_climb():
    assert setups.idx_dip(mk(climb(400))) == []      # RSI(2) is 100 all the way up


@pytest.fixture
def calm(monkeypatch):
    """300+ flat bars (close 100, range 1.0): ATR 1.0; RSI stubbed (recording its length) and the
    SMA stubbed, so one comparison at a time is under test."""
    state = {"rsi": 5.0, "sma": 99.5, "rsi_n": None, "sma_n": None}

    def rsi(closes, n=14):
        state["rsi_n"] = n
        return [state["rsi"]] * len(closes)

    def sma(values, length):
        state["sma_n"] = length
        return [state["sma"]] * len(values)
    monkeypatch.setattr(setups.ind, "rsi", rsi)
    monkeypatch.setattr(setups.ind, "sma", sma)
    return state


def test_idx_dip_the_close_must_be_strictly_above_the_200_day_average(calm):
    bars = mk(flat(305))
    calm["sma"] = 99.99
    assert indices(setups.idx_dip(bars)) == [300, 301, 302, 303, 304]
    calm["sma"] = 100.0                              # equal: no
    assert setups.idx_dip(bars) == []
    calm["sma"] = 100.01
    assert setups.idx_dip(bars) == []
    calm["sma"] = None                               # no average yet
    assert setups.idx_dip(bars) == []


def test_idx_dip_rsi_must_be_strictly_under_ten(calm):
    bars = mk(flat(305))
    calm["rsi"] = 9.999
    assert indices(setups.idx_dip(bars)) == [300, 301, 302, 303, 304]
    calm["rsi"] = 10.0                               # equal: no
    assert setups.idx_dip(bars) == []
    calm["rsi"] = 10.001
    assert setups.idx_dip(bars) == []
    calm["rsi"] = None
    assert setups.idx_dip(bars) == []


def test_idx_dip_reads_rsi_of_length_two_and_the_200_day_average(calm):
    setups.idx_dip(mk(flat(305)))
    assert calm["rsi_n"] == 2 and calm["sma_n"] == 200


def test_idx_dip_the_average_is_exactly_200_bars_long(monkeypatch):
    monkeypatch.setattr(setups.ind, "rsi", lambda closes, n=14: [5.0] * len(closes))

    # bar 320 closes at 101, every other bar at 100; an outlier close of 797 sits `back` bars
    # before it. Inside the 200-bar window the average is (198 * 100 + 797 + 101) / 200 = 103.49,
    # above 101: no signal. One bar further back it is out and the average is
    # (199 * 100 + 101) / 200 = 100.005: signal
    def rows(back):
        r = list(flat(321))
        r[320] = (100.0, 101.5, 99.5, 101.0)
        r[320 - back] = (797.0, 797.5, 796.5, 797.0)
        return r
    assert 320 not in indices(setups.idx_dip(mk(rows(199))))        # bar 121: the oldest in the window
    assert 320 in indices(setups.idx_dip(mk(rows(200))))            # bar 120: just out of it


def test_idx_dip_default_needs_300_bars_before_the_first_signal(calm):
    bars = mk(flat(320))
    assert min(indices(setups.idx_dip(bars))) == 300
    assert indices(setups.idx_dip(bars, min_bars=315)) == [315, 316, 317, 318, 319]
    assert setups.idx_dip(mk(flat(300))) == []


def test_idx_dip_with_too_little_history_is_empty():
    assert setups.idx_dip(mk(climb(100)), min_bars=0) == []
    assert setups.idx_dip([], min_bars=0) == []


def _walk(n, seed, drift=0.0004, vol=0.011):
    rng = random.Random(seed)
    price, rows = 100.0, []
    for _ in range(n):
        o = price
        price *= 1 + rng.gauss(drift, vol)
        rows.append((o, max(o, price) * (1 + abs(rng.gauss(0, 0.003))),
                     min(o, price) * (1 - abs(rng.gauss(0, 0.003))), price))
    return rows


def test_idx_dip_signals_are_ordered_one_per_bar_and_never_see_the_future():
    bars = mk(_walk(1500, 17))
    full = setups.idx_dip(bars, min_bars=210)
    assert len(full) > 5, "the random series should produce some dips"
    assert indices(full) == sorted(set(indices(full)))
    assert all(s.side == "long" and s.stop < bars[s.index].close for s in full)
    for k in (500, 900, 1200):
        assert setups.idx_dip(bars[:k], min_bars=210) == [s for s in full if s.index < k]


# ================================================================== CARRY-FX (H5): the state
@pytest.fixture
def band(monkeypatch):
    """SMA(200) stubbed to 100 on every bar (recording the length) and the ATR to 2 (recording its length),
    so the 2.5 % band is at 102.5 / 97.5 and a stop is the close -/+ 12."""
    seen = {}

    def sma(values, length):
        seen["sma"] = length
        return [100.0] * len(values)

    def atr(bars, length=14):
        seen["atr"] = length
        return [2.0] * len(bars)
    monkeypatch.setattr(setups.ind, "sma", sma)
    monkeypatch.setattr(setups.ind, "atr", atr)
    return seen


def bars_closing_at(*closes):
    return mk([(c, c + 0.5, c - 0.5, c) for c in closes])


def states(closes, base, quote):
    return setups.carry_states(bars_closing_at(*closes), base, quote)


def test_the_state_is_long_above_the_upper_band_with_the_base_rate_higher(band):
    # close 102.6 > 100 * 1.025 and rate(base) 3 > rate(quote) 2
    assert states([102.6], [3.0], [2.0]) == ["long"]
    assert band["sma"] == 200


def test_the_long_band_is_two_and_a_half_percent_above_the_average(band):
    base, quote = [3.0] * 3, [2.0] * 3
    assert states([102.4999, 102.5001, 100.0], base, quote) == ["flat", "long", "flat"]
    assert states([101.0, 102.0, 102.4], base, quote) == ["flat"] * 3        # above the average, inside the band


def test_the_state_is_short_below_the_lower_band_with_the_base_rate_lower(band):
    assert states([97.4], [2.0], [3.0]) == ["short"]


def test_the_short_band_is_two_and_a_half_percent_below_the_average(band):
    base, quote = [2.0] * 3, [3.0] * 3
    assert states([97.5001, 97.4999, 100.0], base, quote) == ["flat", "short", "flat"]
    assert states([99.0, 98.0, 97.6], base, quote) == ["flat"] * 3


def test_the_rate_sign_must_agree_with_the_side(band):
    # a close above the upper band with the base rate LOWER (or equal): flat
    assert states([103.0, 103.0], [2.0, 2.5], [3.0, 2.5]) == ["flat", "flat"]
    # a close below the lower band with the base rate HIGHER (or equal): flat
    assert states([97.0, 97.0], [3.0, 2.5], [2.0, 2.5]) == ["flat", "flat"]


def test_negative_rates_compare_like_any_other(band):
    # the euro and the franc had negative rates for years: -0.1 is above -0.5
    assert states([103.0, 97.0], [-0.1, -0.5], [-0.5, -0.1]) == ["long", "short"]


def test_a_missing_rate_or_average_means_no_state(band):
    assert states([103.0, 103.0, 97.0, 97.0], [None, 3.0, None, 2.0], [2.0, None, 3.0, None]) == ["flat"] * 4


def test_no_average_yet_means_no_state(monkeypatch):
    bars = bars_closing_at(103.0, 103.0)
    monkeypatch.setattr(setups.ind, "sma", lambda v, length: [None, 100.0])
    assert setups.carry_states(bars, [3.0, 3.0], [2.0, 2.0]) == ["flat", "long"]


def test_the_state_series_is_aligned_with_the_bars_and_checks_its_inputs(band):
    bars = bars_closing_at(103.0, 100.0, 97.0)
    out = setups.carry_states(bars, [3.0, 3.0, 2.0], [2.0, 2.0, 3.0])
    assert out == ["long", "flat", "short"] and len(out) == len(bars)
    with pytest.raises(ValueError):
        setups.carry_states(bars, [3.0, 3.0], [2.0, 2.0, 2.0])
    with pytest.raises(ValueError):
        setups.carry_states(bars, [3.0] * 3, [2.0] * 2)
    assert setups.carry_states([], [], []) == []


# ================================================================== CARRY-FX (H5): the hold condition (A1)
# The original's (carry_strategy.py hold_l / hold_s): a long is held while close > SMA200 and rate(base) >
# rate(quote); a short while close < SMA200 and rate(base) < rate(quote). The 2.5 % band applies to entries only.
def holds(closes, base, quote):
    return setups.carry_holds(bars_closing_at(*closes), base, quote)


def test_a_long_is_held_while_the_close_is_above_the_average_and_the_base_rate_is_higher(band):
    assert holds([100.01], [3.0], [2.0]) == ["long"]
    assert band["sma"] == 200


def test_a_short_is_held_while_the_close_is_below_the_average_and_the_base_rate_is_lower(band):
    assert holds([99.99], [2.0], [3.0]) == ["short"]


def test_the_hold_has_no_band_unlike_the_entry_state(band):
    # inside the 2.5 % band (100 .. 102.5) the entry state is flat, but a long is held
    up = [100.01, 101.0, 102.4, 102.6]
    assert states(up, [3.0] * 4, [2.0] * 4) == ["flat", "flat", "flat", "long"]
    assert holds(up, [3.0] * 4, [2.0] * 4) == ["long"] * 4
    # and below the average for a short
    down = [99.99, 99.0, 97.6, 97.4]
    assert states(down, [2.0] * 4, [3.0] * 4) == ["flat", "flat", "flat", "short"]
    assert holds(down, [2.0] * 4, [3.0] * 4) == ["short"] * 4


def test_the_hold_is_strictly_above_or_below_the_average(band):
    assert holds([100.0, 100.0], [3.0, 2.0], [2.0, 3.0]) == ["flat", "flat"]            # equal to the average
    assert holds([100.0001, 99.9999], [3.0, 2.0], [2.0, 3.0]) == ["long", "short"]


def test_the_hold_drops_when_the_close_crosses_back_through_the_average(band):
    assert holds([103.0, 100.5, 100.0, 99.9], [3.0] * 4, [2.0] * 4) == ["long", "long", "flat", "flat"]
    assert holds([97.0, 99.5, 100.0, 100.1], [2.0] * 4, [3.0] * 4) == ["short", "short", "flat", "flat"]


def test_the_hold_drops_when_the_rates_stop_agreeing_with_the_side(band):
    # the close stays above the average; the base rate falls to the quote rate, then below it
    assert holds([103.0] * 4, [3.0, 2.0, 1.0, 3.0], [2.0] * 4) == ["long", "flat", "flat", "long"]
    assert holds([97.0] * 4, [1.0, 2.0, 3.0, 1.0], [2.0] * 4) == ["short", "flat", "flat", "short"]


def test_the_hold_needs_the_carry_on_the_trades_side(band):
    assert holds([103.0, 103.0], [2.0, 2.5], [3.0, 2.5]) == ["flat", "flat"]            # above, rates against / equal
    assert holds([97.0, 97.0], [3.0, 2.5], [2.0, 2.5]) == ["flat", "flat"]
    assert holds([101.0, 99.0], [-0.1, -0.5], [-0.5, -0.1]) == ["long", "short"]        # negative rates compare alike


def test_the_hold_is_off_while_a_rate_or_the_average_is_missing(band, monkeypatch):
    assert holds([103.0, 103.0, 97.0, 97.0], [None, 3.0, None, 2.0], [2.0, None, 3.0, None]) == ["flat"] * 4
    monkeypatch.setattr(setups.ind, "sma", lambda v, length: [None, 100.0])
    assert setups.carry_holds(bars_closing_at(103.0, 103.0), [3.0, 3.0], [2.0, 2.0]) == ["flat", "long"]


def test_the_hold_series_is_aligned_with_the_bars_and_checks_its_inputs(band):
    bars = bars_closing_at(103.0, 100.0, 97.0)
    out = setups.carry_holds(bars, [3.0, 3.0, 2.0], [2.0, 2.0, 3.0])
    assert out == ["long", "flat", "short"] and len(out) == len(bars)
    with pytest.raises(ValueError):
        setups.carry_holds(bars, [3.0, 3.0], [2.0, 2.0, 2.0])
    with pytest.raises(ValueError):
        setups.carry_holds(bars, [3.0] * 3, [2.0] * 2)
    assert setups.carry_holds([], [], []) == []


def test_every_entry_state_is_also_a_hold_and_the_hold_is_the_looser_of_the_two():
    bars = mk(_walk(1500, 31, drift=0.0003, vol=0.007))
    base = [None if i < 40 else 2.0 + 1.5 * math.sin(i / 300.0) for i in range(1500)]
    quote = [None if i < 40 else 2.0 + 1.5 * math.sin(i / 450.0 + 1.0) for i in range(1500)]
    entry = setups.carry_states(bars, base, quote)
    held = setups.carry_holds(bars, base, quote)
    assert all(h == e for e, h in zip(entry, held) if e != "flat")          # inside the band only the hold is on
    assert sum(1 for e, h in zip(entry, held) if e == "flat" and h != "flat") > 100
    assert sum(1 for e in entry if e != "flat") > 100


def test_entry_state_and_hold_match_carry_strategys_own_conditions_in_pandas():
    # carry_strategy.compute_state: enter_l = (close > ma * (1 + band)) & carry_l and hold_l = (close > ma) &
    # carry_l, with carry_l = diff > 0, band 2.5 %, ma the 200-day mean; the short side is the mirror
    bars = mk(_walk(1500, 31, drift=0.0003, vol=0.007))
    base = [None if i < 40 else 2.0 + 1.5 * math.sin(i / 300.0) for i in range(1500)]
    quote = [None if i < 40 else 2.0 + 1.5 * math.sin(i / 450.0 + 1.0) for i in range(1500)]
    close = pd.Series([b.close for b in bars])
    ma = close.rolling(200).mean()
    diff = pd.Series([math.nan if x is None else x for x in base]) - pd.Series(
        [math.nan if x is None else x for x in quote])
    band = 2.5 / 100.0
    enter_l, enter_s = (close > ma * (1 + band)) & (diff > 0), (close < ma * (1 - band)) & (diff < 0)
    hold_l, hold_s = (close > ma) & (diff > 0), (close < ma) & (diff < 0)
    entry = setups.carry_states(bars, base, quote)
    held = setups.carry_holds(bars, base, quote)
    for i in range(len(bars)):
        assert entry[i] == ("long" if enter_l[i] else "short" if enter_s[i] else "flat"), i
        assert held[i] == ("long" if hold_l[i] else "short" if hold_s[i] else "flat"), i


# ================================================================== CARRY-FX (H5): the entries
CLOSES = [100, 103, 104, 100, 103, 97, 96, 100]
BASE = [2.0, 3.0, 3.0, 3.0, 3.0, 1.0, 1.0, 1.0]
QUOTE = [2.0] * 8
# states: flat, long, long, flat, long, short, short, flat


def test_entries_are_the_changes_from_flat_or_from_the_opposite_side_into_long_or_short(band):
    bars = bars_closing_at(*CLOSES)
    assert setups.carry_states(bars, BASE, QUOTE) == [
        "flat", "long", "long", "flat", "long", "short", "short", "flat"]
    sigs = setups.carry_fx(bars, BASE, QUOTE, min_bars=0)
    # bar 1: flat -> long;  bar 2: still long (no new signal);  bar 3: long -> flat (an exit, no signal)
    # bar 4: flat -> long;  bar 5: long -> short (the opposite side);  bar 6: still short;  bar 7: flat
    assert [(s.index, s.side) for s in sigs] == [(1, "long"), (4, "long"), (5, "short")]
    assert band["atr"] == 20                                      # amendment A2: ATR(20) for H5


def test_the_stop_is_six_atr_from_the_signal_close(band):
    sigs = setups.carry_fx(bars_closing_at(*CLOSES), BASE, QUOTE, min_bars=0)
    assert [s.atr for s in sigs] == [2.0, 2.0, 2.0]
    assert sigs[0].stop == pytest.approx(103 - 6.0 * 2.0)            # long: close - 6 ATR
    assert sigs[1].stop == pytest.approx(103 - 12.0)
    assert sigs[2].stop == pytest.approx(97 + 6.0 * 2.0)            # short: close + 6 ATR


def test_a_reversal_from_long_straight_into_short_is_a_signal_of_the_new_side(band):
    # states: flat, long, long, short -- no flat bar between the long and the short
    closes = [100, 103, 103, 97]
    sigs = setups.carry_fx(bars_closing_at(*closes), [2.0, 3.0, 3.0, 1.0], [2.0] * 4, min_bars=0)
    assert [(s.index, s.side) for s in sigs] == [(1, "long"), (3, "short")]


def test_min_bars_drops_early_signals_but_the_state_before_them_still_counts(band):
    bars = bars_closing_at(*CLOSES)
    assert [s.index for s in setups.carry_fx(bars, BASE, QUOTE, min_bars=2)] == [4, 5]
    assert [s.index for s in setups.carry_fx(bars, BASE, QUOTE, min_bars=5)] == [5]
    # starting inside a trend: the long that began at bar 1 is not a "change" at bar 2
    assert 2 not in indices(setups.carry_fx(bars, BASE, QUOTE, min_bars=2))


def test_carry_fx_needs_300_bars_before_the_first_signal_by_default(band):
    assert setups.carry_fx(bars_closing_at(*CLOSES), BASE, QUOTE) == []
    closes = [100.0] * 305 + [103.0]
    sigs = setups.carry_fx(bars_closing_at(*closes), [3.0] * 306, [2.0] * 306)
    assert indices(sigs) == [305]


def test_a_second_entry_needs_the_state_to_have_left_the_side_first(band):
    closes = [100, 103, 103, 103, 100, 103]
    sigs = setups.carry_fx(bars_closing_at(*closes), [3.0] * 6, [2.0] * 6, min_bars=0)
    assert indices(sigs) == [1, 5]


def test_carry_fx_with_missing_rates_has_no_signals(band):
    assert setups.carry_fx(bars_closing_at(*CLOSES), [None] * 8, QUOTE, min_bars=0) == []


def test_carry_fx_checks_its_inputs(band):
    with pytest.raises(ValueError):
        setups.carry_fx(bars_closing_at(*CLOSES), BASE[:-1], QUOTE, min_bars=0)


# ------------------------------------------------------------------ end to end, real indicators
# 320 flat bars (close 100, range 1.0: TR 1.0), then bar 320: open 100, high 103.5, low 99.8, close 103
#   TR = max(3.7, 3.5, 0.2) = 3.7; ATR(20) = (1 * 19 + 3.7) / 20 = 227/200 = 1.135 (an ATR(14) would read 1.1929)
#   SMA(200) = (199 * 100 + 103) / 200 = 100.015, so the bands are 102.5154 and 97.5146:
#   the close of 103 is above the upper band; with rate(base) 3 > rate(quote) 2 the state is long
JUMP = (100.0, 103.5, 99.8, 103.0)
CARRY_ATR = (1.0 * 19 + 3.7) / 20


def test_carry_fx_long_on_real_indicators():
    bars = mk(flat(320) + [JUMP])
    sigs = setups.carry_fx(bars, [3.0] * 321, [2.0] * 321)
    assert len(sigs) == 1
    s = sigs[0]
    assert (s.index, s.side) == (320, "long")
    assert CARRY_ATR == pytest.approx(227 / 200)
    assert s.atr == pytest.approx(CARRY_ATR)
    assert s.stop == pytest.approx(103.0 - 6.0 * CARRY_ATR)
    assert s.stop == pytest.approx(96.19)


def test_carry_fx_short_on_real_indicators_is_the_mirror_image():
    bars = mk(mirror(flat(320) + [JUMP], 200.0))
    sigs = setups.carry_fx(bars, [2.0] * 321, [3.0] * 321)
    assert len(sigs) == 1
    s = sigs[0]
    assert (s.index, s.side) == (320, "short")
    assert s.atr == pytest.approx(CARRY_ATR)
    assert s.stop == pytest.approx(97.0 + 6.0 * CARRY_ATR)
    assert s.stop == pytest.approx(103.81)
    # and with the rates the other way round the mirror image is flat
    assert setups.carry_fx(bars, [3.0] * 321, [2.0] * 321) == []


def test_carry_fx_the_average_is_exactly_200_bars_long():
    # an outlier close of 797 inside the 200-bar window lifts the average to ~103.5, so the close of
    # 103 is no longer above the upper band; one bar further back it is out and the signal stands
    def rows(back):
        r = list(flat(321))
        r[320] = JUMP
        r[320 - back] = (797.0, 797.5, 796.5, 797.0)
        return r
    rates = ([3.0] * 321, [2.0] * 321)
    assert 320 not in indices(setups.carry_fx(mk(rows(199)), *rates))        # bar 121: in the window
    assert 320 in indices(setups.carry_fx(mk(rows(200)), *rates))            # bar 120: out of it


def test_carry_fx_the_stop_and_the_signals_atr_are_atr_20_not_atr_14():
    # amendment A2: the original's ATR_LEN. H3 and H4 keep ATR(14); the two lengths differ here
    bars = mk(flat(320) + [JUMP])
    (s,) = setups.carry_fx(bars, [3.0] * 321, [2.0] * 321)
    assert s.atr == pytest.approx(ind.atr(bars, 20)[-1])
    assert s.atr != pytest.approx(ind.atr(bars, 14)[-1])
    assert s.stop == pytest.approx(103.0 - 6.0 * ind.atr(bars, 20)[-1])
    assert s.stop != pytest.approx(103.0 - 6.0 * ind.atr(bars, 14)[-1])
    # and idx_dip still reads ATR(14)
    rows = climb(300) + [(250.0, 250.25, 246.75, 247.0), (247.0, 247.25, 243.75, 244.0)]
    (d,) = setups.idx_dip(mk(rows))
    assert d.atr == pytest.approx(ind.atr(mk(rows), 14)[-1])


def test_carry_fx_signals_never_see_the_future_and_the_states_are_causal():
    bars = mk(_walk(1200, 9, drift=0.0003, vol=0.008))
    # monthly-looking rates that flip sign now and then
    base = [3.0 if (i // 150) % 2 == 0 else 1.0 for i in range(1200)]
    quote = [2.0] * 1200
    full = setups.carry_fx(bars, base, quote, min_bars=210)
    full_states = setups.carry_states(bars, base, quote)
    assert full, "the random series should produce some entries"
    assert indices(full) == sorted(set(indices(full)))
    for k in (400, 700, 1000):
        assert setups.carry_fx(bars[:k], base[:k], quote[:k], min_bars=210) == [
            s for s in full if s.index < k]
        assert setups.carry_states(bars[:k], base[:k], quote[:k]) == full_states[:k]
    # every signal is a change into its side, in the state series
    for s in full:
        assert full_states[s.index] == s.side and full_states[s.index - 1] != s.side
