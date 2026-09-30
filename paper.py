"""The virtual-book engine: orders that fill at the next close, fees, EUR valuation on
adjusted closes, the daily equity snapshot and the drawdown. The model portfolio (model.py)
is its only trader; the books from the 2026-09-28 design (R1/R2, C-A/C-B, H1/H2) stay in
the database as an archive, and python paper.py prints them. Nothing here ever places a real
order.

A decision made on day D becomes an order filled at the first daily close after D, so a book
never trades at a price the bot couldn't have acted on. Values are measured on adjusted closes
(dividends and splits included), in EUR.
"""
from __future__ import annotations

import datetime as dt
import json
import sys

import assets
import crypto
import db
import fx
import positions
import sources

FEE_FOREIGN = 0.0025            # Trading 212's 0.15% currency fee plus ~0.10% spread
FEE_EUR = 0.0010
FEE_COIN = 0.0050
ORDER_MAX_BUSINESS_DAYS = 5
SKIP_DEDUPE_DAYS = 14           # the same skip (book, ticker, why) is recorded once in this many days
PRICE_DAYS = 420                # room for a 200-day average and a 182-day hold
SUCCESS_DAYS = 182
MIN_STOCK_TRADES = 20
_VENUE_CURRENCY = {".OL": "NOK", ".ST": "SEK", ".DE": "EUR"}


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
    fails. The one network seam -- tests hand their own fetch to Prices/run. A stock
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
    current day as a bar still in progress, and a fill or a value on it would differ
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
                print(f"[paper] no prices for {symbol}: {type(e).__name__}: {e}", file=sys.stderr)
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


def history_days(fill_date: str, today: dt.date) -> int:
    """Days of history that reach back past a position's fill day: a position held
    longer than PRICE_DAYS would otherwise lose its entry close."""
    return max(PRICE_DAYS, (today - dt.date.fromisoformat(fill_date)).days + 30)


def mark_to_market(conn, code: str, prices: Prices, today: dt.date | None = None) -> None:
    """Revalue every open position; one with no price keeps its last value."""
    today = today or dt.date.today()
    for p in open_positions(conn, code):
        bars = prices.bars(p["symbol"], history_days(p["fill_date"], today))
        value = position_value(p, bars, fx.per_eur(p["currency"], conn))
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
            target: float | None = None, stop_pct: float | None = None,
            score: float | None = None) -> None:
    if status == "skipped" and conn.execute(
            "SELECT 1 FROM paper_orders WHERE book = ? AND ticker = ? AND note IS ? "
            "AND status = 'skipped' AND created >= ?",
            (code, ticker, note, (today - dt.timedelta(days=SKIP_DEDUPE_DAYS)).isoformat())).fetchone():
        return      # a signal that stays fresh would otherwise repeat its skip every day
    conn.execute(
        "INSERT INTO paper_orders (book, ticker, source, side, amount_eur, position_id, reason, "
        "created, status, note, insiders, target, stop_pct, score) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (code, ticker, source, side, amount, position_id, reason, today.isoformat(), status, note,
         json.dumps(list(insiders), ensure_ascii=False), target, stop_pct, score))
    conn.commit()


def place_buy(conn, code: str, ticker: str, source: str | None, reason: str, today: dt.date,
              amount_eur: float, *, max_positions: int | None = None, min_fraction: float = 0.5,
              insiders=(), target: float | None = None, stop_pct: float | None = None,
              score: float | None = None) -> str:
    """Queue a buy of `amount_eur`, filled at the next close. Returns "pending",
    "skipped" (recorded, with why) or "duplicate" (not recorded: the book already
    holds the ticker or has a buy pending for it). With less cash than
    `amount_eur`, it buys with what's left if that is at least `min_fraction` of
    the amount. `stop_pct` and `score` (the model portfolio's) ride along on the
    order, and from it on the position."""
    held = {p["ticker"] for p in open_positions(conn, code)}
    buys = [o for o in pending_orders(conn, code) if o["side"] == "buy"]
    if ticker in held or any(o["ticker"] == ticker for o in buys):
        return "duplicate"

    def skip(note: str) -> str:
        _record(conn, code, ticker, source, "buy", reason, today, "skipped", note=note,
                stop_pct=stop_pct, score=score)
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
            insiders=insiders, target=target, stop_pct=stop_pct, score=score)
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
        "net_eur, entry_close, entry_fx, insiders, target, reason, last_value, stop_pct, score) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (code, order["ticker"], order["source"], symbol, currency, day, amount, net, close,
         fx.per_eur(currency, conn), order["insiders"], order["target"], order["reason"], net,
         order["stop_pct"], order["score"]))
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


def _close_at_last_value(conn, code: str, order: dict, today: dt.date) -> None:
    """A sale that never finds a price (a delisting, a takeover) closes the position
    at its last known value, so it doesn't hold a slot forever or go uncounted."""
    [pos] = _rows(conn, "SELECT * FROM paper_positions WHERE id = ?", (order["position_id"],))
    value = pos["last_value"] if pos["last_value"] is not None else pos["net_eur"]
    proceeds = value * (1 - fee(pos["ticker"], pos["currency"]))
    conn.execute("UPDATE paper_positions SET closed_date = ?, close_reason = ?, proceeds_eur = ?, "
                 "last_value = ? WHERE id = ?",
                 (today.isoformat(), f"{order['reason']} (по последней цене: нет котировок)",
                  proceeds, value, pos["id"]))
    _add_cash(conn, code, proceeds)
    _set_order(conn, order["id"], "filled", "по последней цене")


def fill_orders(conn, code: str, prices: Prices, today: dt.date) -> None:
    """Fill every pending order whose first close after its decision day is known --
    sales first, since they free cash, then buys in order. A buy still without a
    price after ORDER_MAX_BUSINESS_DAYS is cancelled; a sale closes at the position's
    last value."""
    for order in sorted(pending_orders(conn, code), key=lambda o: (o["side"] != "sell", o["id"])):
        lst = listing(order["ticker"], order["source"])
        days = PRICE_DAYS
        if order["side"] == "sell":
            (fill_date,) = conn.execute("SELECT fill_date FROM paper_positions WHERE id = ?",
                                        (order["position_id"],)).fetchone()
            days = history_days(fill_date, today)
        bars = prices.bars(lst[0], days) if lst else []
        nxt = first_close_after(bars, order["created"])
        if nxt is None:
            if business_days_between(order["created"], today) > ORDER_MAX_BUSINESS_DAYS:
                if order["side"] == "sell":
                    _close_at_last_value(conn, code, order, today)
                else:
                    _set_order(conn, order["id"], "cancelled", "не исполнено: нет цены")
            continue
        day, close = nxt
        if order["side"] == "sell":
            _fill_sell(conn, code, order, bars, day, close)
        else:
            _fill_buy(conn, code, order, lst[0], lst[1], day, close)
    conn.commit()


# ---------------------------------------------------------------- signals
def insiders_of(sig) -> list[str]:
    names = list(getattr(sig, "member_names", None) or [])
    person = getattr(sig, "person", None)
    return names or ([person] if person else [])


# ------------------------------------------------------- drawdown & snapshot
def max_drawdown(values: list[float]) -> float:
    """The largest fall from a peak to a later low, as a negative fraction (0 if none)."""
    peak, worst = None, 0.0
    for v in values:
        peak = v if peak is None else max(peak, v)
        if peak:
            worst = min(worst, v / peak - 1)
    return worst


def _snapshot(conn, code: str, prices: Prices, today: dt.date) -> None:
    """Store today's value and the benchmark indexed from the start date: start
    money x (close now / close on the start date) x (FX at the start / FX now)."""
    start_date, start_eur, symbol, start_fx = conn.execute(
        "SELECT start_date, start_eur, bench_symbol, bench_start_fx FROM paper_books WHERE code = ?",
        (code,)).fetchone()
    days = max(PRICE_DAYS, (today - dt.date.fromisoformat(start_date)).days + 30)
    bars = prices.bars(symbol, days)
    fx_now = fx.per_eur("USD", conn)
    bench = None
    base = close_on_or_before(bars, start_date) or (bars[0][1] if bars else None)
    if bars and base:
        if start_fx is None:
            start_fx = fx_now
            conn.execute("UPDATE paper_books SET bench_start_fx = ? WHERE code = ?", (start_fx, code))
        bench = start_eur * (bars[-1][1] / base) * (start_fx / fx_now)
    conn.execute("INSERT OR REPLACE INTO paper_equity (book, date, value, cash, bench) VALUES (?,?,?,?,?)",
                 (code, today.isoformat(), book_value(conn, code), cash(conn, code), bench))
    conn.commit()


# -------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    """python paper.py           -- the model portfolio against the 70/30 mix
       python paper.py MODEL-S   -- one book's positions, trades and skips (any code in paper_books)"""
    import argparse
    from pathlib import Path

    import paper_report
    ap = argparse.ArgumentParser(description="Модельный портфель и архив книг")
    ap.add_argument("book", nargs="?", help="код книги: MODEL-S, MODEL-C или архивной, например R1-E1")
    args = ap.parse_args(argv)
    conn = db.connect(Path(__file__).parent / "data" / "disclosures.db")
    if not args.book:
        print(paper_report.format_summary(conn, dt.date.today()))
        return 0
    code = args.book.upper()
    known = [r[0] for r in conn.execute(
        "SELECT code FROM paper_books ORDER BY code LIKE 'MODEL-%' DESC, rowid")]
    if code not in known:
        print(f"Нет такой книги: {args.book}. Есть: {', '.join(known) or 'пока ни одной'}")
        return 2
    print(paper_report.format_book(conn, code))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
