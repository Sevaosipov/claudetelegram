"""backtest.py's small price helpers. The network wrappers (_series,
_return_before) are not tested; the pure math (_excess_return) is."""
from __future__ import annotations

import pandas as pd
import pytest

import backtest


def _series(values):
    return pd.Series(values, dtype=float)


def test_excess_return_is_ticker_minus_benchmark_over_the_window():
    prices = _series([100, 101, 102, 110])   # +10% over 3 bars
    bench = _series([100, 100, 100, 104])     # +4% over 3 bars
    assert backtest._excess_return(prices, bench, days=3) == pytest.approx(6.0)


def test_excess_return_none_when_a_series_is_missing():
    assert backtest._excess_return(None, _series([1, 2, 3, 4]), days=3) is None
    assert backtest._excess_return(_series([1, 2, 3, 4]), None, days=3) is None


def test_excess_return_none_when_history_too_short():
    prices = _series([100, 110])
    bench = _series([100, 104])
    assert backtest._excess_return(prices, bench, days=3) is None
