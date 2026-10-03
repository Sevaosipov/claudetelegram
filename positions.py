"""Positions the user reports buying (/bought, /sold in telegram_bot.py) or holds in the
Trading 212 account, and when to close them.

A position is either reported (origin 'manual': /bought, at the user's own entry price) or read
from the Trading 212 account (origin 't212': t212_account.py keeps those in step with the
account, with its quantity and average price, and closes one that was sold there). The bot
only reads the account and never trades. A Trading 212 holding with a US listing is keyed and
priced like a /bought one; any other is keyed by its ISIN with source T212_SOURCE and priced
from the day prices the sync stores (t212_prices). Every position leaves on the model's own
exits (model.py), and a close alert fires once per position, on the first of:

  insider_sell   one of the insiders behind the signal it came from sells
                 after the open date -- Form 4 (not a 10b5-1 planned sale), a Form 144
                 notice of intent, or a sale row from Oslo, FI or BaFin;
  caution        (coins) a caution signal on the coin -- ETF outflows, a company
                 selling, coins moving onto exchanges -- journaled in the last
                 CAUTION_LOOKBACK_DAYS and not before the open date, and the price
                 confirms it: down over 7 days and below the 20-day average;
  trailing_stop  the price is `stop_pct` or more below the highest close since the open
                 (the entry price counts as one). The stop is fixed when the position is
                 recorded, from the price history then; a position without one takes it
                 from the closes before its open date, else the model's fallback;
  activist_cut   (a position bought on a 13D/G stake) one of its filers has since reported
                 a smaller stake -- model._activist_cut, the model's own rule;
  trend_down     (coins) below the 100-day average and down over 20 days;
  dead_money     (stocks) 60 business days held and a return under 5%;
  time           a year held;
  news           a red-flag headline on the ticker.

The alert doesn't close the position; /sold does (a Trading 212 holding closes when the account
no longer has it). The user stays in control.

position_status is the live view of one open position (its price, result, peak and stop),
and portfolio_rows gathers it for all of them: what /portfolio shows in Telegram and what
`analyst.py portfolio` prints.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from dataclasses import dataclass

import cluster
import crypto
import marketcap
import model_score

# model.py and paper.py import this module, so they are imported inside the functions
# that need them (model for the exit constants and its news seam, paper for the prices).

CAUTION_LOOKBACK_DAYS = 7
MANUAL, T212 = "manual", "t212"         # a position's origin: /bought, or the Trading 212 account
T212_SOURCE = "T212"                    # the source of a Trading 212 holding with no Yahoo listing
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
    stop_pct: float | None = None       # the trailing stop's distance, fixed at open
    origin: str = MANUAL                # MANUAL (/bought) or T212 (read from the account)
    quantity: float | None = None       # a Trading 212 holding's shares
    t212_ticker: str | None = None      # its Trading 212 id: AAPL_US_EQ, SAPd_EQ
    currency: str | None = None         # its instrument's currency (entry_price is in it)


@dataclass
class CloseAlert:
    position: Position
    trigger: str            # insider_sell / caution / trailing_stop / activist_cut / trend_down /
                            # dead_money / time / news
    detail: str
    last_price: float | None


_COLS = ("id, ticker, source, opened_at, entry_price, insiders, signal_id, closed_at, "
         "close_reason, close_alerted_at, stop_pct, origin, quantity, t212_ticker, currency")


def _row(r) -> Position:
    return Position(r[0], r[1], r[2], r[3], r[4], json.loads(r[5] or "[]"), r[6], r[7], r[8], r[9], r[10],
                    r[11] or MANUAL, r[12], r[13], r[14])


def open_positions(conn) -> list[Position]:
    return [_row(r) for r in conn.execute(
        f"SELECT {_COLS} FROM positions WHERE closed_at IS NULL ORDER BY opened_at")]


def find_open(conn, ticker: str) -> Position | None:
    """The open position in `ticker`, of either origin, or None."""
    ticker = ticker.strip().upper()
    return next((p for p in open_positions(conn) if p.ticker == ticker), None)


_EXCHANGE_SUFFIX = re.compile(r"[a-z]*_EQ$")


def name_of(ticker: str, source: str | None = None, t212_ticker: str | None = None) -> str:
    """How the user knows a name: a coin by its symbol, a Trading 212 holding keyed by its ISIN
    by its Trading 212 symbol (SAPd_EQ -> SAP), anything else by its ticker."""
    if crypto.is_crypto(ticker):
        return crypto.symbol_of(ticker)
    if source == T212_SOURCE and t212_ticker:
        return _EXCHANGE_SUFFIX.sub("", t212_ticker) or ticker
    return ticker


def display_name(pos: Position) -> str:
    """name_of an open position."""
    return name_of(pos.ticker, pos.source, pos.t212_ticker)


def oslo_ticker(conn, isin: str) -> str | None:
    """The Oslo ticker of an ISIN, when norway's ISIN cache (oslo_isins) knows it: Oslo's insider
    rows name the company by that ticker, Trading 212 by the ISIN of its Frankfurt listing."""
    isin = (isin or "").strip().upper()
    if not isin:
        return None
    row = conn.execute("SELECT ticker FROM oslo_isins WHERE isin = ? ORDER BY fetched_at DESC LIMIT 1",
                       (isin,)).fetchone()
    return row[0] if row else None


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


def split_venue(key: str) -> tuple[str, str] | None:
    """("EQNR", "NORWAY") for "EQNR.OL", ("VOLV-B", "SWEDEN") for "VOLV-B.ST": the signal tables
    know an Oslo or Stockholm stock as the bare ticker with the source that disclosed it (the
    inverse of yahoo_symbol). None for any other key."""
    for source, venue in marketcap.SOURCE_VENUE.items():
        if venue and key.endswith(venue) and len(key) > len(venue):
            return key[:-len(venue)], source
    return None


def _kind(ticker: str) -> str:
    return "crypto" if crypto.is_crypto(ticker) else "stock"


def _buy_signal(conn, ticker: str, source: str | None):
    """(id, members) of the latest buy-side journal row for `ticker`, or None. On an Oslo or
    Stockholm listing only that source's rows count: the same letters are another company
    on another exchange (NRC is National Research Corp in New York, NRC Group in Oslo). A
    Trading 212 holding keyed by its ISIN takes the rows of that ISIN (BaFin and FI name an
    issuer by it) and, when the ISIN is an Oslo listing's, the Oslo rows of its ticker."""
    if source == T212_SOURCE:
        return conn.execute(
            f"SELECT id, members FROM signal_journal WHERE {_BUY_SIDE_ROW} "
            "AND (ticker = ? OR (ticker = ? AND source = 'NORWAY')) "
            "ORDER BY emitted_at DESC, id DESC LIMIT 1",
            (ticker, oslo_ticker(conn, ticker) or "")).fetchone()
    venue = source if marketcap.SOURCE_VENUE.get(source or "") else None
    return conn.execute(
        f"SELECT id, members FROM signal_journal WHERE ticker = ? AND {_BUY_SIDE_ROW} "
        "AND (? IS NULL OR source = ?) ORDER BY emitted_at DESC, id DESC LIMIT 1",
        (ticker, venue, venue)).fetchone()


def open_position(conn, ticker: str, entry_price: float, today: dt.date | None = None,
                  source: str | None = None, *, closes_fn=None) -> Position:
    """`source` overrides what position_source() would otherwise resolve --
    telegram_bot passes it explicitly, having already resolved it itself to
    price against the same listing before storing.

    The position's trailing stop is fixed here, from the price history now
    (model_score.stop_distance); it is None when there isn't enough history, and the
    exit check then works one out itself. `closes_fn(ticker, source)` returns the
    history, [(iso date, close)] oldest first; the default is daily_closes."""
    ticker = ticker.strip().upper()
    if any(p.ticker == ticker for p in open_positions(conn)):
        raise ValueError(f"position in {ticker} is already open")
    source = source or position_source(conn, ticker)
    sig = _buy_signal(conn, ticker, source)
    signal_id, members = sig if sig else (None, "[]")
    closes = (closes_fn or daily_closes)(ticker, source)
    stop_pct = model_score.stop_distance([c for _d, c in closes], _kind(ticker))
    conn.execute(
        "INSERT INTO positions (ticker, source, opened_at, entry_price, insiders, signal_id, stop_pct) "
        "VALUES (?,?,?,?,?,?,?)",
        (ticker, source, (today or dt.date.today()).isoformat(),
         float(entry_price), members or "[]", signal_id, stop_pct))
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


def t212_closes(conn, ticker: str) -> list[tuple[str, float]]:
    """The day prices the Trading 212 sync stored for `ticker` (t212_prices), oldest first: the
    last price of each day, today's included (still in progress, like a Yahoo bar of today)."""
    return [(d, float(p)) for d, p in conn.execute(
        "SELECT date, price FROM t212_prices WHERE ticker = ? AND price IS NOT NULL ORDER BY date",
        (ticker,))]


def last_close(ticker: str, source: str | None = None, conn=None) -> float | None:
    """Most recent daily close from Yahoo for the listing `source` trades the
    ticker on, or None (an ISIN, a delisting, no network). A Trading 212 holding with no
    Yahoo listing (source T212_SOURCE) has the last price the sync stored, given `conn`."""
    symbol = yahoo_symbol(ticker, source)
    if symbol:
        return _yahoo_close(symbol)
    if source == T212_SOURCE and conn is not None:
        bars = t212_closes(conn, ticker)
        return bars[-1][1] if bars else None
    return None


def daily_closes(ticker: str, source: str | None = None, conn=None) -> list[tuple[str, float]]:
    """The adjusted daily closes, oldest first, of the listing `source` trades the ticker
    on (paper._closes on its Yahoo symbol); [] when it has no reliable symbol or the fetch
    fails. A Trading 212 holding with no Yahoo listing (source T212_SOURCE) has the day
    prices the sync stored instead (t212_closes), given `conn`. Imported here: paper imports
    this module."""
    import paper
    symbol = yahoo_symbol(ticker, source)
    if not symbol:
        return t212_closes(conn, ticker) if source == T212_SOURCE and conn is not None else []
    try:
        return list(paper._closes(symbol, paper.PRICE_DAYS) or [])
    except Exception as e:
        print(f"[positions] no price history for {ticker}: {type(e).__name__}: {e}", file=sys.stderr)
        return []


def _insider_sale(conn, pos: Position) -> str | None:
    keys = {cluster.name_key(n) for n in pos.insiders}
    if not keys:
        return None
    # Oslo's rows name a company by its Oslo ticker; a Trading 212 holding of it, by the ISIN.
    # (model.py hands in a namespace of a book's position, which has no source.)
    oslo = oslo_ticker(conn, pos.ticker) if getattr(pos, "source", None) == T212_SOURCE else None
    for label, sql in _SALE_QUERIES:
        key = oslo if label == "Oslo" and oslo else pos.ticker
        for person, date in conn.execute(sql, (key, pos.opened_at)):
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


def _completed_bars(bars: list[tuple[str, float]], today: dt.date) -> list[tuple[str, float]]:
    """The closes before `today`, as the model's paper.Prices(today) keeps them: a bar for today
    is still in progress (a coin's always is) and must not set a peak or trip a stop or a trend."""
    return [b for b in bars if b[0] < today.isoformat()]


def _stop_and_peak(pos: Position, bars: list[tuple[str, float]]) -> tuple[float, float]:
    """(the stop's distance, the highest close since the open -- the entry price counts as one)
    of a position, from its completed closes `bars`. A position with no stored stop takes the
    one the closes before its open date give, else the model's fallback for its kind. The one
    computation behind the trailing-stop alert and the status /portfolio shows."""
    import model
    stop = pos.stop_pct
    if stop is None:
        kind = _kind(pos.ticker)
        stop = model_score.stop_distance([c for d, c in bars if d < pos.opened_at], kind)
        if stop is None:
            stop = model.FALLBACK_STOP[kind]
    peak = max([pos.entry_price] + [c for d, c in bars if d >= pos.opened_at])
    return stop, peak


def _trailing_stop(pos: Position, bars: list[tuple[str, float]], price: float) -> str | None:
    """The price is the position's stop or more below its highest close since the open
    (_stop_and_peak)."""
    stop, peak = _stop_and_peak(pos, bars)
    if price <= peak * (1 - stop):
        return f"−{stop * 100:.0f}% от максимума {peak:,.2f}"
    return None


def _seams(conn, price_fn, closes_fn):
    """(price_fn, closes_fn): the ones given, else last_close and daily_closes -- looked up when
    called, so a replacement of the module's own counts -- with `conn` for a Trading 212 holding
    with no Yahoo listing (source T212_SOURCE), whose prices are the sync's day prices."""
    def price(ticker, source=None):
        return last_close(ticker, source, conn=conn) if source == T212_SOURCE else last_close(ticker, source)

    def closes(ticker, source=None):
        return (daily_closes(ticker, source, conn=conn) if source == T212_SOURCE
                else daily_closes(ticker, source))
    return price_fn or price, closes_fn or closes


def last_price(conn, ticker: str, source: str | None = None) -> float | None:
    """The price check_exits and /portfolio read for a position by default: last_close, with the
    database for a Trading 212 holding that has no Yahoo listing."""
    return _seams(conn, None, None)[0](ticker, source)


def position_status(pos: Position, today: dt.date, *, closes_fn=None, price_fn=None, conn=None) -> dict:
    """How an open position stands, from the same price and history the exits read (`price_fn`
    and `closes_fn`, `(ticker, source)` seams with check_exits' defaults; `conn` prices a
    Trading 212 holding with no Yahoo listing from the sync's day prices):
      last        the price, or None when there isn't one;
      result      last / entry price - 1, or None;
      days        days since the open;
      peak        the entry price or the highest completed close since the open;
      stop_pct    the stop's distance: the stored one, else the closes before the open give,
                  else the model's fallback;
      stop_level  peak * (1 - stop_pct): the price the trailing stop fires at;
      to_stop     how far the price can still fall before the stop fires, as a share of the
                  price now: 1 - stop_level / last (the price at its peak: the stop's own
                  distance). Zero at the stop level, negative below it (how far below, as
                  a share of the price now); None with no price."""
    price_fn, closes_fn = _seams(conn, price_fn, closes_fn)
    last = price_fn(pos.ticker, pos.source) or None
    bars = _completed_bars(closes_fn(pos.ticker, pos.source), today)
    stop_pct, peak = _stop_and_peak(pos, bars)
    stop_level = peak * (1 - stop_pct)
    return {"last": last,
            "result": last / pos.entry_price - 1 if last else None,
            "days": (today - dt.date.fromisoformat(pos.opened_at)).days,
            "peak": peak, "stop_pct": stop_pct, "stop_level": stop_level,
            "to_stop": 1 - stop_level / last if last else None}


_VENUE_SOURCES = ("NORWAY", "SWEDEN")


def _asset_key(ticker: str, source: str | None = None) -> tuple[str, str, str]:
    """A name as the model's books and the user's positions share it: a coin by its symbol, a
    stock by its ticker (the stock BTC and the coin BTC stay two assets), and an Oslo or
    Stockholm listing with its venue (Oslo's NRC is not the US NRC)."""
    if crypto.is_crypto(ticker):
        return "coin", crypto.symbol_of(ticker).upper(), ""
    return "stock", ticker.upper(), source if source in _VENUE_SOURCES else ""


def _model_names(conn) -> set[tuple[str, str, str]]:
    """What MODEL-S and MODEL-C hold now (_asset_key)."""
    import model
    import paper
    return {_asset_key(p["ticker"], p["source"])
            for code in model.BOOKS for p in paper.open_positions(conn, code)}


def portfolio_rows(conn, today: dt.date, *, origin: str | None = None) -> list[tuple[Position, dict, bool]]:
    """What /portfolio shows: (position, position_status, model_holds) for each open position
    (of `origin` only, when given), oldest first. `model_holds`: MODEL-S or MODEL-C has an open
    position in the same name."""
    held = _model_names(conn)
    return [(pos, position_status(pos, today, conn=conn), _asset_key(pos.ticker, pos.source) in held)
            for pos in open_positions(conn) if origin is None or pos.origin == origin]


def _pct(x: float) -> str:
    return f"{x:.1f}%".replace(".", ",")


def _model_exit(conn, pos: Position, today: dt.date, price: float | None, closes_fn,
                news_fn) -> tuple[str, str] | None:
    """The model's exits (model.py) for a position that no insider or caution rule closed:
    (trigger, detail) for the first that holds, else None -- in the model's order, with the
    model's constants. The headlines are fetched only when every rule before them has
    passed."""
    import model
    import paper
    coin = crypto.is_crypto(pos.ticker)
    bars = _completed_bars(closes_fn(pos.ticker, pos.source), today)
    if price is None:
        print(f"[positions] no price for {pos.ticker}; stop check skipped today")
    else:
        stop = _trailing_stop(pos, bars, price)
        if stop:
            return "trailing_stop", stop
    if pos.source == model.STAKE_SOURCE:
        cut = model._activist_cut(conn, pos.ticker, pos.insiders, pos.opened_at)
        if cut:
            return "activist_cut", f"доля {_pct(cut[0])} → {_pct(cut[1])}"
    if coin:
        trend = model_score.coin_trend([c for _d, c in bars])
        if trend and trend["down"]:
            return "trend_down", "ниже 100-дн. средней, 20 дн. в минусе"
    elif (price is not None
            and paper.business_days_between(pos.opened_at, today) >= model.DEAD_MONEY_BDAYS
            and price / pos.entry_price - 1 < model.DEAD_MONEY_MIN_RETURN):
        return "dead_money", f"{model.DEAD_MONEY_BDAYS} торговых дней без роста"
    held = (today - dt.date.fromisoformat(pos.opened_at)).days
    if held >= model.MAX_HOLD_DAYS:
        return "time", f"{held} дн. в позиции"
    red = model_score.news_part(news_fn(pos.ticker, pos.source), coin=coin)[1]
    if red:
        return "news", f"новости: {red}"
    return None


def check_exits(conn, today: dt.date | None = None, price_fn=None, trend_fn=None, *,
                closes_fn=None, news_fn=None) -> list[CloseAlert]:
    """The close alerts due today, one per open position not alerted before, on the first
    rule that holds (see the top of the file). Seams, all `(ticker, source)`: `price_fn`
    the current price (default last_close), `closes_fn` the history (default daily_closes),
    `news_fn` the recent headlines (default model.default_news); `trend_fn(conn, symbol)`
    is the coin trend a caution is confirmed by. A Trading 212 holding with no Yahoo listing is
    priced, by default, from the day prices the sync stored (t212_prices)."""
    import model
    today = today or dt.date.today()
    price_fn, closes_fn = _seams(conn, price_fn, closes_fn)
    trend_fn = trend_fn or crypto.price_trend
    news_fn = news_fn or model.default_news
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
        found = _model_exit(conn, pos, today, price, closes_fn, news_fn)
        if found:
            alerts.append(CloseAlert(pos, found[0], found[1], price))
    return alerts


def mark_alerted(conn, alerts: list[CloseAlert], today: dt.date | None = None) -> None:
    stamp = (today or dt.date.today()).isoformat()
    for a in alerts:
        conn.execute("UPDATE positions SET close_alerted_at = ? WHERE id = ?", (stamp, a.position.id))
    conn.commit()
