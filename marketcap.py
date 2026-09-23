"""Company size and listing venue, so a purchase can be judged relative to the
company rather than in absolute money.

The whole signal layer used to be denominated in euros alone, which quietly treats
two very different events as the same one: EUR 500,000 is a controlling interest in a
shell and a rounding error in a mega-cap. cluster.py even says so in the comment
justifying its flat solo threshold. Schedule 13D/G supplies percentOfClass directly
and Form 144 supplies shares outstanding, but only for filings that happen to be of
those types -- this covers the rest.

Coverage is honest rather than complete:
  - US tickers resolve directly.
  - Oslo and Stockholm tickers resolve via a venue suffix (.OL / .ST).
  - BaFin and Finansinspektionen identify issuers by ISIN, not ticker, and an ISIN
    does not reliably resolve. Those return None, and callers must treat "unknown
    size" as unknown rather than as small.

Facts are cached for a week: market cap moves daily, but not nearly enough to change
which size bucket a company sits in, and every signal needs a lookup.
"""
from __future__ import annotations

import re

import db
import fx

CACHE_TTL_SECONDS = 7 * 24 * 3600

# Tried in order for a bare ticker with no exchange information. US listings are by
# far the most common here, so the unsuffixed form goes first.
VENUE_SUFFIXES = ("", ".OL", ".ST", ".DE", ".CO", ".HE")

# Where a signal's source already says which market the ticker trades on. Without
# this, a bare Oslo ticker is tried as a US one first -- and Oslo's NRC resolved to
# National Research Corp. Sources not listed here fall back to VENUE_SUFFIXES.
SOURCE_VENUE = {
    "SEC": "", "SEC13DG": "", "SEC144": "", "HOUSE": "", "SENATE": "",
    "NORWAY": ".OL", "SWEDEN": ".ST",
}

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


def clean_ticker(ticker: str | None) -> str | None:
    """A ticker Yahoo could plausibly know, or None.

    Form 4 occasionally lists every share class in one field ("LEN, LEN.B"), and
    13D/G rows with no trading symbol carry the issuer's CIK instead. Both used to go
    to Yahoo as-is, once per venue suffix -- six guaranteed 404s per ticker. The
    first listed class is the one to use; a CIK is not a ticker at all.
    """
    if not ticker:
        return None
    first = re.split(r"[,\s;]+", ticker.strip().upper())[0]
    return first if _TICKER_RE.match(first) else None


# Upper bounds in EUR. Bucketing exists so backtest.py can ask "do signals in small
# companies behave differently from signals in large ones" -- a question the bot
# could not previously express at all.
SIZE_BUCKETS = (
    (50e6, "nano"),
    (300e6, "micro"),
    (2e9, "small"),
    (10e9, "mid"),
    (200e9, "large"),
    (float("inf"), "mega"),
)


def _looks_like_isin(ticker: str) -> bool:
    """ISINs are 12 chars, two-letter country prefix, and appear where BaFin and
    Finansinspektionen rows store their 'ticker'."""
    t = (ticker or "").strip()
    return len(t) == 12 and t[:2].isalpha() and t[2:].isalnum()


def _get(info, *names):
    """Read a FastInfo field.

    yfinance's FastInfo aliases snake_case to camelCase through __getitem__ but its
    .get() only sees the camelCase keys it actually stores -- so info.get("market_cap")
    is silently None while info["market_cap"] works. Reading through __getitem__ with
    both spellings avoids depending on which one this version prefers.
    """
    for name in names:
        try:
            value = info[name]
        except (KeyError, TypeError, AttributeError):
            continue
        if value not in (None, ""):
            return value
    return None


def _fetch(ticker: str, suffixes: tuple[str, ...] = VENUE_SUFFIXES) -> dict | None:
    try:
        import yfinance as yf
    except ImportError:
        return None
    for suffix in suffixes:
        symbol = (ticker.replace(".", "-") if not suffix else ticker) + suffix
        try:
            info = yf.Ticker(symbol).fast_info
            cap = _get(info, "market_cap", "marketCap")
            if not cap:
                continue
            price = _get(info, "last_price", "lastPrice")
            volume = _get(info, "three_month_average_volume", "threeMonthAverageVolume",
                           "ten_day_average_volume", "tenDayAverageVolume")
            return {
                "shares_outstanding": float(_get(info, "shares") or 0) or None,
                "market_cap": float(cap),
                "currency": str(_get(info, "currency") or "USD").upper(),
                "exchange": str(_get(info, "exchange") or "") or None,
                # Typical money traded per day. This, not market cap, is what decides
                # whether a position can actually be opened and closed without moving
                # the price -- a EUR 30m company that trades EUR 20k a day is a different
                # proposition from one that trades EUR 2m.
                "avg_daily_value": (float(volume) * float(price)
                                     if volume and price else None),
            }
        except Exception:
            continue
    return None


def facts(conn, ticker: str | None, source: str | None = None) -> dict | None:
    """Cached company facts for a ticker, or None when it can't be resolved.

    `source` is the signal source (SEC, NORWAY, ...) when known; it pins the lookup
    to that market instead of guessing across all of them.
    """
    if not ticker or _looks_like_isin(ticker) or ticker.startswith("CRYPTO:"):
        return None
    ticker = clean_ticker(ticker)
    if not ticker:
        return None
    venue = SOURCE_VENUE.get(source or "")
    # A non-US venue is part of the cache key: the same bare symbol can be two
    # different companies in New York and Oslo.
    key = ticker + venue if venue else ticker
    cached = db.get_company_facts(conn, key, CACHE_TTL_SECONDS)
    if cached is not None:
        return cached if cached.get("market_cap") else None
    fetched = _fetch(ticker, (venue,) if venue is not None else VENUE_SUFFIXES)
    # A miss is cached too (as an empty row), so a delisted or unresolvable ticker
    # isn't re-fetched on every single run.
    db.save_company_facts(conn, key, fetched or {})
    return fetched


def market_cap_eur(conn, ticker: str | None, source: str | None = None) -> float | None:
    f = facts(conn, ticker, source)
    if not f or not f.get("market_cap"):
        return None
    return fx.to_eur(f["market_cap"], f.get("currency") or "USD", conn)


def size_bucket(market_cap_eur_value: float | None) -> str:
    if not market_cap_eur_value:
        return "unknown"
    for upper, name in SIZE_BUCKETS:
        if market_cap_eur_value < upper:
            return name
    return "mega"
