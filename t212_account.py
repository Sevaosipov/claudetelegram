"""The Trading 212 account, read only: its positions and its summary (spec
docs/superpowers/specs/2026-10-01-trading212-account-tracking.md).

The bot READS the account and nothing else. It calls two GET endpoints of the Public API (v0),
POSITIONS_URL and SUMMARY_URL -- with trading212.py's instrument list, the only Trading 212 URLs
in the project (READ_URLS) -- and it can never place, change or cancel an order: there is no
code for it, and tests/test_t212_account.py checks the source of this module and of
trading212.py for any other call or URL. The key is trading212.py's (TRADING212_API_KEY /
TRADING212_API_SECRET in .env); a read-only key is enough (Portfolio and Account data).
Nothing here prints the key, the secret or the header made from them, not even in an error:
a failed call is a T212Error whose text is a reason for the user, and a network error is
named by its type only.

sync() keeps the bot's positions (positions.py) in step with the account: every holding is an
open position of origin 't212', so the exits (positions.check_exits) and /portfolio cover it. A
new holding is opened and announced, a changed quantity or average price is updated, a holding
that is gone is closed and announced, and each sync stores the day's price of every holding
(t212_prices) and the day's account snapshot (t212_equity). It runs every 15 minutes in
telegram_bot.py and once in the daily run (bot.py). All of a sync's changes are committed
together, and only after both reads succeeded: an API error changes nothing.

portfolio_view() is what /portfolio shows of the account: one live call (it stores the day's
prices and snapshot like a sync, but opens and closes nothing), or -- when the call fails --
the holdings as the last sync left them (stored_holdings), with the reason.
"""
from __future__ import annotations

import datetime as dt
import math
import sys
import time
from dataclasses import dataclass, field

import requests

import db
import model_score
import positions
import telegram_notify
import trading212

POSITIONS_URL = "https://live.trading212.com/api/v0/equity/positions"            # 1 call / 1 s
SUMMARY_URL = "https://live.trading212.com/api/v0/equity/account/summary"        # 1 call / 5 s
READ_URLS = (trading212.INSTRUMENTS_URL, POSITIONS_URL, SUMMARY_URL)  # every endpoint the bot calls
TIMEOUT_SECONDS = 20
RETRY_AFTER_SECONDS = 5                 # a 429 is retried once, after this long

NO_KEY = "ключ Trading 212 не задан"
BAD_KEY = "ключ Trading 212 не подходит"
NO_RIGHTS = "ключу Trading 212 не хватает прав: нужны чтение портфеля и счёта"
TOO_OFTEN = "Trading 212 просит реже: слишком много запросов (429)"
BAD_ANSWER = "ответ не разобран"
KEY_HINT = ("Создайте в Trading 212 → Настройки → API ключ только для чтения (Portfolio, Account data) "
            "и положите в .env")
SOLD_REASON = "продано в Trading 212"   # close_reason of a holding the account no longer has
SYNCED_ONCE_KEY = "t212_synced_once"    # kv: set by the first sync that got through
SYNCED_AT_KEY = "t212_synced_at"        # kv: when the last one did, epoch seconds
_KV_FOREVER = 100 * 365 * 86400         # these two never go stale

_sleep = time.sleep                     # the 429 retry's wait (tests replace it)


class T212Error(Exception):
    """A Trading 212 call that failed. str() is the reason as the user may read it -- never a URL,
    a header or a key. `kind`: no_key, unauthorized, forbidden, rate_limited, network or status
    (any other HTTP status)."""

    def __init__(self, reason: str, kind: str):
        super().__init__(reason)
        self.kind = kind

    @property
    def needs_key(self) -> bool:
        """The cure is a read-only key: none is set (no_key), or it lacks the rights (forbidden)."""
        return self.kind in ("no_key", "forbidden")


@dataclass
class T212Position:
    """One holding. avg_price and current_price are in the instrument's `currency`; value_eur,
    cost_eur and pnl_eur (walletImpact) in the account's, `account_currency`. Any field the
    payload lacks is None."""
    t212_ticker: str | None
    name: str | None
    isin: str | None
    currency: str | None
    quantity: float | None
    avg_price: float | None
    current_price: float | None
    created_at: str | None          # ISO datetime the position was opened
    value_eur: float | None
    cost_eur: float | None
    pnl_eur: float | None
    account_currency: str | None


@dataclass
class T212Summary:
    """The account, in its `currency`; a field the payload lacks is None."""
    currency: str | None
    total_value: float | None
    cash_free: float | None         # cash.availableToTrade
    invested_value: float | None    # investments.currentValue
    invested_cost: float | None     # investments.totalCost
    unrealized_pnl: float | None
    realized_pnl: float | None


# ------------------------------------------------------------------ the client
def _get(url: str, session=None):
    """GET `url` with the account's key: the JSON answer, or T212Error. A 429 is retried once
    after RETRY_AFTER_SECONDS. An answer that isn't JSON is a ValueError."""
    headers = trading212._auth_headers()
    if headers is None:
        raise T212Error(NO_KEY, "no_key")
    client = session or requests
    for attempt in (1, 2):
        failed = None
        try:
            resp = client.get(url, headers=headers, timeout=TIMEOUT_SECONDS)
        except Exception as e:      # requests' own errors, and whatever the layers under it raise
            failed = type(e).__name__
        if failed:
            # The type name only, raised out here rather than in the except: the error's own text
            # can carry the URL or even the header, and it must not survive as the context.
            raise T212Error(failed, "network")
        if resp.status_code == 429 and attempt == 1:
            _sleep(RETRY_AFTER_SECONDS)
            continue
        break
    status = resp.status_code
    if status == 401:
        raise T212Error(BAD_KEY, "unauthorized")
    if status == 403:
        raise T212Error(NO_RIGHTS, "forbidden")
    if status == 429:
        raise T212Error(TOO_OFTEN, "rate_limited")
    if not 200 <= status < 300:
        raise T212Error(f"HTTP {status}", "status")
    try:
        return resp.json()
    except ValueError:
        pass
    raise ValueError("Trading 212 answered with something that is not JSON")


def _obj(value) -> dict:
    return value if isinstance(value, dict) else {}


def _num(value) -> float | None:
    """A number of the payload, or None: missing, null, a bool, not a number, not finite."""
    if value is None or isinstance(value, bool):
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _text(value) -> str | None:
    return (value.strip() or None) if isinstance(value, str) else None


def parse_position(item) -> T212Position:
    item = _obj(item)
    inst, wallet = _obj(item.get("instrument")), _obj(item.get("walletImpact"))
    return T212Position(
        t212_ticker=_text(inst.get("ticker")), name=_text(inst.get("name")),
        isin=_text(inst.get("isin")), currency=_text(inst.get("currency")),
        quantity=_num(item.get("quantity")), avg_price=_num(item.get("averagePricePaid")),
        current_price=_num(item.get("currentPrice")), created_at=_text(item.get("createdAt")),
        value_eur=_num(wallet.get("currentValue")), cost_eur=_num(wallet.get("totalCost")),
        pnl_eur=_num(wallet.get("unrealizedProfitLoss")), account_currency=_text(wallet.get("currency")))


def parse_summary(data) -> T212Summary:
    if not isinstance(data, dict):
        raise ValueError(f"unexpected summary payload: {type(data).__name__}")
    cash, inv = _obj(data.get("cash")), _obj(data.get("investments"))
    return T212Summary(
        currency=_text(data.get("currency")), total_value=_num(data.get("totalValue")),
        cash_free=_num(cash.get("availableToTrade")), invested_value=_num(inv.get("currentValue")),
        invested_cost=_num(inv.get("totalCost")), unrealized_pnl=_num(inv.get("unrealizedProfitLoss")),
        realized_pnl=_num(inv.get("realizedProfitLoss")))


def fetch_positions(session=None) -> list[T212Position]:
    """The open positions (GET POSITIONS_URL). T212Error when the call fails; ValueError when the
    answer is not a list."""
    data = _get(POSITIONS_URL, session)
    if not isinstance(data, list):
        raise ValueError(f"unexpected positions payload: {type(data).__name__}")
    return [parse_position(item) for item in data]


def fetch_summary(session=None) -> T212Summary:
    """The account summary (GET SUMMARY_URL). T212Error when the call fails; ValueError when the
    answer is not an object."""
    return parse_summary(_get(SUMMARY_URL, session))


def fetch_account(session=None) -> tuple[list[T212Position], T212Summary]:
    """Both reads, positions first: what a sync and /portfolio ask for."""
    return fetch_positions(session), fetch_summary(session)


# ------------------------------------------------------------------ instrument -> position
def position_key(t212_ticker: str | None, isin: str | None) -> tuple[str, str | None] | None:
    """The bot's (ticker, source) for a Trading 212 instrument: a US one (AAPL_US_EQ, BRK_B_US_EQ)
    is its symbol (AAPL, BRK.B) with no source, priced from Yahoo like a /bought position; any
    other (Frankfurt, London, Amsterdam ...) is its ISIN with source positions.T212_SOURCE,
    priced from the day prices a sync stores. None when there is neither."""
    symbol = trading212._symbol({"ticker": t212_ticker or ""})
    if symbol:
        return symbol, None
    isin = (isin or "").strip().upper()
    if len(isin) == 12 and isin[:2].isalpha() and isin[2:].isalnum():
        return isin, positions.T212_SOURCE
    return None


# ------------------------------------------------------------------ the sync
_no_key_logged = False                  # the missing key is said once per process


@dataclass
class SyncResult:
    """What one sync did: the keys of the positions it opened, updated (a changed quantity or
    average price, or a /bought position taken over) and closed; `error` -- the reason -- when it
    changed nothing; `at`, when it ran."""
    opened: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    error: str | None = None
    at: dt.datetime | None = None


def last_sync(conn) -> dt.datetime | None:
    """When the last sync got through, or None before the first."""
    stamp = db.get_cached_value(conn, SYNCED_AT_KEY, _KV_FOREVER)
    return None if stamp is None else dt.datetime.fromtimestamp(stamp)


def _keyed(holdings: list[T212Position]) -> tuple[list[tuple[str, str | None, T212Position]], int]:
    """([(ticker, source, holding)], how many could not be keyed). The same key twice (one ISIN
    held on two exchanges) keeps the first."""
    keyed, seen, unkeyed = [], set(), 0
    for h in holdings:
        key = position_key(h.t212_ticker, h.isin)
        if key is None:
            unkeyed += 1
        elif key[0] not in seen:
            seen.add(key[0])
            keyed.append((key[0], key[1], h))
    return keyed, unkeyed


def _store_snapshot(conn, keyed, summary: T212Summary | None, day: str) -> None:
    """The day's price of every holding and the day's account snapshot: one row a day each, the
    last one of the day replacing the earlier ones. Not committed here."""
    for key, _source, h in keyed:
        if h.current_price is not None:
            conn.execute("INSERT OR REPLACE INTO t212_prices (ticker, date, price) VALUES (?,?,?)",
                         (key, day, h.current_price))
    if summary is not None and summary.total_value is not None:
        conn.execute(
            "INSERT OR REPLACE INTO t212_equity (date, total_value, invested_value, invested_cost, "
            "cash_free, currency) VALUES (?,?,?,?,?,?)",
            (day, summary.total_value, summary.invested_value, summary.invested_cost,
             summary.cash_free, summary.currency))


def _entry(h: T212Position) -> float | None:
    """The price a holding is entered at: its average price paid, else the price now."""
    for price in (h.avg_price, h.current_price):
        if price is not None and price > 0:
            return price
    return None


def _open_date(created_at: str | None, day: str) -> str:
    """The date a holding was opened (createdAt), `day` when it isn't told or lies ahead."""
    try:
        opened = dt.date.fromisoformat((created_at or "")[:10]).isoformat()
    except ValueError:
        return day
    return min(opened, day)


def _same(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is b
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)


def _open_holding(conn, key: str, source: str | None, h: T212Position, day: str, closes) -> bool:
    """A position of origin 't212' for a holding seen for the first time: entered at the average
    price paid, opened when Trading 212 says, watching the insiders of the journal's latest signal
    on it, the stop sized from the price history as /bought sizes it. `closes` is that history;
    None for a holding with no Yahoo listing, which has the day prices stored so far. False (and
    nothing stored) when there is no price to enter it at."""
    entry = _entry(h)
    if entry is None:
        return False
    signal = positions._buy_signal(conn, key, source)
    signal_id, members = signal if signal else (None, "[]")
    if closes is None:
        closes = positions.t212_closes(conn, key)
    stop = model_score.stop_distance([c for _d, c in closes], positions._kind(key))
    conn.execute(
        "INSERT INTO positions (ticker, source, opened_at, entry_price, insiders, signal_id, stop_pct, "
        "origin, quantity, t212_ticker, currency) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (key, source, _open_date(h.created_at, day), entry, members or "[]", signal_id, stop,
         positions.T212, h.quantity, h.t212_ticker, h.currency))
    return True


def _update_holding(conn, pos: positions.Position, h: T212Position) -> bool:
    """The account's quantity and average price on a holding already tracked (shares were added or
    trimmed). True when either changed."""
    quantity = pos.quantity if h.quantity is None else h.quantity
    entry = h.avg_price if h.avg_price is not None and h.avg_price > 0 else pos.entry_price
    changed = not (_same(quantity, pos.quantity) and _same(entry, pos.entry_price))
    t212_ticker, currency = h.t212_ticker or pos.t212_ticker, h.currency or pos.currency
    if changed or (t212_ticker, currency) != (pos.t212_ticker, pos.currency):
        conn.execute("UPDATE positions SET quantity = ?, entry_price = ?, t212_ticker = ?, currency = ? "
                     "WHERE id = ?", (quantity, entry, t212_ticker, currency, pos.id))
    return changed


def _take_over(conn, pos: positions.Position, source: str | None, h: T212Position) -> None:
    """A /bought position in a name the account holds becomes the account's: its quantity and
    average price are Trading 212's from now on. One keyed by its ISIN is priced from the day
    prices, so its source becomes T212_SOURCE; a US one keeps the source its signal gave it."""
    entry = _entry(h)
    conn.execute(
        "UPDATE positions SET origin = ?, quantity = ?, entry_price = ?, t212_ticker = ?, currency = ?, "
        "source = ? WHERE id = ?",
        (positions.T212, h.quantity, pos.entry_price if entry is None else entry, h.t212_ticker,
         h.currency, source if source == positions.T212_SOURCE else pos.source, pos.id))


def _new_text(name: str, h: T212Position) -> str:
    esc = telegram_notify._esc
    lot = []
    if h.quantity is not None:
        lot.append(f"{telegram_notify.quantity(h.quantity)} шт.")
    entry = _entry(h)
    if entry is not None:
        lot.append(f"по {telegram_notify._price(entry)}" + (f" {esc(h.currency)}" if h.currency else ""))
    what = (" — " + " ".join(lot)).rstrip(".") if lot else ""
    return f"📥 Вижу в Trading 212: {esc(name)}{what}. Слежу: стоп, продажи инсайдеров, новости."


def _sold_text(name: str) -> str:
    return f"📤 {telegram_notify._esc(name)} больше нет в Trading 212 — слежение закрыто."


def _first_text(names: list[str]) -> str:
    return (f"📥 Слежу за вашими позициями в Trading 212 ({len(names)}): "
            + ", ".join(telegram_notify._esc(n) for n in names))


def _apply(conn, holdings: list[T212Position], summary: T212Summary | None, now: dt.datetime,
           closes_fn, result: SyncResult) -> list[str]:
    """Bring the database in step with what the account holds, in one transaction the caller
    commits. Fills `result` and returns the messages to send once it is committed."""
    day = now.date().isoformat()
    keyed, unkeyed = _keyed(holdings)
    open_now = positions.open_positions(conn)
    tracked = {p.ticker: p for p in open_now if p.origin == positions.T212}
    manual = {p.ticker: p for p in open_now if p.origin != positions.T212}
    first = db.get_cached_value(conn, SYNCED_ONCE_KEY, _KV_FOREVER) is None
    # A US holding's history comes from Yahoo. It is asked for here, before the first write: the
    # database is not kept locked while Yahoo answers.
    closes_fn = closes_fn or positions.daily_closes
    histories = {key: closes_fn(key, source) for key, source, _h in keyed
                 if key not in tracked and key not in manual and source != positions.T212_SOURCE}

    _store_snapshot(conn, keyed, summary, day)
    new_texts, sold_texts, names = [], [], []
    for key, source, h in keyed:
        name = positions.name_of(key, source, h.t212_ticker)
        if key in tracked:
            if _update_holding(conn, tracked[key], h):
                result.updated.append(key)
        elif key in manual:
            _take_over(conn, manual[key], source, h)
            result.updated.append(key)
        elif _open_holding(conn, key, source, h, day, histories.get(key)):
            result.opened.append(key)
            new_texts.append(_new_text(name, h))
        else:
            print(f"[t212] {name}: no price to enter it at, not tracked yet", file=sys.stderr)
            continue
        names.append(name)

    if unkeyed:
        # It can't be matched to a position, so it may be one of them: nothing is called sold.
        print(f"[t212] {unkeyed} holding(s) with neither a US ticker nor an ISIN; "
              f"no position is closed this time", file=sys.stderr)
    else:
        held = {key for key, _source, _h in keyed}
        for key, pos in tracked.items():
            if key not in held:
                conn.execute("UPDATE positions SET closed_at = ?, close_reason = ? WHERE id = ?",
                             (day, SOLD_REASON, pos.id))
                result.closed.append(key)
                sold_texts.append(_sold_text(positions.display_name(pos)))

    if first:           # one message for everything the account holds, not one per holding
        new_texts = [_first_text(names)] if names else []
        db.save_cached_value(conn, SYNCED_ONCE_KEY, 1.0, commit=False)
    db.save_cached_value(conn, SYNCED_AT_KEY, now.timestamp(), commit=False)
    return new_texts + sold_texts


def _say_no_key() -> None:
    global _no_key_logged
    if not _no_key_logged:
        _no_key_logged = True
        print("[t212] Trading 212 key not set (TRADING212_API_KEY in .env): the account is not tracked")


def sync(conn, *, fetch=None, notify=None, now: dt.datetime | None = None, closes_fn=None) -> SyncResult:
    """One sync of the bot's positions with the account (see the top of the file).

    `fetch()` returns (positions, summary) -- fetch_account by default; `notify(text)` sends a
    message -- telegram_notify.send_text by default -- and is best effort: a failed send does not
    undo the sync; `now` is the sync's time (its date is the day of the stored price and account
    rows); `closes_fn(ticker, source)` is the price history a new US holding's stop is sized
    from, positions.daily_closes by default.

    With no key it is a quiet no-op, said once per process. A failed fetch -- T212Error, or a
    ValueError for an answer of the wrong shape -- changes nothing and sends nothing: the result
    carries the reason."""
    now = now or dt.datetime.now()
    result = SyncResult(at=now)
    try:
        holdings, summary = (fetch or fetch_account)()
    except T212Error as e:
        result.error = str(e)
        if e.kind == "no_key":
            _say_no_key()
        else:
            print(f"[t212] sync failed: {e}", file=sys.stderr)
        return result
    except ValueError:              # its text is not printed: only what this module wrote is safe
        result.error = BAD_ANSWER
        print(f"[t212] sync failed: {BAD_ANSWER}", file=sys.stderr)
        return result
    try:
        messages = _apply(conn, holdings, summary, now, closes_fn, result)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    notify = notify or telegram_notify.send_text
    for text in messages:
        try:
            notify(text)
        except Exception as e:
            print(f"[t212] notification not sent: {type(e).__name__}", file=sys.stderr)
    return result


# ------------------------------------------------------------------ what /portfolio shows
@dataclass
class Holding:
    """One line of «💼 Trading 212» (telegram_notify.t212_blocks): a holding by the name the user
    knows it by, its shares, average price and price now (in the instrument's `currency`) and the
    money it has made or lost (`pnl`, in `pnl_currency`). One the bot tracks comes with its
    position and positions.position_status at that price; `opened` (an ISO date) orders them."""
    name: str
    quantity: float | None
    avg_price: float | None
    price: float | None
    currency: str | None
    pnl: float | None = None
    pnl_currency: str | None = None
    position: positions.Position | None = None
    status: dict | None = None
    model_holds: bool = False
    opened: str = ""


@dataclass
class PortfolioView:
    """The account for /portfolio: the holdings and the live `summary`; or, when the live call
    failed, the stored holdings with why (`error`), the hint when the cure is a key (`hint`) and
    the time of the last sync the stored data are from (`as_of`: «14:05», or «30.09 14:05» on
    another day)."""
    holdings: list[Holding]
    summary: T212Summary | None = None
    error: str | None = None
    hint: str | None = None
    as_of: str | None = None


def _status(conn, pos: positions.Position, today: dt.date, price: float | None) -> dict:
    """positions.position_status at Trading 212's own price (the history is the exits')."""
    return positions.position_status(pos, today, price_fn=lambda ticker, source=None: price, conn=conn)


def _model_holds(conn, ticker: str, source: str | None, held: set) -> bool:
    """MODEL-S or MODEL-C holds the same name: the same key, or -- for a holding keyed by the ISIN
    of an Oslo listing -- that listing, which is how the model holds a Norwegian company."""
    if positions._asset_key(ticker, source) in held:
        return True
    oslo = positions.oslo_ticker(conn, ticker) if source == positions.T212_SOURCE else None
    return bool(oslo) and positions._asset_key(oslo, "NORWAY") in held


def stored_holdings(conn, today: dt.date) -> list[Holding]:
    """The holdings as the last sync left them, without a call to Trading 212: quantity and
    average from the positions, the price from the last stored day price, the profit or loss in
    the instrument's own currency (the account's is only known live)."""
    held = positions._model_names(conn)
    rows = []
    for pos in positions.open_positions(conn):
        if pos.origin != positions.T212:
            continue
        bars = positions.t212_closes(conn, pos.ticker)
        price = bars[-1][1] if bars else None
        known = price is not None and pos.quantity is not None and pos.currency
        rows.append(Holding(
            name=positions.display_name(pos), quantity=pos.quantity, avg_price=pos.entry_price,
            price=price, currency=pos.currency,
            pnl=(price - pos.entry_price) * pos.quantity if known else None,
            pnl_currency=pos.currency if known else None, position=pos,
            status=_status(conn, pos, today, price),
            model_holds=_model_holds(conn, pos.ticker, pos.source, held), opened=pos.opened_at))
    return rows


def _stored_view(conn, today: dt.date, now: dt.datetime, error: str, hint: str | None) -> PortfolioView:
    at = last_sync(conn)
    as_of = None if at is None else f"{at:%H:%M}" if at.date() == now.date() else f"{at:%d.%m %H:%M}"
    return PortfolioView(stored_holdings(conn, today), error=error, hint=hint, as_of=as_of)


def _untracked_name(h: T212Position) -> str:
    """A holding with neither a US ticker nor an ISIN, by whatever does name it."""
    return positions.name_of(h.t212_ticker or "", positions.T212_SOURCE, h.t212_ticker) or h.name or "?"


def portfolio_view(conn, today: dt.date, *, fetch=None, now: dt.datetime | None = None) -> PortfolioView:
    """«💼 Trading 212» of /portfolio: one live call for the positions and the summary (`fetch`,
    fetch_account by default). Its prices and account snapshot are stored like a sync's, but it is
    not a sync: a holding bought since the last one is shown without the bot's status lines, and
    nothing is opened or closed. When the call fails the view is the stored holdings, with the
    reason and their time."""
    now = now or dt.datetime.now()
    try:
        holdings, summary = (fetch or fetch_account)()
    except T212Error as e:
        return _stored_view(conn, today, now, str(e), KEY_HINT if e.needs_key else None)
    except ValueError:              # its text is not shown: only what this module wrote is safe
        return _stored_view(conn, today, now, BAD_ANSWER, None)
    try:
        _store_snapshot(conn, _keyed(holdings)[0], summary, now.date().isoformat())
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    tracked = {p.ticker: p for p in positions.open_positions(conn) if p.origin == positions.T212}
    held = positions._model_names(conn)
    rows = []
    for h in holdings:
        key = position_key(h.t212_ticker, h.isin)
        pos = tracked.get(key[0]) if key else None
        rows.append(Holding(
            name=positions.name_of(key[0], key[1], h.t212_ticker) if key else _untracked_name(h),
            quantity=h.quantity, avg_price=h.avg_price, price=h.current_price, currency=h.currency,
            pnl=h.pnl_eur, pnl_currency=h.account_currency or (summary.currency if summary else None),
            position=pos, status=_status(conn, pos, today, h.current_price) if pos else None,
            model_holds=bool(key) and _model_holds(conn, key[0], key[1], held),
            opened=pos.opened_at if pos else (h.created_at or "")[:10]))
    return PortfolioView(rows, summary=summary)
