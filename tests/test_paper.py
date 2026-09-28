"""paper.py: the virtual books. Offline -- prices come from a stub fetch, dates are
fixed so weekday arithmetic is deterministic."""
from __future__ import annotations

import datetime as dt
import json
import types

import pytest

import db
import paper
import strategy
from conftest import add_sec_sale

TODAY = dt.date(2026, 10, 5)          # a Monday


def _days(n: int) -> str:
    return (TODAY - dt.timedelta(days=n)).isoformat()


def _bars(closes, end: dt.date = TODAY):
    """Consecutive daily closes ending on `end`, oldest first."""
    n = len(closes)
    return [((end - dt.timedelta(days=n - 1 - i)).isoformat(), float(c)) for i, c in enumerate(closes)]


class Fetch:
    def __init__(self, series: dict):
        self.series, self.calls = series, []

    def __call__(self, symbol, days=None):
        self.calls.append(symbol)
        return self.series.get(symbol, [])


def _sig(ticker="AAA", source="SEC", tier="strong", score=80.0, members=("Jane Doe",)):
    return types.SimpleNamespace(ticker=ticker, source=source, company=f"{ticker} Corp", tier=tier,
                                 score=score, member_names=list(members))


def _sel(strong=(), candidates=()):
    return strategy.Selection(strong=[strategy.Tiered(s, s.tier) for s in strong],
                              candidates=[strategy.Tiered(s, s.tier) for s in candidates],
                              t212_checked=True)


# ------------------------------------------------------------------ foundation
def test_books_are_created_once_with_their_money(conn):
    paper.create_books(conn, TODAY)
    paper.create_books(conn, TODAY + dt.timedelta(days=1))
    rows = conn.execute("SELECT code, sleeve, start_date, start_eur, cash_eur, bench_symbol "
                        "FROM paper_books").fetchall()
    assert len(rows) == 11
    assert ("R1-E1", "stock", TODAY.isoformat(), 80_000.0, 80_000.0, "SPY") in rows
    assert ("R1-E1-AN", "stock", TODAY.isoformat(), 80_000.0, 80_000.0, "SPY") in rows
    assert ("C-B", "crypto", TODAY.isoformat(), 20_000.0, 20_000.0, "BTC-USD") in rows


def test_book_labels():
    assert paper.BOOK_BY_CODE["R2-E3"].label == "R2·E3"
    assert paper.BOOK_BY_CODE["R1-E1-AN"].label == "R1·E1+аналитики"
    assert paper.BOOK_BY_CODE["C-A"].label == "C-A"


@pytest.mark.parametrize("ticker,source,expected", [
    ("AAPL", "SEC", ("AAPL", "USD")),
    ("BRK.B", "HOUSE", ("BRK-B", "USD")),
    ("EQNR", "NORWAY", ("EQNR.OL", "NOK")),
    ("VOLV-B", "SWEDEN", ("VOLV-B.ST", "SEK")),
    ("CRYPTO:BTC", "CRYPTO", ("BTC-USD", "USD")),
    ("DE0007164600", "BAFIN", None),
])
def test_listing(ticker, source, expected):
    assert paper.listing(ticker, source) == expected


def test_fees():
    assert paper.fee("AAPL", "USD") == 0.0025
    assert paper.fee("SAP", "EUR") == 0.0010
    assert paper.fee("CRYPTO:BTC", "USD") == 0.0050


def test_prices_are_fetched_once_and_a_failure_is_empty():
    calls = []

    def fetch(symbol, days):
        calls.append(symbol)
        if symbol == "BAD":
            raise RuntimeError("down")
        return [("2026-10-01", 1.0)]
    p = paper.Prices(fetch)
    assert p.bars("AAA") == p.bars("AAA") == [("2026-10-01", 1.0)]
    assert p.bars("BAD") == [] and calls == ["AAA", "BAD"]


def test_series_helpers():
    bars = [("2026-10-01", 10.0), ("2026-10-02", 11.0), ("2026-10-05", 12.0)]
    assert paper.close_on_or_before(bars, "2026-10-03") == 11.0
    assert paper.close_on_or_before(bars, "2026-09-30") is None
    assert paper.first_close_after(bars, "2026-10-02") == ("2026-10-05", 12.0)
    assert paper.first_close_after(bars, "2026-10-05") is None


def test_business_days_between():
    assert paper.business_days_between("2026-10-02", dt.date(2026, 10, 9)) == 5
    assert paper.business_days_between("2026-10-05", dt.date(2026, 10, 5)) == 0
