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
