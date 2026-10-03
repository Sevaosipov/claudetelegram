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

What was in the account before the bot first looked is "legacy" (the sync that stores the first
holding ever: _open_holding): its rules count from that day, not from the purchase, and its stop
is measured from its price that day -- so connecting an old account brings no burst of close
alerts. Its entry stays Trading 212's average price, so the result shown is the real one. A
message names only what is really watched for its holding (_watched), and the one-time first
message is left for a sync that can send it: a silent one (--sync, --no-telegram) does not use
it up.

portfolio_view() is what /portfolio shows of the account: one live call (it stores the day's
prices and snapshot like a sync, but opens and closes nothing), or -- when the call fails --
the holdings as the last sync left them (stored_holdings), with the reason.

Usage:
    python t212_account.py --check    # does the key read the account? prints the number of
                                      # positions and the currency, how they are keyed and priced
                                      # and whether the list adds up -- or why not; nothing else
    python t212_account.py --sync     # one sync now, without the Telegram messages
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

import db
import model_score
import positions
import telegram_notify
import trading212

DB_PATH = Path(__file__).parent / "data" / "disclosures.db"
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
EMPTY_LIST = "пустой список позиций при вложенных средствах"
LIST_DISAGREES = "список позиций не сходится со сводкой счёта"
SALE_UNCONFIRMED = "нет суммы вложений в сводке: продажу подтвердит следующая синхронизация"
UNKEYED_HOLDING = "есть бумага без тикера США и без ISIN: продажи не разбираются"
LIST_TOLERANCE, LIST_TOLERANCE_MONEY = 0.02, 5.0   # how far the list may be from the summary: 2 % or €5
SOLD_REASON = "продано в Trading 212"   # close_reason of a holding the account no longer has
SYNCED_ONCE_KEY = "t212_synced_once"    # kv: set by the first sync that got through
SYNCED_AT_KEY = "t212_synced_at"        # kv: when the last one did, epoch seconds
MISSING_KEY = "t212_missing"            # kv (JSON): ids of the positions missing at the last sync
                                        # that had no summary to check the list against
WARNED_KEY = "t212_silent_{day}"        # kv: the day's «не отвечает уже сутки» warning went out
SILENT_AFTER = dt.timedelta(hours=24)   # no sync has got through for this long: say so, once a day
STALE_DAYS = positions.T212_STALE_DAYS  # an account snapshot or a day price older than this is not current
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
YAHOO_TOLERANCE = 0.20          # how far Yahoo's last close may be from Trading 212's price


def _isin_key(isin: str | None) -> str | None:
    isin = (isin or "").strip().upper()
    return isin if len(isin) == 12 and isin[:2].isalpha() and isin[2:].isalnum() else None


def _market_symbol(conn, t212_ticker: str | None) -> str:
    """The market symbol of a US instrument, "" for any other. Trading 212's _US_EQ codes are old
    ones for about one instrument in five (FB_US_EQ is Meta, PCLN_US_EQ Booking, UTX_US_EQ RTX):
    the cached instrument list (t212_instruments.short_name) has the symbol it trades under. With
    no row there -- or no `conn` -- the code itself is the symbol (trading212._symbol)."""
    code = trading212._symbol({"ticker": t212_ticker or ""})
    if not code or conn is None:
        return code
    row = conn.execute("SELECT short_name FROM t212_instruments WHERE ticker = ?", (t212_ticker,)).fetchone()
    return (row[0] or "").strip().upper() if row and (row[0] or "").strip() else code


def position_key(t212_ticker: str | None, isin: str | None, conn=None) -> tuple[str, str | None] | None:
    """The bot's (ticker, source) for a Trading 212 instrument: a US one (AAPL_US_EQ, BRK_B_US_EQ)
    is its market symbol (AAPL, BRK.B; _market_symbol) with no source, priced from Yahoo like a
    /bought position; any other (Frankfurt, London, Amsterdam ...) is its ISIN with source
    positions.T212_SOURCE, priced from the day prices a sync stores. None when there is neither.

    This is the key by the instrument alone. A sync still checks, when it opens a US holding,
    that Yahoo's series is not another instrument's (_yahoo_is_its_own), and keys it by its ISIN
    if it is."""
    symbol = _market_symbol(conn, t212_ticker)
    if symbol:
        return symbol, None
    isin = _isin_key(isin)
    return (isin, positions.T212_SOURCE) if isin else None


def _yahoo_is_its_own(closes, price: float | None, day: str) -> bool:
    """Whether a US holding may be keyed by its symbol, going by Yahoo's history under it. False
    only when Yahoo has a series and it is another instrument's: its last completed close (one
    before `day`) is more than YAHOO_TOLERANCE from Trading 212's own price -- Yahoo would price
    the holding wrong for the stop.

    No history at all is not a verdict: Yahoo may be down, or not know a symbol the instrument
    list vouches for. The symbol stands (the holding keeps its insiders and its news), and its
    stop is priced from the sync's own day prices (positions._pricing). With no price from
    Trading 212 there is nothing to compare either."""
    completed = [c for d, c in closes or [] if d < day and c and c > 0]
    if not completed or price is None or price <= 0:
        return True
    return abs(completed[-1] / price - 1) <= YAHOO_TOLERANCE


# ------------------------------------------------------------------ the sync
_no_key_logged = False                  # the missing key is said once per process


@dataclass
class SyncResult:
    """What one sync did: the keys of the positions it opened, updated (a changed quantity or
    average price, a /bought position taken over, a returning holding restored) and closed;
    `error` -- the reason -- when it changed nothing; `at`, when it ran.

    `held` are the keys of the positions the list did not show and that were NOT closed this
    time, and `note` says why: the list did not add up to the summary, or there was no summary
    value to check it against and the sale waits for the next sync. `error_kind` tells a key
    problem (unauthorized, forbidden: asking again soon is no use) from the rest."""
    opened: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    error: str | None = None
    at: dt.datetime | None = None
    held: list[str] = field(default_factory=list)
    note: str | None = None
    error_kind: str | None = None       # T212Error.kind of the failure, or "answer" for a bad answer


def last_sync(conn) -> dt.datetime | None:
    """When the last sync got through, or None before the first."""
    stamp = db.get_cached_value(conn, SYNCED_AT_KEY, _KV_FOREVER)
    return None if stamp is None else dt.datetime.fromtimestamp(stamp)


def _store_price(conn, ticker: str, day: str, price: float | None) -> None:
    """The day's price of a holding, under its position's ticker: one row a day, the last one of
    the day replacing the earlier ones. Not committed here."""
    if price is not None and price > 0:         # a price of zero is no price: it must not drive a stop
        conn.execute("INSERT OR REPLACE INTO t212_prices (ticker, date, price) VALUES (?,?,?)",
                     (ticker, day, price))


def _store_equity(conn, summary: T212Summary | None, day: str) -> None:
    """The day's account snapshot: one row a day, the last of the day. Not committed here."""
    if summary is not None and summary.total_value is not None:
        conn.execute(
            "INSERT OR REPLACE INTO t212_equity (date, total_value, invested_value, invested_cost, "
            "cash_free, currency) VALUES (?,?,?,?,?,?)",
            (day, summary.total_value, summary.invested_value, summary.invested_cost,
             summary.cash_free, summary.currency))


def _store_snapshot(conn, priced: list[tuple[str, T212Position]], summary: T212Summary | None,
                    day: str) -> None:
    """The day's price of each (ticker, holding) and the day's account snapshot (/portfolio's live
    call keeps them like a sync does). Not committed here."""
    for ticker, h in priced:
        _store_price(conn, ticker, day, h.current_price)
    _store_equity(conn, summary, day)


def _entry(h: T212Position) -> float | None:
    """The price a holding is entered at: its average price paid, else the price now."""
    for price in (h.avg_price, h.current_price):
        if price is not None and price > 0:
            return price
    return None


def _created_date(created_at: str | None) -> str | None:
    """The date Trading 212 says a holding was bought (createdAt), or None when it isn't told."""
    try:
        return dt.date.fromisoformat((created_at or "")[:10]).isoformat()
    except ValueError:
        return None


def _open_date(created_at: str | None, day: str) -> str:
    """The date a holding was opened (createdAt), `day` when it isn't told or lies ahead."""
    return min(_created_date(created_at) or day, day)


def _same(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is b
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)


def _open_holding(conn, key: str, source: str | None, h: T212Position, day: str, closes,
                  legacy: bool) -> list[str] | None:
    """A position of origin 't212' for a holding seen for the first time: entered at the average
    price paid, watching the insiders of the journal's latest signal on it, the stop sized from
    the price history as /bought sizes it. `closes` is Yahoo's history; when there is none (a
    holding keyed by its ISIN, a symbol Yahoo has nothing for, a history not fetched) the day
    prices stored so far are the history. Returns the insiders it watches; None (and nothing stored) when there is no price to
    enter it at.

    Opened when Trading 212 says it was bought -- unless it is `legacy`, a holding that was in
    the account before the bot first looked: then its clock starts today (`day`), so the year and
    the dead-money rule count from the tracking start, and its stop_base is its price now (the
    last close of the history when Trading 212 gives none), so the stop is measured from here and
    not from an average price it may be far below. The entry is the average price either way:
    the result shown is the real one."""
    entry = _entry(h)
    if entry is None:
        return None
    signal = positions._buy_signal(conn, key, source)
    signal_id, members = signal if signal else (None, "[]")
    if not closes:                  # no Yahoo history (an ISIN, an unknown symbol, an outage)
        closes = positions.t212_closes(conn, key)
    stop = model_score.stop_distance([c for _d, c in closes], positions._kind(key))
    base = None
    if legacy:
        priced = h.current_price is not None and h.current_price > 0
        base = h.current_price if priced else (closes[-1][1] if closes else None)
    conn.execute(
        "INSERT INTO positions (ticker, source, opened_at, entry_price, insiders, signal_id, stop_pct, "
        "origin, quantity, t212_ticker, currency, stop_base, t212_created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (key, source, day if legacy else _open_date(h.created_at, day), entry, members or "[]", signal_id,
         stop, positions.T212, h.quantity, h.t212_ticker, h.currency, base, _created_date(h.created_at)))
    try:
        names = json.loads(members or "[]")
    except ValueError:
        names = []
    return [str(n) for n in names] if isinstance(names, list) else []


def _update_holding(conn, pos: positions.Position, h: T212Position) -> bool:
    """The account's quantity and average price on a holding already tracked (shares were added or
    trimmed). True when either changed."""
    quantity = pos.quantity if h.quantity is None else h.quantity
    entry = h.avg_price if h.avg_price is not None and h.avg_price > 0 else pos.entry_price
    changed = not (_same(quantity, pos.quantity) and _same(entry, pos.entry_price))
    told = (h.t212_ticker or pos.t212_ticker, h.currency or pos.currency,
            pos.t212_created or _created_date(h.created_at))       # what was not known before
    if changed or told != (pos.t212_ticker, pos.currency, pos.t212_created):
        conn.execute("UPDATE positions SET quantity = ?, entry_price = ?, t212_ticker = ?, currency = ?, "
                     "t212_created = ? WHERE id = ?", (quantity, entry, *told, pos.id))
    return changed


def _take_over(conn, pos: positions.Position, source: str | None, h: T212Position) -> None:
    """A /bought position in a name the account holds -- the same listing: _same_listing --
    becomes the account's: its quantity and average price are Trading 212's from now on. One keyed by its ISIN is priced from the day
    prices, so its source becomes T212_SOURCE; a US one keeps the source its signal gave it. Its
    clock is not touched: the bot has watched it since the /bought."""
    entry = _entry(h)
    conn.execute(
        "UPDATE positions SET origin = ?, quantity = ?, entry_price = ?, t212_ticker = ?, currency = ?, "
        "source = ?, t212_created = ? WHERE id = ?",
        (positions.T212, h.quantity, pos.entry_price if entry is None else entry, h.t212_ticker,
         h.currency, source if source == positions.T212_SOURCE else pos.source,
         _created_date(h.created_at), pos.id))


def _watched(conn, key: str, source: str | None, insiders: list[str]) -> str:
    """What the bot really watches for a holding, as its message says it. A US holding: its stop,
    its headlines and -- when the journal named some -- its insiders' sales (else its time rules). A holding keyed by its ISIN has no news feed of its own:
    its stop and its time rules -- its insiders' sales only when the journal named some (BaFin
    and FI by the ISIN, Oslo by the listing's ticker), and headlines only through an Oslo
    listing."""
    if source != positions.T212_SOURCE:
        return "стоп, продажи инсайдеров, новости" if insiders else "стоп, срок и новости"
    more = (["продажи инсайдеров"] if insiders else []) \
        + (["новости"] if positions.oslo_ticker(conn, key) else [])
    return "стоп и срок" + (", а также " + " и ".join(more) if more else "")


def _new_text(name: str, h: T212Position, watched: str) -> str:
    esc = telegram_notify._esc
    lot = []
    if h.quantity is not None:
        lot.append(f"{telegram_notify.quantity(h.quantity)} шт.")
    entry = _entry(h)
    if entry is not None:
        lot.append(f"по {telegram_notify._price(entry)}" + (f" {esc(h.currency)}" if h.currency else ""))
    what = (" — " + " ".join(lot)).rstrip(".") if lot else ""
    return f"📥 Вижу в Trading 212: {esc(name)}{what}. Слежу: {watched}."


def _sold_text(name: str) -> str:
    return f"📤 {telegram_notify._esc(name)} больше нет в Trading 212 — слежение закрыто."


def _first_text(names: list[str], legacy_since: str | None, day: str) -> str:
    """The one-time message for everything the account holds. It claims nothing about what is
    watched for each holding; it says when the rules of the holdings that pre-date tracking began
    to count (`legacy_since`, an ISO date): today, or the day a silent sync first stored them."""
    text = (f"📥 Слежу за вашими позициями в Trading 212 ({len(names)}): "
            + ", ".join(telegram_notify._esc(n) for n in names))
    if legacy_since is None:
        return text
    since = "сегодняшнего дня" if legacy_since == day else f"{dt.date.fromisoformat(legacy_since):%d.%m}"
    return f"{text}\nДля уже купленных бумаг правила выхода считаются с {since}."


def _invested(summary: T212Summary | None) -> float | None:
    return None if summary is None else summary.invested_value


def _list_gap(holdings: list[T212Position], summary: T212Summary | None) -> float | None:
    """How far the positions list is from the summary of the same sync: the holdings' summed value
    less the value the summary says is invested (both in the account's currency). None when it
    can't be told: the summary has no such value, or a holding has no value of its own."""
    invested = _invested(summary)
    values = [h.value_eur for h in holdings]
    if invested is None or any(v is None for v in values):
        return None
    return sum(values) - invested


def _list_agrees(holdings: list[T212Position], summary: T212Summary | None) -> bool | None:
    """Whether the list accounts for the money invested, within LIST_TOLERANCE (or
    LIST_TOLERANCE_MONEY, for a small account): only then is a holding that is not on it believed
    sold. None when it can't be told (_list_gap)."""
    gap = _list_gap(holdings, summary)
    if gap is None:
        return None
    return abs(gap) <= max(LIST_TOLERANCE * abs(_invested(summary)), LIST_TOLERANCE_MONEY)


def _close_missing(conn, missing: list[positions.Position], holdings: list[T212Position],
                   summary: T212Summary | None, unkeyed: int, day: str, result: SyncResult) -> list[str]:
    """Close the tracked positions the list did not show -- when the list can be believed. Returns
    the «📤» messages of those closed; the others go to result.held with result.note.

      the list adds up to the summary     every missing one was sold: closed;
      it does not add up                  the list is short, not the account: none is closed;
      it can't be checked                 a missing one is closed only when it was missing at the
                                          sync before too (MISSING_KEY keeps who was);
      a holding that can't be keyed       it may be one of the missing: none is closed.
    """
    agrees = _list_agrees(holdings, summary)
    waiting = set(db.get_cached_json(conn, MISSING_KEY) or [])
    if unkeyed:
        sold, reason = [], UNKEYED_HOLDING
        print(f"[t212] {unkeyed} holding(s) with neither a US ticker nor an ISIN; "
              f"no position is closed this time", file=sys.stderr)
    elif agrees is False:
        sold, reason = [], LIST_DISAGREES
        gap, invested = _list_gap(holdings, summary), abs(_invested(summary))
        how_far = f"by {abs(gap) / invested:.0%}" if invested else "though nothing is invested"
        print(f"[t212] the positions list and the account summary disagree {how_far}: "
              f"no position is closed this time", file=sys.stderr)
    elif agrees is None:
        sold, reason = [p for p in missing if p.id in waiting], SALE_UNCONFIRMED
    else:
        sold, reason = missing, None
    texts = []
    for pos in sold:
        conn.execute("UPDATE positions SET closed_at = ?, close_reason = ? WHERE id = ?",
                     (day, SOLD_REASON, pos.id))
        result.closed.append(pos.ticker)
        texts.append(_sold_text(positions.display_name(pos)))
    closed = {p.id for p in sold}
    kept = [p for p in missing if p.id not in closed]
    if kept:
        result.held, result.note = [p.ticker for p in kept], reason
    # Who is waiting for a second sync: after a list that could not be checked, everyone it missed
    # and did not close; after one that did not add up, whoever was waiting and is still missing
    # (that sync is no evidence either way); after one that adds up, nobody.
    if agrees is None and not unkeyed:
        pending = [p.id for p in kept]
    elif agrees is True and not unkeyed:
        pending = []
    else:
        pending = [p.id for p in kept if p.id in waiting]
    db.save_cached_value(conn, MISSING_KEY, json.dumps(pending), commit=False)
    return texts


def _restore(conn, h: T212Position, in_use: set[str]) -> positions.Position | None:
    """A holding that is back: the closed position of the very same holding -- the same Trading 212
    instrument, bought on the same day -- is reopened, not opened anew. It keeps its clock
    (opened_at), its stop (stop_base, stop_pct), its insiders and the alert it has already raised,
    so a holding that pre-dates tracking does not come back as one bought years ago. None when
    there is no such row, the purchase date is not known (nothing then tells the same holding from
    a new purchase), or its ticker is in use by an open position."""
    created = _created_date(h.created_at)
    if not h.t212_ticker or created is None:
        return None
    row = conn.execute(
        "SELECT id, ticker FROM positions WHERE origin = ? AND closed_at IS NOT NULL AND t212_ticker = ? "
        "AND t212_created = ? ORDER BY id DESC LIMIT 1", (positions.T212, h.t212_ticker, created)).fetchone()
    if row is None or row[1] in in_use:
        return None
    conn.execute("UPDATE positions SET closed_at = NULL, close_reason = NULL WHERE id = ?", (row[0],))
    return next(p for p in positions.open_positions(conn) if p.id == row[0])


@dataclass
class _Plan:
    """What a sync settled about one listed holding before it took the lock: the ticker and source
    a position for it would be opened under (None: it can't be keyed), and -- for a US holding the
    bot does not track yet -- Yahoo's history, which also sizes its stop."""
    holding: T212Position
    key: str | None = None
    source: str | None = None
    closes: list | None = None


def _same_listing(bought: positions.Position, source: str | None) -> bool:
    """Whether a /bought position under a holding's key is that holding's own listing, so that it
    can be taken over. A holding keyed by its ISIN is that security whatever source named the
    position. A US holding is not a position bought on Oslo or Stockholm under the same letters
    (EQNR in New York is not EQNR.OL)."""
    return source == positions.T212_SOURCE or bought.source not in positions._VENUE_SOURCES


def _plan(conn, h: T212Position, tracked_ids: set, bought: dict, closes_fn, day: str) -> _Plan:
    """The key of a holding. A US one the bot does not track yet has Yahoo asked for its history
    (here, before the lock: the database is not kept locked while Yahoo answers), and is keyed by
    its symbol unless Yahoo's series is another instrument's (_yahoo_is_its_own): then by its
    ISIN, priced from the sync's own day prices. No history from Yahoo leaves it its symbol. A holding that is tracked already, or goes to an open /bought
    position of the same listing, is not put to Yahoo: its position has its key. One whose symbol
    is a position bought on another exchange is keyed by its ISIN, beside that position."""
    key = position_key(h.t212_ticker, h.isin, conn)
    if key is None:
        return _Plan(h)
    ticker, source = key
    if source == positions.T212_SOURCE or h.t212_ticker in tracked_ids:
        return _Plan(h, ticker, source)
    if ticker in bought:
        isin = None if _same_listing(bought[ticker], source) else _isin_key(h.isin)
        return _Plan(h, isin, positions.T212_SOURCE) if isin else _Plan(h, ticker, source)
    closes = closes_fn(ticker, None)
    if not _yahoo_is_its_own(closes, h.current_price, day):
        isin = _isin_key(h.isin)
        if isin is not None:
            return _Plan(h, isin, positions.T212_SOURCE)
    return _Plan(h, ticker, None, closes)


def _apply(conn, holdings: list[T212Position], summary: T212Summary | None, now: dt.datetime,
           closes_fn, result: SyncResult, silent: bool) -> list[str]:
    """Bring the database in step with what the account holds, in one transaction the caller
    commits. Fills `result` and returns the messages to send once it is committed (none when
    `silent`).

    A tracked position is found on the list by the Trading 212 id stored with it (t212_ticker),
    never by a key derived again: the key of an instrument can change (its code gets a new market
    symbol, Yahoo starts or stops knowing it), and the holding must stay one position. Only a
    position stored without an id is found by its key."""
    day = now.date().isoformat()
    closes_fn = closes_fn or positions.daily_closes
    first_look = positions.open_positions(conn)
    plans = [_plan(conn, h, {p.t212_ticker for p in first_look if p.origin == positions.T212},
                   {p.ticker: p for p in first_look if p.origin != positions.T212}, closes_fn, day)
             for h in holdings]

    # From here the database is this sync's: the Telegram bot and the daily run are two processes,
    # and what is open is read again under the lock, so a holding the other one has just opened
    # is not opened twice. (It then has no Yahoo history: its stop is sized later.)
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    open_now = positions.open_positions(conn)
    tracked = [p for p in open_now if p.origin == positions.T212]
    by_id = {p.t212_ticker: p for p in tracked if p.t212_ticker}
    by_key = {p.ticker: p for p in tracked}
    manual = {p.ticker: p for p in open_now if p.origin != positions.T212}
    first = db.get_cached_value(conn, SYNCED_ONCE_KEY, _KV_FOREVER) is None
    # No holding was ever stored (open or since sold): what the account holds now was there before
    # the bot looked. This is about the positions, not about the message flag above.
    legacy = conn.execute("SELECT 1 FROM positions WHERE origin = ? LIMIT 1",
                          (positions.T212,)).fetchone() is None

    _store_equity(conn, summary, day)
    listed: set[int] = set()            # the tracked positions the list shows
    taken = set(by_key)                 # tickers that have their position: one position a ticker
    new_texts, names, legacy_days, unkeyed = [], [], [], 0
    for plan in plans:
        h = plan.holding
        pos = by_id.get(h.t212_ticker) if h.t212_ticker else None
        if pos is None and plan.key in by_key and not by_key[plan.key].t212_ticker:
            pos = by_key[plan.key]      # stored without its Trading 212 id: its key is all there is
        if pos is not None:
            if pos.id in listed:
                continue
            listed.add(pos.id)
            _store_price(conn, pos.ticker, day, h.current_price)
            if _update_holding(conn, pos, h):
                result.updated.append(pos.ticker)
            if pos.stop_base is not None:
                legacy_days.append(pos.opened_at)
            names.append(positions.name_of(pos.ticker, pos.source, h.t212_ticker or pos.t212_ticker))
            continue
        if plan.key is None:
            unkeyed += 1
            continue
        key, source = plan.key, plan.source
        if key in taken:                # a second listing of a name that has its position
            continue                    # (one ISIN held on two exchanges): the first one is it
        name = positions.name_of(key, source, h.t212_ticker)
        if key in manual and not _same_listing(manual[key], source):
            # Another listing was /bought under these letters, and there is no ISIN to track this
            # one by beside it: it is left alone (and is no reason to hold back a sale).
            print(f"[t212] {name}: a position bought on another exchange has this ticker; "
                  f"not tracked", file=sys.stderr)
            continue
        if key in manual:
            _store_price(conn, key, day, h.current_price)
            _take_over(conn, manual[key], source, h)
            result.updated.append(key)
        elif (back := _restore(conn, h, taken | set(manual))) is not None:
            key, name = back.ticker, positions.display_name(back)      # as its own row has it
            _store_price(conn, key, day, h.current_price)
            _update_holding(conn, back, h)
            result.updated.append(key)
            if back.stop_base is not None:
                legacy_days.append(back.opened_at)
        else:
            _store_price(conn, key, day, h.current_price)
            insiders = _open_holding(conn, key, source, h, day, plan.closes, legacy)
            if insiders is None:
                print(f"[t212] {name}: no price to enter it at, not tracked yet", file=sys.stderr)
                continue
            result.opened.append(key)
            new_texts.append(_new_text(name, h, _watched(conn, key, source, insiders)))
            if legacy:
                legacy_days.append(day)
        taken.add(key)
        names.append(name)

    missing = [pos for pos in tracked if pos.id not in listed]
    sold_texts = _close_missing(conn, missing, holdings, summary, unkeyed, day, result)

    db.save_cached_value(conn, SYNCED_AT_KEY, now.timestamp(), commit=False)
    if silent:          # nothing is said, and the one-time first message is left for a sync that can
        return []
    if first:           # one message for everything the account holds, not one per holding
        new_texts = []
        if names:       # the flag is this message's: an account that holds nothing has not used it
            new_texts = [_first_text(names, min(legacy_days, default=None), day)]
            db.save_cached_value(conn, SYNCED_ONCE_KEY, 1.0, commit=False)
    return new_texts + sold_texts


def _warn_when_silent_for_a_day(conn, reason: str, notify, now: dt.datetime) -> None:
    """A sync has got through before, and none has for SILENT_AFTER: the positions are not being
    updated, and the owner is told -- once a day (the day is marked in kv once the message went
    out, so one that did not go out is tried again at the next sync)."""
    last = last_sync(conn)
    if last is None or now - last < SILENT_AFTER:
        return
    key = WARNED_KEY.format(day=now.date().isoformat())
    if db.get_cached_value(conn, key, _KV_FOREVER) is not None:
        return
    text = (f"⚠️ Trading 212 не отвечает уже сутки ({telegram_notify._esc(reason)}): "
            f"позиции не обновляются.")
    try:
        sent = (notify or telegram_notify.send_text)(text)
    except Exception as e:
        print(f"[t212] notification not sent: {type(e).__name__}", file=sys.stderr)
        return
    if sent:
        db.save_cached_value(conn, key, 1.0)


def _failed(conn, result: SyncResult, reason: str, kind: str, notify, silent: bool) -> SyncResult:
    """A sync that changed nothing: the result carries why, the log says it (a missing key once
    per process), and after a day without a sync that got through the owner is told."""
    result.error, result.error_kind = reason, kind
    if kind == "no_key":
        _say_no_key()
    else:
        print(f"[t212] sync failed: {reason}", file=sys.stderr)
    if not silent:
        _warn_when_silent_for_a_day(conn, reason, notify, result.at)
    return result


def _say_no_key() -> None:
    global _no_key_logged
    if not _no_key_logged:
        _no_key_logged = True
        print("[t212] Trading 212 key not set (TRADING212_API_KEY in .env): the account is not tracked")


def sync(conn, *, fetch=None, notify=None, now: dt.datetime | None = None, closes_fn=None,
         silent: bool = False) -> SyncResult:
    """One sync of the bot's positions with the account (see the top of the file).

    `fetch()` returns (positions, summary) -- fetch_account by default; `notify(text)` sends a
    message -- telegram_notify.send_text by default -- and is best effort: a failed send does not
    undo the sync; `now` is the sync's time (its date is the day of the stored price and account
    rows); `closes_fn(ticker, source)` is the price history a new US holding's stop is sized
    from, positions.daily_closes by default.

    `silent` is a sync with the notifications off (`--sync`, a --no-telegram run): it stores
    everything and sends nothing, and it does not use up the one-time first message -- the flag
    SYNCED_ONCE_KEY is set only by a sync that sent that message, or tried to. The first sync
    that may notify then lists every holding the bot tracks.

    With no key it is a quiet no-op, said once per process. A failed fetch -- T212Error, or a
    ValueError for an answer of the wrong shape -- changes nothing: the result carries the
    reason. So does an empty list while the summary says money is invested. A failure is not
    announced -- until a sync has got through before and none has for a day: then the owner is
    told once a day (_warn_when_silent_for_a_day; never by a silent sync).

    A holding that is not on the list is closed only when the list can be believed
    (_close_missing): result.held and result.note say what was not closed and why."""
    now = now or dt.datetime.now()
    result = SyncResult(at=now)
    try:
        holdings, summary = (fetch or fetch_account)()
    except T212Error as e:
        return _failed(conn, result, str(e), e.kind, notify, silent)
    except ValueError:              # its text is not printed: only what this module wrote is safe
        return _failed(conn, result, BAD_ANSWER, "answer", notify, silent)
    if not holdings and (_invested(summary) or 0) > 0:
        # Money is invested and no position is listed: the list is wrong, not the account empty.
        # Believing it would call every holding sold.
        return _failed(conn, result, EMPTY_LIST, "answer", notify, silent)
    try:
        messages = _apply(conn, holdings, summary, now, closes_fn, result, silent)
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


def account_value(conn, day: dt.date, *, max_age_days: int | None = None) -> tuple[float, str | None] | None:
    """(the account's value, its currency) from the last snapshot stored on or before `day`, or
    None when there is none that early -- or, with `max_age_days`, when that snapshot is older
    than that before `day`: the sync has not got through, and an old value is not the account's."""
    row = conn.execute("SELECT date, total_value, currency FROM t212_equity WHERE date <= ? "
                       "AND total_value IS NOT NULL ORDER BY date DESC LIMIT 1", (day.isoformat(),)).fetchone()
    if row is None:
        return None
    if max_age_days is not None and (day - dt.date.fromisoformat(row[0])).days > max_age_days:
        return None
    return row[1], row[2]


def stored_account_line(conn) -> str | None:
    """The latest account snapshot as one line of plain text, «Счёт Trading 212 на 01.10: €X ·
    вложено €Y · P/L ±€Z (±W%) · свободно €C» (the profit or loss is the holdings' value less
    their cost), or None before the first one."""
    row = conn.execute("SELECT date, total_value, invested_value, invested_cost, cash_free, currency "
                       "FROM t212_equity WHERE total_value IS NOT NULL ORDER BY date DESC LIMIT 1").fetchone()
    if row is None:
        return None
    day, total, value, cost, cash, currency = row
    pnl = value - cost if value is not None and cost is not None else None
    figures = telegram_notify.format_t212_account(total, cost, pnl, cash, currency).removeprefix("Счёт: ")
    return f"Счёт Trading 212 на {dt.date.fromisoformat(day):%d.%m}: {figures}"


# ------------------------------------------------------------------ what /portfolio shows
@dataclass
class Holding:
    """One line of «💼 Trading 212» (telegram_notify.t212_blocks): a holding by the name the user
    knows it by, its shares, average price and price now (in the instrument's `currency`) and the
    money it has made or lost (`pnl`, in `pnl_currency`). One the bot tracks comes with its
    position and positions.position_status at that price.

    These are the owner's own figures, whatever the bot's rules count from: the result is on the
    average price paid, and `days` is since Trading 212's purchase date (None when it isn't
    known) -- for a holding that pre-dates tracking, not since the tracking start. `opened` (an
    ISO date: the purchase date, else the tracking start) orders the holdings.

    `price_date` is set only when the price is a stored one that is stale (positions.t212_stale):
    it is then shown as the price of that day, and the status has no price -- no stop is read
    from it."""
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
    days: int | None = None
    price_date: str | None = None       # the day of a stored price too old to be one («цена на DD.MM»)


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


def _days_held(created: str | None, today: dt.date) -> int | None:
    """Days since Trading 212's purchase date, or None when it isn't known."""
    try:
        return max((today - dt.date.fromisoformat(created)).days, 0) if created else None
    except ValueError:
        return None


def stored_holdings(conn, today: dt.date) -> list[Holding]:
    """The holdings as the last sync left them, without a call to Trading 212: quantity and
    average from the positions, the price from the last stored day price, the profit or loss in
    the instrument's own currency (the account's is only known live). A stored price older than
    STALE_DAYS is not a price: it is shown with its date, and the status has none."""
    held = positions._model_names(conn)
    rows = []
    for pos in positions.open_positions(conn):
        if pos.origin != positions.T212:
            continue
        bars = positions.t212_closes(conn, pos.ticker)
        price_day, price = bars[-1] if bars else (None, None)
        stale = price_day is not None and positions.t212_stale(price_day, today)
        known = price is not None and pos.quantity is not None and pos.currency
        rows.append(Holding(
            name=positions.display_name(pos), quantity=pos.quantity, avg_price=pos.entry_price,
            price=price, currency=pos.currency,
            pnl=(price - pos.entry_price) * pos.quantity if known else None,
            pnl_currency=pos.currency if known else None, position=pos,
            status=_status(conn, pos, today, None if stale else price),
            model_holds=_model_holds(conn, pos.ticker, pos.source, held),
            opened=pos.t212_created or pos.opened_at, days=_days_held(pos.t212_created, today),
            price_date=price_day if stale else None))
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
    tracked = [p for p in positions.open_positions(conn) if p.origin == positions.T212]
    by_id = {p.t212_ticker: p for p in tracked if p.t212_ticker}
    by_key = {p.ticker: p for p in tracked if not p.t212_ticker}
    found = []                      # (holding, its key by the instrument, its position or None)
    for h in holdings:
        key = position_key(h.t212_ticker, h.isin, conn)
        pos = by_id.get(h.t212_ticker) if h.t212_ticker else None
        if pos is None and key is not None:
            pos = by_key.get(key[0])
        found.append((h, key, pos))
    # The day's price goes under the position's own ticker; a holding not tracked yet has one only
    # when its key is certain (its ISIN): a US one is keyed by the sync, after it asked Yahoo.
    priced = [(pos.ticker if pos else key[0], h) for h, key, pos in found
              if pos is not None or (key is not None and key[1] == positions.T212_SOURCE)]
    try:
        _store_snapshot(conn, priced, summary, now.date().isoformat())
        conn.commit()
    except sqlite3.Error as e:      # the database is busy (the daily run): still worth showing
        conn.rollback()
        print(f"[t212] the day's prices were not stored: {type(e).__name__}", file=sys.stderr)
    except BaseException:
        conn.rollback()
        raise
    held = positions._model_names(conn)
    rows = []
    for h, key, pos in found:
        created = _created_date(h.created_at) or (pos.t212_created if pos else None)
        if pos is not None:
            name, where = positions.display_name(pos), (pos.ticker, pos.source)
        else:
            name = positions.name_of(key[0], key[1], h.t212_ticker) if key else _untracked_name(h)
            where = key
        price = h.current_price if h.current_price is not None and h.current_price > 0 else None
        rows.append(Holding(
            name=name, quantity=h.quantity, avg_price=h.avg_price, price=price,
            currency=h.currency, pnl=h.pnl_eur,
            pnl_currency=h.account_currency or (summary.currency if summary else None),
            position=pos, status=_status(conn, pos, today, price) if pos else None,
            model_holds=bool(where) and _model_holds(conn, where[0], where[1], held),
            opened=created or (pos.opened_at if pos else ""), days=_days_held(created, today)))
    return PortfolioView(rows, summary=summary)


# ------------------------------------------------------------------ command line
def check(fetch=None) -> tuple[bool, str]:
    """Whether the key reads the account, and what to print: the number of positions and the
    account's currency, then how the holdings are keyed and priced and whether the list adds up
    to the summary (_check_details) -- nothing that identifies the account, and never the key --
    or why it can't (with the hint when the cure is a read-only key)."""
    try:
        holdings, summary = (fetch or fetch_account)()
    except T212Error as e:
        return False, f"Trading 212: доступа нет — {e}" + (f"\n{KEY_HINT}" if e.needs_key else "")
    except ValueError:              # its text is not printed: only what this module wrote is safe
        return False, f"Trading 212: доступа нет — {BAD_ANSWER}"
    return True, (f"Trading 212: доступ есть, позиций {len(holdings)}, "
                  f"валюта {summary.currency or 'не указана'}\n{_check_details(holdings, summary)}")


def _check_details(holdings: list[T212Position], summary: T212Summary) -> str:
    """The second line of --check: how the holdings would be keyed (a US ticker, an ISIN, neither),
    how many have a price the bot can use, and whether the list adds up to the summary -- the one
    thing a sale depends on. Counts and a percentage: nothing that identifies the account."""
    keys = [position_key(h.t212_ticker, h.isin) for h in holdings]
    us = sum(1 for k in keys if k is not None and k[1] is None)
    isin = sum(1 for k in keys if k is not None and k[1] == positions.T212_SOURCE)
    none = len(keys) - us - isin
    priced = sum(1 for h in holdings if h.current_price is not None and h.current_price > 0)
    agrees = _list_agrees(holdings, summary)
    if agrees is None:
        adds_up = "список и сводку не сверить"
    elif agrees:
        adds_up = "список и сводка сходятся"
    else:
        invested = abs(_invested(summary))
        gap = f" на {abs(_list_gap(holdings, summary)) / invested:.0%}" if invested else ""
        adds_up = f"список и сводка расходятся{gap}"
    return (f"Ключи: США — {us}, ISIN — {isin}" + (f", без ключа — {none}" if none else "")
            + f"; с ценой — {priced} из {len(holdings)}; {adds_up}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="t212_account.py",
                                 description="The Trading 212 account, read only: check the key or sync once.")
    what = ap.add_mutually_exclusive_group(required=True)
    what.add_argument("--check", action="store_true",
                      help="does the key read the account: the number of positions and the currency")
    what.add_argument("--sync", action="store_true", help="one sync now, without the Telegram messages")
    args = ap.parse_args(argv)
    if args.check:
        ok, line = check()
        print(line)
        return 0 if ok else 1
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(DB_PATH)
    try:
        result = sync(conn, silent=True)
    finally:
        conn.close()
    if result.error:
        print(f"Trading 212: синхронизация не прошла — {result.error}"
              + (f"\n{KEY_HINT}" if result.error in (NO_KEY, NO_RIGHTS) else ""))
        return 1
    print(f"Trading 212: синхронизация прошла — открыто {len(result.opened)}, "
          f"обновлено {len(result.updated)}, закрыто {len(result.closed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
