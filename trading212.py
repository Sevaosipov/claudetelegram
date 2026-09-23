"""Which signals name something that can actually be bought on Trading 212.

The instrument list comes from Trading 212's own public API
(GET /api/v0/equity/metadata/instruments) -- the only source for it: the website's
instrument pages sit behind a bot check (HTTP 403), and the API answers 401 without
credentials. So it needs the account holder's API key, created in the Trading 212 app
under Settings -> API (Beta), and put in .env:

    TRADING212_API_KEY=...
    TRADING212_API_SECRET=...

Only the key's metadata permission is used; nothing here reads the account, and
nothing can place an order. The endpoint allows one call per 50 seconds and the list
changes slowly, so it is cached in the database for a day. With no key, or with the
API down and nothing cached, availability is unknown -- callers say so rather than
treating every stock as unavailable.

Matching: an ISIN (how BaFin and Finansinspektionen signals name an issuer) against
the ISIN column; a US ticker against Trading 212's "<SYMBOL>_US_EQ" instruments; an
Oslo ticker against NOK-quoted instruments' short names. Crypto isn't sold on
Trading 212 Invest; crypto signals are not filtered here.
"""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path

import requests

import crypto
import db

INSTRUMENTS_URL = "https://live.trading212.com/api/v0/equity/metadata/instruments"
CACHE_TTL_SECONDS = 24 * 3600
BUYABLE_TYPES = {"STOCK", "ETF"}
US_SOURCES = {"SEC", "SEC13DG", "SEC144", "HOUSE", "SENATE"}
_CACHE_KEY = "t212_instruments_fetched"


@dataclass
class Availability:
    isins: set[str]
    us_symbols: set[str]
    nok_symbols: set[str]

    def can_buy(self, ticker: str, source: str) -> bool:
        t = (ticker or "").strip().upper()
        if not t:
            return False
        if crypto.is_crypto(t):
            return True
        if len(t) == 12 and t[:2].isalpha() and t[2:].isalnum():   # ISIN
            return t in self.isins
        if source == "NORWAY":
            return t in self.nok_symbols
        if source in US_SOURCES:
            return t in self.us_symbols or t.replace("-", ".") in self.us_symbols
        return t in self.us_symbols or t in self.nok_symbols


ENV_FILE = Path(__file__).parent / ".env"


def _setting(name: str) -> str:
    """From the environment, else from the project's .env -- the launchd jobs source
    .env, but `python menu.py` typed into a terminal doesn't."""
    value = os.environ.get(name)
    if value is None and ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            k, sep, v = line.strip().partition("=")
            if sep and k.strip().removeprefix("export ").strip() == name:
                value = v.strip().strip('"').strip("'")
    return (value or "").strip()


def _auth_headers() -> dict | None:
    key = _setting("TRADING212_API_KEY")
    secret = _setting("TRADING212_API_SECRET")
    if not key:
        return None
    if secret:
        token = base64.b64encode(f"{key}:{secret}".encode()).decode()
        return {"Authorization": f"Basic {token}"}
    # Keys issued before Trading 212 added secrets go in the header as-is.
    return {"Authorization": key}


def fetch_instruments(session: requests.Session | None = None) -> list[dict] | None:
    headers = _auth_headers()
    if headers is None:
        return None
    resp = (session or requests).get(INSTRUMENTS_URL, headers=headers, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise ValueError(f"unexpected instruments payload: {str(data)[:200]}")
    return data


def _symbol(inst: dict) -> str:
    """AAPL_US_EQ -> AAPL; BRK_B_US_EQ -> BRK.B."""
    t = inst.get("ticker") or ""
    return t[: -len("_US_EQ")].replace("_", ".").upper() if t.endswith("_US_EQ") else ""


def _save(conn, instruments: list[dict]) -> None:
    conn.execute("DELETE FROM t212_instruments")
    conn.executemany(
        "INSERT OR REPLACE INTO t212_instruments (ticker, isin, type, short_name, currency) "
        "VALUES (?,?,?,?,?)",
        [(i.get("ticker"), i.get("isin"), i.get("type"), i.get("shortName"), i.get("currencyCode"))
         for i in instruments if i.get("ticker")],
    )
    db.save_cached_value(conn, _CACHE_KEY, 1.0)


def availability(conn, session: requests.Session | None = None) -> Availability | None:
    """The cached instrument list, refreshed when older than a day. None when it has
    never been fetched and can't be now (no key, or the API refused)."""
    fresh = db.get_cached_value(conn, _CACHE_KEY, CACHE_TTL_SECONDS) is not None
    if not fresh:
        try:
            instruments = fetch_instruments(session)
            if instruments:
                _save(conn, instruments)
        except (requests.RequestException, ValueError) as e:
            print(f"[T212] instrument list not refreshed ({e}); using the cached one if any")
    rows = conn.execute(
        "SELECT ticker, isin, type, short_name, currency FROM t212_instruments").fetchall()
    if not rows:
        return None
    isins, us, nok = set(), set(), set()
    for ticker, isin, typ, short, currency in rows:
        if typ not in BUYABLE_TYPES:
            continue
        if isin:
            isins.add(isin.upper())
        sym = _symbol({"ticker": ticker})
        if sym:
            us.add(sym)
            if short:
                us.add(short.upper())
        elif currency == "NOK" and short:
            nok.add(short.upper())
    return Availability(isins=isins, us_symbols=us, nok_symbols=nok)
