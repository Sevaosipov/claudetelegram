"""cfd/indicators.py, round 2: Wilder's RSI, which H4 (IDX-DIP) reads with a length of 2. Hand-computed
values (worked step by step in the comments, exact fractions checked in a scratch script) and an
independent cross-check against a pandas reimplementation."""
from __future__ import annotations

import random

import pandas as pd
import pytest

from cfd import indicators as ind


def approx(x):
    return pytest.approx(x, rel=1e-9, abs=1e-9)


# ------------------------------------------------------------------ hand-computed values
def test_rsi_length_two_is_wilders_average_seeded_with_the_first_two_changes():
    # closes 10, 11, 10.5, 10.8, 10.2, 10.6; changes (from bar 1): +1, -0.5, +0.3, -0.6, +0.4
    #   gains  1, 0, 0.3, 0, 0.4        losses  0, 0.5, 0, 0.6, 0
    # seed at bar 2 (mean of the first two changes): avg gain 0.5, avg loss 0.25  -> RS 2
    #   RSI = 100 - 100 / (1 + 2) = 200/3
    # then avg = (previous * 1 + x) / 2:
    #   bar 3: gain (0.5 + 0.3) / 2 = 0.4,   loss (0.25 + 0) / 2 = 0.125    -> RSI 1600/21
    #   bar 4: gain (0.4 + 0) / 2 = 0.2,     loss (0.125 + 0.6) / 2 = 0.3625 -> RSI 320/9
    #   bar 5: gain (0.2 + 0.4) / 2 = 0.3,   loss (0.3625 + 0) / 2 = 0.18125 -> RSI 4800/77
    out = ind.rsi([10, 11, 10.5, 10.8, 10.2, 10.6], 2)
    assert out[:2] == [None, None]
    assert out[2] == approx(200 / 3) and out[2] == approx(66.6666666667)
    assert out[3] == approx(1600 / 21) and out[3] == approx(76.1904761905)
    assert out[4] == approx(320 / 9) and out[4] == approx(35.5555555556)
    assert out[5] == approx(4800 / 77) and out[5] == approx(62.3376623377)


def test_rsi_length_three_hand_computed():
    # closes 44, 44.5, 43.5, 44.5, 44, 43, 43.5, 44.5, 45, 44
    # changes (from bar 1): +0.5, -1, +1, -0.5, -1, +0.5, +1, +0.5, -1
    # seed at bar 3 (mean of three changes): gain (0.5 + 0 + 1) / 3 = 0.5, loss (0 + 1 + 0) / 3 = 1/3
    #   RSI = 100 * 0.5 / (0.5 + 1/3) = 60
    # bar 4: gain (0.5 * 2 + 0) / 3 = 1/3,  loss (1/3 * 2 + 0.5) / 3 = 7/18   -> RSI 600/13
    # bar 5: gain (1/3 * 2 + 0) / 3 = 2/9,  loss (7/18 * 2 + 1) / 3 = 16/27   -> RSI 300/11
    out = ind.rsi([44, 44.5, 43.5, 44.5, 44, 43, 43.5, 44.5, 45, 44], 3)
    assert out[:3] == [None, None, None]
    assert out[3] == approx(60.0)
    assert out[4] == approx(600 / 13) and out[4] == approx(46.1538461538)
    assert out[5] == approx(300 / 11) and out[5] == approx(27.2727272727)
    assert out[6] == approx(1020 / 23)
    assert out[7] == approx(3300 / 49)
    assert out[8] == approx(77100 / 1027)
    assert out[9] == approx(19275 / 439)


def test_a_sharp_drop_after_a_climb_takes_rsi_two_under_ten():
    # a steady climb of 0.5 a bar: the average gain is 0.5, the average loss 0. Two drops of 3:
    #   after the first: gain 0.25, loss 1.5   -> RSI 100 * 0.25 / 1.75 = 14.2857 (not yet under 10)
    #   after the second: gain 0.125, loss 2.25 -> RSI 100 * 0.125 / 2.375 = 5.2632
    closes = [100 + 0.5 * i for i in range(20)]
    closes += [closes[-1] - 3, closes[-1] - 6]
    out = ind.rsi(closes, 2)
    assert out[19] == approx(100.0)
    assert out[20] == approx(100 * 0.25 / 1.75) and out[20] > 10
    assert out[21] == approx(100 * 0.125 / 2.375) and out[21] < 10


# ------------------------------------------------------------------ the degenerate windows
def test_no_losses_is_100_and_no_gains_is_0():
    assert ind.rsi([1, 2, 3, 4, 5], 2)[2:] == [100.0, 100.0, 100.0]
    assert ind.rsi([5, 4, 3, 2, 1], 2)[2:] == [0.0, 0.0, 0.0]


def test_no_movement_at_all_is_100_as_pine_has_it():
    # Pine's ta.rsi: a window without losses reads 100, whether or not there were gains
    assert ind.rsi([7.0] * 6, 3)[3:] == [100.0, 100.0, 100.0]


# ------------------------------------------------------------------ shape and properties
def test_rsi_is_none_until_it_has_length_changes_and_aligned_with_the_input():
    closes = [10, 11, 12, 11, 12, 13, 12, 13]
    for n in (1, 2, 3, 5):
        out = ind.rsi(closes, n)
        assert len(out) == len(closes)
        assert out[:n] == [None] * n and out[n] is not None


def test_rsi_of_too_few_closes_is_all_none():
    assert ind.rsi([1, 2], 2) == [None, None]
    assert ind.rsi([5.0], 2) == [None]
    assert ind.rsi([], 2) == []


def test_rsi_default_length_is_14():
    closes = [100 + (i % 3) for i in range(30)]
    assert ind.rsi(closes) == ind.rsi(closes, 14)
    assert ind.rsi(closes)[13] is None and ind.rsi(closes)[14] is not None


def test_rsi_length_one_is_the_last_change():
    # with length 1 the average is the latest change itself: up bar -> 100, down bar -> 0
    assert ind.rsi([1, 2, 1, 1.5], 1) == [None, 100.0, 0.0, 100.0]


def test_a_non_positive_length_is_refused():
    for n in (0, -2):
        with pytest.raises(ValueError):
            ind.rsi([1, 2, 3], n)


def _walk(n, seed):
    rng = random.Random(seed)
    price, out = 100.0, []
    for _ in range(n):
        price *= 1 + rng.gauss(0.0003, 0.012)
        out.append(price)
    return out


@pytest.mark.parametrize("n", [2, 5, 14])
def test_rsi_stays_between_0_and_100_and_never_sees_the_future(n):
    closes = _walk(400, 11)
    full = ind.rsi(closes, n)
    assert all(0.0 <= v <= 100.0 for v in full if v is not None)
    assert full[:250] == ind.rsi(closes[:250], n)


def test_rsi_agrees_with_an_independent_pandas_implementation_after_warm_up():
    # pandas' ewm(alpha=1/n, adjust=False) is Wilder's smoothing but seeded with the first value
    # instead of the mean of the first n: the difference decays by (1 - 1/n) a bar, so after the
    # warm-up the two must agree to rounding error
    closes = _walk(600, 5)
    s = pd.Series(closes)
    change = s.diff()
    for n in (2, 14):
        up = change.clip(lower=0).ewm(alpha=1.0 / n, adjust=False).mean()
        down = (-change.clip(upper=0)).ewm(alpha=1.0 / n, adjust=False).mean()
        want = 100.0 - 100.0 / (1.0 + up / down)
        got = ind.rsi(closes, n)
        warm = 300
        assert max(abs(got[i] - want[i]) for i in range(warm, len(closes))) < 1e-6
