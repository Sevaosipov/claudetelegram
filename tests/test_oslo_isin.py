"""Oslo ticker -> ISIN (norway.isin_for_ticker) and the Trading 212 check that needs
it: Trading 212 sells Norwegian companies only as EUR listings in Frankfurt, keyed
by ISIN, never under their Oslo ticker. Offline -- Euronext responses are saved
fixtures, captured from the live search."""
from __future__ import annotations

import datetime as dt
import json

import pytest
import requests

import cluster
import norway
import strategy
import trading212
from conftest import fixture_text

TODAY = dt.date.today()
RECENT = (TODAY - dt.timedelta(days=1)).isoformat()


class _Session:
    """Answers Euronext searches from the saved fixtures; counts calls."""

    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(params["q"])
        if self.fail:
            raise requests.ConnectionError("offline")
        payload = json.loads(fixture_text(f"euronext_search_{params['q']}.json"))

        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return payload
        return Resp()


# ----------------------------------------------------------- norway.isin_for_ticker
@pytest.mark.parametrize("ticker,isin", [
    ("ORK", "NO0003733800"),       # main market (XOSL); the DOSL option row is ignored
    ("HDLY", "NO0013470534"),      # Euronext Growth Oslo (MERK)
    ("BORR", "BMG1466R1732"),      # Bermuda ISIN; "BORR" also returns Borregaard (BRG)
])
def test_isin_for_an_oslo_ticker(ticker, isin):
    assert norway.isin_for_ticker(ticker, _Session()) == isin


def test_only_an_exact_oslo_symbol_counts():
    """Searching "BORR" also finds Borregaard (symbol BRG) -- never take a near miss."""
    session = _Session()
    session.get = lambda url, params=None, headers=None, timeout=None: type("R", (), {
        "raise_for_status": lambda self: None,
        "json": lambda self: json.loads(fixture_text("euronext_search_BORR.json"))})()
    assert norway.isin_for_ticker("BRGX", session) == ""


def test_unreachable_search_is_unknown_not_missing():
    assert norway.isin_for_ticker("ORK", _Session(fail=True)) is None


# -------------------------------------------------------------------- cache
def test_isin_is_cached(conn):
    session = _Session()
    assert norway.cached_isin(conn, "ORK", session) == "NO0003733800"
    assert norway.cached_isin(conn, "ORK", session) == "NO0003733800"
    assert session.calls == ["ORK"]


def test_failed_lookup_is_not_cached(conn):
    assert norway.cached_isin(conn, "ORK", _Session(fail=True)) is None
    assert norway.cached_isin(conn, "ORK", _Session()) == "NO0003733800"


# ----------------------------------------------------------- Trading 212 check
def _t212(isins, resolver):
    return trading212.Availability(isins=set(isins), us_symbols=set(), nok_symbols=set(),
                                   norway_isin=resolver)


def test_oslo_ticker_is_matched_by_isin():
    t212 = _t212({"NO0003733800"}, lambda t: {"ORK": "NO0003733800", "HDLY": "NO0013470534"}[t])
    assert t212.can_buy("ORK", "NORWAY")
    assert not t212.can_buy("HDLY", "NORWAY")          # has an ISIN, not on Trading 212


def test_oslo_ticker_without_an_isin_is_not_buyable():
    assert not _t212({"NO0003733800"}, lambda t: "").can_buy("XXX", "NORWAY")


def test_oslo_ticker_that_could_not_be_checked_is_kept_and_remembered():
    t212 = _t212(set(), lambda t: None)
    assert t212.can_buy("ORK", "NORWAY")
    assert "ORK" in t212.unchecked


def test_availability_resolves_oslo_isins_through_the_cache(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(trading212, "ENV_FILE", tmp_path / "missing.env")
    monkeypatch.setenv("TRADING212_API_KEY", "key")
    monkeypatch.setattr(trading212, "fetch_instruments", lambda session=None: [
        {"ticker": "ORKd_EQ", "isin": "NO0003733800", "type": "STOCK", "shortName": "ORK",
         "currencyCode": "EUR"}])
    monkeypatch.setattr(norway, "isin_for_ticker", lambda ticker, session=None: "NO0003733800")
    assert trading212.availability(conn).can_buy("ORK", "NORWAY")


def test_unchecked_oslo_signal_says_so(conn, monkeypatch):
    def fake_enrich(conn, signals):
        for s in signals:
            s.score, s.market_cap_eur, s.avg_daily_value = 100.0, 5e9, 5e7
        return signals
    monkeypatch.setattr(cluster, "enrich_signals", fake_enrich)
    for i, person in enumerate(("A Person", "B Person", "C Person")):
        conn.execute(
            "INSERT INTO norway_purchases (message_id, person, issuer_name, ticker, txn_type, "
            "txn_date, shares, price, currency, value, source_url) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (500 + i, person, "Orkla ASA", "ORK", "P", RECENT, 1000, 100.0, "NOK", 2_000_000, "u"))
    conn.commit()
    sel = strategy.select(conn, cluster.find_norway_clusters(conn), _t212(set(), lambda t: None))
    [t] = sel.strong + sel.candidates
    assert "Trading 212 не проверен: не удалось узнать ISIN" in t.missed
