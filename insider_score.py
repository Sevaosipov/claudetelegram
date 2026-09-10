"""Rank insiders by trading track record: does their buying beat the market?

Definition (as specified by the user): an insider counts as "top" if their trailing
12-month purchases outperform the S&P 500's trailing 12-month return by at least
OUTPERFORMANCE_THRESHOLD_PP percentage points of alpha, i.e.

    insider_return_pct - sp500_return_pct >= 100.0

insider_return_pct is the simple average, across that insider's own Form 4 "P"
purchases in the last LOOKBACK_MONTHS months (any issuer), of
(current_price / price_paid - 1) * 100. Price paid comes straight from the Form 4
filing; only the *current* price needs an external lookup (via yfinance), which
keeps this cheap: one price fetch per distinct ticker, not per historical trade.

Both numbers are cached in SQLite (per-insider score for SCORE_CACHE_SECONDS, the
S&P 500 benchmark for MARKET_CACHE_SECONDS) so a repeat insider showing up on
consecutive days doesn't re-triger a full recompute each time.
"""
from __future__ import annotations

import datetime as dt

import yfinance as yf

import db
import sec_edgar

LOOKBACK_MONTHS = 12
OUTPERFORMANCE_THRESHOLD_PP = 100.0
SCORE_CACHE_SECONDS = 7 * 24 * 3600
MARKET_CACHE_SECONDS = 24 * 3600


def _current_price(ticker: str) -> float | None:
    try:
        hist = yf.Ticker(ticker).history(period="5d")
        # The most recent row can be NaN (today's bar not settled yet / market
        # not open yet) -- drop those before taking the last close.
        closes = hist["Close"].dropna()
        if closes.empty:
            return None
        return float(closes.iloc[-1])
    except Exception:
        return None


def get_sp500_return_pct(conn) -> float:
    cached = db.get_cached_value(conn, "sp500_12mo_return_pct", MARKET_CACHE_SECONDS)
    if cached is not None:
        return cached
    hist = yf.Ticker("^GSPC").history(period="13mo")
    closes = hist["Close"].dropna()
    start_price = float(closes.iloc[0])
    end_price = float(closes.iloc[-1])
    pct = (end_price / start_price - 1) * 100
    db.save_cached_value(conn, "sp500_12mo_return_pct", pct)
    return pct


def score_insider(conn, owner_cik: str, owner_name: str, session=None) -> tuple[float, int] | None:
    """Returns (avg_return_pct, num_purchases) for this insider's trailing-12mo
    buys, or None if they have no qualifying purchase history to score."""
    cached = db.get_insider_score(conn, owner_cik, SCORE_CACHE_SECONDS)
    if cached is not None:
        return_pct, _market, num = cached
        if num == 0:
            return None
        return return_pct, num

    since = (dt.date.today() - dt.timedelta(days=LOOKBACK_MONTHS * 30)).isoformat()
    purchases = sec_edgar.fetch_owner_recent_purchases(owner_cik, since, session=session)

    price_cache: dict[str, float | None] = {}
    returns = []
    for p in purchases:
        if not p.ticker or not p.price:
            continue
        if p.ticker not in price_cache:
            price_cache[p.ticker] = _current_price(p.ticker)
        cur = price_cache[p.ticker]
        if cur is None:
            continue
        returns.append((cur / p.price - 1) * 100)

    market_pct = get_sp500_return_pct(conn)
    avg_return = sum(returns) / len(returns) if returns else 0.0
    db.save_insider_score(conn, owner_cik, owner_name, avg_return, market_pct, len(returns))

    if not returns:
        return None
    return avg_return, len(returns)


def is_top_insider(conn, owner_cik: str, owner_name: str, session=None) -> bool:
    if not owner_cik:
        return False
    result = score_insider(conn, owner_cik, owner_name, session=session)
    if result is None:
        return False
    avg_return, _num = result
    market_pct = get_sp500_return_pct(conn)
    return (avg_return - market_pct) >= OUTPERFORMANCE_THRESHOLD_PP
