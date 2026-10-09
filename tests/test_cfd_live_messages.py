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
ENTRY = "SOLUSD Long\nEntry 121.50\nStop  108.20\nTP1   134.80\nTP2   148.10\nTP3   161.40\nTP4   174.70"


@pytest.mark.parametrize("x, text", [(121.5, "121.50"), (65000.0, "65000.00"), (99.99, "99.9900"),
                                     (1.0, "1.0000"), (0.152, "0.152000")])
def test_a_plain_price_has_a_point_and_no_spaces(x, text):
    assert tn.cfd_plain(x) == text


def test_entry_is_a_bare_block_with_the_size_when_there_is_a_balance():
    assert tn.format_cfd_notice(entry()) == f"<pre>{ENTRY}\nSize  0.43</pre>"


def test_a_short_says_short_and_has_the_levels_below():
    short = sig(side="short", stop0=134.8, stop=134.8)
    assert tn.format_cfd_notice(entry(short), html=False) == (
        "SOLUSD Short\nEntry 121.50\nStop  134.80\nTP1   108.20\nTP2   94.9000\nTP3   81.6000\nTP4   68.3000\n"
        "Size  0.43")


def test_entry_without_a_balance_or_under_one_step_has_no_size_line():
    no_balance = sig(risk_pct=None, risk_eur=None, qty=None)
    assert tn.format_cfd_notice(live.Notice("entry", no_balance, risk_pct=1.0, qty_per_1000=0.87), html=False) == ENTRY
    assert tn.format_cfd_notice(entry(sig(qty=0.0)), html=False) == ENTRY


def test_entry_says_nothing_else():
    text = tn.format_cfd_notice(entry(over_limit=(4.0, 3.0)), html=False)
    assert text == ENTRY + "\nSize  0.43" and "эксперимент" not in text and "риск" not in text


def test_a_cheap_coin_has_six_decimals_and_whole_coins():
    doge = sig(coin="DOGE", symbol="DOGE-USD", entry=0.152, stop0=0.1402, stop=0.1402, r=0.0118, qty=424.0)
    text = tn.format_cfd_notice(entry(doge), html=False)
    assert text.startswith("DOGEUSD Long\nEntry 0.152000\nStop  0.140200\nTP1   0.163800") and text.endswith("Size  424")


# ------------------------------------------------------------------ the checkpoints
def test_a_checkpoint_is_the_level_and_the_stop_in_force():
    assert tn.format_cfd_notice(checkpoint(1, 119.4, 134.8)) == "<pre>SOLUSD TP1 134.80 stop 119.40</pre>"
    assert tn.format_cfd_notice(checkpoint(4, 150.0, 174.7), html=False) == "SOLUSD TP4 174.70 stop 150.00"
    short = sig(side="short", stop0=134.8, stop=134.8)
    assert tn.format_cfd_notice(checkpoint(1, 124.0, 108.2, short), html=False) == "SOLUSD TP1 108.20 stop 124.00"


# ------------------------------------------------------------------ the close
def test_a_close_is_the_price_and_the_result_in_r_and_in_euros():
    assert tn.format_cfd_notice(close(152.3, 2.32, True)) == "<pre>SOLUSD closed 152.30 +2.32R (+€11.60)</pre>"
    assert tn.format_cfd_notice(close(108.2, -1.02, False), html=False) == "SOLUSD closed 108.20 -1.02R (-€5.10)"


def test_a_close_without_a_balance_is_in_r_only():
    s = sig(risk_pct=None, risk_eur=None, qty=None)
    assert tn.format_cfd_notice(close(152.3, 2.32, True, s), html=False) == "SOLUSD closed 152.30 +2.32R"


def test_a_result_that_rounds_to_zero_is_never_a_minus_zero():
    assert tn.format_cfd_notice(close(121.0, -0.0004, True), html=False) == "SOLUSD closed 121.00 +0.00R (+€0.00)"


def test_only_pre_tags_are_used():
    for n in (entry(), checkpoint(1, 119.4, 134.8), close(152.3, 2.32, True)):
        assert set(re.findall(r"</?(\w+)>", tn.format_cfd_notice(n))) == {"pre"}


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


# ------------------------------------------------------------------ the README shows what the code renders
def test_the_readme_examples_are_what_the_code_renders():
    from pathlib import Path
    readme = (Path(tn.__file__).parent / "README.md").read_text(encoding="utf-8")
    qty = 0.436
    for notice in (live.Notice("entry", sig(qty=qty), risk_pct=1.0),
                   live.Notice("checkpoint", sig(), k=1, level=134.8, stop=119.4),
                   live.Notice("close", sig(), price=152.3, result_r=2.32, trailed=True),
                   live.Notice("close", sig(), price=108.2, result_r=-1.02, trailed=False)):
        assert tn.format_cfd_notice(notice, html=False) in readme
