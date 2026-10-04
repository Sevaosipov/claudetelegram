"""prices.py: the daily closes every price in the bot is read from -- the Yahoo listing of a
ticker, the adjusted-close series behind a trailing stop or a score's momentum, and the small
helpers that read such a series.

It was the price half of the virtual-book engine (paper.py, removed with the model portfolio,
spec 2026-10-04-remove-model-portfolio.md). What stays is generic: model.py scores a stock on
its closes, positions.py watches the user's own positions on them, and the analyst reads them for
a ticker it is asked about. Nothing here touches the database or a real account.

Values are measured on adjusted closes (dividends and splits included).
"""
from __future__ import annotations

import datetime as dt
import sys

import assets
import crypto
import positions
import sources

PRICE_DAYS = 420                # room for a 200-day average and a year-long hold
_VENUE_CURRENCY = {".OL": "NOK", ".ST": "SEK", ".DE": "EUR"}


# ---------------------------------------------------------------- listing
def listing(ticker: str, source: str | None) -> tuple[str, str] | None:
    """(Yahoo symbol, currency) `ticker` is priced on, the same listing a /bought position
    would use -- or None for a ticker with no reliable quote (an ISIN from BaFin or
    Finansinspektionen)."""
    symbol = positions.yahoo_symbol(ticker, source)
    if not symbol:
        return None
    if crypto.is_crypto(ticker):
        return symbol, "USD"
    for suffix, currency in _VENUE_CURRENCY.items():
        if symbol.endswith(suffix):
            return symbol, currency
    return symbol, "USD"


# ----------------------------------------------------------------- prices
def _closes(symbol: str, days: int) -> list[tuple[str, float]]:
    """Adjusted daily closes for a Yahoo symbol, oldest first; [] when every source
    fails. The one network seam -- tests hand their own fetch to Prices/score_day. A stock
    or ETF takes Yahoo's series only: the Nasdaq fallback isn't adjusted for dividends
    and splits, so it would show false dips and stops -- with no price, a position
    keeps its last value instead. A coin pays no dividend, so any source will do."""
    coin = symbol.endswith("-USD")
    asset = assets.crypto_asset(symbol[:-len("-USD")]) if coin else assets.stock_asset(symbol)
    bars, src = sources.price_history(asset, days)
    if not coin and src != "Yahoo":
        return []
    return bars or []


class Prices:
    """Each (symbol, days) fetched once per run; a failing fetch is an empty series.
    With `today`, only completed bars count: Yahoo, Binance and Bybit return the
    current day as a bar still in progress, and a score or a stop on it would differ
    from that day's final close."""

    def __init__(self, fetch=None, today: dt.date | None = None):
        self._fetch = fetch or _closes
        self._cutoff = today.isoformat() if today else None
        self._cache: dict[tuple[str, int], list[tuple[str, float]]] = {}

    def bars(self, symbol: str, days: int = PRICE_DAYS) -> list[tuple[str, float]]:
        key = (symbol, days)
        if key not in self._cache:
            try:
                bars = list(self._fetch(symbol, days) or [])
            except Exception as e:
                print(f"[prices] no prices for {symbol}: {type(e).__name__}: {e}", file=sys.stderr)
                bars = []
            if self._cutoff:
                bars = [b for b in bars if b[0] < self._cutoff]
            self._cache[key] = bars
        return self._cache[key]


def close_on_or_before(bars: list[tuple[str, float]], day: str) -> float | None:
    found = None
    for d, c in bars:
        if d > day:
            break
        found = c
    return found


def first_close_after(bars: list[tuple[str, float]], day: str) -> tuple[str, float] | None:
    return next(((d, c) for d, c in bars if d > day), None)


def business_days_between(start: str, today: dt.date) -> int:
    """Weekdays after `start`, up to and including `today`."""
    d, n = dt.date.fromisoformat(start), 0
    while d < today:
        d += dt.timedelta(days=1)
        n += d.weekday() < 5
    return n
