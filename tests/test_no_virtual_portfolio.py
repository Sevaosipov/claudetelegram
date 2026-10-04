"""The virtual model portfolio is gone (spec 2026-10-04-remove-model-portfolio.md): nothing imports paper or
paper_report, no production code reads or writes a paper_* table, and the flows that replaced it leave the old
tables' rows exactly as they were. Offline -- sources are read as text and syntax trees; the flows run on
in-memory databases with stub seams."""
from __future__ import annotations

import ast
import datetime as dt
import importlib.util
import pathlib
import re
import types

import pytest

import analyst
import bot
import db
import menu
import model
import model_score
import positions
import research
import signals_weekly
import t212_account
import telegram_notify
import weekly

ROOT = pathlib.Path(__file__).resolve().parent.parent
TABLES = ("paper_books", "paper_orders", "paper_positions", "paper_equity")
FRI = dt.date(2026, 10, 9)


def _sources(*, tests: bool) -> list[pathlib.Path]:
    """Every .py file of the project (not a hidden folder, not a cache), the tests' or the rest."""
    found = []
    for path in sorted(ROOT.rglob("*.py")):
        parts = path.relative_to(ROOT).parts
        if any(part.startswith(".") or part == "__pycache__" for part in parts):
            continue
        if (parts[0] == "tests") == tests:
            found.append(path)
    return found


# ------------------------------------------------------------------ nothing imports the engine
def test_the_engine_and_its_report_are_gone():
    assert not (ROOT / "paper.py").exists() and not (ROOT / "paper_report.py").exists()
    assert importlib.util.find_spec("paper") is None and importlib.util.find_spec("paper_report") is None


def test_nothing_in_the_repository_imports_paper_or_paper_report():
    gone = {"paper", "paper_report"}
    offenders = []
    for path in _sources(tests=False) + _sources(tests=True):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name.split(".")[0] in gone for name in names):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == []


def test_no_test_patches_the_engine_by_name():
    """A string target like monkeypatch.setattr("paper._closes", ...) would only fail when it runs."""
    pattern = re.compile(r"""setattr\(\s*["']paper(?:_report)?\.\w""")
    offenders = []
    for path in _sources(tests=True):
        if path.name == pathlib.Path(__file__).name:            # this file names the pattern
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{n}")
    assert offenders == []


# ------------------------------------------------------------------ nothing reads or writes the tables
def test_no_production_code_reads_or_writes_a_paper_table():
    """db.py may name the tables where it creates them and where it adds a column an old database lacks;
    nothing else may -- no INSERT, UPDATE, DELETE, SELECT, DROP or ALTER on them."""
    token = re.compile(r"\bpaper_(?:books|orders|positions|equity)\b")
    offenders = []
    for path in _sources(tests=False):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not token.search(line):
                continue
            plain = line.strip()
            allowed = path.name == "db.py" and (plain.startswith("CREATE TABLE IF NOT EXISTS paper_")
                                                or plain.startswith('("paper_') or plain.startswith(("--", "#")))
            if not allowed:
                offenders.append(f"{path.relative_to(ROOT)}:{n}: {plain}")
    assert offenders == []


def test_the_schema_still_creates_the_four_archive_tables_and_adds_their_late_columns():
    schema = db.SCHEMA
    for table in TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table} (" in schema, table
    assert ("paper_orders", "stop_pct", "REAL") in db._ADDED_COLUMNS
    assert ("paper_positions", "score", "REAL") in db._ADDED_COLUMNS


def test_the_engine_names_are_gone_from_the_modules_that_used_them():
    for name in ("run", "Trade", "DayReport", "create_books", "model_value", "BOOKS", "STOCK_BOOK", "CRYPTO_BOOK",
                 "STOCK_START_EUR", "CRYPTO_START_EUR", "stock_exit_reason", "coin_exit_reason",
                 "MAX_STOCK_POSITIONS", "MAX_PER_SECTOR", "REBUY_COOLDOWN_DAYS", "COIN_REBUY_COOLDOWN_DAYS",
                 "MIN_FILL_FRACTION", "_default_sector", "_buy_stocks", "_buy_coins", "_sell_exits"):
        assert not hasattr(model, name), f"model.{name}"
    for name in ("_run_model", "maybe_send_monthly_report", "paper_report"):
        assert not hasattr(bot, name), f"bot.{name}"
    assert not hasattr(positions, "_model_names") and not hasattr(model_score, "position_size")
    assert not hasattr(menu, "show_paper") and not hasattr(analyst, "paper") and not hasattr(analyst, "paper_report")


# ------------------------------------------------------------------ the flows leave the rows alone
def _seed_old_books(conn):
    conn.execute("INSERT INTO paper_books (code, sleeve, start_date, start_eur, cash_eur, bench_symbol, "
                 "bench_start_fx) VALUES ('MODEL-S', 'stock', '2026-09-01', 70000, 61000, 'SPY', 1.16)")
    conn.execute("INSERT INTO paper_books (code, sleeve, start_date, start_eur, cash_eur, bench_symbol) "
                 "VALUES ('R1-E1', 'stock', '2026-08-01', 80000, 80000, 'SPY')")
    conn.execute("INSERT INTO paper_orders (book, ticker, source, side, amount_eur, reason, created, status, "
                 "insiders, stop_pct, score) VALUES ('MODEL-S', 'AAA', 'SEC', 'buy', 9000, 'балл 64: x', "
                 "'2026-10-08', 'pending', '[]', 0.1, 64)")
    conn.execute("INSERT INTO paper_orders (book, ticker, source, side, position_id, reason, created, status) "
                 "VALUES ('MODEL-S', 'OLD', 'SEC', 'sell', 1, 'стоп', '2026-10-08', 'pending')")
    conn.execute("INSERT INTO paper_positions (book, ticker, source, symbol, currency, fill_date, cost_eur, "
                 "net_eur, entry_close, entry_fx, reason, last_value, stop_pct, score) VALUES "
                 "('MODEL-S', 'OLD', 'SEC', 'OLD', 'USD', '2026-09-25', 9000, 8978, 10, 1.16, 'балл 64', 8400, "
                 "0.12, 64)")
    conn.execute("INSERT INTO paper_equity (book, date, value, cash, bench) VALUES "
                 "('MODEL-S', '2026-10-08', 70500, 61000, 70200)")
    conn.commit()


def _dump(conn) -> dict:
    return {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() for t in TABLES}


def test_old_databases_keep_their_paper_rows_through_a_connect(tmp_path):
    path = tmp_path / "old.db"
    first = db.connect(path)
    _seed_old_books(first)
    before = _dump(first)
    first.close()
    again = db.connect(path)                                  # the schema and the migration run again
    assert _dump(again) == before and all(before[t] for t in TABLES)


def _score(ticker, total=70.0):
    return model_score.StockScore(
        ticker=ticker, source="SEC", company=f"{ticker} Corp", signal=None, insiders=52.0, triggers=5.0,
        momentum=7.0, news=0.0, total=total, decision=model_score.BUY, reasons=["3 инсайдера из руководства"],
        block=None, untradeable=None, t212=True, stop_pct=0.10, last_close=10.0)


def test_scoring_picking_sending_and_every_view_leave_the_old_rows_exactly_as_they_were(conn, monkeypatch):
    _seed_old_books(conn)
    before = _dump(conn)

    # the day is scored (no network: every seam is a stub) ...
    report = model.score_day(conn, FRI, fetch=lambda symbol, days=None: [], signals=[],
                             news_fn=lambda t, s: [], trend_fn=lambda c, s: None,
                             t212=types.SimpleNamespace(can_buy=lambda t, s: True))
    assert report.complete is True
    # ... the week is picked and sent ...
    report.scored.append(_score("AAA"))
    assert bot._pick_week(conn, FRI, report) is True
    monkeypatch.setattr(telegram_notify, "send_text", lambda text: True)
    assert bot._send_weekly(conn, FRI) is True
    assert conn.execute("SELECT ticker FROM buy_signals").fetchall() == [("AAA",)]
    weekly.format_summary(conn, FRI)
    weekly.week_signals(conn, FRI, [signals_weekly.pick_record(_score("AAA"))])
    # ... and the views read the portfolio, the signals and the context
    monkeypatch.setattr(research, "build", lambda c, t: {"ticker": t})
    monkeypatch.setattr(research, "format_brief", lambda rep: f"БРИФ {rep['ticker']}")
    monkeypatch.setattr(t212_account, "portfolio_view", lambda c, today, **k: t212_account.PortfolioView([]))
    analyst.portfolio(conn, scored=report.scored)
    analyst.context(conn, "OLD", scored=report.scored)
    analyst.context(conn, "AAA", scored=report.scored)
    positions.portfolio_rows(conn, FRI)
    t212_account.portfolio_text(conn, FRI)
    menu.show_portfolio(conn)

    assert _dump(conn) == before
