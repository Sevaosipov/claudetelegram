"""EURUSD carry-gated strategy port -- pure logic only (state machine, ATR,
condition computation), same policy as test_tradingview.py: the network calls
(Bundesbank, FRED, yfinance) aren't tested here, offline logic is.
"""
from __future__ import annotations

import pandas as pd
import pytest

import carry_strategy as cs


def test_wilder_atr_matches_hand_computed_value():
    # 5 bars, constant true range of 2.0 after the first (high-low=2 every bar,
    # no gaps) -- Wilder's RMA of a constant series converges to that constant.
    high = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0])
    low = pd.Series([8.0, 9.0, 10.0, 11.0, 12.0])
    close = pd.Series([9.0, 10.0, 11.0, 12.0, 13.0])
    atr = cs._wilder_atr(high, low, close, length=2)
    assert atr.iloc[-1] == pytest.approx(2.0, abs=1e-9)


def _prices(closes, highs=None, lows=None):
    idx = pd.date_range("2024-01-01", periods=len(closes), freq="D")
    highs = highs or [c + 0.001 for c in closes]
    lows = lows or [c - 0.001 for c in closes]
    return pd.DataFrame({"open": closes, "high": highs, "low": lows, "close": closes}, index=idx)


def test_compute_state_entry_condition_needs_both_band_and_carry():
    # 210 flat bars at 1.00 (seeds a 200-day MA of 1.00), then a jump to 1.05
    # (5% above MA, clears the 2.5% band) with DE-US spread positive.
    closes = [1.00] * 210 + [1.05]
    price = _prices(closes)
    idx = price.index
    de = pd.Series(0.5, index=idx)
    us = pd.Series(0.1, index=idx)  # diff = +0.4pp throughout -> carryL true
    s = cs.compute_state(price, de, us)
    assert s["enter_l"] is True
    assert s["hold_l"] is True
    assert s["ev_l"] is True  # fresh cross (yesterday's close was still 1.00)


def test_compute_state_no_entry_without_carry_gate():
    closes = [1.00] * 210 + [1.05]
    price = _prices(closes)
    idx = price.index
    de = pd.Series(0.1, index=idx)
    us = pd.Series(0.5, index=idx)  # diff negative -> carryL false even though price cleared the band
    s = cs.compute_state(price, de, us)
    assert s["enter_l"] is False
    assert s["hold_l"] is False


def _bar(**over):
    base = dict(date=pd.Timestamp("2026-01-01"), close=1.20, ma=1.15, atr=0.008,
                diff=0.35, high=1.202, low=1.195, enter_l=False, enter_s=False,
                hold_l=True, hold_s=False, ev_l=False, ev_s=False)
    base.update(over)
    return base


def test_step_enters_long_on_fresh_cross():
    pos = {"side": 0, "trail_stop": None, "extreme": None}
    s = _bar(enter_l=True, hold_l=True, ev_l=True)
    new_pos, msg = cs.step(pos, s)
    assert new_pos["side"] == 1
    assert new_pos["trail_stop"] == pytest.approx(1.20 - 0.008 * 6)
    assert new_pos["extreme"] == 1.202
    assert msg is not None and "FLAT → LONG" in msg


def test_step_ratchets_trail_stop_favorably_and_stays_silent():
    pos = {"side": 1, "trail_stop": 1.152, "extreme": 1.202}
    s = _bar(date=pd.Timestamp("2026-01-02"), close=1.21, high=1.215, low=1.208, hold_l=True)
    new_pos, msg = cs.step(pos, s)
    assert msg is None  # no alert on a quiet hold
    assert new_pos["extreme"] == 1.215
    assert new_pos["trail_stop"] == pytest.approx(1.215 - 0.008 * 6)
    assert new_pos["trail_stop"] > pos["trail_stop"]  # only ever moves in the trade's favor


def test_step_never_lets_trail_stop_move_backward():
    # today's high (1.205) doesn't beat the existing extreme (1.21), so the
    # candidate stop (1.21 - 6*0.008 = 1.162) is worse than the current one
    # (1.20) -- a quiet/pullback day must not loosen an already-favorable stop.
    pos = {"side": 1, "trail_stop": 1.20, "extreme": 1.21}
    s = _bar(date=pd.Timestamp("2026-01-03"), close=1.203, high=1.205, low=1.201, hold_l=True)
    new_pos, _ = cs.step(pos, s)
    assert new_pos["extreme"] == 1.21  # unchanged, today's high didn't beat it
    assert new_pos["trail_stop"] == 1.20


def test_step_exits_on_trailing_stop_hit():
    pos = {"side": 1, "trail_stop": 1.167, "extreme": 1.215}
    s = _bar(date=pd.Timestamp("2026-01-04"), close=1.16, high=1.17, low=1.16, hold_l=True)
    new_pos, msg = cs.step(pos, s)
    assert new_pos == {"side": 0, "trail_stop": None, "extreme": None}
    assert "trailing stop" in msg


def test_step_exits_on_signal_off_even_without_stop_hit():
    pos = {"side": 1, "trail_stop": 1.00, "extreme": 1.20}  # stop far away, won't trigger
    s = _bar(date=pd.Timestamp("2026-01-05"), close=1.10, high=1.11, low=1.09, hold_l=False)
    new_pos, msg = cs.step(pos, s)
    assert new_pos["side"] == 0
    assert "signal off" in msg


def test_step_flat_and_no_signal_is_a_no_op():
    pos = {"side": 0, "trail_stop": None, "extreme": None}
    s = _bar(enter_l=False, enter_s=False, hold_l=False, hold_s=False)
    new_pos, msg = cs.step(pos, s)
    assert new_pos == pos
    assert msg is None
