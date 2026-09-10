"""Target company universe: S&P 100 union Nasdaq-100, cached locally and refreshed
weekly. Membership rarely changes, so a stale cache is fine for a day or two.

Ticker lists come from Wikipedia (community-maintained, matches the official index
methodology closely enough for a filtering heuristic, not for index-fund purposes).
CIKs -- needed to match SEC filings precisely, since tickers can collide or change --
come from SEC's own ticker map (company_tickers.json), which is the authoritative
source and needs no special access beyond a descriptive User-Agent.
"""
from __future__ import annotations

import json
import re
import time
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

SP100_URL = "https://en.wikipedia.org/wiki/S%26P_100"
NASDAQ100_URL = "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

CACHE_PATH = Path(__file__).parent / "data" / "universe_cache.json"
CACHE_MAX_AGE = 7 * 24 * 3600  # refresh weekly

WIKI_HEADERS = {"User-Agent": "Mozilla/5.0 disclosure-bot (research; contact@example.com)"}


def sec_headers() -> dict:
    """SEC fair-access User-Agent. Public because cik_map.py fetches from the same
    host and must send the same contact string."""
    import os
    ua = os.environ.get("SEC_USER_AGENT", "disclosure-bot research contact@example.com")
    return {"User-Agent": ua}


def _extract_tickers(html: str) -> set[str]:
    """Find the constituents table (by column name, not position -- Wikipedia
    reorders tables over time) and pull its ticker/symbol column."""
    tables = pd.read_html(StringIO(html))
    for t in tables:
        col = next((c for c in t.columns if re.search(r"^(ticker|symbol)", str(c), re.I)), None)
        if col is not None and len(t) >= 50:  # both indexes have ~100 rows
            return {str(v).strip().upper() for v in t[col].dropna()}
    raise ValueError("could not find a ticker/symbol column with >=50 rows")


def _fetch_live() -> dict:
    tickers = set()
    for url in (SP100_URL, NASDAQ100_URL):
        resp = requests.get(url, headers=WIKI_HEADERS, timeout=30)
        resp.raise_for_status()
        tickers |= _extract_tickers(resp.text)

    resp = requests.get(SEC_TICKERS_URL, headers=sec_headers(), timeout=30)
    resp.raise_for_status()
    ticker_to_cik = {row["ticker"].upper(): row["cik_str"] for row in resp.json().values()}

    ciks = sorted({ticker_to_cik[t] for t in tickers if t in ticker_to_cik})
    return {"fetched_at": time.time(), "tickers": sorted(tickers), "ciks": ciks}


def load(force_refresh: bool = False) -> dict:
    if not force_refresh and CACHE_PATH.exists():
        try:
            cached = json.loads(CACHE_PATH.read_text())
            if time.time() - cached.get("fetched_at", 0) < CACHE_MAX_AGE:
                return cached
        except (json.JSONDecodeError, OSError):
            pass
    try:
        data = _fetch_live()
    except (requests.RequestException, ValueError):
        if CACHE_PATH.exists():
            return json.loads(CACHE_PATH.read_text())  # stale cache beats no data
        raise
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(data))
    return data


def _ticker_variants(ticker: str) -> set[str]:
    # Class-share tickers are written inconsistently across sources (BRK.B vs BRK-B).
    t = ticker.strip().upper()
    return {t, t.replace(".", "-"), t.replace("-", ".")}


class Universe:
    """S&P 100 union Nasdaq-100."""

    def __init__(self, force_refresh: bool = False):
        data = load(force_refresh=force_refresh)
        self.ciks = set(data["ciks"])
        self.tickers = set()
        for t in data["tickers"]:
            self.tickers |= _ticker_variants(t)

    def has_cik(self, cik: str | int | None) -> bool:
        if not cik:
            return False
        try:
            return int(str(cik).lstrip("0") or "0") in self.ciks
        except ValueError:
            return False

    def has_ticker(self, ticker: str | None) -> bool:
        if not ticker:
            return False
        return bool(_ticker_variants(ticker) & self.tickers)
