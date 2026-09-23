"""What the user typed -> which asset: one resolver for Telegram, the menu and the
research CLI (spec docs/superpowers/specs/2026-09-23-any-asset-lookup-design.md §1).

Rules, in order:
  1. an ISIN (how BaFin/Finansinspektionen name issuers) -> that issuer, as a stock;
  2. "$XYZ" -> always a stock ("$BTC" is the Grayscale ETF, not bitcoin);
  3. "CRYPTO:XYZ" or "XYZ-USD" -> a coin;
  4. a symbol with an exchange suffix (EQNR.OL, VOLV-B.ST, SAP.DE) -> that listing;
  5. a symbol in the coin list -> a coin -- crypto wins a clash, so "SOL" is Solana;
  6. anything else shaped like a ticker -> a US stock or ETF.
Only the first word counts ("aapl buy now" is AAPL), as it always has in Telegram.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

import crypto

EXCHANGE_SUFFIXES = (".OL", ".ST", ".DE", ".CO", ".HE", ".PA", ".AS", ".L", ".SW", ".MI", ".MC")
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")
_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


@dataclass(frozen=True)
class Asset:
    kind: str                    # "stock" | "crypto"
    symbol: str                  # BTC, NVDA, EQNR.OL, BRK.B, or an ISIN
    yahoo: str | None            # BTC-USD, NVDA, EQNR.OL, BRK-B; None for an ISIN
    tradingview: str | None      # CRYPTO:BTCUSD for coins; None = tradingview.py resolves it
    exchange: str | None = None  # ".OL" etc. for a non-US listing
    is_isin: bool = False

    @property
    def key(self) -> str:
        """Text that resolves back to this same asset -- what the analysis queue stores."""
        if self.kind == "crypto":
            return crypto.ticker(self.symbol)
        if self.is_isin or self.exchange:
            return self.symbol
        return "$" + self.symbol

    @property
    def signal_ticker(self) -> str:
        """How the bot's own signal tables key this asset (CRYPTO:BTC for coins)."""
        return crypto.ticker(self.symbol) if self.kind == "crypto" else self.symbol


def crypto_asset(symbol: str) -> Asset:
    s = symbol.upper()
    return Asset("crypto", s, f"{s}-USD", f"CRYPTO:{s}USD")


def stock_asset(symbol: str) -> Asset:
    s = symbol.upper()
    exchange = next((x for x in EXCHANGE_SUFFIXES if s.endswith(x)), None)
    return Asset("stock", s, s if exchange else s.replace(".", "-"), None, exchange)


def resolve(text: str, coins: set[str] | Callable[[], set[str]] | None = None) -> Asset | None:
    words = (text or "").strip().upper().split()
    if not words:
        return None
    t = words[0]
    if _ISIN_RE.match(t):
        return Asset("stock", t, None, None, None, True)
    if t.startswith("$"):
        return stock_asset(t[1:]) if _SYMBOL_RE.match(t[1:]) else None
    if t.startswith("CRYPTO:"):
        sym = t[len("CRYPTO:"):]
        return crypto_asset(sym) if _SYMBOL_RE.match(sym) and "." not in sym else None
    if t.endswith("-USD") and len(t) > 4:
        sym = t[:-4]
        return crypto_asset(sym) if _SYMBOL_RE.match(sym) and "." not in sym else None
    if not _SYMBOL_RE.match(t):
        return None
    if t.endswith(EXCHANGE_SUFFIXES):
        return stock_asset(t)
    known = coins() if callable(coins) else (coins if coins is not None else crypto.SYMBOLS)
    if t in known:
        return crypto_asset(t)
    return stock_asset(t)
