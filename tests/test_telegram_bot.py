"""telegram_bot.py's message-handling control flow -- subprocess.run and all
network calls (Telegram, research.build) are monkeypatched; no real claude
invocation or Telegram send happens in tests."""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

import telegram_bot as tb


def test_extract_ticker_accepts_plain_and_dollar_prefixed():
    assert tb._extract_ticker("AAPL") == "AAPL"
    assert tb._extract_ticker("$aapl") == "AAPL"
    assert tb._extract_ticker("aapl buy now") == "AAPL"


def test_extract_ticker_rejects_garbage():
    assert tb._extract_ticker("waytoolongtobeaticker") is None  # >10 chars
    assert tb._extract_ticker("???") is None
    assert tb._extract_ticker("") is None


def test_handle_message_success_does_not_send_its_own_reply(conn, monkeypatch):
    """On a successful synchronous run, run_claude_analysis.sh itself sends
    the one merged message -- telegram_bot.py must not also send anything,
    or the user would get two messages, defeating the whole point."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stderr=""))
    sent = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    tb._handle_message(conn, "AAPL")
    assert sent == []


def test_handle_message_enqueues_before_running(conn, monkeypatch):
    import db
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stderr=""))
    tb._handle_message(conn, "AAPL")
    assert [t for _, t in db.pending_analysis(conn)] == ["AAPL"]  # stays pending; only
    # run_claude_analysis.sh itself marks a row processed, on a confirmed send


def test_handle_message_falls_back_on_subprocess_failure(conn, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stderr="boom"))
    fake_rep = {"ticker": "AAPL", "opinion": None, "insiders": {"buys": [], "sells": []},
                "stakes": [], "political": [], "tradingview": None}
    monkeypatch.setattr("research.build", lambda conn, ticker: fake_rep)
    sent = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    tb._handle_message(conn, "AAPL")
    assert len(sent) == 1
    assert "попробуется снова" in sent[0]


def test_handle_message_falls_back_on_subprocess_timeout(conn, monkeypatch):
    def raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="run_claude_analysis.sh", timeout=300)
    monkeypatch.setattr(subprocess, "run", raise_timeout)
    fake_rep = {"ticker": "AAPL", "opinion": None, "insiders": {"buys": [], "sells": []},
                "stakes": [], "political": [], "tradingview": None}
    monkeypatch.setattr("research.build", lambda conn, ticker: fake_rep)
    sent = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    tb._handle_message(conn, "AAPL")
    assert len(sent) == 1


def test_handle_message_double_failure_sends_generic_error(conn, monkeypatch):
    """If the fallback itself blows up too (e.g. research.build fails), the
    user still gets SOME reply, not silence."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stderr="boom"))
    def raise_error(conn, ticker):
        raise ValueError("network down")
    monkeypatch.setattr("research.build", raise_error)
    sent = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    tb._handle_message(conn, "AAPL")
    assert len(sent) == 1
    assert "Не удалось" in sent[0]


def test_handle_message_unrecognized_text_shows_help(conn, monkeypatch):
    sent = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    tb._handle_message(conn, "???")
    assert len(sent) == 1 and "тикер" in sent[0].lower()


def test_get_updates_treats_a_read_timeout_as_an_empty_poll():
    """A long poll outliving its timeout (e.g. waking from sleep) loses nothing, so it
    must not surface as an error -- that used to trigger up to 5 minutes of backoff."""
    import requests

    class Session:
        def get(self, *a, **k):
            raise requests.ReadTimeout("read timed out")
    assert tb._get_updates("tok", None, Session()) == []


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
    the user's price is still stored -- only the stop-loss check is affected."""
    import positions
    tb._handle_message(conn, "/bought ORK 12.5")
    assert positions.open_positions(conn)[0].entry_price == 12.5
    assert "стоп-лосс" in replies[-1] and "не отслеживается" in replies[-1]
