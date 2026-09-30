"""Positions the user reports buying (/bought, /sold in telegram_bot.py), and when
to close them.

Only reported positions are tracked, at the user's own entry price -- the bot never
reads or trades the brokerage account. A close alert fires once per position, on the
first of:

  insider_sell  one of the insiders behind the signal it came from sells
                after the open date -- Form 4 (not a 10b5-1 planned sale), a Form 144
                notice of intent, or a sale row from Oslo, FI or BaFin;
  caution       (coins) a caution signal on the coin -- ETF outflows, a company
                selling, coins moving onto exchanges -- journaled in the last
                CAUTION_LOOKBACK_DAYS and not before the open date, and the price
                confirms it: down over 7 days and below the 20-day average;
  time          EXIT_MAX_DAYS held -- the horizon insider-buying research looks at;
  stop_loss     the last close is EXIT_STOP_LOSS_PCT (stocks) or
                EXIT_STOP_LOSS_PCT_CRYPTO (coins) or more below the entry.

The alert doesn't close the position; /sold does. The user stays in control.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass

import cluster
import crypto
import marketcap

EXIT_MAX_DAYS = 90
EXIT_STOP_LOSS_PCT = 15.0
# Coins swing far more than stocks: 15% is an ordinary month for bitcoin. A fixed 25%
# until the volatility-scaled stop (the bottom of the coin's usual monthly range)
# ships with the range calculation.
EXIT_STOP_LOSS_PCT_CRYPTO = 25.0
CAUTION_LOOKBACK_DAYS = 7
_CAUTION_TEXT = {"etf_flow": "отток из спот-ETF", "treasury": "компания продала монеты",
                 "exchange_flow": "монеты заводят на биржи"}

# (label, SQL returning (person, sale date) for one issuer key and an ISO since-date).
# BaFin is handled separately: its dates are DD.MM.YYYY and don't compare as text.
_SALE_QUERIES = (
    ("Form 4", "SELECT owner_name, transaction_date FROM sec_sales "
               "WHERE ticker = ? AND transaction_date >= ? AND COALESCE(is_10b5_1, 0) = 0"),
    ("Form 144", "SELECT person_name, COALESCE(NULLIF(approx_sale_date, ''), date(found_at)) "
                 "FROM sec_proposed_sales WHERE ticker = ? "
                 "AND COALESCE(NULLIF(approx_sale_date, ''), date(found_at)) >= ?"),
    ("Oslo", "SELECT person, txn_date FROM norway_purchases "
             "WHERE ticker = ? AND txn_type = 'S' AND txn_date >= ?"),
    ("FI", "SELECT person, txn_date FROM sweden_purchases "
           "WHERE isin = ? AND txn_type = 'S' AND status = 'Aktuell' AND txn_date >= ?"),
)


@dataclass
class Position:
    id: int
    ticker: str
    source: str | None
    opened_at: str
    entry_price: float
    insiders: list[str]
    signal_id: int | None
    closed_at: str | None
    close_reason: str | None
    close_alerted_at: str | None


@dataclass
class CloseAlert:
    position: Position
    trigger: str            # insider_sell / caution / time / stop_loss
    detail: str
    last_price: float | None


_COLS = ("id, ticker, source, opened_at, entry_price, insiders, signal_id, closed_at, "
         "close_reason, close_alerted_at")


def _row(r) -> Position:
    return Position(r[0], r[1], r[2], r[3], r[4], json.loads(r[5] or "[]"), r[6], r[7], r[8], r[9])


def open_positions(conn) -> list[Position]:
    return [_row(r) for r in conn.execute(
        f"SELECT {_COLS} FROM positions WHERE closed_at IS NULL ORDER BY opened_at")]


# A journal row that is a buy-side signal, whatever tier the model gave it that day (buy,
# watch, block, skip, none, or the retired strong / candidate): everything but a caution
# (a bearish coin signal) and a group exit. /bought reads the source and the insiders
# from the latest such row.
_BUY_SIDE_ROW = "COALESCE(tier, '') != 'caution' AND COALESCE(kind, '') != 'exit'"


def position_source(conn, ticker: str) -> str | None:
    """The source to price `ticker` against: "CRYPTO" for a crypto ticker,
    otherwise the source of the latest buy-side signal_journal row for it
    (see _BUY_SIDE_ROW), otherwise None.

    The one definition of this lookup -- open_position uses it to decide what to
    store, and telegram_bot's /bought needs the identical answer BEFORE storing,
    to price the ticker against the same listing open_position will save it
    under. Without that, an Oslo-only ticker sharing its letters with a US one
    (EQNR/DNB/FRO/NRC) prices as the wrong, unrelated US quote."""
    ticker = ticker.strip().upper()
    if crypto.is_crypto(ticker):
        return "CRYPTO"
    row = conn.execute(
        f"SELECT source FROM signal_journal WHERE ticker = ? AND {_BUY_SIDE_ROW} "
        "ORDER BY emitted_at DESC, id DESC LIMIT 1", (ticker,)).fetchone()
    return row[0] if row else None


def open_position(conn, ticker: str, entry_price: float, today: dt.date | None = None,
                  source: str | None = None) -> Position:
    """`source` overrides what position_source() would otherwise resolve --
    telegram_bot passes it explicitly, having already resolved it itself to
    price against the same listing before storing."""
    ticker = ticker.strip().upper()
    if any(p.ticker == ticker for p in open_positions(conn)):
        raise ValueError(f"position in {ticker} is already open")
    sig = conn.execute(
        f"SELECT id, members FROM signal_journal WHERE ticker = ? AND {_BUY_SIDE_ROW} "
        "ORDER BY emitted_at DESC, id DESC LIMIT 1", (ticker,)).fetchone()
    signal_id, members = sig if sig else (None, "[]")
    conn.execute(
        "INSERT INTO positions (ticker, source, opened_at, entry_price, insiders, signal_id) "
        "VALUES (?,?,?,?,?,?)",
        (ticker, source or position_source(conn, ticker), (today or dt.date.today()).isoformat(),
         float(entry_price), members or "[]", signal_id))
    conn.commit()
    return next(p for p in open_positions(conn) if p.ticker == ticker)


def close_position(conn, ticker: str, reason: str = "manual",
                   today: dt.date | None = None) -> Position | None:
    ticker = ticker.strip().upper()
    pos = next((p for p in open_positions(conn) if p.ticker == ticker), None)
    if pos is None:
        return None
    pos.closed_at, pos.close_reason = (today or dt.date.today()).isoformat(), reason
    conn.execute("UPDATE positions SET closed_at = ?, close_reason = ? WHERE id = ?",
                 (pos.closed_at, reason, pos.id))
    conn.commit()
    return pos


def yahoo_symbol(ticker: str, source: str | None = None) -> str | None:
    """The Yahoo Finance symbol that prices `ticker` as `source` discloses it, or
    None when there isn't a reliable one.

    Crypto quotes as BTC-USD regardless of source. Otherwise the venue comes from
    marketcap.SOURCE_VENUE (e.g. NORWAY -> ".OL"); a source with no listed venue is
    tried as a bare US ticker, matching marketcap._fetch's own convention. BaFin and
    FI (Finansinspektionen) identify issuers by ISIN, not ticker, and an ISIN never
    resolves on Yahoo -- pricing "BTC" as the Grayscale Bitcoin Mini Trust ETF, or a
    German ISIN as whatever US ticker happens to share its characters, is exactly
    the bug this function exists to avoid.
    """
    if crypto.is_crypto(ticker):
        return crypto.yf_symbol(ticker)
    if marketcap._looks_like_isin(ticker):
        return None
    suffix = marketcap.SOURCE_VENUE.get(source or "", "")
    return (ticker if suffix else ticker.replace(".", "-")) + suffix


def _yahoo_close(symbol: str) -> float | None:
    """Isolated network seam so tests can monkeypatch just the HTTP call."""
    try:
        import yfinance as yf
        hist = yf.Ticker(symbol).history(period="5d")["Close"].dropna()
    except Exception:
        return None
    return float(hist.iloc[-1]) if len(hist) else None


def last_close(ticker: str, source: str | None = None) -> float | None:
    """Most recent daily close from Yahoo for the listing `source` trades the
    ticker on, or None (an ISIN, a delisting, no network)."""
    symbol = yahoo_symbol(ticker, source)
    return _yahoo_close(symbol) if symbol else None


def _insider_sale(conn, pos: Position) -> str | None:
    keys = {cluster.name_key(n) for n in pos.insiders}
    if not keys:
        return None
    for label, sql in _SALE_QUERIES:
        for person, date in conn.execute(sql, (pos.ticker, pos.opened_at)):
            if cluster.name_key(person) in keys:
                return f"{person} — {label}, {date}"
    for person, date in conn.execute(
            "SELECT notifier_name, txn_date FROM bafin_purchases WHERE isin = ? AND txn_type = 'S'",
            (pos.ticker,)):
        try:
            iso = dt.datetime.strptime(date, "%d.%m.%Y").date().isoformat()
        except (TypeError, ValueError):
            continue
        if iso >= pos.opened_at and cluster.name_key(person) in keys:
            return f"{person} — BaFin, {iso}"
    return None


def _crypto_caution(conn, pos: Position, today: dt.date, trend_fn) -> str | None:
    """The newest caution signal on this coin from the last CAUTION_LOOKBACK_DAYS, not
    before the position opened, when the price confirms it today -- else None."""
    if not crypto.is_crypto(pos.ticker):
        return None
    since = max(pos.opened_at, (today - dt.timedelta(days=CAUTION_LOOKBACK_DAYS)).isoformat())
    row = conn.execute(
        "SELECT kind, total_value_eur FROM signal_journal WHERE ticker = ? AND tier = 'caution' "
        "AND date(emitted_at) >= ? AND date(emitted_at) <= ? ORDER BY emitted_at DESC, id DESC LIMIT 1",
        (pos.ticker, since, today.isoformat())).fetchone()
    if row is None:
        return None
    trend = trend_fn(conn, crypto.symbol_of(pos.ticker))
    if not crypto.trend_confirms_down(trend):
        return None
    kind, value = row
    what = _CAUTION_TEXT.get(kind, "сигнал осторожности") + (f" (€{value / 1e6:,.0f} млн)" if value else "")
    return (f"{what}; цена подтверждает: {trend['ret_7d']:+.1f}% за 7 дн., "
            f"ниже 20-дн. средней")


def check_exits(conn, today: dt.date | None = None, price_fn=None,
                trend_fn=None) -> list[CloseAlert]:
    today = today or dt.date.today()
    price_fn = price_fn or last_close
    trend_fn = trend_fn or crypto.price_trend
    alerts = []
    for pos in open_positions(conn):
        if pos.close_alerted_at:
            continue
        sale = _insider_sale(conn, pos)
        price = price_fn(pos.ticker, pos.source)
        if sale:
            alerts.append(CloseAlert(pos, "insider_sell", sale, price))
            continue
        caution = _crypto_caution(conn, pos, today, trend_fn)
        if caution:
            alerts.append(CloseAlert(pos, "caution", caution, price))
            continue
        held = (today - dt.date.fromisoformat(pos.opened_at)).days
        if held >= EXIT_MAX_DAYS:
            alerts.append(CloseAlert(pos, "time", f"{held} дн. в позиции", price))
            continue
        if price is None:
            print(f"[positions] no price for {pos.ticker}; stop-loss check skipped today")
            continue
        stop = EXIT_STOP_LOSS_PCT_CRYPTO if crypto.is_crypto(pos.ticker) else EXIT_STOP_LOSS_PCT
        if price <= pos.entry_price * (1 - stop / 100):
            change = (price / pos.entry_price - 1) * 100
            alerts.append(CloseAlert(pos, "stop_loss", f"{change:+.1f}% от входа", price))
    return alerts


def mark_alerted(conn, alerts: list[CloseAlert], today: dt.date | None = None) -> None:
    stamp = (today or dt.date.today()).isoformat()
    for a in alerts:
        conn.execute("UPDATE positions SET close_alerted_at = ? WHERE id = ?", (stamp, a.position.id))
    conn.commit()
