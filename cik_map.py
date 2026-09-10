"""CIK -> ticker lookup across every SEC-registered company.

Form 144 and Schedule 13D/G identify the issuer by CIK (and, for 13D/G, CUSIP) --
never by ticker. Form 4 is the odd one out in carrying issuerTradingSymbol directly.
Without a mapping, filings from those two forms can't be grouped with Form 4 rows
for the same company, which is the entire point of collecting them.

universe.py already downloads SEC's authoritative company_tickers.json to build the
S&P 100 / Nasdaq-100 CIK set, but keeps only that filtered set and throws the
mapping away. This caches the whole thing instead, in the same style: a JSON file
under data/, refreshed weekly, with a stale cache preferred over no data.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

import universe

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
CACHE_PATH = Path(__file__).parent / "data" / "cik_ticker_cache.json"
CACHE_MAX_AGE = 7 * 24 * 3600  # tickers change rarely; a week-old map is fine


def _fetch_live() -> dict:
    resp = requests.get(SEC_TICKERS_URL, headers=universe.sec_headers(), timeout=30)
    resp.raise_for_status()
    # Keys are arbitrary row indices; the useful part is cik_str -> ticker. A CIK
    # can have several tickers (share classes); first one wins, which matches how
    # the rest of the project treats a company as one symbol.
    mapping: dict[str, str] = {}
    for row in resp.json().values():
        cik = str(int(row["cik_str"]))
        mapping.setdefault(cik, row["ticker"].upper())
    return {"fetched_at": time.time(), "map": mapping}


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
    except (requests.RequestException, ValueError, KeyError):
        if CACHE_PATH.exists():
            return json.loads(CACHE_PATH.read_text())  # stale map beats no map
        raise
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(data))
    return data


class CikMap:
    def __init__(self, force_refresh: bool = False):
        self._map = load(force_refresh=force_refresh)["map"]
        # Reverse direction, for research.py: given a ticker, which company's
        # filings should be pulled. Built once here rather than scanned per lookup.
        self._by_ticker = {t.upper(): cik for cik, t in self._map.items()}

    def ticker(self, cik: str | int | None) -> str | None:
        """Ticker for a CIK, or None. Accepts the zero-padded form SEC uses in XML
        ("0001018840") as well as a plain integer."""
        if cik in (None, ""):
            return None
        try:
            return self._map.get(str(int(str(cik).strip())))
        except ValueError:
            return None

    def cik(self, ticker: str | None) -> str | None:
        """CIK for a ticker, or None. Share classes are written inconsistently
        across sources (BRK.B vs BRK-B), so both spellings are tried."""
        if not ticker:
            return None
        t = ticker.strip().upper()
        for variant in (t, t.replace(".", "-"), t.replace("-", ".")):
            if variant in self._by_ticker:
                return self._by_ticker[variant]
        return None

    def __len__(self) -> int:
        return len(self._map)
