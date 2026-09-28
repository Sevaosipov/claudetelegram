"""The paper portfolio: eleven virtual books that trade the bot's own signals on
fixed rules, so a strategy can prove itself before any real money is involved.
Spec: docs/superpowers/specs/2026-09-28-paper-portfolio-design.md. Nothing here
ever places a real order.

A decision made on day D becomes an order filled at the first daily close after D,
so a book never trades at a price the bot couldn't have acted on. Values are
measured on adjusted closes (dividends and splits included), in EUR.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import types
from dataclasses import dataclass

import assets
import crypto
import db
import fx
import positions
import sources

STOCK_START_EUR = 80_000.0
CRYPTO_START_EUR = 20_000.0
SLICE = 0.10                    # of the book's value, per stock buy
MAX_POSITIONS = 10              # open positions plus pending buys, per stock book
R2_MIN_SCORE = 70.0
FEE_FOREIGN = 0.0025            # Trading 212's 0.15% currency fee plus ~0.10% spread
FEE_EUR = 0.0010
FEE_COIN = 0.0050
ORDER_MAX_BUSINESS_DAYS = 5
PRICE_DAYS = 420                # room for a 200-day average and a 182-day hold
CRYPTO_COINS = ("BTC", "ETH")
CRYPTO_HOLD_DAYS = 90
CRYPTO_STOP = -0.25
TREND_DAYS = 200
SIGNAL_HOLD_DAYS = 30
REBUY_BLOCK_DAYS = 7
SUCCESS_DAYS = 182
MIN_STOCK_TRADES = 20
STOCK_BENCHMARK = "SPY"
CRYPTO_BENCHMARK = "BTC-USD"
_VENUE_CURRENCY = {".OL": "NOK", ".ST": "SEK", ".DE": "EUR"}

# exit -> (days held, stop, take-profit, an insider from the signal selling counts)
EXITS = {
    "E1": (90, -0.15, None, True),
    "E2": (182, None, None, False),
    "E3": (91, -0.15, None, False),
    "E4": (90, -0.15, 0.25, True),
}


@dataclass(frozen=True)
class Book:
    code: str                   # R1-E1 … R2-E4, R1-E1-AN, C-A, C-B
    sleeve: str                 # stock | crypto
    buy: str | None = None      # R1 | R2
    exit: str | None = None     # E1 … E4
    analyst: bool = False       # the analyst-target shadow
    rule: str | None = None     # crypto: A (signals) | B (trend plus signals)

    @property
    def label(self) -> str:
        if self.sleeve == "crypto":
            return self.code
        return f"{self.buy}·{self.exit}" + ("+аналитики" if self.analyst else "")


BOOKS = tuple(
    [Book(f"{r}-{e}", "stock", r, e) for r in ("R1", "R2") for e in ("E1", "E2", "E3", "E4")]
    + [Book("R1-E1-AN", "stock", "R1", "E1", analyst=True),
       Book("C-A", "crypto", rule="A"),
       Book("C-B", "crypto", rule="B")])
BOOK_BY_CODE = {b.code: b for b in BOOKS}


# ---------------------------------------------------------------- listing
def listing(ticker: str, source: str | None) -> tuple[str, str] | None:
    """(Yahoo symbol, currency) a book prices `ticker` on, the same listing a
    /bought position would use -- or None for a ticker with no reliable quote (an
    ISIN from BaFin or Finansinspektionen)."""
    symbol = positions.yahoo_symbol(ticker, source)
    if not symbol:
        return None
    if crypto.is_crypto(ticker):
        return symbol, "USD"
    for suffix, currency in _VENUE_CURRENCY.items():
        if symbol.endswith(suffix):
            return symbol, currency
    return symbol, "USD"


def fee(ticker: str, currency: str) -> float:
    if crypto.is_crypto(ticker):
        return FEE_COIN
    return FEE_EUR if currency == "EUR" else FEE_FOREIGN


# ----------------------------------------------------------------- prices
def _closes(symbol: str, days: int) -> list[tuple[str, float]]:
    """Adjusted daily closes for a Yahoo symbol, oldest first; [] when every source
    fails. The one network seam -- tests hand their own fetch to Prices/run."""
    asset = (assets.crypto_asset(symbol[:-len("-USD")]) if symbol.endswith("-USD")
             else assets.stock_asset(symbol))
    bars, _src = sources.price_history(asset, days)
    return bars or []


class Prices:
    """Each (symbol, days) fetched once per run; a failing fetch is an empty series."""

    def __init__(self, fetch=None):
        self._fetch = fetch or _closes
        self._cache: dict[tuple[str, int], list[tuple[str, float]]] = {}

    def bars(self, symbol: str, days: int = PRICE_DAYS) -> list[tuple[str, float]]:
        key = (symbol, days)
        if key not in self._cache:
            try:
                self._cache[key] = list(self._fetch(symbol, days) or [])
            except Exception as e:
                print(f"[paper] no prices for {symbol}: {type(e).__name__}: {e}", file=sys.stderr)
                self._cache[key] = []
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


# ------------------------------------------------------------------ books
def create_books(conn, today: dt.date) -> None:
    """Every book, once -- the first run is every book's start date."""
    for b in BOOKS:
        start = STOCK_START_EUR if b.sleeve == "stock" else CRYPTO_START_EUR
        bench = STOCK_BENCHMARK if b.sleeve == "stock" else CRYPTO_BENCHMARK
        conn.execute(
            "INSERT OR IGNORE INTO paper_books (code, sleeve, start_date, start_eur, cash_eur, "
            "bench_symbol) VALUES (?,?,?,?,?,?)",
            (b.code, b.sleeve, today.isoformat(), start, start, bench))
    conn.commit()
