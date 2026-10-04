"""prices.py: the listing a ticker is priced on, the adjusted closes behind every stop and every score's
momentum, and the helpers that read a series. Offline -- prices come from a stub fetch, dates are fixed so
weekday arithmetic is deterministic."""
from __future__ import annotations

import datetime as dt

import pytest

import prices

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


# ------------------------------------------------------------------ listing
@pytest.mark.parametrize("ticker,source,expected", [
    ("AAPL", "SEC", ("AAPL", "USD")),
    ("BRK.B", "HOUSE", ("BRK-B", "USD")),
    ("EQNR", "NORWAY", ("EQNR.OL", "NOK")),
    ("VOLV-B", "SWEDEN", ("VOLV-B.ST", "SEK")),
    ("CRYPTO:BTC", "CRYPTO", ("BTC-USD", "USD")),
    ("DE0007164600", "BAFIN", None),
])
def test_listing(ticker, source, expected):
    assert prices.listing(ticker, source) == expected


# ------------------------------------------------------------------- prices
def test_prices_are_fetched_once_and_a_failure_is_empty(capsys):
    calls = []

    def fetch(symbol, days):
        calls.append(symbol)
        if symbol == "BAD":
            raise RuntimeError("down")
        return [("2026-10-01", 1.0)]
    p = prices.Prices(fetch)
    assert p.bars("AAA") == p.bars("AAA") == [("2026-10-01", 1.0)]
    assert p.bars("BAD") == [] and calls == ["AAA", "BAD"]
    assert "[prices] no prices for BAD: RuntimeError: down" in capsys.readouterr().err


def test_prices_with_a_date_keep_only_completed_bars():
    series = {"AAA": _bars([100, 110, 120])}                          # ... TODAY-1, TODAY
    assert prices.Prices(Fetch(series), today=TODAY).bars("AAA") == [(_days(2), 100.0), (_days(1), 110.0)]
    assert prices.Prices(Fetch(series)).bars("AAA") == series["AAA"]   # without a date: every bar


def test_the_default_history_is_price_days_long_and_the_default_fetch_is_closes(monkeypatch):
    asked = []
    monkeypatch.setattr(prices, "_closes", lambda symbol, days: asked.append((symbol, days)) or [])
    prices.Prices().bars("AAA")
    assert asked == [("AAA", prices.PRICE_DAYS)] and prices.PRICE_DAYS == 420


@pytest.mark.parametrize("symbol,source,kept", [
    ("AAPL", "Nasdaq", False),       # not adjusted for dividends and splits
    ("AAPL", "Yahoo", True),
    ("BTC-USD", "Binance", True),    # a coin pays no dividend: any source will do
])
def test_stock_closes_come_from_yahoo_only(monkeypatch, symbol, source, kept):
    bars = [("2026-10-01", 1.0), ("2026-10-02", 2.0)]
    monkeypatch.setattr(prices.sources, "price_history", lambda asset, days: (bars, source))
    assert prices._closes(symbol, 420) == (bars if kept else [])


# ------------------------------------------------------------ series helpers
def test_series_helpers():
    bars = [("2026-10-01", 10.0), ("2026-10-02", 11.0), ("2026-10-05", 12.0)]
    assert prices.close_on_or_before(bars, "2026-10-03") == 11.0
    assert prices.close_on_or_before(bars, "2026-09-30") is None
    assert prices.first_close_after(bars, "2026-10-02") == ("2026-10-05", 12.0)
    assert prices.first_close_after(bars, "2026-10-05") is None


def test_business_days_between():
    assert prices.business_days_between("2026-10-02", dt.date(2026, 10, 9)) == 5
    assert prices.business_days_between("2026-10-05", dt.date(2026, 10, 5)) == 0
