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
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

import requests

import positions
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
KEY_HINT = ("Создайте в Trading 212 → Настройки → API ключ только для чтения (Portfolio, Account data) "
            "и положите в .env")

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
        except requests.RequestException as e:
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
