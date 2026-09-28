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
SMALL_START_EUR = 20_000.0      # the high-risk sleeve: small-company insider buying
SMALL_SLICE = 0.20              # of the book's value, per buy -- few signals a month
SMALL_MAX_POSITIONS = 5
SMALL_BENCHMARK = "IWM"         # the Russell 2000
_START_EUR = {"stock": STOCK_START_EUR, "crypto": CRYPTO_START_EUR, "small": SMALL_START_EUR}
_BENCHMARK = {"stock": STOCK_BENCHMARK, "crypto": CRYPTO_BENCHMARK, "small": SMALL_BENCHMARK}
_VENUE_CURRENCY = {".OL": "NOK", ".ST": "SEK", ".DE": "EUR"}

# exit -> (days held, stop, take-profit, an insider from the signal selling counts)
EXITS = {
    "E1": (90, -0.15, None, True),
    "E2": (182, None, None, False),
    "E3": (91, -0.15, None, False),
    "E4": (90, -0.15, 0.25, True),
    "H1": (182, None, None, False),
    "H2": (91, -0.30, 0.50, False),
}


@dataclass(frozen=True)
class Book:
    code: str                   # R1-E1 … R2-E4, R1-E1-AN, C-A, C-B
    sleeve: str                 # stock | crypto | small
    buy: str | None = None      # R1 | R2
    exit: str | None = None     # E1 … E4
    analyst: bool = False       # the analyst-target shadow
    rule: str | None = None     # crypto: A (signals) | B (trend plus signals)

    @property
    def label(self) -> str:
        if self.sleeve in ("crypto", "small"):
            return self.code
        return f"{self.buy}·{self.exit}" + ("+аналитики" if self.analyst else "")


BOOKS = tuple(
    [Book(f"{r}-{e}", "stock", r, e) for r in ("R1", "R2") for e in ("E1", "E2", "E3", "E4")]
    + [Book("R1-E1-AN", "stock", "R1", "E1", analyst=True),
       Book("C-A", "crypto", rule="A"),
       Book("C-B", "crypto", rule="B"),
       Book("H1", "small", "H", "H1"),
       Book("H2", "small", "H", "H2")])
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


# ------------------------------------------------------------------ books
def create_books(conn, today: dt.date) -> None:
    """Every book, once -- the first run is a book's start date (a book added to the
    code later starts on the next run)."""
    for b in BOOKS:
        start, bench = _START_EUR[b.sleeve], _BENCHMARK[b.sleeve]
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


def history_days(fill_date: str, today: dt.date) -> int:
    """Days of history that reach back past a position's fill day: a position held
    longer than PRICE_DAYS (C-B has no time limit) would otherwise lose its entry close."""
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


# ------------------------------------------------------------ stock books
def stock_signals(selection, book: Book) -> list:
    """R1: the day's Сильный stock signals. R2: plus Кандидаты scoring R2_MIN_SCORE
    or more. A CRYPTO: ticker (a congressional crypto buy) never enters a stock book.
    The small-company books take only the day's high-risk signals."""
    if book.sleeve == "small":
        return [t.signal for t in getattr(selection, "high_risk", [])]
    sigs = [t.signal for t in selection.strong if not crypto.is_crypto(t.signal.ticker)]
    if book.buy == "R2":
        sigs += [t.signal for t in selection.candidates
                 if not crypto.is_crypto(t.signal.ticker)
                 and (getattr(t.signal, "score", 0) or 0) >= R2_MIN_SCORE]
    return sigs


def insiders_of(sig) -> list[str]:
    names = list(getattr(sig, "member_names", None) or [])
    person = getattr(sig, "person", None)
    return names or ([person] if person else [])


def _buy_reason(sig) -> str:
    tier = {"strong": "Сильный", "high_risk": "Высокий риск"}.get(getattr(sig, "tier", None), "Кандидат")
    return f"{tier}: {sig.source}, {getattr(sig, 'company', None) or sig.ticker}"


def _analyst_target(ticker: str, source: str | None) -> float | None:
    """The analysts' consensus target when the shadow book buys, or None. Network seam."""
    try:
        import research
        lst = listing(ticker, source)
        if lst is None:
            return None
        raw, _src = research._analyst_raw(assets.stock_asset(lst[0]))
        view = research.analyst_view(raw, positions.last_close(ticker, source)) if raw else None
        return view.get("target_mean") if view else None
    except Exception as e:
        print(f"[paper] no analyst target for {ticker}: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def stock_exit_reason(conn, book: Book, pos: dict, bars: list[tuple[str, float]],
                      today: dt.date) -> str | None:
    """The first exit that holds for this book's rules, or None."""
    hold, stop, take, insider = EXITS[book.exit]
    if insider:
        sale = positions._insider_sale(conn, types.SimpleNamespace(
            ticker=pos["ticker"], opened_at=pos["fill_date"],
            insiders=json.loads(pos["insiders"] or "[]")))
        if sale:
            return f"продаёт инсайдер: {sale}"
    if book.analyst and pos["target"] and bars and bars[-1][1] >= pos["target"]:
        return f"цель аналитиков {pos['target']:,.2f} достигнута"
    if (today - dt.date.fromisoformat(pos["fill_date"])).days >= hold:
        return f"{hold} дн. в позиции"
    if pos["last_value"] is None:
        return None
    ret = pos["last_value"] / pos["net_eur"] - 1
    if stop is not None and ret <= stop:
        return f"стоп {stop:+.0%}"
    if take is not None and ret >= take:
        return f"цель {take:+.0%}"
    return None


def stock_step(conn, book: Book, selection, prices: Prices, today: dt.date) -> None:
    """Sales for every exit that holds, then a buy per new signal: a tenth of the
    book's value, at most MAX_POSITIONS, half a slice at least."""
    for pos in open_positions(conn, book.code):
        reason = stock_exit_reason(conn, book, pos, prices.bars(pos["symbol"]), today)
        if reason:
            place_sell(conn, book.code, pos, reason, today)
    value = book_value(conn, book.code)
    slice_, max_positions = ((SMALL_SLICE, SMALL_MAX_POSITIONS) if book.sleeve == "small"
                             else (SLICE, MAX_POSITIONS))
    for sig in stock_signals(selection, book):
        target = None
        if book.analyst:    # two network calls: only for a ticker the book can still buy
            taken = ({p["ticker"] for p in open_positions(conn, book.code)}
                     | {o["ticker"] for o in pending_orders(conn, book.code)})
            if sig.ticker not in taken:
                target = _analyst_target(sig.ticker, sig.source)
        place_buy(conn, book.code, sig.ticker, sig.source, _buy_reason(sig), today, slice_ * value,
                  max_positions=max_positions, min_fraction=0.5, insiders=insiders_of(sig),
                  target=target)


# ----------------------------------------------------------- crypto books
def strong_coins(selection) -> set[str]:
    """Coins with a Сильный crypto signal today (CryptoSignals only -- congressional
    crypto buys are never Сильный)."""
    return {t.signal.coin for t in selection.strong if hasattr(t.signal, "crypto_kind")}


def above_trend(bars: list[tuple[str, float]]) -> bool | None:
    """Is the last close above the TREND_DAYS average? None with too little history."""
    if len(bars) < TREND_DAYS:
        return None
    closes = [c for _d, c in bars[-TREND_DAYS:]]
    return bars[-1][1] > sum(closes) / TREND_DAYS


def _recent_strong(conn, coin: str, today: dt.date) -> bool:
    since = (today - dt.timedelta(days=SIGNAL_HOLD_DAYS)).isoformat()
    return conn.execute(
        "SELECT 1 FROM signal_journal WHERE ticker = ? AND tier = 'strong' "
        "AND date(emitted_at) >= ? AND date(emitted_at) <= ? LIMIT 1",
        (crypto.ticker(coin), since, today.isoformat())).fetchone() is not None


def _rebuy_blocked(conn, code: str, coin: str, today: dt.date) -> bool:
    since = (today - dt.timedelta(days=REBUY_BLOCK_DAYS)).isoformat()
    return conn.execute(
        "SELECT 1 FROM paper_positions WHERE book = ? AND ticker = ? AND closed_date >= ? "
        "AND close_reason LIKE 'осторожно%' LIMIT 1",
        (code, crypto.ticker(coin), since)).fetchone() is not None


def _caution(conn, pos: dict, today: dt.date, trend_fn) -> str | None:
    detail = positions._crypto_caution(
        conn, types.SimpleNamespace(ticker=pos["ticker"], opened_at=pos["fill_date"]), today, trend_fn)
    return f"осторожно: {detail}" if detail else None


def crypto_step(conn, book: Book, selection, prices: Prices, today: dt.date, trend_fn) -> None:
    """C-A trades the signals: buy on Сильный, sell on a price-confirmed caution, at
    CRYPTO_HOLD_DAYS or at CRYPTO_STOP. C-B holds a coin above its 200-day average or
    for SIGNAL_HOLD_DAYS after a Сильный signal; a caution sells it and blocks a
    re-buy for REBUY_BLOCK_DAYS. Each coin's share is the book value / coin count."""
    signalled = strong_coins(selection)
    held = {p["ticker"]: p for p in open_positions(conn, book.code)}
    share = book_value(conn, book.code) / len(CRYPTO_COINS)
    for coin in CRYPTO_COINS:
        ticker = crypto.ticker(coin)
        pos = held.get(ticker)
        if book.rule == "A":
            if pos:
                reason = _caution(conn, pos, today, trend_fn)
                if not reason and (today - dt.date.fromisoformat(pos["fill_date"])).days >= CRYPTO_HOLD_DAYS:
                    reason = f"{CRYPTO_HOLD_DAYS} дн. в позиции"
                if (not reason and pos["last_value"] is not None
                        and pos["last_value"] / pos["net_eur"] - 1 <= CRYPTO_STOP):
                    reason = f"стоп {CRYPTO_STOP:+.0%}"
                if reason:
                    place_sell(conn, book.code, pos, reason, today)
            elif coin in signalled:
                place_buy(conn, book.code, ticker, "CRYPTO", "Сильный крипто-сигнал", today, share,
                          min_fraction=0.0)
            continue
        # None = too little history to tell: a held coin stays, and only a signal buys.
        trend = above_trend(prices.bars(listing(ticker, "CRYPTO")[0]))
        recent = coin in signalled or _recent_strong(conn, coin, today)
        if pos:
            caution = _caution(conn, pos, today, trend_fn)
            if caution:
                place_sell(conn, book.code, pos, caution, today)
            elif trend is False and not recent:
                place_sell(conn, book.code, pos, "ниже 200-дн. средней", today)
        elif (trend is True or recent) and not _rebuy_blocked(conn, book.code, coin, today):
            reason = "выше 200-дн. средней" if trend else "Сильный крипто-сигнал"
            place_buy(conn, book.code, ticker, "CRYPTO", reason, today, share, min_fraction=0.0)


# -------------------------------------------------------------- daily run
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


def run(conn, selection, today: dt.date | None = None, fetch=None, trend_fn=None) -> int:
    """One daily pass over every book: fill pending orders at the new close, revalue,
    decide sales and buys (filled at the next close), store the day. One failing book
    is logged and skipped. Returns how many books ran."""
    today = today or dt.date.today()
    trend_fn = trend_fn or crypto.price_trend
    create_books(conn, today)
    prices = Prices(fetch, today)
    ran = 0
    for book in BOOKS:
        try:
            fill_orders(conn, book.code, prices, today)
            mark_to_market(conn, book.code, prices, today)
            if book.sleeve in ("stock", "small"):
                stock_step(conn, book, selection, prices, today)
            else:
                crypto_step(conn, book, selection, prices, today, trend_fn)
            _snapshot(conn, book.code, prices, today)
            ran += 1
        except Exception as e:
            conn.rollback()
            print(f"[paper] {book.code} failed: {type(e).__name__}: {e}", file=sys.stderr)
    return ran


# -------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    """python paper.py         -- every book against its benchmark
       python paper.py R1-E2   -- one book's positions, trades and skips"""
    import argparse
    from pathlib import Path

    import paper_report
    ap = argparse.ArgumentParser(description="Бумажный портфель")
    ap.add_argument("book", nargs="?", help="код книги, например R1-E2 или C-A")
    args = ap.parse_args(argv)
    conn = db.connect(Path(__file__).parent / "data" / "disclosures.db")
    if not args.book:
        print(paper_report.format_summary(conn, dt.date.today()))
        return 0
    code = args.book.upper()
    if code not in BOOK_BY_CODE:
        print(f"Нет такой книги: {args.book}. Есть: {', '.join(BOOK_BY_CODE)}")
        return 2
    print(paper_report.format_book(conn, code))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
