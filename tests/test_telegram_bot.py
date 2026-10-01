"""telegram_bot.py's message-handling control flow -- the analysis runner (subprocess.Popen) and
all network calls (Telegram, research.build) are monkeypatched; no real claude invocation or
Telegram send happens in tests. The runner itself is exercised only against harmless scripts
written to tmp_path, never the real run_claude_analysis.sh."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

import telegram_bot as tb

REAL_POPEN = subprocess.Popen


@pytest.fixture(autouse=True)
def _offline_lookup(monkeypatch):
    import paper
    import sources

    # /bought sizes its stop from the price history: none, offline.
    monkeypatch.setattr(paper, "_closes", lambda symbol, days: [])
    # A queued lookup or question runs run_claude_analysis.sh, i.e. a real headless Claude pass
    # over the real queue that can send Telegram messages. A test that doesn't stub the runner
    # itself must never reach it -- not even while it is still red.
    def no_real_subprocess(*a, **k):
        raise AssertionError("test reached a real subprocess")
    monkeypatch.setattr(subprocess, "run", no_real_subprocess)
    monkeypatch.setattr(subprocess, "Popen", no_real_subprocess)
    monkeypatch.setattr(sources, "cached_coin_symbols", lambda conn: {"BTC", "ETH", "SOL"})
    monkeypatch.setattr(sources, "stock_universe_symbols", lambda: set())
    monkeypatch.setattr(sources, "current_price", lambda asset: (100.0, "Yahoo"))


@pytest.fixture
def analysis(monkeypatch):
    """Stubs the shared runner: records its labels and queue ids; `.ok` is what it returns
    (True by default: the row was answered)."""
    state = SimpleNamespace(labels=[], ids=[], ok=True)

    def run(conn, queue_id, label):
        state.labels.append(label)
        state.ids.append(queue_id)
        return state.ok
    monkeypatch.setattr(tb, "_run_analysis", run)
    return state


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: out.append(msg) or True)
    return out


def test_extract_ticker_accepts_plain_and_dollar_prefixed():
    assert tb._extract_ticker("AAPL") == "AAPL"
    assert tb._extract_ticker("$aapl") == "AAPL"
    assert tb._extract_ticker("aapl buy now") == "AAPL"


def test_extract_ticker_rejects_garbage():
    assert tb._extract_ticker("waytoolongtobeaticker") is None  # >10 chars
    assert tb._extract_ticker("???") is None
    assert tb._extract_ticker("") is None


def test_handle_message_success_does_not_send_its_own_reply(conn, analysis, sent):
    """On a successful synchronous run, run_claude_analysis.sh itself sends
    the one merged message -- telegram_bot.py must not also send anything,
    or the user would get two messages, defeating the whole point."""
    tb._handle_message(conn, "AAPL")
    assert sent == [] and analysis.labels == ["$AAPL"]


def test_handle_message_enqueues_before_running(conn, analysis):
    import db
    tb._handle_message(conn, "AAPL")
    assert [t for _, t in db.pending_analysis(conn)] == ["$AAPL"]  # stays pending; only
    # run_claude_analysis.sh itself marks a row processed, on a confirmed send


_FAKE_REPORT = {"ticker": "AAPL", "opinion": None, "insiders": {"buys": [], "sells": []},
                "stakes": [], "political": [], "tradingview": None}


def test_handle_message_falls_back_when_the_run_fails(conn, analysis, sent, monkeypatch):
    analysis.ok = False
    monkeypatch.setattr("research.build", lambda conn, ticker: _FAKE_REPORT)
    tb._handle_message(conn, "AAPL")
    assert len(sent) == 1
    assert "попробуется снова" in sent[0]


def test_handle_message_double_failure_sends_generic_error(conn, analysis, sent, monkeypatch):
    """If the fallback itself blows up too (e.g. research.build fails), the
    user still gets SOME reply, not silence."""
    analysis.ok = False

    def raise_error(conn, ticker):
        raise ValueError("network down")
    monkeypatch.setattr("research.build", raise_error)
    tb._handle_message(conn, "AAPL")
    assert len(sent) == 1
    assert "Не удалось" in sent[0]


def test_a_ticker_runs_labelled_with_its_key(conn, analysis):
    tb._handle_message(conn, "aapl")
    assert analysis.labels == ["$AAPL"]


def test_a_ticker_run_checks_the_row_it_queued_even_an_existing_one(conn, analysis, sent):
    import db
    analysis.ok = True
    tb._handle_message(conn, "aapl")
    tb._handle_message(conn, "AAPL")                          # still pending: the same row
    [(qid, _t)] = db.pending_analysis(conn)
    assert analysis.ids == [qid, qid]


def test_a_question_run_checks_its_own_row(conn, analysis, sent):
    import db
    tb._handle_message(conn, "как рынок?")
    [(qid, _t)] = db.pending_analysis(conn)
    assert analysis.ids == [qid]


def test_get_updates_treats_a_read_timeout_as_an_empty_poll():
    """A long poll outliving its timeout (e.g. waking from sleep) loses nothing, so it
    must not surface as an error -- that used to trigger up to 5 minutes of backoff."""
    import requests

    class Session:
        def get(self, *a, **k):
            raise requests.ReadTimeout("read timed out")
    assert tb._get_updates("tok", None, Session()) == []


def test_only_new_messages_are_handled_not_edits(conn, monkeypatch):
    """Editing a sent message must not queue (and run) the analyst a second time."""
    import db
    handled = []
    updates = [{"update_id": 7, "edited_message": {"chat": {"id": 5}, "text": "что с NVDA?"}},
               {"update_id": 8, "message": {"chat": {"id": 5}, "text": "AAPL"}},
               {"update_id": 9, "channel_post": {"chat": {"id": 5}, "text": "x"}}]
    monkeypatch.setattr(tb, "_get_updates", lambda token, offset, session: updates)
    monkeypatch.setattr(tb, "_handle_message", lambda c, text: handled.append(text))
    tb._poll_once(conn, "tok", "5", session=None)
    assert handled == ["AAPL"]
    assert db.get_cached_value(conn, tb.STATE_OFFSET, tb.PERSIST_SECONDS) == 10


def test_get_updates_still_raises_on_connection_errors():
    import requests

    class Session:
        def get(self, *a, **k):
            raise requests.ConnectionError("no route")
    with pytest.raises(requests.ConnectionError):
        tb._get_updates("tok", None, Session())


# ------------------------------------------------------- position commands
@pytest.fixture
def replies(monkeypatch):
    """No market price by default -- most of these tests care about the command
    parsing, not the sanity check against a real last close (see the dedicated
    tests below for that). Tests that need an actual number override this."""
    sent = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    monkeypatch.setattr("positions.last_close", lambda ticker, source=None: None)
    return sent


def test_position_ticker_maps_crypto_symbols():
    """A bare crypto symbol or "CRYPTO:XXX" input both map to this project's
    ticker convention; anything not in crypto.SYMBOLS is rejected rather than
    silently treated as an equity ticker."""
    assert tb._position_ticker("BTC") == "CRYPTO:BTC"
    assert tb._position_ticker("eth") == "CRYPTO:ETH"
    assert tb._position_ticker("CRYPTO:btc") == "CRYPTO:BTC"
    assert tb._position_ticker("CRYPTO:ZZZ") is None


def test_bought_with_price_opens_a_position(conn, replies):
    import positions
    tb._handle_message(conn, "/bought grab 18.40")
    [pos] = positions.open_positions(conn)
    assert (pos.ticker, pos.entry_price) == ("GRAB", 18.40) and "GRAB" in replies[-1]


def test_bought_without_price_uses_the_last_close(conn, replies, monkeypatch):
    import positions
    monkeypatch.setattr("positions.last_close", lambda ticker, source=None: 50.0)
    tb._handle_message(conn, "/bought GRAB")
    assert positions.open_positions(conn)[0].entry_price == 50.0


def test_bought_with_a_bad_price_stores_nothing(conn, replies):
    import positions
    tb._handle_message(conn, "/bought GRAB abc")
    assert positions.open_positions(conn) == [] and "/bought" in replies[-1]


def test_bought_twice_is_refused(conn, replies):
    tb._handle_message(conn, "/bought GRAB 18")
    tb._handle_message(conn, "/bought GRAB 19")
    assert "уже" in replies[-1]


def test_sold_closes_and_unknown_sold_says_so(conn, replies):
    import positions
    tb._handle_message(conn, "/bought GRAB 18")
    tb._handle_message(conn, "/sold GRAB")
    assert positions.open_positions(conn) == []
    tb._handle_message(conn, "/sold GRAB")
    assert "нет открытой" in replies[-1]


def test_positions_lists_open_positions(conn, replies, monkeypatch):
    monkeypatch.setattr("positions.last_close", lambda ticker, source=None: 50.0)
    tb._handle_message(conn, "/bought GRAB 40")
    tb._handle_message(conn, "/positions")
    assert "GRAB" in replies[-1] and "+25.0%" in replies[-1]


@pytest.mark.parametrize("non_finite", ["nan", "inf", "-inf"])
def test_bought_with_non_finite_price_stores_nothing(conn, replies, non_finite):
    import positions
    tb._handle_message(conn, f"/bought GRAB {non_finite}")
    assert positions.open_positions(conn) == [] and "/bought" in replies[-1]


def test_bought_crypto_symbol_opens_the_project_ticker_with_crypto_source(conn, replies):
    """/bought BTC must open CRYPTO:BTC, priced as crypto -- not a position in the
    literal string "BTC", which positions.last_close would otherwise price as the
    Grayscale Bitcoin Mini Trust ETF instead of the coin."""
    import positions
    tb._handle_message(conn, "/bought BTC 60000")
    [pos] = positions.open_positions(conn)
    assert pos.ticker == "CRYPTO:BTC" and pos.source == "CRYPTO"


def test_bought_price_far_from_last_close_is_refused(conn, replies, monkeypatch):
    import positions
    monkeypatch.setattr("positions.last_close", lambda ticker, source=None: 100.0)
    tb._handle_message(conn, "/bought GRAB 50")
    assert positions.open_positions(conn) == []
    assert "отличается" in replies[-1] and "100" in replies[-1] and "50" in replies[-1]


def test_bought_price_within_tolerance_of_last_close_is_accepted(conn, replies, monkeypatch):
    import positions
    monkeypatch.setattr("positions.last_close", lambda ticker, source=None: 100.0)
    tb._handle_message(conn, "/bought GRAB 70")
    assert positions.open_positions(conn)[0].entry_price == 70.0


def test_bought_with_no_quote_stores_the_price_and_notes_it(conn, replies):
    """When last_close can't find a price at all (already the fixture's default),
    the user's price is still stored -- only the stop check is affected."""
    import positions
    tb._handle_message(conn, "/bought ORK 12.5")
    assert positions.open_positions(conn)[0].entry_price == 12.5
    assert "стоп-лосс" in replies[-1] and "не отслеживается" in replies[-1]
    assert "90" not in replies[-1]                      # the 90-day term is gone
    assert "стоп −" not in replies[-1] and "по умолчанию" not in replies[-1]     # no stop is being watched


def _august_closes(symbol, days):
    return [(f"2026-08-{d:02d}", 180.0) for d in range(1, 31)]


def test_bought_tells_the_stop_its_price_history_gave(conn, replies, monkeypatch):
    import paper
    import positions
    monkeypatch.setattr("positions.last_close", lambda ticker, source=None: 180.0)
    monkeypatch.setattr(paper, "_closes", _august_closes)
    tb._handle_message(conn, "/bought NVDA 180")
    assert replies[-1].startswith("Записал NVDA по 180,00; стоп −10% от максимума; ")
    assert positions.open_positions(conn)[0].stop_pct == 0.10


def test_bought_says_the_stop_is_the_default_when_there_is_no_history(conn, replies, monkeypatch):
    monkeypatch.setattr("positions.last_close", lambda ticker, source=None: 180.0)
    tb._handle_message(conn, "/bought NVDA 180")
    assert replies[-1].startswith("Записал NVDA по 180,00; стоп — по умолчанию; ")
    assert "не отслеживается" not in replies[-1]


def test_bought_names_the_insiders_it_watches_escaped(conn, replies, monkeypatch):
    import db
    import json
    monkeypatch.setattr("positions.last_close", lambda ticker, source=None: 180.0)
    db.journal_signal(conn, {"source": "SEC", "kind": "cluster", "ticker": "NVDA", "tier": "buy",
                             "members": json.dumps(["A&B <Boss>"])})
    tb._handle_message(conn, "/bought NVDA 180")
    assert "слежу за продажами: A&amp;B &lt;Boss&gt;" in replies[-1]


# ------------------------------------------------------- Oslo pricing (EQNR bug)
#
# EQNR is both Equinor's real Oslo listing and an unrelated US OTC pink-sheet
# ticker. telegram_bot used to price /bought against source=None (bare US
# ticker) even when the position would end up stored with source=NORWAY (read
# from signal_journal by positions.open_position) -- so a correct NOK price got
# refused as "too far from the market", or a price-less /bought stored the
# wrong (US) close. positions.position_source() is the one place this is now
# resolved, and telegram_bot must use it BEFORE pricing, not just at storage
# time.

def _norway_journal(conn, ticker):
    """A row as the tiered design wrote it (tier "strong"); the model's own rows are covered
    in test_bot_model.py."""
    import db
    db.journal_signal(conn, {"source": "NORWAY", "kind": "cluster", "ticker": ticker,
                             "tier": "strong", "members": "[]"})


def test_bought_oslo_ticker_with_a_correct_price_is_accepted(conn, monkeypatch):
    """Without the fix, last_close("EQNR", None) prices the bare US ticker (25.0)
    via _yahoo_close, so the genuine Oslo price 270 looks "too far from the
    market" and gets refused. Deliberately does NOT use the `replies` fixture,
    which stubs positions.last_close itself -- this test needs the real
    last_close -> yahoo_symbol -> _yahoo_close path to exercise the bug."""
    import positions
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: True)
    _norway_journal(conn, "EQNR")
    prices = {"EQNR": 25.0, "EQNR.OL": 270.0}
    monkeypatch.setattr(positions, "_yahoo_close", lambda symbol: prices.get(symbol))
    tb._handle_message(conn, "/bought EQNR 270")
    [pos] = positions.open_positions(conn)
    assert pos.ticker == "EQNR" and pos.entry_price == 270.0 and pos.source == "NORWAY"


def test_bought_oslo_ticker_without_a_price_uses_its_own_close(conn, monkeypatch):
    """Without the fix, the stored entry price is last_close("EQNR", None) == 25.0
    -- the wrong, unrelated US quote -- instead of the real Oslo close."""
    import positions
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: True)
    _norway_journal(conn, "EQNR")
    prices = {"EQNR": 25.0, "EQNR.OL": 270.0}
    monkeypatch.setattr(positions, "_yahoo_close", lambda symbol: prices.get(symbol))
    tb._handle_message(conn, "/bought EQNR")
    [pos] = positions.open_positions(conn)
    assert pos.entry_price == 270.0 and pos.source == "NORWAY"


# ------------------------------------------------------------- any-asset lookup
def test_a_coin_is_queued_as_a_coin(conn, analysis):
    import db
    tb._handle_message(conn, "btc")
    tb._handle_message(conn, "$BTC")
    assert [t for _, t in db.pending_analysis(conn)] == ["CRYPTO:BTC", "$BTC"]
    assert analysis.labels == ["CRYPTO:BTC", "$BTC"]


def test_a_coin_symbol_in_the_stock_universe_is_queued_as_the_stock(conn, analysis, monkeypatch):
    import db
    import sources
    monkeypatch.setattr(sources, "cached_coin_symbols", lambda conn: {"BTC", "DASH"})
    monkeypatch.setattr(sources, "stock_universe_symbols", lambda: {"DASH"})
    tb._handle_message(conn, "dash")
    assert [t for _, t in db.pending_analysis(conn)] == ["$DASH"]


def test_an_unknown_stock_is_not_queued(conn, analysis, sent, monkeypatch):
    import db
    import sources
    monkeypatch.setattr(sources, "current_price", lambda asset: (None, None))
    tb._handle_message(conn, "ZZZZQ")
    assert db.pending_analysis(conn) == [] and analysis.labels == []
    assert sent[0].startswith("Не нашёл такой тикер: ZZZZQ (или источники цен сейчас не отвечают). ")
    assert sent[0].endswith(tb.LOOKUP_HINT)


@pytest.mark.parametrize("text", ["CRYPTO:FOO", "foo-usd"])
def test_an_unknown_coin_is_not_queued(conn, analysis, sent, monkeypatch, text):
    import db
    import sources
    priced = []
    monkeypatch.setattr(sources, "current_price", lambda asset: priced.append(asset) or (None, None))
    tb._handle_message(conn, text)
    assert db.pending_analysis(conn) == [] and [a.symbol for a in priced] == ["FOO"]
    assert analysis.labels == []
    assert "Не нашёл такой тикер: FOO (или источники цен сейчас не отвечают)" in sent[0]


def test_a_listed_coin_is_queued_without_a_price_call(conn, analysis, monkeypatch):
    import db
    import sources
    priced = []
    monkeypatch.setattr(sources, "current_price", lambda asset: priced.append(asset) or (None, None))
    for text in ("sol", "CRYPTO:ETH", "btc-usd"):
        tb._handle_message(conn, text)
    assert priced == []
    assert [t for _, t in db.pending_analysis(conn)] == ["CRYPTO:SOL", "CRYPTO:ETH", "CRYPTO:BTC"]


def test_an_unknown_coin_with_a_price_is_queued(conn, analysis):
    import db
    tb._handle_message(conn, "CRYPTO:NEWCOIN")
    assert [t for _, t in db.pending_analysis(conn)] == ["CRYPTO:NEWCOIN"]


def test_crypto_fallback_reply_uses_the_crypto_format(conn, analysis, sent, monkeypatch):
    analysis.ok = False
    monkeypatch.setattr("research.build", lambda conn, text: {
        "kind": "crypto", "ticker": "BTC", "name": "Bitcoin", "current": 1.0, "changes": {},
        "trend": None, "treasury": [], "etf_flows": [], "political": [], "onchain": [],
        "outlook": {"status": "no_table"}})
    tb._handle_message(conn, "BTC")
    assert sent[0].startswith("<b>BTC — Bitcoin</b>") and "Прогноз на месяц" in sent[0]


# ------------------------------------------------------------------- questions
def test_free_text_about_a_ticker_is_a_question_not_a_ticker(conn, analysis, sent):
    import db
    tb._handle_message(conn, "что думаешь про NVDA?")
    [(qid, ticker)] = db.pending_analysis(conn)
    assert ticker == "ВОПРОС" and db.queued_question(conn, qid) == "что думаешь про NVDA?"
    assert analysis.labels == [f"question {qid}"]
    assert sent == ["Думаю над вопросом… (1–5 мин)"]      # the analyst sends the answer itself


def test_several_words_starting_with_a_ticker_are_a_question(conn, analysis):
    import db
    tb._handle_message(conn, "AAPL buy now")
    assert [t for _, t in db.pending_analysis(conn)] == ["ВОПРОС"]


def test_an_unresolved_single_word_is_a_question(conn, analysis, sent):
    import db
    tb._handle_message(conn, "привет")
    [(qid, ticker)] = db.pending_analysis(conn)
    assert ticker == "ВОПРОС" and db.queued_question(conn, qid) == "привет"
    assert analysis.labels == [f"question {qid}"] and sent == ["Думаю над вопросом… (1–5 мин)"]


@pytest.mark.parametrize("text", ["???", "#$%"])
def test_punctuation_is_a_question_too(conn, analysis, text):
    import db
    tb._handle_message(conn, text)
    assert [t for _, t in db.pending_analysis(conn)] == ["ВОПРОС"]


def test_a_failed_question_run_says_it_stays_queued(conn, analysis, sent):
    import db
    analysis.ok = False
    tb._handle_message(conn, "как дела у Tesla и Nvidia")
    assert sent == ["Думаю над вопросом… (1–5 мин)",
                    "Не успел ответить — вопрос в очереди, ответ придёт позже."]
    assert len(db.pending_analysis(conn)) == 1            # still pending for the next run


def test_ask_command_takes_the_text_after_it(conn, analysis, sent):
    import db
    tb._handle_message(conn, "/ask как выглядит BTC?")
    [(qid, _)] = db.pending_analysis(conn)
    assert db.queued_question(conn, qid) == "как выглядит BTC?"
    assert analysis.labels == [f"question {qid}"]


def test_ask_command_with_the_bot_name_and_any_case(conn, analysis):
    import db
    tb._handle_message(conn, "/Ask@my_bot  что с рынком")
    [(qid, _)] = db.pending_analysis(conn)
    assert db.queued_question(conn, qid) == "что с рынком"


@pytest.mark.parametrize("text", ["/ask", "/ask   ", "/ask@my_bot"])
def test_ask_without_text_shows_the_usage_line(conn, analysis, sent, text):
    import db
    tb._handle_message(conn, text)
    assert sent == ["/ask ваш вопрос"]
    assert db.pending_analysis(conn) == [] and analysis.labels == []


def test_a_command_that_only_starts_with_ask_is_not_ask(conn, analysis, sent):
    import db
    tb._handle_message(conn, "/asking something")
    assert sent == [tb.HELP_TEXT] and db.pending_analysis(conn) == []


def test_a_question_is_capped_at_2000_characters(conn, analysis):
    import db
    tb._handle_message(conn, "я " * 3000)
    [(qid, _)] = db.pending_analysis(conn)
    assert len(db.queued_question(conn, qid)) == 2000


def test_two_identical_questions_are_two_rows(conn, analysis):
    import db
    tb._handle_message(conn, "что нового?")
    tb._handle_message(conn, "что нового?")
    assert len(db.pending_analysis(conn)) == 2 and len(analysis.labels) == 2


# ------------------------------------------------------------ /portfolio, help
def test_portfolio_sends_the_model_summary_without_claude(conn, analysis, sent, monkeypatch):
    import datetime as dt
    import paper_report
    seen = []
    monkeypatch.setattr(paper_report, "format_summary",
                        lambda c, today, **kw: seen.append((c, today, kw)) or "СВОДКА <b>x</b>")
    tb._handle_message(conn, "/portfolio")
    assert sent == ["СВОДКА <b>x</b>"] and analysis.labels == []
    assert seen == [(conn, dt.date.today(), {"html": True})]


def test_portfolio_before_any_run_says_so(conn, analysis, sent):
    tb._handle_message(conn, "/portfolio@my_bot")
    assert len(sent) == 1 and "ещё не запущен" in sent[0]


def test_portfolio_failure_is_a_reply_not_a_crash(conn, analysis, sent, monkeypatch):
    import paper_report

    def boom(*a, **k):
        raise ValueError("bad row")
    monkeypatch.setattr(paper_report, "format_summary", boom)
    tb._handle_message(conn, "/portfolio")
    assert len(sent) == 1 and "ValueError" in sent[0]


@pytest.mark.parametrize("text", ["/start", "/help", "/whatever", "", "   ", "/HELP@my_bot"])
def test_help_for_start_help_unknown_commands_and_empty(conn, analysis, sent, text):
    import db
    tb._handle_message(conn, text)
    assert sent == [tb.HELP_TEXT] and db.pending_analysis(conn) == [] and analysis.labels == []


def test_help_text_lists_questions_and_the_portfolio():
    assert "Любой вопрос текстом (или /ask …) — ответит аналитик с графиком TradingView " \
           "и данными бота." in tb.HELP_TEXT
    assert "/portfolio — модельный портфель." in tb.HELP_TEXT
    assert "/backtest" in tb.HELP_TEXT and "/bought" in tb.HELP_TEXT


def test_help_text_says_the_summary_comes_on_fridays():
    assert ("Сводка модельного портфеля приходит по пятницам; /portfolio — в любой момент."
            in tb.HELP_TEXT.splitlines())


def test_the_module_docstring_mentions_questions():
    assert "/ask" in tb.__doc__ and "question" in tb.__doc__.lower()


def test_the_positions_commands_and_backtest_come_first(conn, analysis, sent, monkeypatch):
    import backtest
    monkeypatch.setattr(backtest, "backtest_ticker", lambda conn, t: {"n_purchases": 0, "ticker": t})
    monkeypatch.setattr("telegram_notify.format_ticker_backtest", lambda r: f"BT {r['ticker']}")
    tb._handle_message(conn, "/backtest aapl")
    tb._handle_message(conn, "/positions")
    assert sent[0] == "BT AAPL" and analysis.labels == []


# ------------------------------------------------------------------ the runner
def _script(tmp_path, body, name="run.sh"):
    path = tmp_path / name
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(0o755)
    return path


def _real_runner(monkeypatch, script, timeout=20, grace=15, reap=5):
    monkeypatch.setattr(subprocess, "Popen", REAL_POPEN)      # only ever the tmp script below
    monkeypatch.setattr(tb, "RUN_ANALYSIS_SCRIPT", script)
    monkeypatch.setattr(tb, "RUN_ANALYSIS_TIMEOUT", timeout)
    monkeypatch.setattr(tb, "RUN_ANALYSIS_KILL_GRACE", grace)
    monkeypatch.setattr(tb, "RUN_ANALYSIS_REAP_WAIT", reap)


def _gone(pid, within=5.0):
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_the_run_timeout_is_420_seconds():
    assert tb.RUN_ANALYSIS_TIMEOUT == 420


@pytest.fixture
def row(conn):
    """A queued question: (conn, its id)."""
    import db
    return conn, db.enqueue_question(conn, "?")


def test_the_runner_succeeds_only_when_its_row_was_answered(monkeypatch, tmp_path, row):
    """An exit 0 is not an answer (the pass may never have reached the row); a failed pass may
    still have answered it (another row failed) -- then no fallback may follow."""
    import db
    conn, qid = row
    _real_runner(monkeypatch, _script(tmp_path, "exit 0\n"))
    assert tb._run_analysis(conn, qid, "t") is False
    _real_runner(monkeypatch, _script(tmp_path, "echo boom >&2; exit 3\n", "bad.sh"))
    assert tb._run_analysis(conn, qid, "t") is False
    db.mark_analysis_processed(conn, qid)
    assert tb._run_analysis(conn, qid, "t") is True
    _real_runner(monkeypatch, _script(tmp_path, "exit 0\n", "ok.sh"))
    assert tb._run_analysis(conn, qid, "t") is True


def test_the_runner_sees_the_row_the_analyst_marked(monkeypatch, tmp_path):
    """The analyst marks the row from a process of its own, on the same database file."""
    import db
    path = tmp_path / "queue.db"
    conn = db.connect(path)
    qid = db.enqueue_question(conn, "?")
    marker = tmp_path / "mark.py"
    marker.write_text("import sqlite3, sys\n"
                      "c = sqlite3.connect(sys.argv[1])\n"
                      "c.execute(\"UPDATE claude_analysis_queue SET processed_at = datetime('now') "
                      "WHERE id = ?\", (int(sys.argv[2]),))\n"
                      "c.commit()\n")
    _real_runner(monkeypatch, _script(tmp_path, f"{sys.executable} {marker} {path} {qid}\n"))
    assert tb._run_analysis(conn, qid, "t") is True


def test_the_runner_runs_the_script_from_the_project_in_a_session_of_its_own(monkeypatch, row):
    import db
    calls = []

    class Proc:
        pid, returncode = 4242, 0

        def communicate(self, timeout=None):
            calls.append(("communicate", timeout))
            return "", ""

    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: calls.append((argv, kw)) or Proc())
    conn, qid = row
    db.mark_analysis_processed(conn, qid)
    assert tb._run_analysis(conn, qid, "question 5") is True
    (argv, kw), (name, timeout) = calls
    assert argv == [str(tb.RUN_ANALYSIS_SCRIPT)] and kw["cwd"] == str(tb.BASE_DIR)
    assert kw["start_new_session"] is True and kw["text"] is True
    assert kw["errors"] == "replace"                       # a stray byte in a log line is no crash
    assert kw["stdout"] == subprocess.PIPE and kw["stderr"] == subprocess.PIPE
    assert kw["stdin"] == subprocess.DEVNULL              # Claude never waits on a terminal
    assert timeout == tb.RUN_ANALYSIS_TIMEOUT


def test_the_runner_survives_a_script_that_cannot_start(monkeypatch, tmp_path, row):
    _real_runner(monkeypatch, tmp_path / "missing.sh")
    assert tb._run_analysis(*row, "t") is False


def test_a_timeout_sends_sigterm_to_the_group_first(monkeypatch, row):
    signals, waits = [], []

    class Proc:
        pid, returncode = 4242, None

        def communicate(self, timeout=None):
            waits.append(timeout)
            if len(waits) == 1:
                raise subprocess.TimeoutExpired("run.sh", timeout)
            self.returncode = -15
            return "", ""

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: Proc())
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: signals.append((pgid, sig)))
    assert tb._run_analysis(*row, "t") is False
    assert signals == [(4242, signal.SIGTERM)]                 # the analyst was given time to clean up
    assert waits[0] == tb.RUN_ANALYSIS_TIMEOUT and waits[1] == tb.RUN_ANALYSIS_KILL_GRACE


def test_a_group_that_ignores_sigterm_is_killed_after_the_grace(monkeypatch, row):
    signals = []

    class Proc:
        pid, returncode = 4242, None
        calls = 0

        def communicate(self, timeout=None):
            Proc.calls += 1
            if Proc.calls <= 2:
                raise subprocess.TimeoutExpired("run.sh", timeout)
            return "", ""

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: Proc())
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: signals.append(sig))
    assert tb._run_analysis(*row, "t") is False
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert tb.RUN_ANALYSIS_KILL_GRACE == 15


def test_a_run_that_already_exited_is_not_signalled(monkeypatch, row):
    """Its process group id may already belong to somebody else."""
    signals = []

    class Proc:
        pid, returncode = 4242, 0

        def communicate(self, timeout=None):
            raise KeyboardInterrupt

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: Proc())
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: signals.append(sig))
    with pytest.raises(KeyboardInterrupt):
        tb._run_analysis(*row, "t")
    assert signals == []
    proc = Proc()
    tb._stop_group(proc)
    assert signals == []


def test_the_group_stops_being_signalled_once_the_run_has_exited(monkeypatch):
    signals = []

    class Proc:
        pid, returncode = 4242, None

        def communicate(self, timeout=None):
            self.returncode = -15                    # SIGTERM ended it, and the pipes stay open
            raise subprocess.TimeoutExpired("run.sh", timeout)

    monkeypatch.setattr(os, "killpg", lambda pgid, sig: signals.append(sig))
    tb._stop_group(Proc())
    assert signals == [signal.SIGTERM]


def test_a_real_timeout_stops_the_whole_group_with_sigterm(monkeypatch, tmp_path, row):
    """The script starts a background child and traps SIGTERM: the group gets SIGTERM (the
    marker), the child dies with it, and the runner is back well inside the grace."""
    marker, child = tmp_path / "term", tmp_path / "child.pid"
    script = _script(tmp_path, f"""
trap 'echo term > {marker}; kill $CHILD; exit 143' TERM
sleep 60 &
CHILD=$!
echo $CHILD > {child}
wait
""")
    _real_runner(monkeypatch, script, timeout=1)
    started = time.monotonic()
    assert tb._run_analysis(*row, "t") is False
    assert time.monotonic() - started < 10
    assert marker.read_text().strip() == "term"
    assert _gone(int(child.read_text()))


def test_a_real_group_that_ignores_sigterm_is_killed(monkeypatch, tmp_path, row):
    child = tmp_path / "child.pid"
    script = _script(tmp_path, f"""
trap '' TERM
sleep 60 &
echo $! > {child}
while true; do sleep 1; done
""")
    _real_runner(monkeypatch, script, timeout=1, grace=1)
    assert tb._run_analysis(*row, "t") is False
    assert _gone(int(child.read_text()))


def test_a_real_run_does_not_wait_for_a_grandchild_that_keeps_the_pipes(monkeypatch, tmp_path, row):
    """Claude runs in a session of its own and can hold the bot's pipes after the group is
    gone: the runner must still come back."""
    grandchild = tmp_path / "grandchild.pid"
    script = _script(tmp_path, f"""
trap '' TERM
{sys.executable} -c "import os, time; os.setsid(); open('{grandchild}', 'w').write(str(os.getpid())); time.sleep(30)" &
while true; do sleep 1; done
""")
    _real_runner(monkeypatch, script, timeout=1, grace=1, reap=1)
    try:
        started = time.monotonic()
        assert tb._run_analysis(*row, "t") is False
        assert time.monotonic() - started < 10
    finally:
        try:
            os.kill(int(grandchild.read_text()), signal.SIGKILL)
        except (OSError, ValueError):
            pass
