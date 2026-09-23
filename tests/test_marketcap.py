"""marketcap.py's lookup routing -- yfinance is never called; _fetch is patched."""
from __future__ import annotations

import pytest

import marketcap


@pytest.mark.parametrize("raw,expected", [
    ("AAPL", "AAPL"), ("brk.b", "BRK.B"), ("LEN, LEN.B", "LEN"),
    ("0002097545", None),      # a CIK standing in for a missing ticker
    ("", None), (None, None), ("N/A", None),
])
def test_clean_ticker(raw, expected):
    assert marketcap.clean_ticker(raw) == expected


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def fake_fetch(ticker, suffixes=marketcap.VENUE_SUFFIXES):
        seen.append((ticker, tuple(suffixes)))
        return {"market_cap": 1e9, "currency": "USD"}
    monkeypatch.setattr(marketcap, "_fetch", fake_fetch)
    return seen


def test_us_source_tries_only_the_us_listing(conn, calls):
    marketcap.facts(conn, "LEN, LEN.B", "SEC")
    assert calls == [("LEN", ("",))]


def test_oslo_source_goes_straight_to_oslo(conn, calls):
    """Oslo's NRC used to resolve to National Research Corp, a US company."""
    marketcap.facts(conn, "NRC", "NORWAY")
    assert calls == [("NRC", (".OL",))]


def test_same_symbol_is_cached_separately_per_venue(conn, calls):
    marketcap.facts(conn, "NRC", "SEC")
    marketcap.facts(conn, "NRC", "NORWAY")
    assert len(calls) == 2


def test_unknown_source_falls_back_to_every_venue(conn, calls):
    marketcap.facts(conn, "EQNR")
    assert calls == [("EQNR", marketcap.VENUE_SUFFIXES)]


@pytest.mark.parametrize("ticker", ["0002097545", "DE0007657231", "CRYPTO:BTC", ""])
def test_non_tickers_never_reach_yahoo(conn, calls, ticker):
    assert marketcap.facts(conn, ticker, "SEC13DG") is None
    assert calls == []
