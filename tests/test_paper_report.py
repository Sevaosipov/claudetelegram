"""paper_report.py: what the paper portfolio shows -- statistics, the success line,
the menu/CLI text and the monthly Telegram report."""
from __future__ import annotations

import datetime as dt
import re

import pytest

import db
import paper
import paper_report

TODAY = dt.date(2026, 10, 5)


def _start(conn, days_ago):
    paper.create_books(conn, TODAY - dt.timedelta(days=days_ago))


def _equity(conn, code, rows):
    """rows: [(days_ago, value, bench)]"""
    for days_ago, value, bench in rows:
        conn.execute("INSERT OR REPLACE INTO paper_equity (book, date, value, cash, bench) "
                     "VALUES (?,?,?,?,?)",
                     (code, (TODAY - dt.timedelta(days=days_ago)).isoformat(), value, 0.0, bench))
    conn.commit()


def _closed_trades(conn, code, n):
    for i in range(n):
        conn.execute(
            "INSERT INTO paper_positions (book, ticker, source, symbol, currency, fill_date, cost_eur, "
            "net_eur, entry_close, entry_fx, closed_date, close_reason, proceeds_eur) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (code, f"T{i}", "SEC", f"T{i}", "USD", "2026-06-01", 8000, 7980, 10, 1.16, "2026-07-01",
             "90 дн. в позиции", 8100))
    conn.commit()


def test_running_before_day_182(conn):
    _start(conn, 100)
    _equity(conn, "R1-E1", [(100, 80_000, 80_000), (0, 88_000, 84_000)])
    s = paper_report.stats(conn, paper.BOOK_BY_CODE["R1-E1"], TODAY)
    assert (s["day"], s["status"]) == (100, "идёт")
    assert s["ret"] == pytest.approx(0.10) and s["bench_ret"] == pytest.approx(0.05)


def test_passing_needs_a_better_return_a_smaller_drop_and_20_trades(conn):
    _start(conn, 190)
    _equity(conn, "R1-E1", [(190, 80_000, 80_000), (100, 76_000, 64_000), (0, 90_000, 84_000)])
    _closed_trades(conn, "R1-E1", 20)
    assert paper_report.stats(conn, paper.BOOK_BY_CODE["R1-E1"], TODAY)["status"] == "пройдено"


def test_a_stock_book_with_too_few_trades_waits_for_them(conn):
    _start(conn, 190)
    _equity(conn, "R1-E1", [(190, 80_000, 80_000), (100, 76_000, 64_000), (0, 90_000, 84_000)])
    _closed_trades(conn, "R1-E1", 19)
    assert paper_report.stats(conn, paper.BOOK_BY_CODE["R1-E1"], TODAY)["status"] == \
        "идёт (сделок 19 из 20)"


def test_a_crypto_book_needs_no_trade_minimum(conn):
    _start(conn, 190)
    _equity(conn, "C-A", [(190, 20_000, 20_000), (100, 19_000, 15_000), (0, 24_000, 22_000)])
    assert paper_report.stats(conn, paper.BOOK_BY_CODE["C-A"], TODAY)["status"] == "пройдено"


def test_a_deeper_drop_than_the_benchmark_does_not_pass(conn):
    _start(conn, 190)
    _equity(conn, "C-A", [(190, 20_000, 20_000), (100, 10_000, 18_000), (0, 24_000, 22_000)])
    assert paper_report.stats(conn, paper.BOOK_BY_CODE["C-A"], TODAY)["status"] == "не пройдено"


def test_the_shadow_is_always_a_shadow(conn):
    _start(conn, 190)
    assert paper_report.stats(conn, paper.BOOK_BY_CODE["R1-E1-AN"], TODAY)["status"] == "тень"


def test_summary_lists_every_book_under_its_sleeve(conn):
    _start(conn, 47)
    _equity(conn, "R1-E1", [(47, 80_000, 80_000), (0, 84_320, 82_480)])
    text = paper_report.format_summary(conn, TODAY)
    assert "Бумажный портфель — день 47 из 182" in text
    assert "АКЦИИ (S&P 500:" in text and "КРИПТО (BTC:" in text
    assert "R1·E1 " in text and "+5.4%" in text and "(+2.3 п.п.)" in text
    assert "R1·E1+аналитики" in text and "тень" in text and "C-B" in text


def test_summary_before_the_first_run(conn):
    assert "ещё не запущен" in paper_report.format_summary(conn, TODAY)


def test_book_detail_lists_positions_trades_and_skips(conn):
    _start(conn, 30)
    _closed_trades(conn, "R1-E2", 1)
    paper.place_buy(conn, "R1-E2", "DE0007164600", "BAFIN", "Сильный: BAFIN, X", TODAY, 8_000.0,
                    max_positions=10)
    text = paper_report.format_book(conn, "R1-E2")
    assert "R1·E2" in text and "T0" in text and "90 дн. в позиции" in text and "нет котировки" in text


def test_book_detail_marks_the_shadow_and_shows_entries_and_results(conn):
    _start(conn, 30)
    conn.execute(
        "INSERT INTO paper_positions (book, ticker, source, symbol, currency, fill_date, cost_eur, "
        "net_eur, entry_close, entry_fx, reason, last_value) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("R1-E1-AN", "AAA", "SEC", "AAA", "USD", "2026-09-20", 8000, 7980, 123.45, 1.16, "Сильный", 8400))
    _closed_trades(conn, "R1-E1-AN", 1)                             # 8000 -> 8100
    text = paper_report.format_book(conn, "R1-E1-AN")
    assert text.startswith("R1·E1+аналитики (тень) — открытые позиции:")
    assert "вход 123.45" in text
    assert "+1.2% (+€100)" in text
    assert "(тень)" not in paper_report.format_book(conn, "R1-E1")


def test_summary_after_day_182_says_the_verdict_is_in(conn):
    _start(conn, 190)
    text = paper_report.format_summary(conn, TODAY)
    assert "день 190 (итог — после 182 дней)" in text and "из 182" not in text


def test_monthly_html_summary_aligns_books_and_shows_the_benchmarks_month(conn):
    paper.create_books(conn, dt.date(2026, 8, 15))
    for day, value, bench in (("2026-08-31", 80_000, 80_000), ("2026-09-30", 82_000, 84_000)):
        conn.execute("INSERT INTO paper_equity (book, date, value, cash, bench) VALUES (?,?,?,?,?)",
                     ("R1-E1", day, value, 0.0, bench))
    conn.commit()
    text = paper_report.format_summary(conn, TODAY, monthly=True, html=True)
    assert "<b>АКЦИИ (S&amp;P 500: +5.0%, худшая просадка +0.0%, за месяц +5.0%)</b>" in text
    assert "за месяц —)</b>" in text                                   # КРИПТО: no benchmark yet
    assert text.count("<pre>") == 2 and text.count("</pre>") == 2
    assert "<pre>R1·E1 " in text and re.search(r"R1·E1 .* за месяц +\+2\.5%", text)


def test_monthly_report_goes_once_a_month_and_not_in_the_start_month(conn):
    sent = []
    paper.create_books(conn, dt.date(2026, 9, 28))
    _equity(conn, "R1-E1", [(7, 80_000, 80_000)])
    assert paper_report.maybe_send_monthly_report(conn, dt.date(2026, 9, 30), sent.append) is False
    assert paper_report.maybe_send_monthly_report(conn, dt.date(2026, 10, 1),
                                                  lambda t: sent.append(t) or True) is True
    assert paper_report.maybe_send_monthly_report(conn, dt.date(2026, 10, 2),
                                                  lambda t: sent.append(t) or True) is False
    assert len(sent) == 1 and "за месяц" in sent[0]


def test_cli_shows_a_book_and_rejects_an_unknown_one(conn, monkeypatch, capsys):
    _start(conn, 10)
    monkeypatch.setattr(db, "connect", lambda path: conn)
    assert paper.main(["R1-E1"]) == 0 and "R1·E1" in capsys.readouterr().out
    assert paper.main(["X-9"]) == 2 and "Нет такой книги" in capsys.readouterr().out


def test_menu_shows_the_paper_portfolio(conn, capsys):
    import menu
    _start(conn, 10)
    menu.show_paper(conn)
    assert "Бумажный портфель" in capsys.readouterr().out
