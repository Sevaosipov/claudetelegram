"""outlook.py: situations, frequency tables and the walk-forward check. Offline --
synthetic price paths."""
from __future__ import annotations

import pandas as pd
import pytest

import outlook


def _path(n, daily=0.001, wiggle=0.01, tail=()):
    """A steady drift with a regular zig-zag (volatility never zero), then `tail`
    extra daily returns."""
    closes, p = [], 100.0
    for i in range(n):
        p *= 1 + daily + (wiggle if i % 2 else -wiggle)
        closes.append(p)
    for r in tail:
        p *= 1 + r
        closes.append(p)
    return closes


# ------------------------------------------------------------------ situations
def test_steady_uptrend_is_up_flat_normal():
    assert outlook.situation(_path(400), 21) == "up|flat|normal"


def test_steady_downtrend_is_down():
    assert outlook.situation(_path(400, daily=-0.001), 21).startswith("down|")


def test_a_big_month_is_a_strong_move():
    assert outlook.situation(_path(400, tail=[0.015] * 21), 21).split("|")[1] == "strong_up"
    assert outlook.situation(_path(400, tail=[-0.015] * 21), 21).split("|")[1] == "strong_down"


def test_a_volatility_spike_is_high_volatility():
    spike = [0.05 if i % 2 else -0.05 for i in range(21)]
    assert outlook.situation(_path(400, tail=spike), 21).endswith("|high")


def test_too_little_history_has_no_situation():
    assert outlook.situation(_path(outlook.MIN_HISTORY - 1), 21) is None
    assert outlook.situation(_path(outlook.MIN_HISTORY), 21) is not None


def test_situation_from_tradingview_is_pooled_over_volatility():
    snap = {"close": 110.0, "SMA50": 105.0, "SMA200": 100.0, "Perf.1M": 20.0, "Volatility.M": 2.0}
    assert outlook.situation_from_tv(snap, 21) == "up|strong_up|*"
    assert outlook.situation_from_tv({**snap, "Perf.1M": 1.0}, 21) == "up|flat|*"
    assert outlook.situation_from_tv({**snap, "SMA200": None}, 21) is None


def test_pooled_key():
    assert outlook.pooled("down|flat|high") == "down|flat|*"


# ---------------------------------------------------------------- observations
def test_observations_are_monthly_and_labelled():
    dates = [d.date().isoformat() for d in pd.bdate_range("2015-01-01", periods=600)]
    closes = _path(600, daily=0.002, wiggle=0.0005)          # rises every day
    obs = outlook.observations(list(zip(dates, closes)), 21)
    expected = len(range(outlook.MIN_HISTORY - 1, 600 - 21, 21))
    assert len(obs) == expected and all(up for _y, _k, up in obs)
    assert obs[0][0] == int(dates[outlook.MIN_HISTORY - 1][:4])


# ---------------------------------------------------------- table, walk-forward
def _obs(key, per_year, up_fn, years=range(2010, 2022)):
    return [(y, key, up_fn(i)) for y in years for i in range(per_year)]


def test_a_planted_effect_has_an_edge_and_noise_does_not():
    noise = _obs("mixed|flat|normal", 100, lambda i: i % 2 == 0)
    rows = outlook.build_table(noise)
    assert not outlook.has_edge(rows["mixed|flat|normal"])     # its rate is the base rate

    planted = _obs("up|strong_up|normal", 30, lambda i: True)
    rows = outlook.build_table(noise + planted)
    row = rows["up|strong_up|normal"]
    assert outlook.has_edge(row) and row["n"] == 30 * 12 and row["up"] == 30 * 12


def test_a_thin_situation_has_no_edge():
    rows = outlook.build_table(_obs("mixed|flat|normal", 100, lambda i: i % 2 == 0)
                               + _obs("down|strong_down|high", 3, lambda i: True))
    assert rows["down|strong_down|high"]["oos_n"] < outlook.MIN_OOS
    assert not outlook.has_edge(rows["down|strong_down|high"])


def test_table_includes_pooled_rows():
    rows = outlook.build_table(_obs("up|flat|normal", 5, lambda i: True)
                               + _obs("up|flat|high", 5, lambda i: False))
    assert rows["up|flat|*"]["n"] == rows["up|flat|normal"]["n"] + rows["up|flat|high"]["n"]


def test_tables_and_bars_round_trip(conn):
    rows = outlook.build_table(_obs("up|flat|normal", 5, lambda i: True))
    outlook.save_table(conn, "stock", rows)
    loaded, built_at = outlook.load_table(conn, "stock")
    assert loaded["up|flat|normal"]["n"] == 60 and built_at
    outlook.save_bars(conn, "AAA", [("2026-01-02", 1.0), ("2026-01-05", 2.0)])
    assert outlook.load_bars(conn, "AAA") == [("2026-01-02", 1.0), ("2026-01-05", 2.0)]
