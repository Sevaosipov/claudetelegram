"""cfd/indicators.py against hand-computed values. The ATR and ADX follow Pine's ta.atr and
ta.dmi (Wilder's RMA seeded with a simple average), because the gold setup is a port of a Pine
script and a different smoothing would be a different strategy."""
from __future__ import annotations

import datetime as dt

import pytest

from cfd import indicators as ind
from cfd.data import Bar

DAY0 = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)


def mk(rows):
    """Bars from (high, low, close) rows; the open is the close (these tests don't use it)."""
    return [Bar(DAY0 + dt.timedelta(days=i), c, h, low, c) for i, (h, low, c) in enumerate(rows)]


def approx(x):
    return pytest.approx(x, rel=1e-9, abs=1e-9)


# ------------------------------------------------------------------ sma / ema
def test_sma_is_none_until_it_has_length_values():
    assert ind.sma([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]


def test_sma_of_a_single_window_and_of_too_short_input():
    assert ind.sma([4, 6], 2) == [None, 5.0]
    assert ind.sma([4], 2) == [None]
    assert ind.sma([], 3) == []


def test_ema_is_seeded_with_the_sma_of_the_first_length_values():
    # length 3: alpha = 2/(3+1) = 0.5; seed = (2+4+6)/3 = 4;
    # 0.5*8 + 0.5*4 = 6; 0.5*7 + 0.5*6 = 6.5
    assert ind.ema([2, 4, 6, 8, 7], 3) == [None, None, 4.0, 6.0, 6.5]


def test_ema_length_four():
    # alpha = 2/5 = 0.4; seed = (10+11+12+13)/4 = 11.5;
    # 0.4*14 + 0.6*11.5 = 12.5; 0.4*12 + 0.6*12.5 = 12.3
    out = ind.ema([10, 11, 12, 13, 14, 12], 4)
    assert out[:3] == [None, None, None]
    assert out[3] == approx(11.5) and out[4] == approx(12.5) and out[5] == approx(12.3)


def test_ema_of_a_constant_series_is_the_constant():
    out = ind.ema([7.0] * 60, 50)
    assert out[48] is None and out[49] == approx(7.0) and out[59] == approx(7.0)


# ------------------------------------------------------------------ highest / lowest
def test_highest_and_lowest_include_the_current_element():
    v = [3, 1, 4, 1, 5, 9, 2, 6]
    assert ind.highest(v, 3) == [None, None, 4, 4, 5, 9, 9, 9]
    assert ind.lowest(v, 3) == [None, None, 1, 1, 1, 1, 2, 2]


def test_highest_of_length_one_is_the_value_itself():
    assert ind.highest([3, 1, 2], 1) == [3, 1, 2]
    assert ind.lowest([3, 1, 2], 1) == [3, 1, 2]


# ------------------------------------------------------------------ true range / ATR
def test_true_range_first_bar_is_high_minus_low_then_the_gap_aware_range():
    bars = mk([(10, 8, 9),        # TR0 = 2
               (11, 9, 10),       # max(2, |11-9|, |9-9|) = 2
               (13, 10, 12),      # max(3, |13-10|, |10-10|) = 3
               (12, 9, 10),       # max(3, |12-12|, |9-12|) = 3
               (14, 11, 13),      # max(3, |14-10|, |11-10|) = 4
               (21, 20, 20.5)])   # a gap up from 13: max(1, |21-13|, |20-13|) = 8
    assert ind.true_range(bars) == [2, 2, 3, 3, 4, 8]


def test_atr_is_wilders_rma_seeded_with_the_sma_of_the_first_true_ranges():
    bars = mk([(10, 8, 9), (11, 9, 10), (13, 10, 12), (12, 9, 10), (14, 11, 13)])
    out = ind.atr(bars, 3)
    # seed at index 2: (2 + 2 + 3) / 3 = 7/3; then (prev*2 + TR)/3
    assert out[:2] == [None, None]
    assert out[2] == approx(7 / 3)
    assert out[3] == approx((7 / 3 * 2 + 3) / 3) == approx(23 / 9)
    assert out[4] == approx((23 / 9 * 2 + 4) / 3) == approx(82 / 27)


def test_atr_default_length_is_14_and_starts_at_the_14th_bar():
    bars = mk([(11, 9, 10)] * 20)           # TR = 2 on every bar (flat)
    out = ind.atr(bars)
    assert out[12] is None
    assert out[13] == approx(2.0) and out[19] == approx(2.0)


def test_atr_of_too_few_bars_is_all_none():
    assert ind.atr(mk([(11, 9, 10)] * 5), 14) == [None] * 5
    assert ind.atr([], 14) == []


# ------------------------------------------------------------------ ADX
def test_adx_matches_the_hand_computed_wilder_values_for_length_2():
    # bars (high, low, close); worked by hand following Pine's ta.dmi with length 2:
    #   TR   (i>=1): 3, 3, 3, 5, 2
    #   +DM  (i>=1): 2, 1, 0, 3, 0        -DM (i>=1): 0, 0, 1, 0, 0
    #   RMA seeded at i=2 (mean of i=1..2), then (prev*1 + x)/2:
    #     TR   3, 3, 4, 3          +DM 1.5, 0.75, 1.875, 0.9375      -DM 0, 0.5, 0.25, 0.125
    #   DX = |+DI - -DI| / (+DI + -DI) at i=2..5: 1.0, 0.2, 13/17, 13/17
    #   ADX = 100 * RMA(DX), seeded at i=3: (1.0 + 0.2)/2 = 0.6 -> 60,
    #     i=4: (0.6 + 13/17)/2, i=5: (that + 13/17)/2
    bars = mk([(10, 8, 9), (12, 9, 11), (13, 10, 12), (12, 9, 10), (15, 11, 14), (14, 12, 13)])
    out = ind.adx(bars, 2)
    assert out[:3] == [None, None, None]
    assert out[3] == approx(60.0)
    dx = 13 / 17
    assert out[4] == approx(100 * (0.6 + dx) / 2)
    assert out[5] == approx(100 * ((0.6 + dx) / 2 + dx) / 2)
    assert out[4] == approx(68.2352941176) and out[5] == approx(72.3529411765)


def test_adx_default_length_first_value_is_at_bar_28():
    bars = mk([(10 + i, 8 + i, 9 + i) for i in range(40)])      # a steady climb
    out = ind.adx(bars)
    assert out[26] is None and out[27] is not None


def test_adx_of_a_pure_trend_is_100():
    bars = mk([(10 + i, 8 + i, 9 + i) for i in range(40)])
    out = ind.adx(bars)
    assert out[27] == approx(100.0) and out[39] == approx(100.0)


def test_adx_of_flat_bars_is_zero_not_a_division_error():
    bars = mk([(10, 10, 10)] * 40)
    out = ind.adx(bars)
    assert out[27] == 0.0 and out[39] == 0.0


def test_adx_of_a_range_is_low():
    # up one, down one, ... : +DM and -DM alternate, DX stays small
    rows = [(10.5 + (i % 2), 9.5 + (i % 2), 10 + (i % 2)) for i in range(60)]
    out = ind.adx(mk(rows))
    assert out[59] < 10


# ------------------------------------------------------------------ properties
@pytest.mark.parametrize("name", ["atr", "adx", "ema", "sma", "highest", "lowest"])
def test_every_indicator_is_aligned_with_its_input_and_causal(name):
    import random
    rng = random.Random(7)
    price, rows = 100.0, []
    for _ in range(120):
        price *= 1 + rng.uniform(-0.01, 0.01)
        rows.append((price * 1.004, price * 0.996, price))
    bars = mk(rows)
    closes = [b.close for b in bars]

    def run(bs):
        cs = [b.close for b in bs]
        return {"atr": lambda: ind.atr(bs, 14), "adx": lambda: ind.adx(bs, 14),
                "ema": lambda: ind.ema(cs, 20), "sma": lambda: ind.sma(cs, 20),
                "highest": lambda: ind.highest(cs, 10), "lowest": lambda: ind.lowest(cs, 10)}[name]()

    full, cut = run(bars), run(bars[:80])
    assert len(full) == len(bars) and len(cut) == 80
    assert full[:80] == cut            # the value at t never depends on a later bar
    assert closes                      # (keeps the helper honest)


def test_a_non_positive_length_is_refused():
    for fn in (lambda: ind.sma([1, 2], 0), lambda: ind.ema([1, 2], 0), lambda: ind.highest([1], 0),
               lambda: ind.lowest([1], -1), lambda: ind.atr(mk([(2, 1, 1.5)]), 0),
               lambda: ind.adx(mk([(2, 1, 1.5)]), 0)):
        with pytest.raises(ValueError):
            fn()


def test_series_helpers_pull_the_columns():
    bars = [Bar(DAY0, 1.0, 3.0, 0.5, 2.0), Bar(DAY0 + dt.timedelta(days=1), 2.0, 4.0, 1.5, 3.0)]
    assert ind.highs(bars) == [3.0, 4.0]
    assert ind.lows(bars) == [0.5, 1.5]
    assert ind.closes(bars) == [2.0, 3.0]
