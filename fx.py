"""Normalise signal amounts to a single currency (EUR).

Signals mix sources denominated differently -- SEC/House in USD, BaFin in EUR,
Oslo Børs mostly in NOK but occasionally SEK/USD/EUR/DKK for cross-listed
issuers. That made two things bad: amounts from different sources couldn't be
compared at a glance, and a single threshold constant (e.g. "500,000") silently
meant a different real bar per source. Everything user-facing *in a signal* is
therefore converted to EUR here, and the cluster.py thresholds are EUR too.

Only signals are converted. The raw purchase log (menu.py list, CSV, DB rows,
the per-filing Telegram lines) deliberately keeps each source's native currency,
so those still match the linked filing exactly.

Rates come from Yahoo Finance -- the same dependency already used for prices --
and are cached in SQLite for a day; FX doesn't move nearly enough intraday to
matter at the precision signals are shown with. Every failure path falls back to
a hardcoded approximate rate instead of raising: a signal carrying a slightly
stale conversion is much better than no signal at all.
"""
from __future__ import annotations

import datetime as dt
import sys

import db

# Yahoo quotes these as "how many <currency> per 1 EUR".
_PAIR = {
    "USD": "EURUSD=X",
    "NOK": "EURNOK=X",
    "SEK": "EURSEK=X",
    "DKK": "EURDKK=X",
    "GBP": "EURGBP=X",
    # Swedish filings are mostly SEK but cross-listed issuers report in whatever
    # currency the trade settled in -- CAD, CHF and ISK all show up in a single
    # week of Finansinspektionen data. An unlisted currency falls through to the
    # 1:1 path below, which would price CAD as EUR and overstate it by ~60%.
    "CAD": "EURCAD=X",
    "CHF": "EURCHF=X",
    "ISK": "EURISK=X",
    "PLN": "EURPLN=X",
}

# Used only when a live rate can't be fetched (offline, Yahoo hiccup). Measured
# 2026-09-08; being a little stale here just shifts a displayed amount by a few
# percent, it never drops or corrupts a signal.
_FALLBACK_PER_EUR = {
    "USD": 1.16,
    "NOK": 10.74,
    "SEK": 11.14,
    "DKK": 7.47,
    "GBP": 0.86,
    "CAD": 1.60,
    "CHF": 0.94,
    "ISK": 138.80,
    "PLN": 4.31,
}

CACHE_TTL_SECONDS = 24 * 3600

# A hardcoded fallback is held for much less time than a real rate: it means the
# fetch just failed, and we want the next attempt reasonably soon (network back
# after a wake-up, Yahoo recovered) instead of a long-running --interval process
# freezing on it all day. Still long enough that an offline run converting
# thousands of rows doesn't retry the network on every single one.
_FALLBACK_TTL_SECONDS = 600

# Second-level in-process memo on top of the SQLite cache, so converting a few
# thousand rows in one signal pass doesn't do a few thousand SQLite lookups.
# Carries its own timestamp so a long-running --interval process still picks up
# a fresh rate the next day rather than freezing the one it started with.
_MEMO: dict[str, tuple[float, dt.datetime, bool]] = {}   # currency -> (rate, at, is_fallback)

# Currencies already reported as unconvertible, so the warning prints once per run
# rather than once per row.
_WARNED_UNKNOWN: set[str] = set()


def _fetch_per_eur(currency: str) -> float | None:
    pair = _PAIR.get(currency)
    if not pair:
        return None
    try:
        import yfinance as yf

        # Same NaN guard as the price lookups elsewhere: the most recent bar can
        # be unsettled and come back NaN, which would poison every conversion.
        closes = yf.Ticker(pair).history(period="5d")["Close"].dropna()
        if closes.empty:
            return None
        rate = float(closes.iloc[-1])
        return rate if rate > 0 else None
    except Exception:
        return None


def per_eur(currency: str, conn=None) -> float:
    """How many units of `currency` make one EUR. Always returns something
    usable -- live rate, else cached, else hardcoded fallback, else 1.0."""
    currency = (currency or "EUR").upper()
    if currency == "EUR":
        return 1.0

    now = dt.datetime.now()
    memo = _MEMO.get(currency)
    if memo:
        rate, fetched_at, was_fallback = memo
        ttl = _FALLBACK_TTL_SECONDS if was_fallback else CACHE_TTL_SECONDS
        if (now - fetched_at).total_seconds() < ttl:
            return rate

    rate = db.get_cached_value(conn, f"fx_per_eur_{currency}", CACHE_TTL_SECONDS) if conn is not None else None
    if rate is None:
        rate = _fetch_per_eur(currency)
        if rate is not None and conn is not None:
            db.save_cached_value(conn, f"fx_per_eur_{currency}", rate)
    is_fallback = rate is None
    if is_fallback:
        rate = _FALLBACK_PER_EUR.get(currency)
    if not rate or rate <= 0:
        # Unknown currency code: treat as 1:1 rather than silently zeroing the
        # amount, which would make a real purchase vanish below every threshold.
        # Say so once, though -- 1:1 is a guess, and for anything far from parity
        # it silently misprices every threshold comparison for that currency.
        if currency not in _WARNED_UNKNOWN:
            _WARNED_UNKNOWN.add(currency)
            print(f"[fx] no rate for {currency!r}; treating it as 1:1 with EUR. "
                  f"Add it to fx._PAIR if it keeps appearing.", file=sys.stderr)
        return 1.0

    _MEMO[currency] = (rate, now, is_fallback)
    return rate


def to_eur(amount: float | None, currency: str, conn=None) -> float:
    if not amount:
        return 0.0
    return amount / per_eur(currency, conn)
