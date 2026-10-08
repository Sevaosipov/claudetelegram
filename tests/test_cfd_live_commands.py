"""/cfd and the menu (spec 2026-10-08-cfd-live-crypto-breakout.md, "Commands (Telegram) and menu"): the view,
the five settings commands with their validation, the help line, the menu entry 5."""
from __future__ import annotations

import datetime as dt

import pytest

import menu
import telegram_bot as tb
import telegram_notify as tn
from cfd import live

TODAY = dt.date(2026, 10, 12)


def add(conn, coin="SOL", **kw):
    base = dict(coin=coin, symbol=f"{coin}-USD", side="long", signal_date="2026-10-06", entry=121.5,
                stop0=108.2, r=13.3, last_bar="2026-10-06")
    base.update(kw)
    return live.insert_signal(conn, **base)


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: out.append(msg) or True)
    return out


# ------------------------------------------------------------------ the view
def test_the_view_with_no_signals_yet(conn):
    text = live.handle_command(conn, "/cfd", today=TODAY)
    assert text == "\n".join([f"<b>{tn.CFD_HEADER}</b>", tn.format_cfd_settings(live.get_settings(conn)),
                              "Сигналов пока не было."])


def test_the_view_shows_open_signals_and_the_record(conn):
    sid = add(conn)
    live.update_signal(conn, sid, checkpoint=1, stop=119.4, last_bar="2026-10-11")
    done = add(conn, "BTC", signal_date="2026-10-01", risk_pct=1.0, risk_eur=5.0, qty=0.01)
    live.update_signal(conn, done, status="closed", closed_date="2026-10-05", exit_price=1.0, result_r=2.0)
    text = live.handle_command(conn, "/cfd", today=TODAY)
    assert "Открытые (1):" in text
    assert "🟢 <b>SOLUSD</b> — покупка по 121,50, стоп 119,40, отметка TP1, 6 дн." in text
    assert text.splitlines()[-1] == ("Закрытые, если брать каждый сигнал: 1, прибыльных 100%, профит-фактор —, "
                                     "средний результат +2,00R, всего +2,0R (+€10,00)")


def test_the_command_may_carry_the_bots_name_and_any_case(conn):
    assert live.handle_command(conn, "/CFD@my_bot", today=TODAY) == live.handle_command(conn, "/cfd", today=TODAY)


def test_the_menu_text_is_the_same_view_without_tags(conn):
    add(conn)
    html = live.status_text(conn, today=TODAY)
    plain = live.status_text(conn, html=False, today=TODAY)
    assert "<b>" in html and "<" not in plain
    assert plain == html.replace("<b>", "").replace("</b>", "")


# ------------------------------------------------------------------ the settings commands
def test_balance_is_kept_and_the_new_settings_line_answers(conn):
    reply = live.handle_command(conn, "/cfd balance 500")
    assert live.get_settings(conn).balance_eur == 500.0
    assert reply == "Баланс €500, риск 1% на сигнал, лимит открытого риска 3%, новые сигналы: включены"


def test_a_comma_decimal_is_read(conn):
    live.handle_command(conn, "/cfd balance 1250,5")
    assert live.get_settings(conn).balance_eur == 1250.5


def test_balance_zero_clears_it(conn):
    live.handle_command(conn, "/cfd balance 500")
    assert live.handle_command(conn, "/cfd balance 0").startswith("Баланс не задан,")
    assert live.get_settings(conn).balance_eur is None


def test_risk_is_between_a_tenth_and_five_percent(conn):
    assert live.handle_command(conn, "/cfd risk 2").split(",")[1] == " риск 2% на сигнал"
    assert live.get_settings(conn).risk_pct == 2.0
    live.handle_command(conn, "/cfd risk 0.1")
    live.handle_command(conn, "/cfd risk 5")
    assert live.get_settings(conn).risk_pct == 5.0
    for bad in ("0.09", "5.1", "0", "-1"):
        assert live.handle_command(conn, f"/cfd risk {bad}") == live.USAGE["risk"]
    assert live.get_settings(conn).risk_pct == 5.0


def test_maxrisk_is_between_one_and_twenty_percent(conn):
    live.handle_command(conn, "/cfd maxrisk 1")
    live.handle_command(conn, "/cfd maxrisk 20")
    assert live.get_settings(conn).max_open_risk_pct == 20.0
    for bad in ("0.9", "20.1", "abc"):
        assert live.handle_command(conn, f"/cfd maxrisk {bad}") == live.USAGE["maxrisk"]
    assert live.get_settings(conn).max_open_risk_pct == 20.0


@pytest.mark.parametrize("bad", ["", "abc", "-5", "nan", "inf", "1e12", "5 6"])
def test_a_bad_balance_gets_the_usage_line_and_changes_nothing(conn, bad):
    live.handle_command(conn, "/cfd balance 300")
    assert live.handle_command(conn, f"/cfd balance {bad}") == live.USAGE["balance"]
    assert live.get_settings(conn).balance_eur == 300.0


def test_off_and_on_pause_the_new_signals(conn):
    assert live.handle_command(conn, "/cfd off").endswith("новые сигналы: на паузе")
    assert live.get_settings(conn).paused is True
    assert live.handle_command(conn, "/cfd on").endswith("новые сигналы: включены")
    assert live.get_settings(conn).paused is False


def test_off_and_on_take_no_argument(conn):
    assert live.handle_command(conn, "/cfd off now") == live.USAGE["off"]
    assert live.get_settings(conn).paused is False


def test_an_unknown_subcommand_gets_the_whole_usage(conn):
    reply = live.handle_command(conn, "/cfd wat")
    assert reply == live.usage_text()
    for line in live.USAGE.values():
        assert line in reply
    assert "эксперимент" in live.usage_text().splitlines()[0]


# ------------------------------------------------------------------ Telegram
def test_telegram_routes_cfd_to_the_view(conn, sent):
    tb._handle_message(conn, "/cfd")
    assert sent == [live.handle_command(conn, "/cfd")]


def test_telegram_routes_the_settings_commands(conn, sent):
    tb._handle_message(conn, "/cfd risk 2")
    tb._handle_message(conn, "/cfd@some_bot maxrisk 4")
    assert live.get_settings(conn).risk_pct == 2.0 and live.get_settings(conn).max_open_risk_pct == 4.0
    assert len(sent) == 2 and sent[1].endswith("новые сигналы: включены")


def test_a_failing_view_answers_instead_of_dying(conn, sent, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db")
    monkeypatch.setattr(live, "handle_command", boom)
    tb._handle_message(conn, "/cfd")
    assert len(sent) == 1 and "RuntimeError" in sent[0]


def test_the_help_text_has_one_line_for_cfd():
    lines = [l for l in tb.HELP_TEXT.splitlines() if "/cfd" in l]
    assert len(lines) == 1 and "эксперимент" in lines[0]


# ------------------------------------------------------------------ the menu
def test_the_menu_lists_option_5_and_prints_the_same_text(conn, monkeypatch, capsys):
    add(conn)
    answers = iter(["5", "0"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(menu.db, "connect", lambda path: conn)
    menu.main()
    out = capsys.readouterr().out
    assert "5) CFD-сигналы (эксперимент)" in out
    assert live.status_text(conn, html=False).splitlines()[0] in out
    assert "SOLUSD" in out


def test_a_failing_cfd_view_does_not_end_the_menu(conn, monkeypatch, capsys):
    def boom(*a, **k):
        raise OSError("disk")
    monkeypatch.setattr(live, "status_text", boom)
    menu.show_cfd(conn)
    assert "Ошибка: OSError" in capsys.readouterr().out
