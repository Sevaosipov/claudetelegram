"""The model's views: the scored list (telegram_notify.format_scored) and the menu's signals and portfolio
views. Offline -- scores are hand-built. (The weekly messages are tested in test_weekly.py, the portfolio
picks in test_signals_weekly.py.)"""
from __future__ import annotations

import datetime as dt

import pytest

import model
import model_score
import positions
import telegram_notify as tn


# ------------------------------------------------------------------ builders
def _stock(ticker="AAA", total=64.0, decision=model_score.BUY, **kw):
    base = dict(ticker=ticker, source="SEC", company=f"{ticker} Corp", signal=None, insiders=52.0,
                triggers=5.0, momentum=7.0, news=0.0, total=total, decision=decision,
                reasons=["3 инсайдера"], block=None, untradeable=None, t212=True, stop_pct=0.10,
                last_close=10.0)
    base.update(kw)
    return model_score.StockScore(**base)


def _coin(coin="BTC", total=60.0, decision=model_score.BUY, **kw):
    base = dict(coin=coin, ticker=f"CRYPTO:{coin}", trend=45.0, flows=15.0, news=0.0, total=total,
                trend_up=True, trend_down=False, caution=None, block=None, decision=decision,
                reasons=["выше 100-дн. средней"], stop_pct=0.20, last_close=100.0)
    base.update(kw)
    return model_score.CoinScore(**base)


def _close(ticker="CCC"):
    pos = positions.Position(1, ticker, "SEC", "2026-09-01", 100.0, ["A"], None, None, None, None)
    return positions.CloseAlert(pos, "trailing_stop", "−10% от максимума 100.00", 84.0)


# --------------------------------------------------------------- format_scored
def test_scored_header_and_empty_message():
    text = tn.format_scored([_stock()])
    assert text.splitlines()[0] == "СИГНАЛЫ — оценка модели (покупка от 60, наблюдение 45–59)"
    assert tn.format_scored([]) == "Свежих сигналов за 14 дней нет."


def test_stock_line_has_the_icon_the_total_and_the_four_parts():
    line = tn.format_scored([_stock("AAA", 64.0, model_score.BUY)]).splitlines()[1]
    assert line.startswith("🟢 AAA")
    assert "64" in line and "инсайдеры 52 · поводы 5 · импульс 7 · новости 0" in line


def test_coin_line_has_trend_flows_and_news():
    line = tn.format_scored([_coin("BTC", 60.0)]).splitlines()[1]
    assert line.startswith("🟢 CRYPTO:BTC") and "тренд 45 · потоки 15 · новости 0" in line


def test_icons_by_decision():
    text = tn.format_scored([
        _stock("BUY1", 64.0, model_score.BUY), _stock("WAT1", 50.0, model_score.WATCH),
        _stock("BLK1", 30.0, model_score.BLOCK, block="SEC investigation"),
        _coin("ETH", 30.0, model_score.WATCH)])
    lines = {ln.split()[1]: ln for ln in text.splitlines()[1:]}
    assert lines["BUY1"].startswith("🟢") and lines["WAT1"].startswith("👀")
    assert lines["BLK1"].startswith("⛔") and lines["CRYPTO:ETH"].startswith("👀")


def test_block_and_untradeable_reasons_and_the_t212_label():
    text = tn.format_scored([
        _stock("BLK1", 30.0, model_score.BLOCK, block="SEC investigation"),
        _stock("SML1", 66.0, model_score.WATCH, untradeable="компания меньше €20 млн", t212=False),
        _coin("BTC", 20.0, model_score.BLOCK, block="hack news")])
    lines = {ln.split()[1]: ln for ln in text.splitlines()[1:]}
    assert "— SEC investigation" in lines["BLK1"]
    assert "— компания меньше €20 млн" in lines["SML1"] and "нет на T212" in lines["SML1"]
    assert "— hack news" in lines["CRYPTO:BTC"]
    assert "нет на T212" not in lines["BLK1"]


def test_a_negative_news_part_reads_with_a_minus():
    line = tn.format_scored([_stock(news=-10.0)]).splitlines()[1]
    assert "новости −10" in line


def test_skipped_stocks_go_below_a_line_and_at_most_15():
    scored = [_stock("TOP", 64.0)] + [_stock(f"S{i:02d}", 30.0 - i * 0.1, model_score.SKIP)
                                      for i in range(20)]
    lines = tn.format_scored(scored).splitlines()
    cut = lines.index("Прочие (балл ниже 45):")
    assert any(ln.startswith("🟢 TOP") for ln in lines[:cut])
    below = lines[cut + 1:]
    assert len(below) == 15 and all(ln.startswith("·") for ln in below)
    assert below[0].startswith("· S00")


def test_the_header_and_the_prochie_line_follow_the_models_bars(monkeypatch):
    monkeypatch.setattr(model_score, "STOCK_BUY", 70.0)
    monkeypatch.setattr(model_score, "STOCK_WATCH", 50.0)
    lines = tn.format_scored([_stock("TOP", 74.0), _stock("LOW", 30.0, model_score.SKIP)]).splitlines()
    assert lines[0] == "СИГНАЛЫ — оценка модели (покупка от 70, наблюдение 50–69)"
    assert "Прочие (балл ниже 50):" in lines


def test_no_prochie_line_without_skipped_stocks():
    assert "Прочие" not in tn.format_scored([_stock(), _coin()])


def test_scored_html_is_bold_and_escaped():
    text = tn.format_scored([_stock("A&B", 30.0, model_score.BLOCK, block="<fraud> & co")], html=True)
    assert text.splitlines()[0].startswith("<b>СИГНАЛЫ")
    assert "A&amp;B" in text and "&lt;fraud&gt; &amp; co" in text


# ---------------------------------------------------------------------- the menu
def test_menu_signals_print_the_scored_list(conn, capsys, monkeypatch):
    import menu

    monkeypatch.setattr(model, "score_today", lambda c, *a, **k: [_stock("AAA", 64.0), _coin()])
    monkeypatch.setattr("positions.check_exits", lambda c, *a, **k: [_close("CCC")])
    monkeypatch.setattr("positions.last_close", lambda ticker, source=None: 110.0)
    monkeypatch.setattr("prices._closes", lambda symbol, days: [])        # no history to size a stop from
    positions.open_position(conn, "AAA", 100.0)
    menu.show_signals(conn)
    out = capsys.readouterr().out
    assert "СИГНАЛЫ — оценка модели (покупка от 60, наблюдение 45–59)" in out
    assert "🟢 AAA" in out and "CRYPTO:BTC" in out
    assert "🔴 CCC!: сработал стоп — пора продавать" in out     # the pending close alert
    assert "Открытые позиции" in out                            # the /bought positions


def test_menu_prices_a_trading_212_holding_from_its_stored_day_price(conn, capsys, monkeypatch):
    """A holding with no Yahoo listing (keyed by its ISIN) has the sync's prices, also here."""
    import menu

    today = dt.date.today().isoformat()
    monkeypatch.setattr(model, "score_today", lambda c, *a, **k: [])
    monkeypatch.setattr("positions.check_exits", lambda c, *a, **k: [])
    monkeypatch.setattr("positions.last_close", lambda ticker, source=None: None)     # Yahoo has no ISIN
    conn.execute("INSERT INTO positions (ticker, source, opened_at, entry_price, origin, quantity, t212_ticker, "
                 "currency) VALUES ('DE0007164600', 'T212', ?, 100.0, 't212', 5, 'SAPd_EQ', 'EUR')", (today,))
    conn.execute("INSERT INTO t212_prices (ticker, date, price) VALUES ('DE0007164600', ?, 110.0)", (today,))
    conn.commit()
    menu.show_signals(conn)
    out = capsys.readouterr().out
    assert "• SAP: вход 100.00, сейчас 110.00 (+10.0%)" in out and "цена недоступна" not in out
    assert "DE0007164600" not in out                               # by the name its owner knows


def test_menu_signals_survive_a_failing_score(conn, capsys, monkeypatch):
    import menu

    def fail(c, *a, **k):
        raise RuntimeError("offline")
    monkeypatch.setattr(model, "score_today", fail)
    monkeypatch.setattr("positions.check_exits", lambda c, *a, **k: [])
    menu.show_signals(conn)
    assert "offline" in capsys.readouterr().out


def test_menu_signals_use_todays_kept_scores_without_scoring_again(conn, capsys, monkeypatch):
    import menu

    model.keep_scores(conn, dt.date.today(), [_stock("KEPT", 64.0), _coin()])
    monkeypatch.setattr(model, "score_today", lambda *a, **k: 1 / 0)
    monkeypatch.setattr("positions.check_exits", lambda c, *a, **k: [])
    menu.show_signals(conn)
    out = capsys.readouterr().out
    assert "🟢 KEPT 64" in out and "CRYPTO:BTC" in out
    assert "ZeroDivisionError" not in out and "Считаю оценки" not in out


def test_menu_signals_score_afresh_when_only_another_days_scores_are_kept(conn, capsys, monkeypatch):
    import menu

    model.keep_scores(conn, dt.date.today() - dt.timedelta(days=1), [_stock("OLD", 64.0)])
    monkeypatch.setattr(model, "score_today", lambda c, *a, **k: [_stock("NEW", 61.0)])
    monkeypatch.setattr("positions.check_exits", lambda c, *a, **k: [])
    menu.show_signals(conn)
    out = capsys.readouterr().out
    assert "NEW 61" in out and "OLD" not in out and "Считаю оценки" in out


def test_menu_option_3_shows_your_portfolio_as_plain_text(conn, capsys, monkeypatch):
    """«3) Мой портфель» is the /portfolio text -- your Trading 212 account and what you recorded with /bought --
    without markup."""
    import menu
    import t212_account

    monkeypatch.setattr(t212_account, "portfolio_view", lambda c, today, **k: t212_account.PortfolioView(
        [], error="ключ Trading 212 не задан", hint="положите ключ в .env"))
    monkeypatch.setattr("positions.last_close", lambda ticker, source=None: 50.0)
    monkeypatch.setattr("prices._closes", lambda symbol, days: [])
    positions.open_position(conn, "GRAB", 40.0)
    menu.show_portfolio(conn)
    out = capsys.readouterr().out
    assert "💼 Trading 212\n⚠️ Ключ Trading 212 не задан\nположите ключ в .env" in out
    assert "✍️ Вне Trading 212" in out and "• GRAB: вход 40,00" in out and "сейчас 50,00 (+25,0%)" in out
    assert "<b>" not in out and "&amp;" not in out
    assert "Сигнал на продажу придёт сразу. /sold TICKER — закрыть." in out
    assert "модел" not in out.lower() and "paper.py" not in out


def test_menu_option_3_with_nothing_held_says_how_to_add_a_position(conn, capsys, monkeypatch):
    import menu
    import t212_account

    monkeypatch.setattr(t212_account, "portfolio_view", lambda c, today, **k: t212_account.PortfolioView([]))
    menu.show_portfolio(conn)
    assert "Ваших позиций нет. Купили? /bought TICKER [цена]" in capsys.readouterr().out


def test_menu_option_3_survives_a_failure(conn, capsys, monkeypatch):
    import menu
    import t212_account

    def boom(c, today, **k):
        raise RuntimeError("offline")
    monkeypatch.setattr(t212_account, "portfolio_view", boom)
    menu.show_portfolio(conn)
    assert "Ошибка: RuntimeError: offline" in capsys.readouterr().out


def test_the_menu_dispatches_3_to_the_portfolio(conn, capsys, monkeypatch):
    import menu

    answers = iter(["3", "0"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(menu.db, "connect", lambda path: conn)
    shown = []
    monkeypatch.setattr(menu, "show_portfolio", lambda c: shown.append(c))
    menu.main()
    assert shown == [conn] and "3) Мой портфель" in capsys.readouterr().out


def test_menu_labels():
    import inspect

    import menu
    src = inspect.getsource(menu.main)
    assert '"1) Сигналы"' in src and '"3) Мой портфель"' in src and '"4) Спросить аналитика"' in src
    assert "Модельный портфель" not in src and "Бумажный портфель" not in src
    assert not hasattr(menu, "show_paper") and not hasattr(menu, "paper_report")
