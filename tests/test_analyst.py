"""analyst.py: the Claude analyst's plumbing -- the command it runs, the context it prints, the
queue and the prompt files. Offline: Claude itself is never started (`run=` is stubbed), the
dossier and the news are stubbed, and scores are hand-built."""
from __future__ import annotations

import datetime as dt
import pathlib
import subprocess

import pytest

import analyst
import db
import model
import model_score
import research

ROOT = pathlib.Path(__file__).resolve().parent.parent
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


def _paper_position(conn, code, ticker, *, fill="2026-09-25", cost=8_000.0, last=8_400.0, stop=0.10):
    conn.execute(
        "INSERT INTO paper_positions (book, ticker, source, symbol, currency, fill_date, cost_eur, "
        "net_eur, entry_close, entry_fx, reason, last_value, stop_pct, score) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (code, ticker, "SEC", ticker, "USD", fill, cost, cost * 0.998, 10.0, 1.16, "балл 64", last,
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
    assert cmd[3:5] == ["--permission-mode", "acceptEdits"]
    i = cmd.index("--allowedTools")
    assert cmd[i + 1:] == list(analyst.ALLOWED_TOOLS)
    assert cmd.count("--allowedTools") == 1
    assert "Bash" in cmd and "mcp__tradingview__quote_get" in cmd
    assert all(f"mcp__tradingview__{t}" in cmd for t in analyst.TV_TOOLS)


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
    monkeypatch.setattr(model, "score_today", lambda c: [_stock("NVDA", 61.0)])
    assert "МОДЕЛЬ: балл 61" in analyst.context(conn, "NVDA")


def test_context_scoring_failure_does_not_lose_the_dossier(conn, dossier, monkeypatch):
    def boom(c):
        raise RuntimeError("network")

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
    monkeypatch.setattr(model, "score_today", lambda c: [_stock("NVDA", 64.0)])
    assert cli("context", "$NVDA") == 0
    out = capsys.readouterr().out
    assert "МОДЕЛЬ: балл 64" in out
    assert dossier == ["NVDA"]                # validated tickers lose the "$"


def test_cli_context_by_queue_id_uses_the_queued_key_as_is(cli, conn, dossier, monkeypatch, capsys):
    monkeypatch.setattr(model, "score_today", lambda c: [_stock("NVDA", 64.0)])
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
    monkeypatch.setattr(model, "score_today", lambda c: [])
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
    assert "the symbol and the timeframe" in method
    assert "WITHOUT any arguments" in method and "quote_get" in method
    assert "indicators, drawings, alerts, Pine scripts, replay, layouts" in method


def test_the_method_says_no_disclaimers_and_only_b_tags():
    method = _text("analyst_method.txt")
    assert "🎯 Итоговый вердикт:" in method
    assert "NO disclaimers" in method
    assert "matched <b>...</b> pairs" in method and 'bare "<" or ">"' in method
    assert "python analyst.py context 'NVDA'" in method
    assert "summary true" in method and "kill_existing false" in method


def test_both_prompts_read_the_method_and_forbid_disclaimers():
    for name in ("claude_analysis_prompt.txt", "claude_ask_prompt.txt"):
        text = _text(name)
        assert "analyst_method.txt" in text, name
        assert "disclaimer" in text.lower(), name


def test_the_telegram_prompt_reads_questions_and_contexts_by_queue_id():
    text = _text("claude_analysis_prompt.txt")
    assert "analyst.py question <ID>" in text
    assert "analyst.py context --queue <ID>" in text
    assert "ВОПРОС" in text
    assert "/tmp/claude_analysis_msg.txt" in text and "sent: True" in text
    assert "mark_analysis_processed" in text


def test_the_terminal_prompt_sends_nothing_to_telegram():
    text = _text("claude_ask_prompt.txt")
    assert 'python3 -c "' not in text and "mark_analysis_processed" not in text
    assert "/tmp/claude_analysis_msg.txt" not in text
    assert "Do not send anything to Telegram" in text
    assert "do not touch the analysis queue" in text and "do not create or edit any files" in text
    assert "source .env" in text and "ВОПРОС:" in text


def test_the_launch_script_runs_the_analyst():
    script = _text("run_claude_analysis.sh")
    assert "analyst.py process-queue" in script
    assert "/opt/homebrew/bin" in script and "/usr/local/bin" in script
    assert "set -euo pipefail" in script
