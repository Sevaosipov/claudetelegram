"""cfd/indicators.py, round 3: the population standard deviation over a window, which H6 (FX-REV)
reads as SD20. Hand-computed values and an independent cross-check against numpy."""
from __future__ import annotations

import math
import random

import numpy as np
import pytest

from cfd import indicators as ind


def approx(x):
    return pytest.approx(x, rel=1e-12, abs=1e-12)


def test_the_classic_eight_numbers_have_a_population_sd_of_exactly_two():
    # 2 4 4 4 5 5 7 9: mean 5; squared deviations 9 1 1 1 0 0 4 16 = 32; 32 / 8 = 4 (population: divide by n)
    out = ind.stdev([2, 4, 4, 4, 5, 5, 7, 9], 8)
    assert out[:7] == [None] * 7
    assert out[7] == approx(2.0)


def test_it_divides_by_n_not_n_minus_one():
    # 1, 3: mean 2, squared deviations 1 + 1 = 2; population variance 2 / 2 = 1 (sample would be 2)
    assert ind.stdev([1, 3], 2)[1] == approx(1.0)


def test_a_rolling_window_of_three_hand_computed():
    # [1,2,3]: mean 2, squares 1+0+1 = 2, var 2/3, sd sqrt(2/3)
    # [2,3,4]: the same
    # [3,4,6]: mean 13/3, deviations -4/3 -1/3 5/3, squares (16+1+25)/9 = 42/9, var 14/9, sd sqrt(14)/3
    out = ind.stdev([1, 2, 3, 4, 6], 3)
    assert out[:2] == [None, None]
    assert out[2] == approx(math.sqrt(2 / 3))
    assert out[3] == approx(math.sqrt(2 / 3))
    assert out[4] == approx(math.sqrt(14) / 3)


def test_the_window_includes_the_current_value_and_uses_no_later_one():
    a = [5.0, 1.0, 4.0, 9.0, 2.0, 7.0]
    full = ind.stdev(a, 3)
    cut = ind.stdev(a[:4], 3)
    assert full[:4] == cut


def test_a_constant_series_has_no_deviation():
    assert ind.stdev([7.0] * 5, 3)[2:] == [0.0, 0.0, 0.0]


def test_length_one_is_zero_everywhere_and_a_short_series_is_all_none():
    assert ind.stdev([3.0, 9.0], 1) == [0.0, 0.0]
    assert ind.stdev([1.0, 2.0], 3) == [None, None]
    assert ind.stdev([], 3) == []


def test_a_length_below_one_is_refused():
    with pytest.raises(ValueError):
        ind.stdev([1.0, 2.0], 0)


def test_it_agrees_with_numpy_on_random_prices():
    rng = random.Random(7)
    closes = [1.1 + 0.01 * rng.random() for _ in range(80)]
    out = ind.stdev(closes, 20)
    assert out[:19] == [None] * 19
    for i in range(19, 80):
        assert out[i] == pytest.approx(float(np.std(closes[i - 19:i + 1], ddof=0)), rel=1e-9)
