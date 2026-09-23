"""Rank members of Congress by estimated profit on their House PTR stock purchases.

House PTRs disclose only a dollar bracket (e.g. "$50,001 - $100,000"), never the
exact price paid or share count -- unlike SEC Form 4, which gives an exact price
(see insider_score.py for the SEC-side equivalent). So for each purchase this
estimates:

    return_pct  = (current price / closing price on the trade date - 1) * 100
    est. profit = midpoint of the disclosed bracket * return_pct / 100

and sums estimated profit across a member's trades in the lookback window. This is
a heuristic ranking signal, not real accounting -- it ignores when a position was
actually sold, dividends, and treats the bracket midpoint as the trade size.

Historical closes are cached forever in SQLite (a past close never changes);
current prices are fetched fresh each call.
"""
from __future__ import annotations

import datetime as dt
import re

import yfinance as yf

import crypto
import db

LOOKBACK_MONTHS = 12
_AMOUNT_RE = re.compile(r"\$([\d,]+)")


def _parse_amount_mid(amount_range: str) -> float:
    nums = [float(x.replace(",", "")) for x in _AMOUNT_RE.findall(amount_range)]
    return sum(nums) / len(nums) if nums else 0.0


def _yf_symbol(ticker: str) -> str:
    # yfinance wants share classes as e.g. "BRK-B", not the "BRK.B" style PTRs use,
    # and crypto as "BTC-USD" rather than this project's "CRYPTO:BTC".
    return crypto.yf_symbol(ticker)


def _price_on(conn, ticker: str, date: dt.date) -> float | None:
    date_s = date.isoformat()
    cached, hit = db.get_price_on(conn, ticker, date_s)
    if hit:
        return cached
    price = None
    try:
        hist = yf.Ticker(_yf_symbol(ticker)).history(
            start=date_s, end=(date + dt.timedelta(days=7)).isoformat()
        )
        closes = hist["Close"].dropna()
        if not closes.empty:
            price = float(closes.iloc[0])
    except Exception:
        pass
    db.save_price_on(conn, ticker, date_s, price)
    return price


def top_politicians(conn, n: int = 10, lookback_months: int = LOOKBACK_MONTHS, progress=None) -> list[dict]:
    """Returns up to `n` politicians sorted by estimated total profit (descending),
    each a dict with name, state_district, num_trades, total_profit,
    total_invested, avg_return_pct, and trades (list of (ticker, date, return_pct,
    est_profit)). `progress`, if given, is called with a status string as it works
    through tickers (this can take a minute+ on a cold price cache)."""
    cutoff = dt.date.today() - dt.timedelta(days=lookback_months * 30)
    rows = conn.execute(
        "SELECT member_name, state_district, ticker, txn_date, amount_range FROM house_purchases "
        "WHERE txn_type = 'P' AND ticker IS NOT NULL AND ticker != ''"
    ).fetchall()

    current_price_cache: dict[str, float | None] = {}
    per_politician: dict[str, dict] = {}

    for i, (member_name, state_district, ticker, txn_date, amount_range) in enumerate(rows):
        try:
            d = dt.datetime.strptime(txn_date, "%m/%d/%Y").date()
        except ValueError:
            continue
        if d < cutoff:
            continue

        if progress and i % 10 == 0:
            progress(f"{i}/{len(rows)} сделок обработано...")

        buy_price = _price_on(conn, ticker, d)
        if not buy_price:
            continue

        if ticker not in current_price_cache:
            try:
                hist = yf.Ticker(_yf_symbol(ticker)).history(period="5d")
                # The most recent row can be NaN (today's bar not settled yet /
                # market not open yet) -- drop those before taking the last close.
                closes = hist["Close"].dropna()
                current_price_cache[ticker] = float(closes.iloc[-1]) if not closes.empty else None
            except Exception:
                current_price_cache[ticker] = None
        cur_price = current_price_cache[ticker]
        if not cur_price:
            continue

        return_pct = (cur_price / buy_price - 1) * 100
        amount = _parse_amount_mid(amount_range)
        profit = amount * return_pct / 100

        slot = per_politician.setdefault(member_name, {
            "state_district": state_district, "trades": [], "total_profit": 0.0, "total_invested": 0.0,
        })
        slot["trades"].append((ticker, txn_date, return_pct, profit))
        slot["total_profit"] += profit
        slot["total_invested"] += amount

    ranked = []
    for name, data in per_politician.items():
        if not data["trades"]:
            continue
        avg_return = sum(t[2] for t in data["trades"]) / len(data["trades"])
        ranked.append({
            "name": name,
            "state_district": data["state_district"],
            "num_trades": len(data["trades"]),
            "total_profit": data["total_profit"],
            "total_invested": data["total_invested"],
            "avg_return_pct": avg_return,
            "trades": data["trades"],
        })

    ranked = [r for r in ranked if r["total_profit"] > 0]
    ranked.sort(key=lambda r: r["total_profit"], reverse=True)
    return ranked[:n]
