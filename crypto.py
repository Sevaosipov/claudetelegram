"""Shared crypto plumbing: coin names, the CRYPTO:<SYM> ticker convention, and prices.

Every crypto row in this project is keyed by a ticker of the form "CRYPTO:BTC". The
prefix is deliberate: "BTC" alone is also a US-listed ETF (the Grayscale Bitcoin Mini
Trust), and "ETH" was Ethan Allen's ticker for decades -- a bare symbol would let a
politician's bitcoin purchase corroborate, or cluster with, an unrelated equity. With
the prefix, a congressional BTC buy, a company's BTC treasury purchase and a spot-ETF
inflow all share one key, so cluster.find_corroboration links them without any
special-casing, and nothing equity-shaped (marketcap, research) ever mistakes one for
a stock.
"""
from __future__ import annotations

import re

import requests

import db

PREFIX = "CRYPTO:"

# Name fragments as they appear in House PTR asset text and in 8-K prose -> symbol.
# Checked in order, longest names first, so "bitcoin cash" is not read as bitcoin.
ALIASES = (
    ("bitcoin cash", "BCH"),
    ("ethereum classic", "ETC"),
    ("bitcoin", "BTC"),
    ("ethereum", "ETH"),
    ("ether", "ETH"),
    ("solana", "SOL"),
    ("cardano", "ADA"),
    ("dogecoin", "DOGE"),
    ("litecoin", "LTC"),
    ("ripple", "XRP"),
    ("chainlink", "LINK"),
    ("avalanche", "AVAX"),
    ("polkadot", "DOT"),
)
SYMBOLS = {"BTC", "ETH", "SOL", "ADA", "DOGE", "LTC", "XRP", "LINK", "AVAX", "DOT",
           "BCH", "ETC"}
COINGECKO_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "ADA": "cardano",
    "DOGE": "dogecoin", "LTC": "litecoin", "XRP": "ripple", "LINK": "chainlink",
    "AVAX": "avalanche-2", "DOT": "polkadot", "BCH": "bitcoin-cash",
    "ETC": "ethereum-classic",
}
PRICE_TTL_SECONDS = 3600
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"

_SYMBOL_RE = re.compile(r"\b(?:CRYPTO:)?([A-Z]{2,5})\b")


def ticker(symbol: str) -> str:
    return PREFIX + symbol.upper()


def is_crypto(t: str | None) -> bool:
    return bool(t) and t.upper().startswith(PREFIX)


def symbol_of(t: str) -> str:
    return t[len(PREFIX):] if is_crypto(t) else t


def symbol_for_text(text: str | None) -> str | None:
    """The coin a free-text asset name refers to, or None.

    House filers write the same holding every way there is: "Bitcoin [CT]",
    "BTC [CT]", "Bitcoin (BTC) [CT]", "Bitcoin (CRYPTO:BTC) [CT]".
    """
    if not text:
        return None
    low = text.lower()
    for name, sym in ALIASES:
        if re.search(rf"\b{name}\b", low):
            return sym
    for m in _SYMBOL_RE.finditer(text.upper()):
        if m.group(1) in SYMBOLS:
            return m.group(1)
    return None


def yf_symbol(t: str) -> str:
    """The Yahoo Finance symbol for a ticker this project stores. Crypto quotes as
    BTC-USD; share classes as BRK-B rather than the BRK.B the filings use."""
    if is_crypto(t):
        return f"{symbol_of(t)}-USD"
    return t.replace(".", "-")


def price_usd(conn, symbol: str, session: requests.Session | None = None) -> float | None:
    """Spot USD price from CoinGecko's free endpoint, cached for an hour. None when
    it can't be had -- callers treat an unknown value as unknown, not as zero."""
    symbol = symbol.upper()
    key = f"crypto_price_usd_{symbol}"
    if conn is not None:
        cached = db.get_cached_value(conn, key, PRICE_TTL_SECONDS)
        if cached:
            return cached
    cg_id = COINGECKO_IDS.get(symbol)
    if not cg_id:
        return None
    try:
        resp = (session or requests).get(COINGECKO_URL, params={"ids": cg_id, "vs_currencies": "usd"},
                                          timeout=20)
        resp.raise_for_status()
        price = float(resp.json()[cg_id]["usd"])
    except (requests.RequestException, KeyError, TypeError, ValueError):
        return None
    if conn is not None:
        db.save_cached_value(conn, key, price)
    return price
