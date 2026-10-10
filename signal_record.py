"""signal_record.py: how the bot's own buy signals did.

Every Friday signal that went out is a row of buy_signals. This reads them back: for each, the price at
the first close on or after the day it was sent, and the return 1, 4 and 12 weeks later (a horizon the
calendar has not reached is left out) -- next to what the market did over the same days: the S&P 500
(SPY) for a stock, bitcoin for a coin. The table says whether the signals are worth following; nothing
here changes which signals are picked.

It goes out by itself at the first run of each month (monthly) and on /signals.
"""
from __future__ import annotations

import datetime as dt
import sys
from dataclasses import dataclass

import crypto
import db

HORIZONS = (("1w", 7), ("4w", 28), ("12w", 84))
BENCHMARK_STOCK, BENCHMARK_COIN = "SPY", "CRYPTO:BTC"
MONTH_KEY = "signal_record_{month}"
_FOREVER = 100 * 365 * 24 * 3600


@dataclass(frozen=True)
class Outcome:
    ticker: str
    sent: str
    returns: dict           # horizon label -> the signal's return, for the horizons reached
    excess: dict            # horizon label -> that return less the benchmark's over the same days
    now: float | None       # the return to the last close


def _at(closes: list[tuple[str, float]], day: str) -> float | None:
    """The first close on or after `day`."""
    return next((c for d, c in closes if d >= day and c), None)


def _move(closes: list[tuple[str, float]], start: str, end: str, last: str) -> float | None:
    """The return from the first close on or after `start` to the first on or after `end`; None when the
    series does not reach `end` (the calendar has not got there) or has no start."""
    if end > last:
        return None
    a, b = _at(closes, start), _at(closes, end)
    return b / a - 1 if a and b else None


def outcome(ticker: str, sent: str, closes: list, bench: list) -> Outcome | None:
    """How one signal did; None when its price history does not cover the day it was sent."""
    if not closes or _at(closes, sent) is None:
        return None
    last = closes[-1][0]
    returns, excess = {}, {}
    for label, days in HORIZONS:
        end = (dt.date.fromisoformat(sent) + dt.timedelta(days=days)).isoformat()
        r = _move(closes, sent, end, last)
        if r is None:
            continue
        returns[label] = r
        b = _move(bench, sent, end, bench[-1][0]) if bench else None
        if b is not None:
            excess[label] = r - b
    return Outcome(ticker, sent, returns, excess, closes[-1][1] / _at(closes, sent) - 1)


def outcomes(conn, *, closes_fn=None) -> list[Outcome]:
    """The outcome of every buy signal sent, oldest first; a signal whose prices fail is left out (logged)."""
    import positions
    closes_fn = closes_fn or positions.daily_closes
    cache: dict = {}

    def series(ticker, source):
        if (ticker, source) not in cache:
            cache[(ticker, source)] = closes_fn(ticker, source) or []
        return cache[(ticker, source)]

    out = []
    for ticker, source, sent in conn.execute("SELECT ticker, source, sent_at FROM buy_signals ORDER BY sent_at, id"):
        try:
            coin = crypto.is_crypto(ticker)
            bench = series(BENCHMARK_COIN if coin else BENCHMARK_STOCK, None)
            got = outcome(ticker, sent, series(ticker, None if coin else source), bench)
            if got is not None:
                out.append(got)
        except Exception as e:
            print(f"[signal_record] {ticker}: {type(e).__name__}: {e}", file=sys.stderr)
    return out


def report(conn, *, closes_fn=None, html: bool = True) -> str:
    import telegram_notify
    return telegram_notify.format_signal_record(outcomes(conn, closes_fn=closes_fn), html=html)


def monthly(conn, *, today: dt.date | None = None, send=None, closes_fn=None) -> bool:
    """Send the record once a month, at the first run of the month, when there is any signal to show.
    True when it went out."""
    import telegram_notify
    today = today or dt.date.today()
    key = MONTH_KEY.format(month=f"{today.year}-{today.month:02d}")
    if db.get_cached_value(conn, key, _FOREVER) is not None:
        return False
    got = outcomes(conn, closes_fn=closes_fn)
    if not got:
        return False
    if not (send or telegram_notify.send_text)(telegram_notify.format_signal_record(got)):
        return False
    db.save_cached_value(conn, key, 1.0)
    return True
