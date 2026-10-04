"""EURUSD carry-gated strategy port -- pure logic only (state machine, ATR,
condition computation), same policy as test_tradingview.py: the network calls
(Bundesbank, FRED, yfinance) aren't tested here, offline logic is.
"""
from __future__ import annotations

import re

import pandas as pd
import pytest

import carry_strategy as cs
import telegram_notify as tn

_TAGS = re.compile(r"</?[a-zA-Z][^>]*>")


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
    assert msg == ("🟢 <b>EURUSD!</b>: вход в лонг (новый сигнал) — цена 1,2000, 200-дн. средняя 1,1500, "
                   "спред DE-US 2 г. +0,35 п.п., стоп ~1,1520 (6×ATR)")


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
    assert msg == "⚪ <b>EURUSD!</b>: выход во флэт (сработал стоп) — цена 1,1600, выход ~1,1670"


def test_step_exits_on_signal_off_even_without_stop_hit():
    pos = {"side": 1, "trail_stop": 1.00, "extreme": 1.20}  # stop far away, won't trigger
    s = _bar(date=pd.Timestamp("2026-01-05"), close=1.10, high=1.11, low=1.09, hold_l=False)
    new_pos, msg = cs.step(pos, s)
    assert new_pos["side"] == 0
    assert msg == "⚪ <b>EURUSD!</b>: выход во флэт (сигнал снят) — цена 1,1000, выход ~1,1000"


def test_step_flat_and_no_signal_is_a_no_op():
    pos = {"side": 0, "trail_stop": None, "extreme": None}
    s = _bar(enter_l=False, enter_s=False, hold_l=False, hold_s=False)
    new_pos, msg = cs.step(pos, s)
    assert new_pos == pos
    assert msg is None


def test_step_enters_short_on_a_fresh_cross_below():
    pos = {"side": 0, "trail_stop": None, "extreme": None}
    s = _bar(close=1.10, diff=-0.35, high=1.102, low=1.095, enter_s=True, hold_s=True, hold_l=False, ev_s=True)
    new_pos, msg = cs.step(pos, s)
    assert new_pos["side"] == -1
    assert msg == ("🔴 <b>EURUSD!</b>: вход в шорт (новый сигнал) — цена 1,1000, 200-дн. средняя 1,1500, "
                   "спред DE-US 2 г. −0,35 п.п., стоп ~1,1480 (6×ATR)")


# ------------------------------------------------------------------ the message
_S = dict(close=1.1327, ma=1.1615, diff=-1.68, atr=0.0068)


def test_a_long_entry_has_a_green_dot_the_price_the_average_the_spread_and_the_stop():
    text = tn.format_carry_signal("FLAT", "LONG", _S, reason="entry", level=1.1200)
    assert text == ("🟢 <b>EURUSD!</b>: вход в лонг (новый сигнал) — цена 1,1327, 200-дн. средняя 1,1615, "
                    "спред DE-US 2 г. −1,68 п.п., стоп ~1,1200 (6×ATR)")


def test_a_short_entry_has_a_red_dot():
    text = tn.format_carry_signal("FLAT", "SHORT", _S, reason="entry", level=1.1500)
    assert text == ("🔴 <b>EURUSD!</b>: вход в шорт (новый сигнал) — цена 1,1327, 200-дн. средняя 1,1615, "
                    "спред DE-US 2 г. −1,68 п.п., стоп ~1,1500 (6×ATR)")


def test_an_exit_to_flat_has_a_white_dot_the_price_and_the_exit_level():
    text = tn.format_carry_signal("LONG", "FLAT", _S, reason="trailing stop", level=1.1200)
    assert text == "⚪ <b>EURUSD!</b>: выход во флэт (сработал стоп) — цена 1,1327, выход ~1,1200"
    text = tn.format_carry_signal("SHORT", "FLAT", _S, reason="signal off", level=1.1327)
    assert text == "⚪ <b>EURUSD!</b>: выход во флэт (сигнал снят) — цена 1,1327, выход ~1,1327"


def test_without_a_level_there_is_no_stop_or_exit_part():
    assert tn.format_carry_signal("FLAT", "LONG", _S, reason="entry", level=None).endswith("−1,68 п.п.")
    assert tn.format_carry_signal("LONG", "FLAT", _S, reason="signal off", level=None).endswith("— цена 1,1327")


def test_what_is_not_known_is_left_out():
    text = tn.format_carry_signal("FLAT", "LONG", dict(_S, ma=None, diff=None), reason="entry", level=1.12)
    assert text == "🟢 <b>EURUSD!</b>: вход в лонг (новый сигнал) — цена 1,1327, стоп ~1,1200 (6×ATR)"


@pytest.mark.parametrize("diff, shown", [(0.35, "+0,35 п.п."), (-1.68, "−1,68 п.п."), (0.0, "+0,00 п.п."),
                                         (-0.001, "+0,00 п.п."), (12.0, "+12,00 п.п.")])
def test_the_spread_is_signed_in_percentage_points_with_a_comma_and_a_real_minus(diff, shown):
    assert f"спред DE-US 2 г. {shown}" in tn.format_carry_signal("FLAT", "LONG", dict(_S, diff=diff),
                                                                 reason="entry", level=1.1)


def test_the_message_is_one_russian_line_without_the_old_english_caveat():
    for to_state in ("LONG", "SHORT", "FLAT"):
        text = tn.format_carry_signal("FLAT", to_state, _S, reason="entry", level=1.1)
        assert "\n" not in text and not re.search(r"[A-Za-z]{4,}", _TAGS.sub("", text).replace("EURUSD", "")
                                                   .replace("DE-US", "").replace("ATR", ""))
        for gone in ("Manual execution", "backtested", "OOS", "broker-side", "price ", "initial stop", "→"):
            assert gone not in text


def test_a_reason_nobody_translated_is_shown_as_it_is_and_escaped():
    text = tn.format_carry_signal("LONG", "FLAT", _S, reason="a<b>&c", level=None)
    assert "выход во флэт (a&lt;b&gt;&amp;c)" in text
    assert set(_TAGS.findall(text)) == {"<b>", "</b>"}
