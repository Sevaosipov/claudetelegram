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
    import prices
    import sources

    # /bought sizes its stop from the price history: none, offline.
    monkeypatch.setattr(prices, "_closes", lambda symbol, days: [])
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


@pytest.mark.parametrize("coin", ["SOL", "XRP", "BNB", "DOGE", "AVAX", "HYPE", "LTC", "ENA", "LINK", "TRX", "SUI"])
def test_position_ticker_knows_the_alts(coin):
    assert tb._position_ticker(coin) == f"CRYPTO:{coin}"
    assert tb._position_ticker(coin.lower()) == f"CRYPTO:{coin}"
    assert tb._position_ticker(f"CRYPTO:{coin}") == f"CRYPTO:{coin}"


def test_bought_and_sold_work_for_the_new_coins(conn, replies):
    """/bought HYPE 90 opens CRYPTO:HYPE as a coin, /sold SUI closes CRYPTO:SUI."""
    import positions
    tb._handle_message(conn, "/bought HYPE 90")
    [pos] = positions.open_positions(conn)
    assert (pos.ticker, pos.source, pos.entry_price) == ("CRYPTO:HYPE", "CRYPTO", 90.0)
    tb._handle_message(conn, "/bought SUI 3.5")
    assert {p.ticker for p in positions.open_positions(conn)} == {"CRYPTO:HYPE", "CRYPTO:SUI"}
    tb._handle_message(conn, "/sold SUI")
    assert [p.ticker for p in positions.open_positions(conn)] == ["CRYPTO:HYPE"]
    assert replies[-1] == "Позиция CRYPTO:SUI закрыта."


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
    assert "GRAB" in replies[-1] and "+25,0%" in replies[-1]


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
    import prices
    import positions
    monkeypatch.setattr("positions.last_close", lambda ticker, source=None: 180.0)
    monkeypatch.setattr(prices, "_closes", _august_closes)
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
    """A row as the tiered design wrote it (tier "strong"); the scoring's own rows are covered
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


# ------------------------------------------- Oslo and Stockholm listings, the Yahoo way
def test_position_listing_splits_a_venue_and_leaves_the_rest_alone():
    assert tb._position_listing("EQNR.OL") == ("EQNR", "NORWAY")
    assert tb._position_listing("$volv-b.st") == ("VOLV-B", "SWEDEN")
    assert tb._position_listing("ESSITY-B.ST") == ("ESSITY-B", "SWEDEN")      # 11 characters: still a ticker
    assert tb._position_listing("GRAB") == ("GRAB", None)
    assert tb._position_listing("BRK.B") == ("BRK.B", None)
    assert tb._position_listing("btc") == ("CRYPTO:BTC", None)
    assert tb._position_listing("CRYPTO:eth") == ("CRYPTO:ETH", None)
    assert tb._position_listing("SE0000000001") == ("SE0000000001", None)     # an ISIN: its source comes from the journal
    for bad in ("", ".OL", ".ST", "CRYPTO:ZZZ", "TOOLONGTICKER.OL", "TOOLONGTICKERX", "???"):
        assert tb._position_listing(bad) is None, bad


def test_bought_an_oslo_listing_stores_the_bare_ticker_with_its_source(conn, replies):
    import positions
    tb._handle_message(conn, "/bought EQNR.OL 150")
    [pos] = positions.open_positions(conn)
    assert (pos.ticker, pos.source, pos.entry_price) == ("EQNR", "NORWAY", 150.0)
    assert replies[-1].startswith("Записал EQNR по 150,00; ")


def test_bought_a_stockholm_listing_stores_the_bare_ticker_with_its_source(conn, replies):
    import positions
    tb._handle_message(conn, "/bought volv-b.st 270")
    tb._handle_message(conn, "/bought ESSITY-B.ST 300")
    assert [(p.ticker, p.source, p.entry_price) for p in positions.open_positions(conn)] == [
        ("VOLV-B", "SWEDEN", 270.0), ("ESSITY-B", "SWEDEN", 300.0)]


def test_bought_a_stockholm_listing_says_its_insiders_are_not_watched(conn, replies):
    """Finansinspektionen's signals are keyed by ISIN: the reply must not claim there was no signal."""
    tb._handle_message(conn, "/bought VOLV-B.ST 270")
    assert "за продажами инсайдеров Стокгольма не слежу (сигналы идут по ISIN)" in replies[-1]
    assert "сильного сигнала по нему не было" not in replies[-1]


def test_sold_an_oslo_listing_closes_it(conn, replies):
    import positions
    tb._handle_message(conn, "/bought EQNR.OL 150")
    tb._handle_message(conn, "/sold EQNR.OL")
    assert positions.open_positions(conn) == [] and replies[-1] == "Позиция EQNR закрыта."
    tb._handle_message(conn, "/sold eqnr.ol")
    assert "нет открытой" in replies[-1]
    tb._handle_message(conn, "/bought EQNR.OL 150")
    tb._handle_message(conn, "/sold EQNR")                       # the bare ticker closes it as well
    assert positions.open_positions(conn) == []


def test_an_oslo_listing_is_priced_on_oslo_with_no_signal_to_say_so(conn, monkeypatch):
    """/bought EQNR.OL used to store the ticker "EQNR.OL", which Yahoo reads as EQNR-OL. Now it is
    the bare EQNR of NORWAY, priced on EQNR.OL -- not on the unrelated US EQNR -- with or without a
    signal in the journal. (No `replies` fixture: this needs the real last_close.)"""
    import positions
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: True)
    prices = {"EQNR": 25.0, "EQNR.OL": 270.0}
    monkeypatch.setattr(positions, "_yahoo_close", lambda symbol: prices.get(symbol))
    tb._handle_message(conn, "/bought EQNR.OL")
    [pos] = positions.open_positions(conn)
    assert (pos.ticker, pos.source, pos.entry_price) == ("EQNR", "NORWAY", 270.0)
    tb._handle_message(conn, "/sold EQNR.OL")
    tb._handle_message(conn, "/bought EQNR.OL 275")              # the Oslo price is not "too far" from the Oslo close
    assert positions.open_positions(conn)[0].entry_price == 275.0
    tb._handle_message(conn, "/sold EQNR.OL")
    tb._handle_message(conn, "/bought EQNR.OL 25")               # the US price is
    assert positions.open_positions(conn) == []


def test_portfolio_shows_an_oslo_position_at_its_oslo_price(conn, monkeypatch):
    """The whole chain: /bought EQNR.OL, then /portfolio reads the price of EQNR.OL (not of the
    unrelated US EQNR) for the stored EQNR of NORWAY."""
    import positions
    out = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: out.append(msg) or True)
    prices = {"EQNR": 25.0, "EQNR.OL": 270.0}
    monkeypatch.setattr(positions, "_yahoo_close", lambda symbol: prices.get(symbol))
    tb._handle_message(conn, "/bought EQNR.OL 250")
    out.clear()
    tb._handle_message(conn, "/portfolio")
    assert "• EQNR: вход 250,00 (" in out[0] and "сейчас 270,00 (+8,0%)" in out[0]


def test_bought_an_oslo_listing_watches_the_oslo_insiders(conn, replies):
    import db
    import json
    for source, who in (("NORWAY", "Oslo Boss"), ("SEC", "US Boss")):       # NRC: Oslo's and New York's
        db.journal_signal(conn, {"source": source, "kind": "cluster", "ticker": "NRC", "tier": "buy",
                                 "members": json.dumps([who])})
    tb._handle_message(conn, "/bought NRC.OL 100")
    assert "слежу за продажами: Oslo Boss" in replies[-1] and "US Boss" not in replies[-1]


def test_with_no_price_to_be_found_the_hint_is_a_command_that_keeps_the_listing(conn, replies):
    """The `replies` fixture has no quotes: the hint has to be something to copy, and /bought EQNR
    would be the US EQNR."""
    import positions
    tb._handle_message(conn, "/bought EQNR.OL")
    assert replies[-1] == "Не нашёл цену EQNR — укажите её: /bought EQNR.OL 12.34"
    tb._handle_message(conn, "/bought volv-b.st")
    assert replies[-1] == "Не нашёл цену VOLV-B — укажите её: /bought VOLV-B.ST 12.34"
    tb._handle_message(conn, "/bought GRAB")
    assert replies[-1] == "Не нашёл цену GRAB — укажите её: /bought GRAB 12.34"
    assert positions.open_positions(conn) == []


@pytest.mark.parametrize("text", ["/bought .OL 5", "/bought EQNR.OL abc", "/bought TOOLONGTICKER.OL 5",
                                  "/bought EQNR.OL 0", "/sold .ST", "/bought"])
def test_a_bad_listing_or_price_gets_the_usage(conn, replies, text):
    import positions
    tb._handle_message(conn, text)
    assert positions.open_positions(conn) == [] and replies[-1] == tb.POSITIONS_USAGE


def test_a_us_ticker_a_share_class_and_a_coin_are_unchanged(conn, replies):
    import positions
    tb._handle_message(conn, "/bought grab 18.40")
    tb._handle_message(conn, "/bought brk.b 400")
    tb._handle_message(conn, "/bought BTC 60000")
    assert [(p.ticker, p.source) for p in positions.open_positions(conn)] == [
        ("GRAB", None), ("BRK.B", None), ("CRYPTO:BTC", "CRYPTO")]


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


# ------------------------------------------------------------ /model, /portfolio, help
@pytest.mark.parametrize("text", ["/model", "/model@my_bot", "/Model", "/MODEL extra words"])
def test_model_is_no_command_any_more_it_answers_with_the_help(conn, analysis, sent, text):
    """The virtual portfolio is gone: /model is an unknown command, which gets the help text -- no summary, no
    Claude, no queued row."""
    import db
    tb._handle_message(conn, text)
    assert sent == [tb.HELP_TEXT] and analysis.labels == [] and db.pending_analysis(conn) == []


def test_the_bot_has_no_model_handler_and_does_not_import_the_old_report():
    assert not hasattr(tb, "_handle_model") and not hasattr(tb, "paper_report")


def test_portfolio_shows_your_own_positions_with_their_status(conn, replies, monkeypatch):
    monkeypatch.setattr("positions.last_close", lambda ticker, source=None: 50.0)
    tb._handle_message(conn, "/bought GRAB 40")
    replies.clear()
    tb._handle_message(conn, "/portfolio")
    [text] = replies
    # «💼 Trading 212» comes first (no key here: see the Trading 212 tests below), then what was /bought
    assert "\n\n<b>✍️ Вне Trading 212</b>\n\n• GRAB: вход 40,00 (" in text
    assert text.startswith("<b>💼 Trading 212</b>\n") and "Ваш портфель" not in text
    assert "сейчас 50,00 (+25,0%)" in text
    assert "   стоп 34,00 (−15% от максимума 40,00), до стопа 32,0%" in text      # no history: the fallback
    assert text.endswith("Сигнал на продажу придёт сразу. /sold TICKER — закрыть.")


def test_a_price_at_the_peak_can_fall_by_the_stop_before_it_fires(conn, replies, monkeypatch):
    """The 10% stop of a calm stock, the price at its peak: «до стопа 10,0%»."""
    import prices
    monkeypatch.setattr("positions.last_close", lambda ticker, source=None: 180.0)
    monkeypatch.setattr(prices, "_closes", _august_closes)
    tb._handle_message(conn, "/bought NVDA 180")
    replies.clear()
    tb._handle_message(conn, "/portfolio")
    assert "   стоп 162,00 (−10% от максимума 180,00), до стопа 10,0%" in replies[0]


def test_portfolio_and_positions_send_format_my_portfolio_of_the_rows(conn, sent, monkeypatch):
    import datetime as dt
    import positions
    status = {"last": 1.0}
    seen = []
    monkeypatch.setattr("positions.position_status", lambda pos, today, **kw: dict(status, d=pos.ticker))
    monkeypatch.setattr("telegram_notify.format_my_portfolio", lambda rows, **kw: seen.append(rows) or "MINE")
    positions.open_position(conn, "GRAB", 18.0, today=dt.date.today(), closes_fn=lambda t, s=None: [])
    for text in ("/portfolio", "/positions", "/Portfolio@my_bot"):
        tb._handle_message(conn, text)
    assert sent == ["MINE"] * 3
    [(pos, st)] = seen[0]
    assert (pos.ticker, st) == ("GRAB", {"last": 1.0, "d": "GRAB"})


def test_portfolio_has_no_model_line_even_when_the_old_virtual_books_hold_the_name(conn, replies, monkeypatch):
    """The paper_* rows of the removed portfolio stay in the database as an archive; nothing reads them."""
    monkeypatch.setattr("positions.last_close", lambda ticker, source=None: 50.0)
    conn.execute(
        "INSERT INTO paper_positions (book, ticker, source, symbol, currency, fill_date, cost_eur, "
        "net_eur, entry_close, entry_fx) VALUES ('MODEL-S', 'GRAB', 'SEC', 'GRAB', 'USD', '2026-09-25', "
        "1000, 998, 40, 1.16)")
    conn.commit()
    tb._handle_message(conn, "/bought GRAB 40")
    replies.clear()
    tb._handle_message(conn, "/portfolio")
    [text] = replies
    assert "• GRAB: вход 40,00" in text and "модел" not in text.lower()


def test_portfolio_with_nothing_bought_says_how_to_add_one(conn, sent, monkeypatch):
    tb._handle_message(conn, "/portfolio")
    [text] = sent
    assert text.endswith("\n\nВаших позиций нет. Купили? /bought TICKER [цена] — например /bought GME 23.10.")
    assert text.startswith("<b>💼 Trading 212</b>\n")              # and why it shows no account: no key


def test_portfolio_failure_is_a_reply_not_a_crash(conn, sent, monkeypatch):
    def boom(*a, **k):
        raise ValueError("bad row")
    monkeypatch.setattr("positions.portfolio_rows", boom)
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
    assert "/portfolio — ваш счёт Trading 212 и позиции /bought." in tb.HELP_TEXT.splitlines()
    assert "/backtest" in tb.HELP_TEXT and "/bought" in tb.HELP_TEXT


def test_help_text_says_when_the_signals_come():
    assert ("Сигналы на покупку приходят по пятницам, сигнал на продажу по вашим позициям — сразу."
            in tb.HELP_TEXT.splitlines())
    assert "в любой момент" not in tb.HELP_TEXT


def test_help_text_has_no_model_lines():
    assert "/model" not in tb.HELP_TEXT and "модел" not in tb.HELP_TEXT.lower()


def test_the_positions_usage_says_how_to_record_a_buy_and_a_sale():
    assert tb.POSITIONS_USAGE.splitlines() == [
        "/bought TICKER [цена] — отметить покупку (без цены — последнее закрытие); "
        "биржи Осло/Стокгольма: EQNR.OL, VOLV-B.ST",
        "/sold TICKER — отметить продажу",
        "/portfolio — ваши позиции"]
    assert tb.POSITIONS_USAGE in tb.HELP_TEXT


def test_the_module_docstring_mentions_questions():
    assert "/ask" in tb.__doc__ and "question" in tb.__doc__.lower()


def test_the_module_docstring_says_what_portfolio_shows():
    doc = " ".join(tb.__doc__.split())
    assert "/portfolio" in doc and "/positions" in doc and "own positions" in doc
    assert "/model" not in doc and "model portfolio" not in doc


def test_the_positions_commands_and_backtest_come_first(conn, analysis, sent, monkeypatch):
    import backtest
    monkeypatch.setattr(backtest, "backtest_ticker", lambda conn, t: {"n_purchases": 0, "ticker": t})
    monkeypatch.setattr("telegram_notify.format_ticker_backtest", lambda r: f"BT {r['ticker']}")
    tb._handle_message(conn, "/backtest aapl")
    tb._handle_message(conn, "/positions")
    assert sent[0] == "BT AAPL" and analysis.labels == []


# ------------------------------------------------------------------ Trading 212
_T212_SUMMARY_LINE = "Счёт: €12 346 · вложено €10 000 · P/L +€346 (+3,5%) · свободно €2 000"
_KEY_HINT = ("Создайте в Trading 212 → Настройки → API ключ только для чтения (Portfolio, Account data) "
             "и положите в .env")


def _t212_holding(**over):
    import t212_account as ta
    fields = dict(t212_ticker="GME_US_EQ", name="GameStop", isin="US36467W1099", currency="USD",
                  quantity=10.0, avg_price=23.10, current_price=24.05,
                  created_at="2026-09-28T14:03:11.000+02:00", value_eur=207.3, cost_eur=199.0,
                  pnl_eur=8.30, account_currency="EUR")
    return ta.T212Position(**{**fields, **over})


def _t212_account(monkeypatch, *holdings, error=None):
    """What Trading 212 answers to the bot's live call (never the network)."""
    import t212_account as ta
    summary = ta.T212Summary("EUR", 12345.67, 2000.0, 10345.67, 10000.0, 345.67, 12.5)

    def fetch(session=None):
        if error is not None:
            raise error
        return list(holdings), summary
    monkeypatch.setattr(ta, "fetch_account", fetch)
    return fetch


def _t212_synced(conn, *holdings, at=None):
    """The holdings as a sync stored them (no messages)."""
    import datetime as dt
    import t212_account as ta
    summary = ta.T212Summary("EUR", 12345.67, 2000.0, 10345.67, 10000.0, 345.67, 12.5)
    # Yahoo knows the symbol at about Trading 212's price (one close: too few to size a stop)
    return ta.sync(conn, fetch=lambda: (list(holdings), summary), notify=lambda text: True,
                   now=at or dt.datetime.now(), closes_fn=lambda t, s=None: [("2026-08-03", 24.0)])


def test_bought_a_ticker_held_in_trading_212_says_it_is_already_tracked(conn, replies):
    import positions
    _t212_synced(conn, _t212_holding())
    for text in ("/bought gme 23.10", "/bought GME"):
        tb._handle_message(conn, text)
    assert replies == ["GME уже отслеживается из Trading 212."] * 2
    [pos] = positions.open_positions(conn)
    assert (pos.origin, pos.entry_price) == ("t212", 23.10)


def test_sold_a_trading_212_holding_says_to_sell_it_there_and_closes_nothing(conn, replies):
    import positions
    _t212_synced(conn, _t212_holding(), _t212_holding(t212_ticker="SAPd_EQ", isin="DE0007164600", currency="EUR"))
    tb._handle_message(conn, "/sold GME")
    tb._handle_message(conn, "/sold de0007164600")
    assert replies == ["GME отслеживается из Trading 212: продайте там — бот увидит продажу сам.",
                       "DE0007164600 отслеживается из Trading 212: продайте там — бот увидит продажу сам."]
    assert {p.ticker for p in positions.open_positions(conn)} == {"GME", "DE0007164600"}


def test_a_frankfurt_holding_is_not_taken_for_the_us_stock_of_the_same_symbol(conn, replies):
    """SAPd_EQ is shown as SAP, but SAP typed in Telegram is the US listing: hundreds of non-US
    instruments share a US company's symbol. Only its own key -- the ISIN -- names the holding."""
    import positions
    _t212_synced(conn, _t212_holding(t212_ticker="SAPd_EQ", isin="DE0007164600", currency="EUR"))
    tb._handle_message(conn, "/sold sap")
    assert replies[-1] == "По SAP нет открытой позиции."
    tb._handle_message(conn, "/bought SAP 250")                     # the US SAP: a position of its own
    assert positions.find_open(conn, "SAP").origin == "manual"
    tb._handle_message(conn, "/sold SAP")
    assert replies[-1] == "Позиция SAP закрыта."
    tb._handle_message(conn, "/sold DE0007164600")                  # the holding, by its key
    assert replies[-1] == ("DE0007164600 отслеживается из Trading 212: продайте там — "
                           "бот увидит продажу сам.")
    assert [(p.ticker, p.origin) for p in positions.open_positions(conn)] == [("DE0007164600", "t212")]


def test_a_us_holding_is_known_by_its_ticker_and_by_its_trading_212_code(conn, replies):
    """Meta is held as FB_US_EQ and tracked as META: both names mean that holding."""
    import positions
    conn.execute("INSERT INTO t212_instruments (ticker, isin, type, short_name, currency) "
                 "VALUES ('FB_US_EQ', 'US30303M1027', 'STOCK', 'META', 'USD')")
    conn.commit()
    _t212_synced(conn, _t212_holding(t212_ticker="FB_US_EQ", isin="US30303M1027"))
    assert [p.ticker for p in positions.open_positions(conn)] == ["META"]
    for text in ("/sold META", "/sold fb", "/bought FB 500", "/bought META"):
        tb._handle_message(conn, text)
    assert replies == ["META отслеживается из Trading 212: продайте там — бот увидит продажу сам.",
                       "FB отслеживается из Trading 212: продайте там — бот увидит продажу сам.",
                       "FB уже отслеживается из Trading 212.", "META уже отслеживается из Trading 212."]
    assert [(p.ticker, p.origin) for p in positions.open_positions(conn)] == [("META", "t212")]


def test_bought_and_sold_still_work_for_what_is_not_in_trading_212(conn, replies):
    import positions
    _t212_synced(conn, _t212_holding())
    tb._handle_message(conn, "/bought GRAB 18")
    assert positions.find_open(conn, "GRAB").origin == "manual"
    tb._handle_message(conn, "/sold GRAB")
    assert [p.ticker for p in positions.open_positions(conn)] == ["GME"] and "закрыта" in replies[-1]


def test_portfolio_asks_trading_212_live_and_shows_the_account_then_the_rest(conn, replies, monkeypatch):
    monkeypatch.setattr("positions.last_close", lambda ticker, source=None: 50.0)
    tb._handle_message(conn, "/bought GRAB 40")
    _t212_synced(conn, _t212_holding(current_price=23.0))
    _t212_account(monkeypatch, _t212_holding())                     # now it is 24,05
    replies.clear()
    tb._handle_message(conn, "/portfolio")
    [text] = replies
    blocks = text.split("\n\n")
    assert blocks[0] == "<b>💼 Trading 212</b>\n" + _T212_SUMMARY_LINE
    import datetime as dt
    held = (dt.date.today() - dt.date(2026, 9, 28)).days            # since Trading 212's purchase date
    assert blocks[1].splitlines() == [
        f"• GME — 10 шт., средняя 23,10, сейчас 24,05 USD (+4,1%), €+8,30, {held} дн.",   # Trading 212's price now
        # it was in the account before the bot looked: the stop is measured from its price then (23,00)
        "   стоп 19,55 (−15% от максимума 23,00), до стопа 18,7%"]
    assert blocks[2] == "<b>✍️ Вне Trading 212</b>" and blocks[3].startswith("• GRAB: вход 40,00 (")
    assert blocks[4].startswith("Средний результат: +14,6% по 2 позициям\n")     # (+4,1% and +25,0%) / 2
    assert text.endswith("Сигнал на продажу придёт сразу. /sold TICKER — закрыть.")
    # the live answer is kept like a sync's
    assert conn.execute("SELECT price FROM t212_prices WHERE ticker = 'GME'").fetchall() == [(24.05,)]


def test_portfolio_shows_the_stored_holdings_when_trading_212_does_not_answer(conn, replies, monkeypatch):
    import datetime as dt
    import t212_account as ta
    _t212_synced(conn, _t212_holding(), at=dt.datetime.now().replace(hour=14, minute=5, second=0))
    _t212_account(monkeypatch, error=ta.T212Error("ReadTimeout", "network"))
    tb._handle_message(conn, "/portfolio")
    [text] = replies
    head, block = text.split("\n\n")[:2]
    assert head == ("<b>💼 Trading 212</b>\n"
                    "⚠️ Trading 212 не ответил (ReadTimeout) — данные на 14:05 последней синхронизации")
    held = (dt.date.today() - dt.date(2026, 9, 28)).days
    assert block.startswith(f"• GME — 10 шт., средняя 23,10, сейчас 24,05 USD (+4,1%), $+9,50, {held} дн.\n"
                            "   стоп ")
    assert "Счёт:" not in text and _KEY_HINT not in text


def test_portfolio_with_no_key_says_so_and_how_to_get_one(conn, replies):
    tb._handle_message(conn, "/portfolio")                          # no key in the tests
    assert replies[0].split("\n\n")[0] == "<b>💼 Trading 212</b>\n⚠️ Ключ Trading 212 не задан\n" + _KEY_HINT


def test_portfolio_with_a_key_that_lacks_the_rights_says_so_and_how_to_get_one(conn, replies, monkeypatch):
    import t212_account as ta
    _t212_account(monkeypatch, error=ta.T212Error(ta.NO_RIGHTS, "forbidden"))
    tb._handle_message(conn, "/portfolio")
    assert replies[0].split("\n\n")[0] == (
        "<b>💼 Trading 212</b>\n⚠️ Ключу Trading 212 не хватает прав: нужны чтение портфеля и счёта\n"
        + _KEY_HINT)


def test_a_trading_212_holding_is_not_listed_again_outside_trading_212(conn, replies, monkeypatch):
    _t212_synced(conn, _t212_holding())
    _t212_account(monkeypatch, _t212_holding())
    tb._handle_message(conn, "/portfolio")
    assert replies[0].count("GME") == 1 and "Вне Trading 212" not in replies[0]
    assert "• GME — 10 шт." in replies[0]


class _Clock:
    """A clock the loop's polls move: each poll takes `step` seconds."""

    def __init__(self, start=1000.0, step=100.0):
        self.t, self.step, self.polls, self.sleeps = start, step, [], []

    def __call__(self):
        return self.t

    def poll(self, conn, token, chat_id, session):
        self.polls.append(self.t)
        self.t += self.step

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


def test_the_loop_syncs_trading_212_at_start_and_then_every_900_seconds(conn, monkeypatch):
    clock, syncs = _Clock(), []
    monkeypatch.setattr(tb, "_poll_once", clock.poll)
    monkeypatch.setattr(tb.t212_account, "sync", lambda c: syncs.append(clock.t))
    tb._serve(conn, "tok", "1", object(), clock=clock, sleep=clock.sleep, rounds=28)
    assert tb.T212_SYNC_SECONDS == 900
    assert syncs == [1000.0, 1900.0, 2800.0, 3700.0]                # the first one before the first poll
    assert len(clock.polls) == 28 and clock.polls[0] == 1000.0


def test_the_loop_reads_the_wall_clock_so_a_mac_that_slept_syncs_when_it_wakes(conn, monkeypatch):
    """time.monotonic stands still while the Mac sleeps; the wall clock does not."""
    import inspect
    assert inspect.signature(tb._serve).parameters["clock"].default is time.time
    clock, syncs = _Clock(step=10.0), []

    def poll(conn_, token, chat_id, session):
        clock.poll(conn_, token, chat_id, session)
        if len(clock.polls) == 2:
            clock.t += 8 * 3600                                     # asleep for eight hours
    monkeypatch.setattr(tb, "_poll_once", poll)
    monkeypatch.setattr(tb.t212_account, "sync", lambda c: syncs.append(clock.t))
    tb._serve(conn, "tok", "1", object(), clock=clock, sleep=clock.sleep, rounds=4)
    assert syncs == [1000.0, 1020.0 + 8 * 3600]                     # at once after the wake, not 15 minutes later


def test_a_clock_set_back_does_not_put_the_sync_off(conn, monkeypatch):
    clock, syncs = _Clock(start=100_000.0, step=10.0), []

    def poll(conn_, token, chat_id, session):
        clock.poll(conn_, token, chat_id, session)
        if len(clock.polls) == 1:
            clock.t -= 50_000.0                                     # the clock was corrected, far back
    monkeypatch.setattr(tb, "_poll_once", poll)
    monkeypatch.setattr(tb.t212_account, "sync", lambda c: syncs.append(clock.t))
    tb._serve(conn, "tok", "1", object(), clock=clock, sleep=clock.sleep, rounds=3)
    assert syncs == [100_000.0, 50_010.0]                           # not 50 000 seconds later


@pytest.mark.parametrize("kind", ["unauthorized", "forbidden"])
def test_after_a_key_error_the_loop_tries_again_in_an_hour_not_in_fifteen_minutes(conn, monkeypatch, kind):
    """A 401 or a 403 does not heal by itself: asking every 15 minutes only hammers the API."""
    import t212_account as ta
    clock, syncs = _Clock(step=300.0), []

    def sync(c):
        syncs.append(clock.t)
        return ta.SyncResult(error="ключ", error_kind=kind)
    monkeypatch.setattr(tb, "_poll_once", clock.poll)
    monkeypatch.setattr(tb.t212_account, "sync", sync)
    tb._serve(conn, "tok", "1", object(), clock=clock, sleep=clock.sleep, rounds=30)
    assert tb.T212_KEY_RETRY_SECONDS == 3600
    assert syncs == [1000.0, 4600.0, 8200.0]


def test_any_other_failure_and_a_key_that_works_again_keep_the_fifteen_minutes(conn, monkeypatch):
    import t212_account as ta
    clock, syncs = _Clock(step=300.0), []
    answers = iter([ta.SyncResult(error="ключ", error_kind="forbidden"),          # an hour
                    ta.SyncResult(error="ReadTimeout", error_kind="network"),      # 15 minutes
                    ta.SyncResult(), ta.SyncResult(), ta.SyncResult()])            # 15 minutes each

    def sync(c):
        syncs.append(clock.t)
        return next(answers)
    monkeypatch.setattr(tb, "_poll_once", clock.poll)
    monkeypatch.setattr(tb.t212_account, "sync", sync)
    tb._serve(conn, "tok", "1", object(), clock=clock, sleep=clock.sleep, rounds=20)
    assert syncs == [1000.0, 4600.0, 5500.0, 6400.0]


def test_a_failed_poll_does_not_print_the_bot_token(conn, monkeypatch, capsys):
    """requests puts the whole URL -- the token in it -- into its error text."""
    import requests
    token = "123456789:AAH-fake_TOKEN-value_xyz"

    def poll(conn_, tok, chat_id, session):
        raise requests.ConnectionError(
            f"HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded with url: "
            f"/bot{token}/getUpdates?timeout=25 (Caused by NewConnectionError('nodename nor servname'))")
    monkeypatch.setattr(tb, "_poll_once", poll)
    monkeypatch.setattr(tb.t212_account, "sync", lambda c: None)
    tb._serve(conn, token, "1", object(), clock=lambda: 0.0, sleep=lambda s: None, rounds=2)
    err = capsys.readouterr().err
    assert err.count("poll failed") == 2 and "retrying in 5s" in err and "getUpdates" in err
    assert token not in err and "AAH-fake" not in err and "bot<token>" in err


def test_a_sync_that_raises_is_logged_and_the_polling_goes_on(conn, monkeypatch, capsys):
    clock = _Clock(step=1000.0)

    def boom(c):
        raise RuntimeError("Authorization: Basic c2VjcmV0")
    monkeypatch.setattr(tb, "_poll_once", clock.poll)
    monkeypatch.setattr(tb.t212_account, "sync", boom)
    tb._serve(conn, "tok", "1", object(), clock=clock, sleep=clock.sleep, rounds=3)
    assert len(clock.polls) == 3
    err = capsys.readouterr().err
    assert err.count("RuntimeError") == 3 and "c2VjcmV0" not in err   # the type, never the text


def test_a_failed_poll_still_backs_off_and_the_sync_keeps_its_time(conn, monkeypatch, capsys):
    import requests
    clock, syncs = _Clock(step=0.0), []
    answers = iter([requests.ConnectionError("down")] * 3 + [None, requests.ConnectionError("down")])

    def poll(conn_, token, chat_id, session):
        clock.polls.append(clock.t)
        answer = next(answers)
        if answer is not None:
            raise answer
    monkeypatch.setattr(tb, "_poll_once", poll)
    monkeypatch.setattr(tb.t212_account, "sync", lambda c: syncs.append(clock.t))
    tb._serve(conn, "tok", "1", object(), clock=clock, sleep=clock.sleep, rounds=5)
    assert clock.sleeps == [5, 10, 20, 5]                           # doubled, and back to 5 after a good poll
    assert syncs == [1000.0] and len(clock.polls) == 5


def test_main_runs_the_loop_and_once_only_polls(monkeypatch, tmp_path):
    served, polled = [], []
    monkeypatch.setattr(tb, "DB_PATH", tmp_path / "d.db")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setattr(tb, "_serve", lambda conn, token, chat_id, session: served.append((token, chat_id)))
    monkeypatch.setattr(tb, "_poll_once", lambda conn, token, chat_id, session: polled.append(token))
    monkeypatch.setattr(tb.t212_account, "sync", lambda c: pytest.fail("--once does not sync"))
    monkeypatch.setattr(sys, "argv", ["telegram_bot.py", "--once"])
    assert tb.main() == 0 and polled == ["tok"] and served == []
    monkeypatch.setattr(sys, "argv", ["telegram_bot.py"])
    tb.main()
    assert served == [("tok", "1")] and polled == ["tok"]


def test_the_module_docstring_says_the_account_is_only_read():
    doc = " ".join(tb.__doc__.split())
    assert "Trading 212" in doc and "every 15 minutes" in doc and "never places an order" in doc


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
