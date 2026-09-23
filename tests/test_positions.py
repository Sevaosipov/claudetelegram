"""positions.py: what the user reports buying, and when to close it."""
from __future__ import annotations

import datetime as dt
import json

import pytest

import db
import positions
from conftest import add_form_144, add_sec_sale

TODAY = dt.date(2026, 9, 23)


def _strong_journal(conn, ticker, members):
    db.journal_signal(conn, {"source": "SEC", "kind": "cluster", "ticker": ticker,
                             "tier": "strong", "members": json.dumps(members)})


def _open(conn, ticker="AAA", price=100.0, days_ago=5):
    return positions.open_position(conn, ticker, price, today=TODAY - dt.timedelta(days=days_ago))


def _no_price(ticker):
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
    [alert] = positions.check_exits(conn, today=TODAY, price_fn=lambda t: 84.9)
    assert alert.trigger == "stop_loss" and alert.last_price == 84.9


def test_small_drawdown_does_not_close(conn):
    _open(conn, price=100.0)
    assert positions.check_exits(conn, today=TODAY, price_fn=lambda t: 90.0) == []


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
