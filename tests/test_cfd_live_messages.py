"""telegram_notify.py, the CFD messages (spec 2026-10-08-cfd-live-crypto-breakout.md, "Messages" and the /cfd
view): one message per event in the signal-line style, exact strings."""
from __future__ import annotations

import re

import pytest

import telegram_notify as tn
from cfd import live


def sig(**kw):
    base = dict(id=1, coin="SOL", symbol="SOL-USD", side="long", signal_date="2026-10-06", entry=121.5,
                stop0=108.2, stop=108.2, r=13.3, checkpoint=0, last_bar="2026-10-06", status="open",
                closed_date=None, exit_price=None, result_r=None, risk_pct=1.0, risk_eur=5.0, qty=0.43,
                created="2026-10-07T07:00:00")
    base.update(kw)
    return live.Signal(**base)


def entry(s=None, **kw):
    return live.Notice("entry", s or sig(), risk_pct=1.0, **kw)


def checkpoint(k, stop, level, s=None):
    return live.Notice("checkpoint", s or sig(), k=k, level=level, stop=stop, date="2026-10-09")


def close(price, res, trailed, s=None):
    return live.Notice("close", s or sig(), price=price, result_r=res, trailed=trailed, date="2026-10-12")


def plain(text):
    return re.sub(r"</?b>", "", text)


# ------------------------------------------------------------------ numbers
@pytest.mark.parametrize("x, text", [(121.5, "121,50"), (100.0, "100,00"), (1234.5, "1 234,50"),
                                     (65000.0, "65 000,00"), (99.99, "99,9900"), (12.3456789, "12,3457"),
                                     (1.0, "1,0000"), (0.99999, "0,999990"), (0.123456789, "0,123457"),
                                     (0.00001234, "0,000012")])
def test_prices_have_the_precision_of_their_size(x, text):
    assert tn.cfd_price(x) == text


# ------------------------------------------------------------------ the entry
def test_entry_with_a_balance_is_the_spec_line():
    assert tn.format_cfd_notice(entry()) == (
        "🟢 <b>SOLUSD!</b>: покупка по 121,50 — стоп 108,20, трейлинг-стоп 3×ATR; TP1 134,80 · TP2 148,10 · "
        "TP3 161,40 · TP4 174,70 (отметки, позиция не закрывается); риск 1% = €5,00, объём 0,43 SOL; эксперимент")


def test_a_short_reads_prodazha_with_a_red_dot_and_the_levels_below():
    short = sig(side="short", stop0=134.8, stop=134.8)
    assert tn.format_cfd_notice(entry(short)) == (
        "🔴 <b>SOLUSD!</b>: продажа по 121,50 — стоп 134,80, трейлинг-стоп 3×ATR; TP1 108,20 · TP2 94,9000 · "
        "TP3 81,6000 · TP4 68,3000 (отметки, позиция не закрывается); риск 1% = €5,00, объём 0,43 SOL; эксперимент")


def test_entry_without_a_balance_names_the_percent_and_the_quantity_for_a_thousand():
    no_balance = sig(risk_pct=None, risk_eur=None, qty=None)
    text = tn.format_cfd_notice(live.Notice("entry", no_balance, risk_pct=1.0, qty_per_1000=0.87))
    assert text.endswith("(отметки, позиция не закрывается); риск 1%, объём на €1 000 баланса: 0,87 SOL; эксперимент")


def test_entry_over_the_open_risk_limit_carries_the_note_before_the_word_experiment():
    text = tn.format_cfd_notice(entry(over_limit=(4.0, 3.0)))
    assert text.endswith("риск 1% = €5,00, объём 0,43 SOL; открытый риск уже 4% — выше вашего лимита 3%; эксперимент")


def test_a_fractional_percent_has_a_comma():
    text = tn.format_cfd_notice(live.Notice("entry", sig(risk_pct=0.5, risk_eur=2.5), risk_pct=0.5,
                                            over_limit=(3.5, 3.0)))
    assert "риск 0,5% = €2,50" in text and "уже 3,5% — выше вашего лимита 3%" in text


def test_a_quantity_under_one_step_says_so():
    text = tn.format_cfd_notice(entry(sig(qty=0.0)))
    assert "риск 1% = €5,00, объём меньше минимального шага; эксперимент" in text


def test_a_cheap_coin_has_six_decimals_and_whole_coins():
    doge = sig(coin="DOGE", symbol="DOGE-USD", entry=0.152, stop0=0.1402, stop=0.1402, r=0.0118, qty=424.0)
    text = tn.format_cfd_notice(entry(doge))
    assert text.startswith("🟢 <b>DOGEUSD!</b>: покупка по 0,152000 — стоп 0,140200,")
    assert "TP1 0,163800" in text and "объём 424 DOGE" in text


# ------------------------------------------------------------------ the checkpoints
def test_checkpoint_with_a_balance():
    assert tn.format_cfd_notice(checkpoint(1, 119.4, 134.8)) == (
        "🟢 <b>SOLUSD!</b>: достигнут TP1 134,80 — стоп подтянут до 119,40, сейчас <b>+1,0R</b> (+€5,00)")


def test_checkpoint_four_is_four_r_and_four_times_the_risk():
    text = tn.format_cfd_notice(checkpoint(4, 150.0, 174.7))
    assert text.endswith("достигнут TP4 174,70 — стоп подтянут до 150,00, сейчас <b>+4,0R</b> (+€20,00)")


def test_checkpoint_without_a_balance_is_in_r_only():
    text = tn.format_cfd_notice(checkpoint(2, 125.0, 148.1, sig(risk_pct=None, risk_eur=None, qty=None)))
    assert text == "🟢 <b>SOLUSD!</b>: достигнут TP2 148,10 — стоп подтянут до 125,00, сейчас <b>+2,0R</b>"


def test_a_stop_that_did_not_move_is_not_said_to_be_pulled_up():
    text = tn.format_cfd_notice(checkpoint(1, 108.2, 134.8))
    assert "— стоп 108,20, сейчас" in text and "подтянут" not in text


def test_a_short_checkpoint_has_a_green_dot_and_a_stop_pulled_down():
    short = sig(side="short", stop0=134.8, stop=134.8)
    assert tn.format_cfd_notice(checkpoint(1, 124.0, 108.2, short)) == (
        "🟢 <b>SOLUSD!</b>: достигнут TP1 108,20 — стоп подтянут до 124,00, сейчас <b>+1,0R</b> (+€5,00)")


# ------------------------------------------------------------------ the close
def test_a_trailing_stop_in_profit():
    assert tn.format_cfd_notice(close(152.3, 2.32, True)) == (
        "🟢 <b>SOLUSD!</b>: сработал трейлинг-стоп — закрыто по 152,30, итог <b>+€11,60</b> (+2,3R)")


def test_the_initial_stop_in_loss():
    assert tn.format_cfd_notice(close(108.2, -1.02, False)) == (
        "🔴 <b>SOLUSD!</b>: сработал стоп — закрыто по 108,20, итог <b>−€5,10</b> (−1,0R)")


def test_the_dot_of_a_close_follows_the_sign_of_the_result():
    assert tn.format_cfd_notice(close(110.0, -0.4, True)).startswith("🔴")
    assert tn.format_cfd_notice(close(130.0, 0.4, True)).startswith("🟢")
    assert "сработал трейлинг-стоп" in tn.format_cfd_notice(close(110.0, -0.4, True))


def test_a_close_without_a_balance_is_in_r_only():
    s = sig(risk_pct=None, risk_eur=None, qty=None)
    assert tn.format_cfd_notice(close(152.3, 2.32, True, s)) == (
        "🟢 <b>SOLUSD!</b>: сработал трейлинг-стоп — закрыто по 152,30, итог <b>+2,3R</b>")
    assert tn.format_cfd_notice(close(108.2, -1.02, False, s)) == (
        "🔴 <b>SOLUSD!</b>: сработал стоп — закрыто по 108,20, итог <b>−1,0R</b>")


def test_a_result_that_rounds_to_zero_is_never_a_minus_zero():
    s = sig(risk_pct=None, risk_eur=None, qty=None)
    text = tn.format_cfd_notice(close(121.0, -0.03, True, s))
    assert "<b>+0,0R</b>" in text and text.startswith("🟢")


def test_html_false_has_no_tags():
    assert tn.format_cfd_notice(close(152.3, 2.32, True), html=False) == (
        "🟢 SOLUSD!: сработал трейлинг-стоп — закрыто по 152,30, итог +€11,60 (+2,3R)")


def test_only_b_tags_are_used():
    for n in (entry(), checkpoint(1, 119.4, 134.8), close(152.3, 2.32, True)):
        assert set(re.findall(r"</?(\w+)>", tn.format_cfd_notice(n))) == {"b"}


# ------------------------------------------------------------------ the settings line and the view
def test_settings_line():
    s = live.Settings(500.0, 1.0, 3.0, False)
    assert tn.format_cfd_settings(s) == "Баланс €500, риск 1% на сигнал, лимит открытого риска 3%, новые сигналы: включены"
    assert tn.format_cfd_settings(live.Settings(None, 0.5, 3.5, True)) == (
        "Баланс не задан, риск 0,5% на сигнал, лимит открытого риска 3,5%, новые сигналы: на паузе")
    assert tn.format_cfd_settings(live.Settings(1234.5, 1.0, 3.0, False)).startswith("Баланс €1 234,50,")


HEADER = "CFD-сигналы (эксперимент): пробой тренда на 13 монетах, выход по трейлинг-стопу"
TODAY = __import__("datetime").date(2026, 10, 12)


def test_the_view_with_nothing_yet():
    text = tn.format_cfd_status(live.Settings(), [], [], TODAY)
    assert text.splitlines() == [f"<b>{HEADER}</b>", tn.format_cfd_settings(live.Settings()), "Сигналов пока не было."]


def test_the_view_lists_the_open_signals():
    open_ = [sig(checkpoint=2, stop=125.0), sig(id=2, coin="ETH", symbol="ETH-USD", side="short", entry=2500.0,
                                               stop0=2700.0, stop=2600.0, signal_date="2026-10-11")]
    lines = tn.format_cfd_status(live.Settings(), open_, [], TODAY).splitlines()
    assert lines[2:] == ["Открытые (2):",
                         "🟢 <b>SOLUSD</b> — покупка по 121,50, стоп 125,00, отметка TP2, 6 дн.",
                         "🔴 <b>ETHUSD</b> — продажа по 2 500,00, стоп 2 600,00, отметок нет, 1 дн.",
                         "Закрытых пока нет."]


def closed(res, eur=5.0, **kw):
    return sig(status="closed", result_r=res, risk_eur=eur, **kw)


def test_the_record_of_closed_signals():
    rows = [closed(2.0, id=1), closed(-1.0, id=2), closed(-1.0, id=3), closed(0.5, id=4)]
    lines = tn.format_cfd_status(live.Settings(), [], rows, TODAY).splitlines()
    assert lines[2:] == ["Открытых нет.",
                         "Закрытые, если брать каждый сигнал: 4, прибыльных 50%, профит-фактор 1,25, "
                         "средний результат +0,12R, всего +0,5R (+€2,50)"]


def test_the_record_without_balance_has_no_euro():
    rows = [closed(2.0, eur=None, id=1), closed(-1.0, eur=None, id=2)]
    text = tn.format_cfd_status(live.Settings(), [], rows, TODAY)
    assert text.endswith("всего +1,0R") and "€" not in text.splitlines()[-1]


def test_the_profit_factor_of_a_record_with_no_loss_is_a_dash():
    text = tn.format_cfd_status(live.Settings(), [], [closed(1.5, id=1)], TODAY)
    assert "профит-фактор —" in text and "прибыльных 100%" in text


def test_the_view_without_html_has_no_tags():
    text = tn.format_cfd_status(live.Settings(), [sig()], [closed(1.0, id=2)], TODAY, html=False)
    assert "<" not in text and text.startswith(HEADER)
