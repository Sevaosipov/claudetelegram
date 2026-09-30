"""paper_report.py: one book's detail, the monthly Telegram report and the paper.py CLI.
The model summary and its statistics are in test_model_views.py."""
from __future__ import annotations

import datetime as dt

import db
import model
import paper
import paper_report

TODAY = dt.date(2026, 10, 5)
S, C = model.STOCK_BOOK, model.CRYPTO_BOOK


def _start(conn, days_ago):
    model.create_books(conn, TODAY - dt.timedelta(days=days_ago))


def _archive(conn, code="R1-E1", start="2026-08-01"):
    """An old book, as an earlier database holds it."""
    conn.execute("INSERT INTO paper_books (code, sleeve, start_date, start_eur, cash_eur, bench_symbol) "
                 "VALUES (?,?,?,?,?,?)", (code, "stock", start, 80_000.0, 80_000.0, "SPY"))
    conn.commit()


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


def test_book_detail_lists_positions_trades_and_skips(conn):
    _start(conn, 30)
    _closed_trades(conn, S, 1)
    paper.place_buy(conn, S, "DE0007164600", "BAFIN", "Сильный: BAFIN, X", TODAY, 8_000.0,
                    max_positions=10)
    text = paper_report.format_book(conn, S)
    assert text.startswith("MODEL-S — открытые позиции:")
    assert "T0" in text and "90 дн. в позиции" in text and "нет котировки" in text


def test_book_detail_shows_entries_and_results(conn):
    _start(conn, 30)
    conn.execute(
        "INSERT INTO paper_positions (book, ticker, source, symbol, currency, fill_date, cost_eur, "
        "net_eur, entry_close, entry_fx, reason, last_value) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (S, "AAA", "SEC", "AAA", "USD", "2026-09-20", 8000, 7980, 123.45, 1.16, "Сильный", 8400))
    _closed_trades(conn, S, 1)                                      # 8000 -> 8100
    text = paper_report.format_book(conn, S)
    assert "вход 123.45" in text
    assert "+1.2% (+€100)" in text


def test_book_detail_works_for_an_archived_code(conn):
    _start(conn, 30)
    _archive(conn, "R1-E1")
    _closed_trades(conn, "R1-E1", 1)
    text = paper_report.format_book(conn, "R1-E1")
    assert text.startswith("R1-E1 — открытые позиции:") and "T0" in text
    assert "тень" not in text


def test_book_detail_of_an_empty_book(conn):
    _start(conn, 30)
    text = paper_report.format_book(conn, C)
    assert text.startswith("MODEL-C — открытые позиции:") and text.count("  нет") == 2


def test_monthly_report_goes_once_a_month_and_not_in_the_start_month(conn):
    sent = []
    model.create_books(conn, dt.date(2026, 9, 28))
    _equity(conn, S, [(7, 70_000, 70_000)])
    _equity(conn, C, [(7, 30_000, 30_000)])
    assert paper_report.maybe_send_monthly_report(conn, dt.date(2026, 9, 30), sent.append) is False
    assert paper_report.maybe_send_monthly_report(conn, dt.date(2026, 10, 1),
                                                  lambda t: sent.append(t) or True) is True
    assert paper_report.maybe_send_monthly_report(conn, dt.date(2026, 10, 2),
                                                  lambda t: sent.append(t) or True) is False
    assert len(sent) == 1 and "Модельный портфель" in sent[0] and "за месяц" in sent[0]
    assert "<b>Модельный портфель" in sent[0]                       # sent as Telegram HTML


def test_monthly_report_is_keyed_to_the_models_start_not_the_archives(conn):
    sent = []
    _archive(conn, "R1-E1", start="2026-08-01")                      # older than the model
    model.create_books(conn, dt.date(2026, 10, 1))
    assert paper_report.maybe_send_monthly_report(conn, dt.date(2026, 10, 3),
                                                  lambda t: sent.append(t) or True) is False
    assert sent == []


def test_monthly_report_waits_for_the_model(conn):
    _archive(conn, "R1-E1", start="2026-08-01")
    assert paper_report.maybe_send_monthly_report(conn, TODAY, lambda t: True) is False


def test_monthly_report_needs_the_models_statistics(conn):
    """A stock book without its crypto book (a half-created model) has no statistics: nothing
    is sent -- not the «ещё не запущен» line as a monthly report."""
    sent = []
    conn.execute("INSERT INTO paper_books (code, sleeve, start_date, start_eur, cash_eur, bench_symbol) "
                 "VALUES (?,?,?,?,?,?)", (S, "stock", "2026-08-01", 70_000.0, 70_000.0, "SPY"))
    conn.commit()
    assert paper_report.model_stats(conn, TODAY) is None
    assert paper_report.maybe_send_monthly_report(conn, TODAY, lambda t: sent.append(t) or True) is False
    assert sent == []


def test_a_failed_send_is_retried_the_next_run(conn):
    _start(conn, 60)
    assert paper_report.maybe_send_monthly_report(conn, TODAY, lambda t: False) is False
    assert paper_report.maybe_send_monthly_report(conn, TODAY, lambda t: True) is True


def test_cli_shows_a_model_book_and_an_archived_one(conn, monkeypatch, capsys):
    _start(conn, 10)
    _archive(conn, "R1-E1")
    monkeypatch.setattr(db, "connect", lambda path: conn)
    assert paper.main(["model-s"]) == 0 and "MODEL-S" in capsys.readouterr().out
    assert paper.main(["R1-E1"]) == 0 and "R1-E1" in capsys.readouterr().out


def test_cli_rejects_an_unknown_book_and_lists_the_ones_it_has(conn, monkeypatch, capsys):
    _start(conn, 10)
    _archive(conn, "R1-E1")
    monkeypatch.setattr(db, "connect", lambda path: conn)
    assert paper.main(["X-9"]) == 2
    out = capsys.readouterr().out
    assert "Нет такой книги" in out and "MODEL-S" in out and "MODEL-C" in out and "R1-E1" in out


def test_cli_does_not_know_a_book_the_database_lacks(conn, monkeypatch, capsys):
    monkeypatch.setattr(db, "connect", lambda path: conn)
    assert paper.main(["MODEL-S"]) == 2 and "Нет такой книги" in capsys.readouterr().out


def test_cli_prints_the_model_summary_without_a_code(conn, monkeypatch, capsys):
    _start(conn, 10)
    monkeypatch.setattr(db, "connect", lambda path: conn)
    assert paper.main([]) == 0 and "Модельный портфель — день" in capsys.readouterr().out


def test_report_no_longer_depends_on_the_old_book_list():
    import inspect
    source = inspect.getsource(paper_report)
    for name in ("paper.Book", "paper.BOOKS", "BOOK_BY_CODE", "_GROUPS", "_BENCH_NAME"):
        assert name not in source
