"""positions.py: what the user reports buying, and when to close it."""
from __future__ import annotations

import datetime as dt
import json

import pytest

import db
import positions
from conftest import add_bafin_txn, add_form_144, add_sec_sale, add_sweden_txn

TODAY = dt.date(2026, 9, 23)


def _strong_journal(conn, ticker, members, source="SEC"):
    db.journal_signal(conn, {"source": source, "kind": "cluster", "ticker": ticker,
                             "tier": "strong", "members": json.dumps(members)})


def _open(conn, ticker="AAA", price=100.0, days_ago=5):
    return positions.open_position(conn, ticker, price, today=TODAY - dt.timedelta(days=days_ago))


def _no_price(ticker, source=None):
    return None


def test_open_takes_the_insiders_from_the_latest_strong_signal(conn):
    _strong_journal(conn, "AAA", ["Old Buyer"])
    _strong_journal(conn, "AAA", ["Boss Person", "Board Person"])
    pos = _open(conn)
    assert pos.insiders == ["Boss Person", "Board Person"] and pos.signal_id is not None


def test_open_without_a_signal_has_no_insiders(conn):
    assert _open(conn).insiders == []


def test_second_open_on_the_same_ticker_is_refused(conn):
    _open(conn)
    with pytest.raises(ValueError):
        _open(conn)


def test_insider_sale_after_opening_closes(conn):
    _strong_journal(conn, "AAA", ["Boss Person"])
    _open(conn)
    add_sec_sale(conn, "AAA", "PERSON BOSS", 500_000, date=(TODAY - dt.timedelta(days=1)).isoformat())
    [alert] = positions.check_exits(conn, today=TODAY, price_fn=_no_price)
    assert alert.trigger == "insider_sell" and "PERSON BOSS" in alert.detail


def test_sale_before_opening_does_not_count(conn):
    _strong_journal(conn, "AAA", ["Boss Person"])
    add_sec_sale(conn, "AAA", "Boss Person", 500_000, date=(TODAY - dt.timedelta(days=30)).isoformat())
    _open(conn)
    assert positions.check_exits(conn, today=TODAY, price_fn=_no_price) == []


def test_planned_10b5_1_sale_does_not_count(conn):
    _strong_journal(conn, "AAA", ["Boss Person"])
    _open(conn)
    add_sec_sale(conn, "AAA", "Boss Person", 500_000, date=(TODAY - dt.timedelta(days=1)).isoformat())
    conn.execute("UPDATE sec_sales SET is_10b5_1 = 1")
    assert positions.check_exits(conn, today=TODAY, price_fn=_no_price) == []


def test_form_144_notice_counts_as_selling(conn):
    _strong_journal(conn, "AAA", ["Boss Person"])
    _open(conn)
    add_form_144(conn, "AAA", "Boss Person", 900_000, date=(TODAY - dt.timedelta(days=1)).isoformat())
    [alert] = positions.check_exits(conn, today=TODAY, price_fn=_no_price)
    assert alert.trigger == "insider_sell" and "144" in alert.detail


def test_time_limit_closes(conn):
    _open(conn, days_ago=positions.EXIT_MAX_DAYS)
    [alert] = positions.check_exits(conn, today=TODAY, price_fn=_no_price)
    assert alert.trigger == "time"


def test_stop_loss_closes(conn):
    _open(conn, price=100.0)
    [alert] = positions.check_exits(conn, today=TODAY, price_fn=lambda t, s=None: 84.9)
    assert alert.trigger == "stop_loss" and alert.last_price == 84.9


def test_small_drawdown_does_not_close(conn):
    _open(conn, price=100.0)
    assert positions.check_exits(conn, today=TODAY, price_fn=lambda t, s=None: 90.0) == []


def test_each_position_alerts_once(conn):
    _open(conn, days_ago=positions.EXIT_MAX_DAYS)
    alerts = positions.check_exits(conn, today=TODAY, price_fn=_no_price)
    positions.mark_alerted(conn, alerts, today=TODAY)
    assert positions.check_exits(conn, today=TODAY, price_fn=_no_price) == []


def test_close_removes_it_from_open_positions(conn):
    _open(conn)
    closed = positions.close_position(conn, "aaa", today=TODAY)
    assert closed.close_reason == "manual" and positions.open_positions(conn) == []
    assert positions.close_position(conn, "AAA", today=TODAY) is None


def test_open_position_stores_an_explicit_source(conn):
    """telegram_bot passes "CRYPTO" for crypto tickers even when there's no strong
    signal to read a source off of."""
    pos = positions.open_position(conn, "CRYPTO:BTC", 50_000.0, source="CRYPTO")
    assert pos.source == "CRYPTO"


def test_open_position_explicit_source_overrides_signal_journal(conn):
    _strong_journal(conn, "AAA", ["Boss Person"], source="SEC")
    pos = positions.open_position(conn, "AAA", 100.0, source="CRYPTO")
    assert pos.source == "CRYPTO"


# ------------------------------------------------------- pricing by listing
def test_last_close_uses_the_crypto_symbol(conn, monkeypatch):
    """positions.last_close must never price a crypto position through the bare
    "BTC"/"ETH" Yahoo tickers -- those are unrelated US-listed ETFs."""
    seen = []
    monkeypatch.setattr(positions, "_yahoo_close", lambda symbol: seen.append(symbol) or 65_000.0)
    assert positions.last_close("CRYPTO:BTC") == 65_000.0
    assert seen == ["BTC-USD"]


def test_last_close_uses_the_source_venue_suffix(conn, monkeypatch):
    seen = []
    monkeypatch.setattr(positions, "_yahoo_close", lambda symbol: seen.append(symbol) or 100.0)
    positions.last_close("NRC", "NORWAY")
    assert seen == ["NRC.OL"]


def test_last_close_treats_an_unknown_source_as_the_us_listing(conn, monkeypatch):
    seen = []
    monkeypatch.setattr(positions, "_yahoo_close", lambda symbol: seen.append(symbol) or 100.0)
    positions.last_close("EQNR")
    assert seen == ["EQNR"]


def test_last_close_is_none_for_an_isin(conn, monkeypatch):
    """BaFin and FI identify issuers by ISIN, not ticker -- an ISIN never resolves
    on Yahoo, so pricing it would silently price the wrong instrument (or nothing)."""
    monkeypatch.setattr(positions, "_yahoo_close", lambda symbol: 100.0)
    assert positions.last_close("DE0007164600", "BAFIN") is None


def test_check_exits_prices_through_the_positions_own_source(conn):
    """check_exits must pass the position's source along, not just its ticker --
    otherwise the caller has no way to price a non-US listing correctly."""
    _open(conn, ticker="NRC")
    conn.execute("UPDATE positions SET source = 'NORWAY'")
    seen = []

    def price_fn(ticker, source):
        seen.append((ticker, source))
        return None
    positions.check_exits(conn, today=TODAY, price_fn=price_fn)
    assert seen == [("NRC", "NORWAY")]


# --------------------------------------------------- European sale triggers
def test_oslo_sale_after_opening_closes(conn):
    _strong_journal(conn, "NRC", ["Boss Person"], source="NORWAY")
    _open(conn, ticker="NRC")
    conn.execute(
        "INSERT INTO norway_purchases (message_id, person, issuer_name, ticker, txn_type, "
        "txn_date, shares, price, currency, value, source_url) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (1, "Boss Person", "Test ASA", "NRC", "S", (TODAY - dt.timedelta(days=1)).isoformat(),
         1000, 100.0, "NOK", 100_000, "u"))
    conn.commit()
    [alert] = positions.check_exits(conn, today=TODAY, price_fn=_no_price)
    assert alert.trigger == "insider_sell" and "Oslo" in alert.detail


def test_sweden_sale_after_opening_closes(conn):
    _strong_journal(conn, "SE0000000001", ["Boss Person"], source="SWEDEN")
    _open(conn, ticker="SE0000000001")
    add_sweden_txn(conn, "SE0000000001", "Boss Person", 500_000,
                   date=(TODAY - dt.timedelta(days=1)).isoformat(), txn_type="S")
    [alert] = positions.check_exits(conn, today=TODAY, price_fn=_no_price)
    assert alert.trigger == "insider_sell" and "FI" in alert.detail


def test_bafin_sale_after_opening_closes(conn):
    _strong_journal(conn, "DE0007164600", ["Boss Person"], source="BAFIN")
    _open(conn, ticker="DE0007164600")
    add_bafin_txn(conn, "DE0007164600", "Boss Person", 500_000,
                  date=(TODAY - dt.timedelta(days=1)).strftime("%d.%m.%Y"), txn_type="S")
    [alert] = positions.check_exits(conn, today=TODAY, price_fn=_no_price)
    assert alert.trigger == "insider_sell" and "BaFin" in alert.detail


def test_bafin_sale_before_opening_does_not_count(conn):
    _strong_journal(conn, "DE0007164600", ["Boss Person"], source="BAFIN")
    add_bafin_txn(conn, "DE0007164600", "Boss Person", 500_000,
                  date=(TODAY - dt.timedelta(days=30)).strftime("%d.%m.%Y"), txn_type="S")
    _open(conn, ticker="DE0007164600")
    assert positions.check_exits(conn, today=TODAY, price_fn=_no_price) == []
