"""backtest.py's small price helpers. The network wrappers (_series,
_return_before) are not tested; the pure math (_excess_return) is."""
from __future__ import annotations

import pandas as pd
import pytest

import backtest
from conftest import add_house_txn


def _series(values):
    return pd.Series(values, dtype=float)


def test_excess_return_is_ticker_minus_benchmark_over_the_window():
    prices = _series([100, 101, 102, 110])   # +10% over 3 bars
    bench = _series([100, 100, 100, 104])     # +4% over 3 bars
    assert backtest._excess_return(prices, bench, days=3) == pytest.approx(6.0)


def test_excess_return_none_when_a_series_is_missing():
    assert backtest._excess_return(None, _series([1, 2, 3, 4]), days=3) is None
    assert backtest._excess_return(_series([1, 2, 3, 4]), None, days=3) is None


def test_excess_return_none_when_history_too_short():
    prices = _series([100, 110])
    bench = _series([100, 104])
    assert backtest._excess_return(prices, bench, days=3) is None


@pytest.mark.parametrize("raw,iso", [
    ("9/3/2026", "2026-09-03"), ("09/03/2026", "2026-09-03"),
    ("12/31/2025", "2025-12-31"), ("", None), ("not a date", None), (None, None),
])
def test_mdy_to_iso(raw, iso):
    assert backtest._mdy_to_iso(raw) == iso


def test_collect_political_trades_labels_by_beat_or_miss(conn, monkeypatch):
    add_house_txn(conn, "AAA", "Rep A", "$1,001 - $15,000",
                  date="06/01/2025", notification_date="06/15/2025")
    add_house_txn(conn, "BBB", "Rep B", "$50,001 - $100,000",
                  date="06/01/2025", notification_date="06/15/2025")

    def fake_forward(ticker, start, horizons):
        return {21: {"return": 5.0, "excess": 3.0 if ticker == "AAA" else -2.0}}
    monkeypatch.setattr(backtest, "forward_returns", fake_forward)

    rows = backtest.collect_political_trades(conn, horizon=21, since_days=100000)
    by_ticker = {r["ticker"]: r for r in rows}
    assert by_ticker["AAA"]["label"] == 1
    assert by_ticker["BBB"]["label"] == 0
    assert by_ticker["AAA"]["disclosure_date"] == "2025-06-15"
    assert by_ticker["AAA"]["trade_date"] == "2025-06-01"
    assert by_ticker["AAA"]["chamber"] == "house"


def test_collect_political_trades_drops_unlabelable_and_junk(conn, monkeypatch):
    add_house_txn(conn, "AAA", "Rep A", "$1,001 - $15,000", notification_date="06/15/2025")
    add_house_txn(conn, "NONE", "Rep C", "$1,001 - $15,000", notification_date="06/15/2025")
    add_house_txn(conn, "DDD", "Rep D", "$1,001 - $15,000", notification_date="")

    monkeypatch.setattr(backtest, "forward_returns",
                        lambda t, s, h: {21: {"return": 1.0, "excess": 1.0}} if t == "AAA" else None)
    rows = backtest.collect_political_trades(conn, horizon=21, since_days=100000)
    assert [r["ticker"] for r in rows] == ["AAA"]   # NONE = junk, DDD = no disclosure date, others unlabelable


from conftest import add_sec_purchase


def test_collect_purchases_default_shape_unchanged(conn, monkeypatch):
    add_sec_purchase(conn, "AAA", "Buyer", 200_000, "2026-09-01")
    monkeypatch.setattr(backtest, "forward_returns",
                        lambda t, s, h: {1: {"return": 2.0, "excess": 1.0},
                                          21: {"return": 3.0, "excess": 2.0}})
    rows = backtest.collect_purchases(conn, (1, 21), since_days=100000)
    assert rows and "returns" in rows[0]
    assert "label" not in rows[0] and "disclosure_date" not in rows[0]


def test_collect_purchases_extended_adds_label_and_features(conn, monkeypatch):
    add_sec_purchase(conn, "AAA", "Winner", 200_000, "2026-09-01",
                     shares=100, shares_owned_after=1100, filed_date="2026-09-03")
    add_sec_purchase(conn, "BBB", "Loser", 200_000, "2026-09-01",
                     shares=50, shares_owned_after=50, filed_date="2026-09-02")
    monkeypatch.setattr(backtest, "forward_returns",
                        lambda t, s, h: {21: {"return": 3.0,
                                              "excess": 4.0 if t == "AAA" else -1.0}})
    rows = {r["owner"]: r for r in
            backtest.collect_purchases(conn, (21,), since_days=100000, extended_features=True)}
    assert rows["Winner"]["label"] == 1 and rows["Loser"]["label"] == 0
    assert rows["Winner"]["disclosure_date"] == "2026-09-03"
    assert rows["Winner"]["trade_date"] == "2026-09-01"
    assert rows["Winner"]["shares"] == 100 and rows["Winner"]["owned_after"] == 1100
    assert rows["Loser"]["owned_after"] == 50
