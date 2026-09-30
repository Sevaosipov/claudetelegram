"""analyst.py: the Claude analyst's plumbing -- the command it runs, the context it prints, the
queue and the prompt files. Offline: Claude itself is never started (`run=` is stubbed), the
dossier and the news are stubbed, and scores are hand-built."""
from __future__ import annotations

import datetime as dt
import fcntl
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time

import pytest

import analyst
import db
import model
import model_score
import paper
import research

ROOT = pathlib.Path(__file__).resolve().parent.parent
REAL_LOAD_ENV = analyst.load_env        # the autouse fixture below replaces the module's own
S, C = model.STOCK_BOOK, model.CRYPTO_BOOK


# ------------------------------------------------------------------ builders
def _stock(ticker="NVDA", total=64.0, decision=model_score.BUY, **kw):
    base = dict(ticker=ticker, source="SEC", company=f"{ticker} Corp", signal=None, insiders=52.0,
                triggers=5.0, momentum=7.0, news=0.0, total=total, decision=decision,
                reasons=["3 инсайдера", "цена растёт"], block=None, untradeable=None, t212=True,
                stop_pct=0.10, last_close=10.0)
    base.update(kw)
    return model_score.StockScore(**base)


def _coin(coin="BTC", total=60.0, decision=model_score.BUY, **kw):
    base = dict(coin=coin, ticker=f"CRYPTO:{coin}", trend=45.0, flows=15.0, news=0.0, total=total,
                trend_up=True, trend_down=False, caution=None, block=None, decision=decision,
                reasons=["выше 100-дн. средней"], stop_pct=0.20, last_close=100.0)
    base.update(kw)
    return model_score.CoinScore(**base)


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    """No test touches the repository's data/ directory, its .env or the network: the lock
    lives in tmp_path, .env is not read, and a stock the model has no signal on
    has no prices and no headlines unless the test says otherwise."""
    monkeypatch.setattr(analyst, "LOCK_FILE", tmp_path / "analyst.lock")
    monkeypatch.setattr(analyst, "load_env", lambda *a, **k: [])
    monkeypatch.setattr(paper, "_closes", lambda symbol, days: [])
    monkeypatch.setattr(model, "default_news", lambda ticker, source: [])


@pytest.fixture
def dossier(monkeypatch):
    """research.build/format_brief stubbed; returns the tickers build was asked for."""
    asked = []

    def build(conn, text):
        asked.append(text)
        return {"ticker": text}

    monkeypatch.setattr(research, "build", build)
    monkeypatch.setattr(research, "format_brief", lambda rep: f"БРИФ {rep['ticker']}")
    return asked


def _stub_scoring(monkeypatch, scores, seen=None):
    """model.candidate_signals / score_today stubbed; `seen` collects what they were asked."""
    seen = {} if seen is None else seen

    def candidates(conn, today, *, tickers=None):
        seen["tickers"] = tickers
        return ["sig"]

    def score(conn, today=None, *, signals=None, **kw):
        seen["signals"] = signals
        return scores

    monkeypatch.setattr(model, "candidate_signals", candidates)
    monkeypatch.setattr(model, "score_today", score)
    return seen


def _paper_position(conn, code, ticker, *, fill="2026-09-25", cost=8_000.0, last=8_400.0, stop=0.10,
                    source="SEC"):
    conn.execute(
        "INSERT INTO paper_positions (book, ticker, source, symbol, currency, fill_date, cost_eur, "
        "net_eur, entry_close, entry_fx, reason, last_value, stop_pct, score) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (code, ticker, source, ticker, "USD", fill, cost, cost * 0.998, 10.0, 1.16, "балл 64", last,
         stop, 64.0))
    conn.commit()


def _paper_order(conn, code, ticker, created, side="buy", score=64.0):
    conn.execute(
        "INSERT INTO paper_orders (book, ticker, source, side, amount_eur, reason, created, status, "
        "score) VALUES (?,?,?,?,?,?,?,?,?)",
        (code, ticker, "SEC", side, 9_000.0, "балл 64", created, "pending", score))
    conn.commit()


# ------------------------------------------------------------------ the command
def test_claude_command_has_the_flags_and_every_allowed_tool(monkeypatch, tmp_path):
    monkeypatch.setattr(analyst, "CLAUDE_BIN", tmp_path / "claude")
    cmd = analyst.claude_command("вопрос")
    assert cmd[:3] == [str(tmp_path / "claude"), "-p", "вопрос"]
    assert cmd[3] == "--allowedTools" and cmd.count("--allowedTools") == 1
    assert cmd[4:] == list(analyst.ALLOWED_TOOLS)                  # the last flag, its tools after it
    assert "Bash(.venv/bin/python analyst.py:*)" in cmd and "Bash" not in cmd   # never the whole shell
    assert all(f"mcp__tradingview__{t}" in cmd for t in analyst.TV_TOOLS)


def test_claude_cannot_write_or_edit_anything(monkeypatch):
    """A file it could write (analyst.py, a shim in .venv) would be run by the allowed prefix."""
    cmd = analyst.claude_command("x")
    assert "--permission-mode" not in cmd and "acceptEdits" not in cmd
    assert not {"Write", "Edit", "MultiEdit", "NotebookEdit"} & set(analyst.ALLOWED_TOOLS)


def test_the_tradingview_allow_list_is_the_nine_read_and_navigate_tools():
    assert analyst.TV_TOOLS == (
        "tv_health_check", "tv_launch", "chart_get_state", "chart_set_symbol",
        "chart_set_timeframe", "quote_get", "data_get_ohlcv", "symbol_info", "symbol_search")
    assert "data_get_study_values" not in " ".join(analyst.ALLOWED_TOOLS)


def test_claude_env_prepends_the_missing_paths_once():
    env = analyst.claude_env({"PATH": "/usr/bin:/bin", "HOME": "/h"})
    assert env["PATH"] == "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
    assert env["HOME"] == "/h"
    again = analyst.claude_env(env)
    assert again["PATH"] == env["PATH"]


def test_claude_env_leaves_a_path_that_already_has_one_alone():
    env = analyst.claude_env({"PATH": "/opt/homebrew/bin:/usr/bin"})
    assert env["PATH"] == "/usr/local/bin:/opt/homebrew/bin:/usr/bin"


def test_claude_env_does_not_touch_its_input_or_os_environ(monkeypatch):
    base = {"PATH": "/usr/bin"}
    analyst.claude_env(base)
    assert base == {"PATH": "/usr/bin"}
    monkeypatch.setenv("PATH", "/usr/bin")
    assert analyst.claude_env()["PATH"].endswith("/usr/bin")
    assert analyst.os.environ["PATH"] == "/usr/bin"


def test_claude_env_without_a_path_still_gets_the_extras():
    assert analyst.claude_env({})["PATH"].split(":")[:2] == ["/usr/local/bin", "/opt/homebrew/bin"]


# ------------------------------------------------------------------ tickers
@pytest.mark.parametrize("text,expected", [
    ("NVDA", "NVDA"), ("$NVDA", "NVDA"), ("EQNR.OL", "EQNR.OL"), ("VOLV-B.ST", "VOLV-B.ST"),
    (" nvda \n", "NVDA"), ("brk.b", "BRK.B"),
])
def test_valid_ticker_accepts_tickers(text, expected):
    assert analyst.valid_ticker(text) == expected


@pytest.mark.parametrize("text", ["rm -rf", "NV DA", "a;b", "", "   ", "$", "NVDA;ls", "$(id)",
                                  "`id`", "A" * 16, "NVDA\nls", "-rf"])
def test_valid_ticker_rejects_everything_else(text):
    assert analyst.valid_ticker(text) is None


# ------------------------------------------------------------------ the queue
def test_enqueue_question_returns_the_row_id_and_stores_a_question(conn):
    qid = db.enqueue_question(conn, "что с Nvidia?")
    assert isinstance(qid, int)
    kind, question, ticker = conn.execute(
        "SELECT kind, question, ticker FROM claude_analysis_queue WHERE id = ?", (qid,)).fetchone()
    assert (kind, question, ticker) == ("question", "что с Nvidia?", "ВОПРОС")
    assert db.pending_analysis(conn) == [(qid, "ВОПРОС")]


def test_questions_are_never_deduplicated(conn):
    a = db.enqueue_question(conn, "один и тот же вопрос")
    b = db.enqueue_question(conn, "один и тот же вопрос")
    assert a != b
    assert len(db.pending_analysis(conn)) == 2


def test_enqueue_analysis_still_dedupes_tickers_but_not_against_questions(conn):
    db.enqueue_analysis(conn, "$NVDA")
    db.enqueue_analysis(conn, "$NVDA")
    assert len(db.pending_analysis(conn)) == 1
    conn.execute("UPDATE claude_analysis_queue SET processed_at = datetime('now')")
    db.enqueue_question(conn, "?")
    db.enqueue_analysis(conn, "ВОПРОС")     # a ticker row is only compared with ticker rows
    assert [t for _i, t in db.pending_analysis(conn)] == ["ВОПРОС", "ВОПРОС"]


def test_a_ticker_row_has_kind_ticker_and_no_question(conn):
    db.enqueue_analysis(conn, "$NVDA")
    assert conn.execute("SELECT kind, question FROM claude_analysis_queue").fetchone() == (
        "ticker", None)


def test_queued_question_returns_the_text_only_for_a_question_row(conn):
    qid = db.enqueue_question(conn, "как дела у рынка?")
    db.enqueue_analysis(conn, "AAPL")
    (tid, _t) = next(r for r in db.pending_analysis(conn) if r[1] == "AAPL")
    assert db.queued_question(conn, qid) == "как дела у рынка?"
    assert db.queued_question(conn, tid) is None
    assert db.queued_question(conn, 9999) is None


def test_the_new_columns_are_added_to_an_old_queue_table(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE claude_analysis_queue (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "ticker TEXT NOT NULL, requested_at TEXT DEFAULT (datetime('now')), processed_at TEXT)")
    old.execute("INSERT INTO claude_analysis_queue (ticker) VALUES ('AAPL')")
    old.commit()
    old.close()
    conn = db.connect(path)
    assert conn.execute("SELECT kind, question FROM claude_analysis_queue").fetchone() == (
        "ticker", None)
    assert db.enqueue_question(conn, "?") == 2


# ------------------------------------------------------------------ process_queue
def test_process_queue_with_nothing_queued_runs_nothing(conn):
    calls = []
    assert analyst.process_queue(conn, run=lambda *a, **k: calls.append((a, k))) == 0
    assert calls == []


def test_process_queue_with_a_row_runs_claude_once_with_the_prompt(conn, monkeypatch):
    db.enqueue_analysis(conn, "$NVDA")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return _Proc(returncode=3)

    prompt = (ROOT / "claude_analysis_prompt.txt").read_text(encoding="utf-8")
    assert analyst.process_queue(conn, run=run) == 3
    [(argv, kwargs)] = calls
    assert argv == analyst.claude_command(prompt)
    assert prompt in argv
    assert kwargs["cwd"] == analyst.BASE_DIR
    assert kwargs["check"] is False
    assert kwargs["timeout"] == analyst.CLAUDE_TIMEOUT_SECONDS == 900
    assert kwargs["start_new_session"] is True
    assert set(analyst.EXTRA_PATH) <= set(kwargs["env"]["PATH"].split(":"))


def test_process_queue_with_a_question_also_runs(conn):
    db.enqueue_question(conn, "?")
    calls = []
    analyst.process_queue(conn, run=lambda *a, **k: calls.append(a) or _Proc())
    assert len(calls) == 1


def test_process_queue_reads_the_prompt_when_called(conn, monkeypatch, tmp_path):
    (tmp_path / "claude_analysis_prompt.txt").write_text("ПРОМПТ-ИЗ-ФАЙЛА", encoding="utf-8")
    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    db.enqueue_analysis(conn, "AAPL")
    seen = []
    analyst.process_queue(conn, run=lambda argv, **k: seen.append(argv) or _Proc())
    assert "ПРОМПТ-ИЗ-ФАЙЛА" in seen[0]


def test_process_queue_without_the_binary_returns_127(conn, capsys):
    db.enqueue_analysis(conn, "AAPL")

    def run(*a, **k):
        raise FileNotFoundError(2, "No such file", "claude")

    assert analyst.process_queue(conn, run=run) == 127
    assert "claude не найден" in capsys.readouterr().out


# ------------------------------------------------------------------ ask
def test_ask_strips_bold_tags_and_returns_the_returncode(capsys):
    seen = []

    def run(argv, **kwargs):
        seen.append((argv, kwargs))
        return _Proc(0, "<b>NVDA</b>\n• растёт\n🎯 Итоговый вердикт: держать\n")

    assert analyst.ask("что с NVDA?", run=run) == 0
    out = capsys.readouterr().out
    assert "<b>" not in out and "</b>" not in out
    assert "NVDA\n• растёт" in out and "Итоговый вердикт" in out
    [(argv, kwargs)] = seen
    prompt = (ROOT / "claude_ask_prompt.txt").read_text(encoding="utf-8")
    assert argv == analyst.claude_command(prompt + "\n\nВОПРОС:\nчто с NVDA?")
    assert kwargs["cwd"] == analyst.BASE_DIR
    assert kwargs["capture_output"] is True and kwargs["text"] is True


def test_ask_returns_a_failing_returncode(capsys):
    assert analyst.ask("?", run=lambda *a, **k: _Proc(1, "")) == 1


def test_ask_with_a_missing_binary_says_so_and_returns_127(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(analyst, "CLAUDE_BIN", tmp_path / "no-such-claude")
    assert analyst.ask("?") == 127          # the real subprocess.run: nothing is started
    assert "claude не найден" in capsys.readouterr().out


def test_ask_with_a_run_that_cannot_find_the_binary(capsys):
    def run(*a, **k):
        raise FileNotFoundError(2, "No such file", "claude")

    assert analyst.ask("?", run=run) == 127
    assert "claude не найден" in capsys.readouterr().out


# ------------------------------------------------------------------ context
def test_context_prints_the_model_score_when_the_ticker_was_scored(conn, dossier):
    out = analyst.context(conn, "NVDA", scored=[_stock("NVDA", 64.0, model_score.BUY)])
    assert "МОДЕЛЬ: балл 64 — покупка" in out
    assert "3 инсайдера" in out and "цена растёт" in out
    assert "стоп −10% от максимума" in out
    assert "T212" in out
    assert "ДОСЬЕ:" in out and "БРИФ NVDA" in out


def test_context_says_there_is_no_fresh_signal_otherwise(conn, dossier):
    out = analyst.context(conn, "NVDA", scored=[_stock("AAA")])
    assert "МОДЕЛЬ: свежего сигнала за 14 дней нет" in out
    assert "МОДЕЛЬ: балл" not in out


@pytest.mark.parametrize("decision,word", [
    (model_score.BUY, "покупка"), (model_score.WATCH, "наблюдение"),
    (model_score.BLOCK, "блок"), (model_score.SKIP, "пропуск")])
def test_context_names_the_decision_in_russian(conn, dossier, decision, word):
    out = analyst.context(conn, "NVDA", scored=[_stock("NVDA", 50.0, decision)])
    assert f"— {word}" in out


def test_context_ignores_the_dollar_sign_the_queue_stores(conn, dossier):
    out = analyst.context(conn, "$NVDA", scored=[_stock("NVDA", 64.0)])
    assert "МОДЕЛЬ: балл 64" in out
    assert dossier == ["$NVDA"]             # the dossier gets the key as it is


def test_context_matches_a_coin_by_symbol_or_by_key(conn, dossier):
    for key in ("BTC", "CRYPTO:BTC", "btc"):
        out = analyst.context(conn, key, scored=[_coin("BTC", 72.0)])
        assert "МОДЕЛЬ: балл 72 — покупка" in out, key
        assert "выше 100-дн. средней" in out
    assert "МОДЕЛЬ: свежего сигнала" in analyst.context(conn, "ETH", scored=[_coin("BTC")])


def test_context_shows_why_a_score_is_only_watch_and_the_block(conn, dossier):
    out = analyst.context(conn, "NVDA", scored=[
        _stock("NVDA", 66.0, model_score.WATCH, untradeable="компания меньше €20 млн", t212=False)])
    assert "компания меньше €20 млн" in out and "нет на T212" in out
    out = analyst.context(conn, "NVDA", scored=[
        _stock("NVDA", 70.0, model_score.BLOCK, block="SEC investigation opens")])
    assert "SEC investigation opens" in out


def test_context_scores_today_when_not_given_any(conn, dossier, monkeypatch):
    _stub_scoring(monkeypatch, [_stock("NVDA", 61.0)])
    assert "МОДЕЛЬ: балл 61" in analyst.context(conn, "NVDA")


def test_context_scoring_failure_does_not_lose_the_dossier(conn, dossier, monkeypatch):
    def boom(c, *a, **k):
        raise RuntimeError("network")

    monkeypatch.setattr(model, "candidate_signals", lambda c, today, **k: [])
    monkeypatch.setattr(model, "score_today", boom)
    out = analyst.context(conn, "NVDA")
    assert "МОДЕЛЬ: не посчитана: RuntimeError" in out
    assert "БРИФ NVDA" in out


def test_context_lists_the_models_position_and_the_users(conn, dossier):
    model.create_books(conn, dt.date(2026, 9, 1))
    _paper_position(conn, S, "NVDA", fill="2026-09-25", cost=8_000.0, last=8_400.0, stop=0.12)
    _paper_position(conn, C, "CRYPTO:BTC", fill="2026-09-20", cost=5_000.0, last=4_500.0, stop=0.2)
    conn.execute("INSERT INTO positions (ticker, source, opened_at, entry_price, insiders, stop_pct) "
                 "VALUES ('NVDA', 'SEC', '2026-09-28', 100.0, '[]', 0.15)")
    conn.commit()
    out = analyst.context(conn, "$NVDA", scored=[])
    assert "MODEL-S" in out and "с 2026-09-25" in out and "+5,0%" in out and "стоп −12%" in out
    assert "/bought" in out and "2026-09-28" in out and "100" in out and "стоп −15%" in out
    coin = analyst.context(conn, "BTC", scored=[])
    assert "MODEL-C" in coin and "с 2026-09-20" in coin and "−10,0%" in coin
    none = analyst.context(conn, "AAPL", scored=[])
    assert "MODEL-S" not in none and "/bought" not in none


def test_context_not_a_ticker(conn, monkeypatch):
    def build(c, text):
        raise research.NotATicker("nope")

    monkeypatch.setattr(research, "build", build)
    out = analyst.context(conn, "!!!", scored=[])
    assert "ДОСЬЕ:" in out and "не похоже на тикер" in out


def test_context_dossier_failure_is_named_but_not_fatal(conn, monkeypatch):
    def build(c, text):
        raise RuntimeError("yahoo is down")

    monkeypatch.setattr(research, "build", build)
    out = analyst.context(conn, "NVDA", scored=[_stock("NVDA")])
    assert "досье недоступно: RuntimeError" in out
    assert "МОДЕЛЬ: балл" in out


# ------------------------------------------------------------------ portfolio / news
def test_portfolio_has_the_summary_the_watchlist_and_todays_buys(conn):
    model.create_books(conn, dt.date(2026, 9, 1))
    today = dt.date.today().isoformat()
    _paper_order(conn, S, "AAA", today)
    _paper_order(conn, C, "CRYPTO:BTC", today)
    _paper_order(conn, S, "OLD", "2020-01-01")
    _paper_order(conn, S, "SOLD", today, side="sell")
    _paper_order(conn, "R1-E1", "ARCH", today)           # an archived book is not the model
    scored = [_stock("BBB", 52.0, model_score.WATCH, reasons=["один инсайдер", "растёт"]),
              _stock("CCC", 70.0, model_score.BUY), _coin("ETH", 50.0, model_score.WATCH)]
    out = analyst.portfolio(conn, scored=scored)
    assert "Модельный портфель" in out
    assert "НАБЛЮДЕНИЕ:" in out and "BBB" in out and "52" in out and "один инсайдер" in out
    assert "ETH" in out
    watch = out.split("НАБЛЮДЕНИЕ:")[1].split("ПОКУПКИ СЕГОДНЯ:")[0]
    assert "CCC" not in watch                              # a buy is not on the watchlist
    buys = out.split("ПОКУПКИ СЕГОДНЯ:")[1]
    assert "AAA" in buys and "CRYPTO:BTC" in buys
    assert "OLD" not in buys and "SOLD" not in buys and "ARCH" not in buys


def test_portfolio_when_nothing_to_show(conn):
    out = analyst.portfolio(conn, scored=[])
    assert "НАБЛЮДЕНИЕ: нет" in out and "ПОКУПКИ СЕГОДНЯ: нет" in out


def test_portfolio_lists_at_most_ten_watch_scores(conn):
    scored = [_stock(f"W{i:02d}", 50.0 - i * 0.1, model_score.WATCH) for i in range(14)]
    out = analyst.portfolio(conn, scored=scored)
    assert "W09" in out and "W10" not in out


def test_news_prints_dated_headlines(monkeypatch):
    items = [{"title": f"Заголовок {i}", "publisher": "Reuters", "published": f"2026-09-{i + 1:02d}",
              "url": "u"} for i in range(12)]
    monkeypatch.setattr(analyst.sources, "_google_news", lambda q: items)
    lines = analyst.news("NVDA stock").splitlines()
    assert len(lines) == 10
    assert lines[0] == "01.09 · Reuters · Заголовок 0"


def test_news_failure_and_empty(monkeypatch):
    def boom(q):
        raise RuntimeError("offline")

    monkeypatch.setattr(analyst.sources, "_google_news", boom)
    assert analyst.news("x") == "новости недоступны"
    monkeypatch.setattr(analyst.sources, "_google_news", lambda q: [])
    assert analyst.news("x") == "новости недоступны"


# ------------------------------------------------------------------ the command line
@pytest.fixture
def cli(conn, monkeypatch):
    monkeypatch.setattr(analyst.db, "connect", lambda path: conn)

    def run(*argv):
        return analyst.main(list(argv))
    return run


def test_cli_rejects_a_ticker_that_looks_like_a_command(cli, dossier, capsys):
    assert cli("context", "rm -rf") == 2
    captured = capsys.readouterr()
    assert "тикер" in (captured.out + captured.err)
    assert dossier == []


def test_cli_context_prints_the_context(cli, dossier, monkeypatch, capsys):
    _stub_scoring(monkeypatch, [_stock("NVDA", 64.0)])
    assert cli("context", "$NVDA") == 0
    out = capsys.readouterr().out
    assert "МОДЕЛЬ: балл 64" in out
    assert dossier == ["NVDA"]                # validated tickers lose the "$"


def test_cli_context_by_queue_id_uses_the_queued_key_as_is(cli, conn, dossier, monkeypatch, capsys):
    _stub_scoring(monkeypatch, [_stock("NVDA", 64.0)])
    db.enqueue_analysis(conn, "$NVDA")
    [(qid, _t)] = db.pending_analysis(conn)
    assert cli("context", "--queue", str(qid)) == 0
    assert "МОДЕЛЬ: балл 64" in capsys.readouterr().out
    assert dossier == ["$NVDA"]


def test_cli_context_by_a_queue_id_that_is_not_pending(cli, dossier, capsys):
    assert cli("context", "--queue", "999") == 2
    assert dossier == []


def test_cli_context_by_the_id_of_a_question_row_is_refused(cli, conn, dossier, capsys):
    qid = db.enqueue_question(conn, "вопрос")
    assert cli("context", "--queue", str(qid)) == 2
    assert dossier == []


def test_cli_context_needs_a_ticker_or_a_queue_id(cli, capsys):
    assert cli("context") == 2


def test_cli_question_prints_the_queued_text(cli, conn, capsys):
    qid = db.enqueue_question(conn, "что там с золотом?")
    assert cli("question", str(qid)) == 0
    assert capsys.readouterr().out.strip() == "что там с золотом?"


def test_cli_question_for_a_missing_row(cli, capsys):
    assert cli("question", "42") == 2


def test_cli_portfolio_and_news(cli, monkeypatch, capsys):
    monkeypatch.setattr(model, "score_today", lambda c, *a, **k: [])
    assert cli("portfolio") == 0
    assert "НАБЛЮДЕНИЕ:" in capsys.readouterr().out
    seen = []
    monkeypatch.setattr(analyst, "news", lambda q: seen.append(q) or "новость")
    assert cli("news", "nvidia", "earnings") == 0
    assert seen == ["nvidia earnings"] and capsys.readouterr().out.strip() == "новость"


def test_cli_ask_and_process_queue(cli, conn, monkeypatch):
    asked = []
    monkeypatch.setattr(analyst, "ask", lambda q, **k: asked.append(q) or 0)
    assert cli("ask", "как", "дела?") == 0 and asked == ["как дела?"]
    monkeypatch.setattr(analyst, "process_queue", lambda c, **k: 7)
    assert cli("process-queue") == 7


def test_cli_opens_the_bots_database(monkeypatch, conn):
    paths = []
    monkeypatch.setattr(analyst.db, "connect", lambda path: paths.append(path) or conn)
    monkeypatch.setattr(analyst, "process_queue", lambda c, **k: 0)
    analyst.main(["process-queue"])
    assert paths == [analyst.BASE_DIR / "data" / "disclosures.db"]


def test_analyst_stays_out_of_the_telegram_bot():
    src = (ROOT / "analyst.py").read_text(encoding="utf-8")
    assert "import telegram_bot" not in src and "from telegram_bot" not in src


# ------------------------------------------------------------------ the prompt files
def _text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_the_method_names_every_allowed_tradingview_tool():
    method = _text("analyst_method.txt")
    for tool in analyst.TV_TOOLS:
        assert tool in method, tool


def test_the_method_forbids_study_values_and_restores_the_chart():
    method = _text("analyst_method.txt")
    [line] = [ln for ln in method.splitlines() if "data_get_study_values" in ln]
    assert "FORBIDDEN" in line
    assert "chart_get_state" in method
    restore = method[method.index("RESTORE"):]
    assert "chart_set_symbol" in restore and "chart_set_timeframe" in restore
    assert "the symbol from step b" in method
    assert "WITHOUT any arguments" in method and "quote_get" in method
    assert "indicators, drawings, alerts, Pine scripts, replay, layouts" in method


def test_the_method_says_no_disclaimers_and_only_b_tags():
    method = _text("analyst_method.txt")
    assert "🎯 Итоговый вердикт:" in method
    assert "NO disclaimers" in method
    assert "matched <b>...</b> pairs" in method and 'bare "<" or ">"' in method
    assert "`.venv/bin/python analyst.py context 'TICKER'`" in method
    assert "summary true" in method and "kill_existing false" in method


def test_both_prompts_read_the_method_and_forbid_disclaimers():
    for name in ("claude_analysis_prompt.txt", "claude_ask_prompt.txt"):
        text = _text(name)
        assert "analyst_method.txt" in text, name
        assert "`.venv/bin/python analyst.py method`" in text, name
        assert "disclaimer" in text.lower(), name


def test_the_telegram_prompt_reads_the_queue_and_sends_the_answer_as_one_quoted_argument():
    text = _text("claude_analysis_prompt.txt")
    flat = " ".join(text.split())
    assert "`.venv/bin/python analyst.py pending`" in text and "очередь пуста" in text
    assert "`.venv/bin/python analyst.py question <ID>`" in text
    assert "`.venv/bin/python analyst.py context --queue <ID>`" in text
    assert "`.venv/bin/python analyst.py send <ID> '<the whole message>'`" in text
    assert "ONE single-quoted argument" in flat and "must not contain the ' character" in flat
    assert "write ’ instead" in flat
    assert "sent: True" in text and "sent: False (why)" in text and "marks row <ID> processed" in flat
    assert "ВОПРОС" in text
    assert "Write" not in text and "analyst_msg" not in text and "/tmp/" not in text
    assert "telegram_notify" not in text


def test_the_method_forbids_the_straight_quote_in_an_answer():
    flat = " ".join(_text("analyst_method.txt").split())
    assert "Never write the ' character" in flat and "use ’ instead" in flat
    assert "you cannot write or edit files" in flat


def test_the_terminal_prompt_sends_nothing_to_telegram():
    text = " ".join(_text("claude_ask_prompt.txt").split())          # line wraps do not count
    assert "analyst.py send" not in text and "analyst.py pending" not in text
    assert "Do not send anything to Telegram" in text
    assert "do not touch the analysis queue" in text and "do not create or edit any files" in text
    assert "ВОПРОС:" in text


def test_the_launch_script_runs_the_analyst():
    script = _text("run_claude_analysis.sh")
    assert "analyst.py process-queue" in script
    assert "/opt/homebrew/bin" in script and "/usr/local/bin" in script
    assert "set -euo pipefail" in script


def test_the_lock_file_is_git_ignored_and_there_is_no_message_file_any_more():
    ignored = _text(".gitignore").splitlines()
    assert "data/analyst.lock" in ignored and "data/analyst_msg.txt" not in ignored
    assert not hasattr(analyst, "MESSAGE_FILE")
    assert "analyst_msg" not in _text("analyst.py")


# ------------------------------------------------------------------ the queue lock, the timeout
def test_process_queue_gives_up_when_another_run_keeps_the_lock(conn, monkeypatch, capsys):
    db.enqueue_analysis(conn, "AAPL")
    monkeypatch.setattr(analyst, "LOCK_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(analyst, "LOCK_POLL_SECONDS", 0.01)
    calls = []
    with open(analyst.LOCK_FILE, "a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert analyst.process_queue(conn, run=lambda *a, **k: calls.append(a)) == 0
    assert calls == []
    assert "очередь занята другим прогоном" in capsys.readouterr().out
    assert len(db.pending_analysis(conn)) == 1


def test_the_lock_wait_is_ten_minutes_and_the_run_limit_fifteen():
    assert analyst.LOCK_WAIT_SECONDS == 600 and analyst.CLAUDE_TIMEOUT_SECONDS == 900


def _hold_the_lock_until_the_first_wait(monkeypatch, then=lambda: None):
    """Someone else holds the lock; the first time process_queue sleeps, they let go."""
    held = open(analyst.LOCK_FILE, "a+")
    fcntl.flock(held, fcntl.LOCK_EX)
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        then()
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()

    monkeypatch.setattr(analyst.time, "sleep", sleep)
    return waits


def test_process_queue_waits_for_the_lock_then_runs(conn, monkeypatch):
    db.enqueue_analysis(conn, "AAPL")
    waits = _hold_the_lock_until_the_first_wait(monkeypatch)
    calls = []
    assert analyst.process_queue(conn, run=lambda argv, **k: calls.append(argv) or _Proc(5)) == 5
    assert waits == [analyst.LOCK_POLL_SECONDS] and len(calls) == 1


def test_process_queue_skips_rows_the_run_it_waited_for_answered(conn, monkeypatch):
    db.enqueue_analysis(conn, "AAPL")
    _hold_the_lock_until_the_first_wait(
        monkeypatch, then=lambda: db.mark_analysis_processed(conn, 1))
    calls = []
    assert analyst.process_queue(conn, run=lambda *a, **k: calls.append(a)) == 0
    assert calls == []


def test_process_queue_holds_the_lock_while_claude_runs(conn):
    db.enqueue_analysis(conn, "AAPL")

    def run(argv, **kwargs):
        with open(analyst.LOCK_FILE, "a+") as other:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _Proc()

    assert analyst.process_queue(conn, run=run) == 0
    with open(analyst.LOCK_FILE, "a+") as after:            # released again
        fcntl.flock(after, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_process_queue_releases_the_lock_when_claude_is_missing(conn):
    db.enqueue_analysis(conn, "AAPL")

    def run(*a, **k):
        raise FileNotFoundError(2, "No such file", "claude")

    assert analyst.process_queue(conn, run=run) == 127
    with open(analyst.LOCK_FILE, "a+") as after:
        fcntl.flock(after, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_process_queue_stops_a_run_past_its_time_limit_and_returns_124(conn, capsys):
    db.enqueue_analysis(conn, "AAPL")

    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    assert analyst.process_queue(conn, run=run) == 124
    assert "900" in capsys.readouterr().out
    assert len(db.pending_analysis(conn)) == 1                  # retried on the next run
    with open(analyst.LOCK_FILE, "a+") as after:
        fcntl.flock(after, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_run_group_is_subprocess_run_for_what_process_queue_and_ask_need():
    done = analyst._run_group([sys.executable, "-c", "print('привет')"], capture_output=True,
                              text=True, check=False, timeout=30, start_new_session=True)
    assert (done.returncode, done.stdout.strip(), done.stderr) == (0, "привет", "")
    failed = analyst._run_group([sys.executable, "-c", "raise SystemExit(3)"], cwd=ROOT,
                                env=dict(os.environ), timeout=30)
    assert failed.returncode == 3


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _gone_soon(pid: int) -> bool:
    for _ in range(50):
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return False


def test_run_group_kills_the_whole_process_group_on_a_timeout(tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    script = ("import subprocess, sys, time\n"
              "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
              f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
              "time.sleep(60)\n")
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        analyst._run_group([sys.executable, "-c", script], timeout=1.5)
    assert time.monotonic() - started < 20
    assert _gone_soon(int(pidfile.read_text()))              # not just Claude: what it started


def test_run_group_kills_a_run_that_ignores_sigterm(tmp_path, monkeypatch):
    monkeypatch.setattr(analyst, "KILL_GRACE_SECONDS", 0.2)
    pidfile = tmp_path / "leader.pid"
    script = ("import os, signal, time\n"
              "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
              f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
              "time.sleep(60)\n")
    with pytest.raises(subprocess.TimeoutExpired):
        analyst._run_group([sys.executable, "-c", script], timeout=1.5)
    assert _gone_soon(int(pidfile.read_text()))


def test_the_default_runner_of_process_queue_is_the_group_killing_one(conn, monkeypatch):
    db.enqueue_analysis(conn, "AAPL")
    seen = []
    monkeypatch.setattr(analyst, "_run_group", lambda argv, **k: seen.append(k) or _Proc())
    assert analyst.process_queue(conn) == 0
    assert seen[0]["timeout"] == 900 and seen[0]["start_new_session"] is True


# ------------------------------------------------------------------ a missing prompt file
def test_a_missing_prompt_file_is_not_reported_as_a_missing_binary(conn, monkeypatch, capsys):
    db.enqueue_analysis(conn, "AAPL")
    monkeypatch.setattr(analyst, "ANALYSIS_PROMPT", "no-such-prompt.txt")
    calls = []
    assert analyst.process_queue(conn, run=lambda *a, **k: calls.append(a)) == 2
    out = capsys.readouterr().out
    assert "no-such-prompt.txt" in out and "claude не найден" not in out
    assert calls == []


def test_ask_with_a_missing_prompt_file_returns_2(monkeypatch, capsys):
    monkeypatch.setattr(analyst, "ASK_PROMPT", "no-such-prompt.txt")
    calls = []
    assert analyst.ask("?", run=lambda *a, **k: calls.append(a)) == 2
    out = capsys.readouterr().out
    assert "no-such-prompt.txt" in out and "claude не найден" not in out
    assert calls == []


def test_an_empty_queue_needs_no_prompt_file(conn, monkeypatch):
    monkeypatch.setattr(analyst, "ANALYSIS_PROMPT", "no-such-prompt.txt")
    assert analyst.process_queue(conn, run=lambda *a, **k: 1 / 0) == 0


# ------------------------------------------------------------------ send: the reply goes out by id
MESSAGE = "<b>NVDA</b>\n• растёт\n🎯 Итоговый вердикт: держать"


def test_send_delivers_the_text_and_marks_the_row_processed_after_a_confirmed_send(conn):
    qid = db.enqueue_question(conn, "что с Nvidia?")
    sent = []
    assert analyst.send_message(conn, qid, MESSAGE + "\n",
                                send=lambda t: sent.append(t) or True) == (True, None)
    assert sent == [MESSAGE]                                   # stripped
    assert db.pending_analysis(conn) == []


def test_a_send_telegram_did_not_confirm_leaves_the_row(conn):
    db.enqueue_analysis(conn, "AAPL")
    [(qid, _t)] = db.pending_analysis(conn)
    sent, why = analyst.send_message(conn, qid, MESSAGE, send=lambda t: False)
    assert sent is False and why
    assert db.pending_analysis(conn) == [(qid, "AAPL")]


def test_send_that_raises_is_a_failed_send(conn):
    qid = db.enqueue_question(conn, "?")

    def boom(text):
        raise RuntimeError("bot123456:SECRET-token is unreachable")

    sent, why = analyst.send_message(conn, qid, MESSAGE, send=boom)
    assert sent is False and "RuntimeError" in why
    assert "SECRET" not in why                                 # only the type is ever reported
    assert len(db.pending_analysis(conn)) == 1


@pytest.mark.parametrize("text", ["", " \n\t\n", None])
def test_send_without_a_message_sends_nothing(conn, text):
    qid = db.enqueue_question(conn, "?")
    calls = []
    assert analyst.send_message(conn, qid, text, send=lambda t: calls.append(t) or True) == (
        False, "пустое сообщение")
    assert calls == [] and len(db.pending_analysis(conn)) == 1


def test_send_for_a_row_that_is_not_pending_sends_nothing(conn):
    qid = db.enqueue_question(conn, "?")
    db.mark_analysis_processed(conn, qid)
    calls = []
    for row in (qid, 999):
        sent, why = analyst.send_message(conn, row, MESSAGE, send=lambda t: calls.append(t) or True)
        assert sent is False and str(row) in why
    assert calls == []


def test_a_row_that_cannot_be_marked_after_a_confirmed_send_is_reported_not_hidden(conn, monkeypatch):
    qid = db.enqueue_question(conn, "?")

    def locked(c, row):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(analyst.db, "mark_analysis_processed", locked)
    sent, note = analyst.send_message(conn, qid, MESSAGE, send=lambda t: True)
    assert sent is True and "RuntimeError" in note and str(qid) in note


def test_send_uses_telegram_notify_by_default(conn, monkeypatch):
    qid = db.enqueue_question(conn, "?")
    seen = []
    monkeypatch.setattr(analyst.telegram_notify, "send_text", lambda t: seen.append(t) or True)
    assert analyst.send_message(conn, qid, MESSAGE) == (True, None) and seen == [MESSAGE]


def test_cli_send_takes_the_message_as_the_argument(cli, conn, monkeypatch, capsys):
    qid = db.enqueue_question(conn, "?")
    seen = []
    monkeypatch.setattr(analyst.telegram_notify, "send_text", lambda t: seen.append(t) or True)
    assert cli("send", str(qid), MESSAGE) == 0
    assert capsys.readouterr().out.strip() == "sent: True"
    assert seen == [MESSAGE] and db.pending_analysis(conn) == []


def test_cli_send_joins_a_message_that_arrives_as_several_arguments_and_may_start_with_a_dash(
        cli, conn, monkeypatch):
    qid = db.enqueue_question(conn, "?")
    seen = []
    monkeypatch.setattr(analyst.telegram_notify, "send_text", lambda t: seen.append(t) or True)
    assert cli("send", str(qid), "-5%", "за", "день") == 0
    assert seen == ["-5% за день"]
    qid = db.enqueue_question(conn, "?")
    assert cli("send", str(qid), "--", "--x") == 0 and seen[-1] == "--x"


def test_cli_send_prints_sent_false_with_a_reason_and_exits_1(cli, conn, monkeypatch, capsys):
    qid = db.enqueue_question(conn, "?")
    monkeypatch.setattr(analyst.telegram_notify, "send_text", lambda t: False)
    assert cli("send", str(qid), MESSAGE) == 1
    assert capsys.readouterr().out.strip() == "sent: False (Telegram не принял сообщение)"
    assert len(db.pending_analysis(conn)) == 1


def test_cli_send_without_text_is_sent_false_not_a_usage_error(cli, conn, capsys):
    qid = db.enqueue_question(conn, "?")
    assert cli("send", str(qid)) == 1
    assert capsys.readouterr().out.strip() == "sent: False (пустое сообщение)"
    assert cli("send", str(qid), "  ") == 1


def test_cli_send_needs_a_row_number(cli, capsys):
    assert cli("send") == 2 and cli("send", "abc", "text") == 2
    assert "номер строки" in capsys.readouterr().err


def test_cli_send_never_shows_a_traceback(cli, conn, monkeypatch, capsys):
    qid = db.enqueue_question(conn, "?")

    def boom(*a, **k):
        raise RuntimeError("bot123456:SECRET-token")

    monkeypatch.setattr(analyst, "send_message", boom)
    assert cli("send", str(qid), MESSAGE) == 1
    out = capsys.readouterr()
    assert out.out.strip() == "sent: False (RuntimeError)" and "SECRET" not in out.out + out.err


def test_cli_send_with_an_unreachable_database_is_sent_false_too(monkeypatch, capsys):
    def boom(path):
        raise RuntimeError("unable to open database file")

    monkeypatch.setattr(analyst.db, "connect", boom)
    assert analyst.main(["send", "1", MESSAGE]) == 1
    assert capsys.readouterr().out.strip() == "sent: False (RuntimeError)"


# ------------------------------------------------------------------ .env
def test_load_env_reads_the_shell_style_lines(tmp_path):
    f = tmp_path / "env"
    f.write_text('# a comment\n\nTOKEN=abc123\nexport CHAT="123 456"\nQ=\'single\'\nEMPTY=\n'
                 'SPACED = padded \nHASH=val # trailing note\nQUOTED="x # y" # note\n'
                 'not a pair\n1BAD=x\nKEEP=from-the-file\nURL=https://h/p?a=b&c=d\n', encoding="utf-8")
    env = {"KEEP": "already-set"}
    loaded = REAL_LOAD_ENV(f, env)
    assert env == {"KEEP": "already-set", "TOKEN": "abc123", "CHAT": "123 456", "Q": "single",
                   "EMPTY": "", "SPACED": "padded", "HASH": "val", "QUOTED": "x # y",
                   "URL": "https://h/p?a=b&c=d"}
    assert loaded == ["TOKEN", "CHAT", "Q", "EMPTY", "SPACED", "HASH", "QUOTED", "URL"]


def test_a_later_line_of_the_file_wins_like_source_but_never_over_the_environment(tmp_path):
    f = tmp_path / "env"
    f.write_text("A=1\nA=2\nB=3\n", encoding="utf-8")
    env = {"B": "set"}
    REAL_LOAD_ENV(f, env)
    assert env == {"A": "2", "B": "set"}


def test_load_env_prints_nothing_and_a_missing_file_is_fine(tmp_path, capsys):
    f = tmp_path / "env"
    f.write_text("TELEGRAM_BOT_TOKEN=secret-value\n", encoding="utf-8")
    REAL_LOAD_ENV(f, {})
    assert REAL_LOAD_ENV(tmp_path / "nope", {}) == []
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_load_env_defaults_to_the_repository_env_file_and_os_environ(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("ANALYST_TEST_KEY=1\n", encoding="utf-8")
    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    fake_environ = {}
    monkeypatch.setattr(analyst.os, "environ", fake_environ)
    assert REAL_LOAD_ENV() == ["ANALYST_TEST_KEY"] and fake_environ == {"ANALYST_TEST_KEY": "1"}


def test_the_cli_loads_env_for_the_commands_claude_runs_but_not_for_the_ones_that_start_it(
        cli, monkeypatch):
    loads = []
    monkeypatch.setattr(analyst, "load_env", lambda *a, **k: loads.append(1) or [])
    monkeypatch.setattr(analyst, "process_queue", lambda c, **k: 0)
    monkeypatch.setattr(analyst, "ask", lambda q, **k: 0)
    cli("pending")
    assert loads == [1]
    cli("process-queue")
    cli("ask", "вопрос")
    assert loads == [1]         # Claude would inherit the keys; its own commands load them


# ------------------------------------------------------------------ pending, method, free text
def test_cli_pending_lists_the_rows_or_says_the_queue_is_empty(cli, conn, capsys):
    assert cli("pending") == 0
    assert capsys.readouterr().out.strip() == "очередь пуста"
    db.enqueue_analysis(conn, "$NVDA")
    qid = db.enqueue_question(conn, "?")
    assert cli("pending") == 0
    out = capsys.readouterr().out
    assert "(1, '$NVDA')" in out and f"({qid}, 'ВОПРОС')" in out


def test_cli_method_prints_the_method(cli, capsys):
    assert cli("method") == 0
    assert capsys.readouterr().out == _text("analyst_method.txt") + "\n"


def test_cli_method_without_the_file(cli, monkeypatch, capsys):
    monkeypatch.setattr(analyst, "METHOD_FILE", "no-such-method.txt")
    assert cli("method") == 2


def test_ask_and_news_take_text_that_starts_with_a_dash(cli, monkeypatch, capsys):
    asked, searched = [], []
    monkeypatch.setattr(analyst, "ask", lambda q, **k: asked.append(q) or 0)
    monkeypatch.setattr(analyst, "news", lambda q: searched.append(q) or "новость")
    assert cli("ask", "-5% за день: почему?") == 0
    assert cli("ask", "--verbose", "как", "дела") == 0
    assert cli("ask", "--", "--x", "y") == 0
    assert asked == ["-5% за день: почему?", "--verbose как дела", "--x y"]
    assert cli("news", "-nvidia", "earnings") == 0
    assert cli("news", "--bad") == 0
    assert searched == ["-nvidia earnings", "--bad"]
    assert capsys.readouterr().out.count("новость") == 2


def test_ask_and_news_need_some_text(cli, capsys):
    assert cli("ask") == 2 and cli("news") == 2 and cli("ask", "--") == 2
    assert "нужен текст" in capsys.readouterr().err


def test_ask_help_still_works(cli, capsys):
    with pytest.raises(SystemExit) as stop:
        cli("ask", "--help")
    assert stop.value.code == 0


# ------------------------------------------------------------------ context scores only the asked ticker
def test_context_asks_for_the_signals_of_that_ticker_only(conn, dossier, monkeypatch):
    seen = _stub_scoring(monkeypatch, [_stock("NVDA", 61.0)])
    assert "МОДЕЛЬ: балл 61" in analyst.context(conn, "$NVDA")
    assert seen["tickers"] == {"NVDA"} and seen["signals"] == ["sig"]
    analyst.context(conn, "BTC")
    assert seen["tickers"] == {"BTC", "CRYPTO:BTC"}
    analyst.context(conn, "eqnr.ol")
    assert seen["tickers"] == {"EQNR.OL", "EQNR"}           # the bare name of the source NORWAY
    analyst.context(conn, "SAP.DE")
    assert seen["tickers"] == {"SAP.DE"}                    # no coin has a dot, no venue source
    analyst.context(conn, "CRYPTO:SOL")
    assert seen["tickers"] == {"CRYPTO:SOL"}


def test_context_never_enriches_anything_but_the_asked_ticker(conn, dossier, monkeypatch):
    import types
    sigs = [types.SimpleNamespace(ticker=t) for t in ("NVDA", "AAA", "NVDA", "BBB", "CRYPTO:BTC")]
    enriched, scored_with = [], {}
    monkeypatch.setattr(model.strategy, "buy_side_signals", lambda c, **k: sigs)
    monkeypatch.setattr(model.cluster, "disclosed_on", lambda c, s: dt.date.today().isoformat())
    monkeypatch.setattr(model.cluster, "enrich_signals",
                        lambda c, signals: enriched.extend(signals) or signals)

    def score(c, today=None, *, signals=None, **kw):
        scored_with["signals"] = signals
        return [_stock("NVDA", 61.0)]

    monkeypatch.setattr(model, "score_today", score)
    analyst.context(conn, "$NVDA")
    assert [s.ticker for s in enriched] == ["NVDA", "NVDA"]
    assert scored_with["signals"] == enriched


def test_portfolio_still_scores_everything(conn, monkeypatch):
    calls = []
    monkeypatch.setattr(model, "candidate_signals", lambda *a, **k: 1 / 0)
    monkeypatch.setattr(model, "score_today", lambda c, *a, **k: calls.append((a, k)) or [])
    assert "НАБЛЮДЕНИЕ: нет" in analyst.portfolio(conn)
    assert calls == [((), {})]


# ------------------------------------------------------------------ no signal: momentum and news
def _rising_bars(n=260, start=100.0, step=0.5):
    today = dt.date.today()
    return [((today - dt.timedelta(days=n - i)).isoformat(), start + i * step) for i in range(n)]


def test_a_ticker_without_a_signal_gets_its_momentum_and_news_parts(conn, dossier, monkeypatch):
    bars, priced = _rising_bars(), []
    monkeypatch.setattr(paper, "_closes", lambda symbol, days: priced.append(symbol) or bars)
    headlines = [{"title": "Acme downgrade after probe", "published": "2026-09-28"}]
    monkeypatch.setattr(model, "default_news", lambda ticker, source: headlines)
    momentum = model_score.momentum_part([c for _d, c in bars])
    assert momentum.points > 0 and momentum.lines
    out = analyst.context(conn, "$NVDA", scored=[])
    assert "МОДЕЛЬ: свежего сигнала за 14 дней нет" in out
    line = next(ln for ln in out.splitlines() if ln.strip().startswith("импульс"))
    assert line == (f"  импульс {round(momentum.points)} ({', '.join(momentum.lines)})"
                    " · новости −10 (1 плохая)")
    assert priced == ["NVDA"]
    assert out.index("МОДЕЛЬ") < out.index("импульс") < out.index("ДОСЬЕ")


def test_the_news_part_shows_a_red_flag_headline(conn, dossier, monkeypatch):
    monkeypatch.setattr(model, "default_news", lambda t, s: [
        {"title": "Acme faces SEC investigation", "published": "2026-09-28"},
        {"title": "Acme raises guidance", "published": "2026-09-28"}])
    out = analyst.context(conn, "NVDA", scored=[])
    assert "новости 5 (1 хорошая)" in out and "красный флаг: Acme faces SEC investigation" in out


def test_without_prices_the_momentum_is_said_to_be_unknown(conn, dossier):
    out = analyst.context(conn, "NVDA", scored=[])
    assert "импульс: нет истории цен · новости 0" in out


def test_no_extra_lines_when_the_model_has_a_signal(conn, dossier, monkeypatch):
    monkeypatch.setattr(model, "default_news", lambda t, s: 1 / 0)      # would be named if asked
    out = analyst.context(conn, "NVDA", scored=[_stock("NVDA")])
    assert "недоступны" not in out and "нет истории цен" not in out
    assert "красный флаг" not in out


def test_the_listing_of_a_norwegian_ticker_is_its_own(conn, dossier, monkeypatch):
    priced, asked = [], []
    monkeypatch.setattr(paper, "_closes", lambda symbol, days: priced.append(symbol) or [])
    monkeypatch.setattr(model, "default_news", lambda t, s: asked.append((t, s)) or [])
    analyst.context(conn, "EQNR.OL", scored=[])
    assert priced == ["EQNR.OL"] and asked == [("EQNR", "NORWAY")]


def test_a_signal_journal_source_picks_the_venue_of_a_bare_ticker(conn, dossier, monkeypatch):
    priced = []
    monkeypatch.setattr(paper, "_closes", lambda symbol, days: priced.append(symbol) or [])
    conn.execute("INSERT INTO signal_journal (ticker, source, emitted_at, tier) "
                 "VALUES ('EQNR', 'NORWAY', '2026-09-20', 'buy')")
    conn.commit()
    analyst.context(conn, "EQNR", scored=[])
    assert priced == ["EQNR.OL"]


@pytest.mark.parametrize("ticker", ["CRYPTO:SOL", "SOL", "DE0007164600", "SAP.DE"])
def test_coins_isins_and_unpriced_venues_get_no_extra_lines(conn, dossier, monkeypatch, ticker):
    monkeypatch.setattr(paper, "_closes", lambda symbol, days: 1 / 0)
    monkeypatch.setattr(model, "default_news", lambda t, s: 1 / 0)
    out = analyst.context(conn, ticker, scored=[]).splitlines()
    assert out[0] == "МОДЕЛЬ: свежего сигнала за 14 дней нет"
    assert out[1] == "ДОСЬЕ:"


def test_a_failure_in_the_extra_lines_is_named_not_fatal(conn, dossier, monkeypatch):
    def boom(ticker, source):
        raise RuntimeError("feed down")

    monkeypatch.setattr(model, "default_news", boom)
    out = analyst.context(conn, "NVDA", scored=[])
    assert "импульс и новости недоступны: RuntimeError" in out and "БРИФ NVDA" in out


# ------------------------------------------------------------------ SIGTERM takes Claude down too
def _wait_for_pids(pidfile: pathlib.Path, count: int = 2, seconds: float = 20.0) -> list[int]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            pids = [int(p) for p in pidfile.read_text().split()]
        except (OSError, ValueError):
            pids = []
        if len(pids) == count:
            return pids
        time.sleep(0.05)
    raise AssertionError(f"{pidfile} never got {count} pids")


def test_a_sigterm_to_the_analyst_takes_claude_and_its_children_down_and_exits_143(tmp_path):
    """The real thing, in a process of its own: an analyst waiting on a stand-in for Claude (which
    has a child of its own) is sent SIGTERM."""
    pidfile = tmp_path / "pids"
    stand_in = ("import os, subprocess, sys, time\n"
                "kid = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                f"open({str(pidfile)!r}, 'w').write(f'{{os.getpid()}} {{kid.pid}}')\n"
                "time.sleep(60)\n")
    runner = ("import sys\n"
              f"sys.path.insert(0, {str(ROOT)!r})\n"
              "import analyst\n"
              f"analyst._run_group([sys.executable, '-c', {stand_in!r}], timeout=60)\n")
    analyst_proc = subprocess.Popen([sys.executable, "-c", runner])
    try:
        claude_pid, kid_pid = _wait_for_pids(pidfile)
        analyst_proc.send_signal(signal.SIGTERM)
        assert analyst_proc.wait(timeout=30) == 143
    finally:
        if analyst_proc.poll() is None:
            analyst_proc.kill()
    assert _gone_soon(claude_pid) and _gone_soon(kid_pid)


def test_a_sigterm_in_process_ends_in_143_and_puts_the_old_handler_back(tmp_path):
    caught = []
    previous = signal.signal(signal.SIGTERM, lambda signum, frame: caught.append(signum))
    sentinel = signal.getsignal(signal.SIGTERM)
    pidfile = tmp_path / "pid"
    script = (f"import os; open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
              "import time; time.sleep(60)\n")
    try:
        timer = threading.Timer(1.0, os.kill, (os.getpid(), signal.SIGTERM))
        timer.start()
        with pytest.raises(SystemExit) as stop:
            analyst._run_group([sys.executable, "-c", script], timeout=60)
        assert stop.value.code == analyst.TERMINATED_EXIT == 143
        assert signal.getsignal(signal.SIGTERM) is sentinel and caught == []
        assert _gone_soon(int(pidfile.read_text()))
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_the_sigterm_handler_is_put_back_after_every_way_a_run_can_end():
    previous = signal.signal(signal.SIGTERM, lambda signum, frame: None)
    sentinel = signal.getsignal(signal.SIGTERM)
    try:
        analyst._run_group([sys.executable, "-c", "pass"], timeout=30)
        assert signal.getsignal(signal.SIGTERM) is sentinel
        with pytest.raises(subprocess.TimeoutExpired):
            analyst._run_group([sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.5)
        assert signal.getsignal(signal.SIGTERM) is sentinel
        with pytest.raises(FileNotFoundError):
            analyst._run_group(["/no/such/claude"], timeout=5)
        assert signal.getsignal(signal.SIGTERM) is sentinel
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_run_group_works_off_the_main_thread_without_touching_signals():
    result = []
    thread = threading.Thread(target=lambda: result.append(
        analyst._run_group([sys.executable, "-c", "print('ok')"], capture_output=True, text=True,
                           timeout=30)))
    thread.start()
    thread.join(30)
    assert result and result[0].stdout.strip() == "ok"


def test_a_stopped_analyst_releases_the_lock_and_leaves_the_rows_queued(conn):
    db.enqueue_analysis(conn, "AAPL")

    def run(*a, **k):
        raise SystemExit(143)

    with pytest.raises(SystemExit) as stop:
        analyst.process_queue(conn, run=run)
    assert stop.value.code == 143 and len(db.pending_analysis(conn)) == 1
    with open(analyst.LOCK_FILE, "a+") as after:
        fcntl.flock(after, fcntl.LOCK_EX | fcntl.LOCK_NB)


# ------------------------------------------------------------------ EQNR.OL is the bare EQNR of NORWAY
def test_a_venue_suffix_finds_the_bare_signal_of_that_source_only(conn, dossier):
    norway, us = _stock("EQNR", 66.0, source="NORWAY"), _stock("EQNR", 40.0, source="SEC")
    out = analyst.context(conn, "EQNR.OL", scored=[us, norway])
    assert "МОДЕЛЬ: балл 66" in out
    assert "МОДЕЛЬ: свежего сигнала" in analyst.context(conn, "EQNR.OL", scored=[us])
    swedish = _stock("VOLV-B", 61.0, source="SWEDEN")
    assert "МОДЕЛЬ: балл 61" in analyst.context(conn, "VOLV-B.ST", scored=[swedish])
    assert "МОДЕЛЬ: свежего сигнала" in analyst.context(conn, "VOLV-B.OL", scored=[swedish])
    assert "МОДЕЛЬ: балл 40" in analyst.context(conn, "EQNR", scored=[us])       # a bare key: any source
    assert "МОДЕЛЬ: балл 66" in analyst.context(conn, "EQNR.OL", scored=[norway, us])


def test_a_venue_suffix_finds_the_positions_of_that_source_only(conn, dossier):
    model.create_books(conn, dt.date(2026, 9, 1))
    _paper_position(conn, S, "EQNR", source="NORWAY", fill="2026-09-25")
    _paper_position(conn, S, "EQNR", source="SEC", fill="2026-09-26")
    for source, day in (("NORWAY", "2026-09-27"), ("SEC", "2026-09-28")):
        conn.execute("INSERT INTO positions (ticker, source, opened_at, entry_price, insiders, "
                     "stop_pct) VALUES ('EQNR', ?, ?, 100.0, '[]', 0.15)", (source, day))
    conn.commit()
    out = analyst.context(conn, "EQNR.OL", scored=[])
    assert "с 2026-09-25" in out and "с 2026-09-26" not in out
    assert "с 2026-09-27" in out and "с 2026-09-28" not in out
    assert "с 2026-09-26" in analyst.context(conn, "$EQNR", scored=[])


def test_the_signals_of_a_venue_key_are_asked_for_by_the_bare_name(conn, dossier, monkeypatch):
    seen = _stub_scoring(monkeypatch, [_stock("EQNR", 66.0, source="NORWAY")])
    assert "МОДЕЛЬ: балл 66" in analyst.context(conn, "EQNR.OL")
    assert seen["tickers"] == {"EQNR.OL", "EQNR"}
