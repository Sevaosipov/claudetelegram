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


# ----------------------------------------------------------- orders & fills
def _rows(conn, sql: str, params: tuple) -> list[dict]:
    cur = conn.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def orders(conn, code: str) -> list[dict]:
    return _rows(conn, "SELECT * FROM paper_orders WHERE book = ? ORDER BY id", (code,))


def pending_orders(conn, code: str) -> list[dict]:
    return _rows(conn, "SELECT * FROM paper_orders WHERE book = ? AND status = 'pending' "
                       "ORDER BY id", (code,))


def open_positions(conn, code: str) -> list[dict]:
    return _rows(conn, "SELECT * FROM paper_positions WHERE book = ? AND closed_date IS NULL "
                       "ORDER BY id", (code,))


def closed_positions(conn, code: str) -> list[dict]:
    return _rows(conn, "SELECT * FROM paper_positions WHERE book = ? AND closed_date IS NOT NULL "
                       "ORDER BY closed_date, id", (code,))


def cash(conn, code: str) -> float:
    return conn.execute("SELECT cash_eur FROM paper_books WHERE code = ?", (code,)).fetchone()[0]


def _add_cash(conn, code: str, delta: float) -> None:
    conn.execute("UPDATE paper_books SET cash_eur = cash_eur + ? WHERE code = ?", (delta, code))


def position_value(pos: dict, bars: list[tuple[str, float]], fx_now: float) -> float | None:
    """EUR value at the latest close: net x (close now / close on the fill day) x
    (FX then / FX now). Both closes come from the same fresh series, so a dividend
    adjustment made since the fill moves them together."""
    if not bars:
        return None
    entry = close_on_or_before(bars, pos["fill_date"])
    if not entry:
        return None
    return pos["net_eur"] * (bars[-1][1] / entry) * (pos["entry_fx"] / fx_now)


def mark_to_market(conn, code: str, prices: Prices) -> None:
    """Revalue every open position; one with no price keeps its last value."""
    for p in open_positions(conn, code):
        value = position_value(p, prices.bars(p["symbol"]), fx.per_eur(p["currency"], conn))
        if value is not None:
            conn.execute("UPDATE paper_positions SET last_value = ? WHERE id = ?", (value, p["id"]))
    conn.commit()


def book_value(conn, code: str) -> float:
    return cash(conn, code) + sum(
        p["last_value"] if p["last_value"] is not None else p["net_eur"]
        for p in open_positions(conn, code))


def _record(conn, code: str, ticker: str, source: str | None, side: str, reason: str,
            today: dt.date, status: str, *, amount: float | None = None,
            position_id: int | None = None, note: str | None = None, insiders=(),
            target: float | None = None) -> None:
    conn.execute(
        "INSERT INTO paper_orders (book, ticker, source, side, amount_eur, position_id, reason, "
        "created, status, note, insiders, target) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (code, ticker, source, side, amount, position_id, reason, today.isoformat(), status, note,
         json.dumps(list(insiders), ensure_ascii=False), target))
    conn.commit()


def place_buy(conn, code: str, ticker: str, source: str | None, reason: str, today: dt.date,
              amount_eur: float, *, max_positions: int | None = None, min_fraction: float = 0.5,
              insiders=(), target: float | None = None) -> str:
    """Queue a buy of `amount_eur`, filled at the next close. Returns "pending",
    "skipped" (recorded, with why) or "duplicate" (not recorded: the book already
    holds the ticker or has a buy pending for it). With less cash than
    `amount_eur`, it buys with what's left if that is at least `min_fraction` of
    the amount."""
    held = {p["ticker"] for p in open_positions(conn, code)}
    buys = [o for o in pending_orders(conn, code) if o["side"] == "buy"]
    if ticker in held or any(o["ticker"] == ticker for o in buys):
        return "duplicate"

    def skip(note: str) -> str:
        _record(conn, code, ticker, source, "buy", reason, today, "skipped", note=note)
        return "skipped"

    if listing(ticker, source) is None:
        return skip("нет котировки")
    if max_positions is not None and len(held) + len(buys) >= max_positions:
        return skip("мест нет")
    available = cash(conn, code) - sum(o["amount_eur"] or 0.0 for o in buys)
    if available >= amount_eur:
        amount = amount_eur
    elif available >= max(amount_eur * min_fraction, 1.0):
        amount = available
    else:
        return skip("нет денег")
    _record(conn, code, ticker, source, "buy", reason, today, "pending", amount=amount,
            insiders=insiders, target=target)
    return "pending"


def place_sell(conn, code: str, position: dict, reason: str, today: dt.date) -> None:
    if any(o["side"] == "sell" and o["position_id"] == position["id"]
           for o in pending_orders(conn, code)):
        return
    _record(conn, code, position["ticker"], position["source"], "sell", reason, today, "pending",
            position_id=position["id"])


def _set_order(conn, order_id: int, status: str, note: str | None = None) -> None:
    conn.execute("UPDATE paper_orders SET status = ?, note = ? WHERE id = ?", (status, note, order_id))


def _fill_buy(conn, code: str, order: dict, symbol: str, currency: str, day: str,
              close: float) -> None:
    amount = min(order["amount_eur"], cash(conn, code))
    if amount <= 0:
        _set_order(conn, order["id"], "cancelled", "нет денег")
        return
    net = amount * (1 - fee(order["ticker"], currency))
    conn.execute(
        "INSERT INTO paper_positions (book, ticker, source, symbol, currency, fill_date, cost_eur, "
        "net_eur, entry_close, entry_fx, insiders, target, reason, last_value) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (code, order["ticker"], order["source"], symbol, currency, day, amount, net, close,
         fx.per_eur(currency, conn), order["insiders"], order["target"], order["reason"], net))
    _add_cash(conn, code, -amount)
    _set_order(conn, order["id"], "filled")


def _fill_sell(conn, code: str, order: dict, bars: list[tuple[str, float]], day: str,
               close: float) -> None:
    [pos] = _rows(conn, "SELECT * FROM paper_positions WHERE id = ?", (order["position_id"],))
    entry = close_on_or_before(bars, pos["fill_date"]) or pos["entry_close"]
    value = pos["net_eur"] * (close / entry) * (pos["entry_fx"] / fx.per_eur(pos["currency"], conn))
    proceeds = value * (1 - fee(pos["ticker"], pos["currency"]))
    conn.execute("UPDATE paper_positions SET closed_date = ?, close_reason = ?, proceeds_eur = ?, "
                 "last_value = ? WHERE id = ?", (day, order["reason"], proceeds, value, pos["id"]))
    _add_cash(conn, code, proceeds)
    _set_order(conn, order["id"], "filled")


def fill_orders(conn, code: str, prices: Prices, today: dt.date) -> None:
    """Fill every pending order whose first close after its decision day is known --
    sales first, since they free cash, then buys in order. An order still without a
    price after ORDER_MAX_BUSINESS_DAYS is cancelled."""
    for order in sorted(pending_orders(conn, code), key=lambda o: (o["side"] != "sell", o["id"])):
        lst = listing(order["ticker"], order["source"])
        bars = prices.bars(lst[0]) if lst else []
        nxt = first_close_after(bars, order["created"])
        if nxt is None:
            if business_days_between(order["created"], today) > ORDER_MAX_BUSINESS_DAYS:
                _set_order(conn, order["id"], "cancelled", "не исполнено: нет цены")
            continue
        day, close = nxt
        if order["side"] == "sell":
            _fill_sell(conn, code, order, bars, day, close)
        else:
            _fill_buy(conn, code, order, lst[0], lst[1], day, close)
    conn.commit()
