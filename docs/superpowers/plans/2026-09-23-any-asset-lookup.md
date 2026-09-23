# Any-Asset Lookup with a One-Month Outlook Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Any ticker (US/European stock, ETF, crypto coin) typed in Telegram or the
terminal returns a dossier plus a historical-analogue "up in a month" outlook, with
every kind of data fetched through a chain of fallback sources.

**Architecture:**
- **`assets.py`** turns text into an `Asset`.
- **`sources.py`** holds ordered chains of free keyless providers (prices, indicators,
  analyst targets, news, coin list).
- **`outlook.py`** keeps daily closes in `price_bars` and builds 18-situation
  frequency tables with a walk-forward check. A lookup places the asset in a
  situation and reads the table.
- **`research.build(conn, text)`** resolves the text and dispatches: stocks go to the
  existing dossier (now with the outlook and fallbacks), crypto to the new
  `crypto_research.py`.
- **Telegram, the Claude analysis prompt and the menu** all go through
  `research.build` / `research.format_brief`.

**Tech Stack:** Python 3.12, SQLite (`db.py`), pandas (rolling windows), yfinance,
requests, stdlib `xml.etree`. Run tests with
`.venv/bin/python -m pytest -q -p no:cacheprovider`. The suite is offline; the new
tests stub every source.

**Spec:** `docs/superpowers/specs/2026-09-23-any-asset-lookup-design.md`

## Global Constraints

- **Outlook horizon:** 21 trading days (stocks) / 30 days (crypto). "Up" means the
  price itself is higher.
- **Situation:**
  - trend `up` (close > SMA50 > SMA200), `down` (close < SMA50 < SMA200), or `mixed`;
  - move vs σ_m = std(last 252 daily returns) × √h: `strong_down` < −σ_m <
    `flat` < +σ_m < `strong_up`;
  - volatility `high` when the 21-day return std exceeds the 75th percentile of its
    rolling values over the last 252 observations, else `normal`.

  This gives 18 keys like `up|flat|normal`. At least 273 observations are required.
- **TradingView fallback:** `close`, `SMA50`, `SMA200`, `Perf.1M`, with σ_m ≈
  `Volatility.M`/100 × √h. Volatility becomes `*` (pooled over both states).
- **Tables:** `stock` (the universe.py S&P 100 ∪ Nasdaq-100 list) and `crypto` (top
  50 coins by market cap minus stablecoins, ≥ 730 days of history). One observation
  per asset every h observations (no overlap).
- **Walk-forward:** folds from the table's 5th year to the last complete year. Edge
  = oos_n ≥ 50 and out-of-sample Brier with the situation rate < Brier with the base
  rate.
- **Output wording:**
  - `📈 Прогноз на месяц: рост в 61% похожих ситуаций (n = 4 210) · обычно 56% · проверено на истории`;
  - no edge → `📈 Прогноз на месяц: нет преимущества над базовой частотой (обычно 56%)`;
  - always a line ending `частота в прошлом, не гарантия`;
  - an own-asset line only when the asset has ≥ 30 own cases.
- **Resolver order:** ISIN → `$X` stock → `CRYPTO:X` / `X-USD` coin → exchange suffix
  (`.OL .ST .DE .CO .HE .PA .AS .L .SW .MI .MC`) stock → coin list → US stock. Crypto
  wins clashes.
- **Source chains** (first that answers wins; show the source only when it isn't the
  first):
  - stock history: Yahoo → Nasdaq (US only);
  - crypto history: Yahoo → Binance → Bybit → Kraken;
  - current price: Yahoo → TradingView → Nasdaq (US) / CoinGecko → Binance (crypto);
  - indicators: TradingView → local calculation;
  - analyst targets: Yahoo → Nasdaq (US);
  - news: Yahoo → Google News → (crypto) CoinDesk + Cointelegraph;
  - coin list: CoinGecko → CoinPaprika → the built-in list.
- **When a whole chain fails:** that section says `недоступно сейчас`; the rest of
  the lookup still arrives.
- **Style:** code and comments in English; user-facing text in Russian, matching the
  existing style. Commits end with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

---

### Task 1: `assets.py`, resolving what the user typed

**Files:**
- Create: `assets.py`
- Test: `tests/test_assets.py`

**Interfaces:**
- Produces:
  - `assets.Asset` (frozen dataclass) with fields `kind`, `symbol`, `yahoo`,
    `tradingview`, `exchange`, `is_isin`, and properties `key` and `signal_ticker`;
  - `assets.resolve(text, coins=None) -> Asset | None`. `coins` is a set, a zero-arg
    callable returning a set, or None for the built-in `crypto.SYMBOLS`. The callable
    is only called when rule 4 needs it;
  - `assets.stock_asset(symbol) -> Asset` and `assets.crypto_asset(symbol) -> Asset`;
  - `assets.EXCHANGE_SUFFIXES`.

- [ ] **Step 1: Write the failing tests** (`tests/test_assets.py`)

```python
"""assets.resolve: what the user typed -> which asset. Offline: the coin list is
passed in."""
from __future__ import annotations

import pytest

import assets

COINS = {"BTC", "ETH", "SOL", "PEPE", "IP"}


@pytest.mark.parametrize("text,kind,symbol,yahoo,key", [
    ("BTC", "crypto", "BTC", "BTC-USD", "CRYPTO:BTC"),
    ("btc", "crypto", "BTC", "BTC-USD", "CRYPTO:BTC"),
    ("$BTC", "stock", "BTC", "BTC", "$BTC"),
    ("btc-usd", "crypto", "BTC", "BTC-USD", "CRYPTO:BTC"),
    ("CRYPTO:SOL", "crypto", "SOL", "SOL-USD", "CRYPTO:SOL"),
    ("SOL", "crypto", "SOL", "SOL-USD", "CRYPTO:SOL"),
    ("PEPE", "crypto", "PEPE", "PEPE-USD", "CRYPTO:PEPE"),
    ("NVDA", "stock", "NVDA", "NVDA", "$NVDA"),
    ("$aapl", "stock", "AAPL", "AAPL", "$AAPL"),
    ("aapl buy now", "stock", "AAPL", "AAPL", "$AAPL"),
    ("BRK.B", "stock", "BRK.B", "BRK-B", "$BRK.B"),
    ("EQNR.OL", "stock", "EQNR.OL", "EQNR.OL", "EQNR.OL"),
    ("volv-b.st", "stock", "VOLV-B.ST", "VOLV-B.ST", "VOLV-B.ST"),
    ("SAP.DE", "stock", "SAP.DE", "SAP.DE", "SAP.DE"),
])
def test_resolve(text, kind, symbol, yahoo, key):
    a = assets.resolve(text, COINS)
    assert (a.kind, a.symbol, a.yahoo, a.key) == (kind, symbol, yahoo, key)


def test_crypto_assets_carry_their_tradingview_pair():
    assert assets.resolve("BTC", COINS).tradingview == "CRYPTO:BTCUSD"
    assert assets.resolve("NVDA", COINS).tradingview is None


def test_key_resolves_back_to_the_same_asset():
    """The analysis queue stores `key`; resolving it again must give the same asset."""
    for text in ("BTC", "$BTC", "NVDA", "EQNR.OL", "DE0007164600", "BRK.B"):
        a = assets.resolve(text, COINS)
        assert assets.resolve(a.key, COINS) == a


def test_isin_is_a_stock_without_market_symbols():
    a = assets.resolve("DE0007164600", COINS)
    assert a.is_isin and a.kind == "stock" and a.yahoo is None and a.key == "DE0007164600"


@pytest.mark.parametrize("text", ["", "   ", "$", "CRYPTO:", "-USD", "!!!", "A" * 20, "#$%"])
def test_not_a_ticker(text):
    assert assets.resolve(text, COINS) is None


def test_coin_list_is_only_consulted_when_needed():
    calls = []

    def coins():
        calls.append(1)
        return COINS
    for text in ("$BTC", "EQNR.OL", "DE0007164600", "BTC-USD", "CRYPTO:ETH"):
        assets.resolve(text, coins)
    assert calls == []
    assert assets.resolve("BTC", coins).kind == "crypto" and calls == [1]


def test_default_coins_are_the_builtin_list():
    assert assets.resolve("ETH").kind == "crypto" and assets.resolve("NVDA").kind == "stock"


def test_signal_ticker_matches_the_signal_tables():
    assert assets.resolve("BTC", COINS).signal_ticker == "CRYPTO:BTC"
    assert assets.resolve("NVDA", COINS).signal_ticker == "NVDA"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_assets.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'assets'`.

- [ ] **Step 3: Implement `assets.py`**

```python
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
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_assets.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add assets.py tests/test_assets.py
git commit -m "feat(assets): resolve typed text to a stock, coin or ISIN

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: `sources.py`, price chains and the coin list

**Files:**
- Create: `sources.py`
- Modify: `db.py` (add a `coin_list` table to `SCHEMA`)
- Test: `tests/test_sources.py`

**Interfaces:**
- Consumes: `assets.Asset` (Task 1), `crypto.price_usd`, `crypto.SYMBOLS`,
  `crypto.COINGECKO_IDS`, `db.get_cached_value` / `db.save_cached_value`,
  `tradingview.fetch_snapshot`.
- Produces:
  - `sources.first_available(attempts: list[tuple[str, Callable]]) -> tuple[data, str] | (None, None)`;
  - `sources.price_history(asset, days=800) -> (list[tuple[iso_date, close]] oldest-first, source)`;
  - `sources.current_price(asset) -> (float, source)`;
  - `sources.coin_list() -> (list[(symbol, coin_id, name, rank)], source)`;
  - `sources.cached_coins(conn) -> list[(symbol, coin_id, name, rank)]`, ordered by rank;
  - `sources.cached_coin_symbols(conn) -> set[str]`;
  - `sources.coin_name(conn, symbol) -> str | None`;
  - `sources.STABLECOINS`;
  - `sources._get(url, **params) -> Response` (the HTTP seam tests patch);
  - the providers `_yahoo_history`, `_nasdaq_history`, `_binance_history`,
    `_bybit_history`, `_kraken_history`, `_tradingview_close`, `_binance_price`,
    `_coingecko_coins`, `_coinpaprika_coins`.

- [ ] **Step 1: Write the failing tests** (`tests/test_sources.py`)

```python
"""sources.py: each chain tries independent providers in order. Offline -- providers
and the HTTP seam are stubbed."""
from __future__ import annotations

import datetime as dt

import pytest

import assets
import sources

BARS = [("2026-09-21", 10.0), ("2026-09-22", 11.0)]
NVDA = assets.stock_asset("NVDA")
EQNR = assets.stock_asset("EQNR.OL")
BTC = assets.crypto_asset("BTC")
ISIN = assets.resolve("DE0007164600")


class _Resp:
    def __init__(self, payload=None, content=b""):
        self.payload, self.content = payload, content

    def json(self):
        return self.payload


def _stub_history(monkeypatch, **results):
    """Each history provider returns its given value, raises it if it's an exception,
    or returns None when not given."""
    for name in ("_yahoo_history", "_nasdaq_history", "_binance_history",
                 "_bybit_history", "_kraken_history"):
        value = results.get(name)

        def fake(*args, _v=value):
            if isinstance(_v, Exception):
                raise _v
            return _v
        monkeypatch.setattr(sources, name, fake)


def test_first_available_takes_the_first_source_that_answers(capsys):
    def boom():
        raise ValueError("down")
    result = sources.first_available([("A", boom), ("B", lambda: None), ("C", lambda: [1]),
                                      ("D", lambda: [2])])
    assert result == ([1], "C")
    assert "A недоступен" in capsys.readouterr().out


def test_first_available_when_everything_fails():
    assert sources.first_available([("A", lambda: None), ("B", lambda: [])]) == (None, None)


def test_stock_history_falls_back_to_nasdaq(monkeypatch):
    _stub_history(monkeypatch, _yahoo_history=ConnectionError("x"), _nasdaq_history=BARS)
    assert sources.price_history(NVDA, 30) == (BARS, "Nasdaq")


def test_european_stock_has_no_nasdaq_fallback(monkeypatch):
    _stub_history(monkeypatch, _yahoo_history=ConnectionError("x"), _nasdaq_history=BARS)
    assert sources.price_history(EQNR, 30) == (None, None)


def test_crypto_history_chain_order(monkeypatch):
    _stub_history(monkeypatch, _yahoo_history=[], _binance_history=ConnectionError("x"),
                  _bybit_history=BARS, _kraken_history=[("2020-01-01", 1.0)])
    assert sources.price_history(BTC, 30) == (BARS, "Bybit")


def test_isin_has_no_price_history(monkeypatch):
    _stub_history(monkeypatch, _yahoo_history=BARS)
    assert sources.price_history(ISIN, 30) == (None, None)


def test_current_price_falls_back_to_tradingview(monkeypatch):
    _stub_history(monkeypatch, _yahoo_history=ConnectionError("x"))
    monkeypatch.setattr(sources, "_tradingview_close", lambda asset: 42.0)
    assert sources.current_price(EQNR) == (42.0, "TradingView")


def test_crypto_current_price_falls_back_to_binance(monkeypatch):
    _stub_history(monkeypatch, _yahoo_history=None)
    monkeypatch.setattr(sources, "_tradingview_close", lambda asset: None)
    monkeypatch.setattr(sources.crypto, "price_usd", lambda conn, sym: None)
    monkeypatch.setattr(sources, "_binance_price", lambda sym: 86000.0)
    assert sources.current_price(BTC) == (86000.0, "Binance")


def test_nasdaq_history_parses_rows(monkeypatch):
    monkeypatch.setattr(sources, "_get", lambda url, **p: _Resp({"data": {"tradesTable": {"rows": [
        {"date": "09/22/2026", "close": "$1,339.75"}, {"date": "09/21/2026", "close": "$338.98"}]}}}))
    assert sources._nasdaq_history("AAPL", 10) == [("2026-09-21", 338.98), ("2026-09-22", 1339.75)]


def test_binance_history_parses_klines(monkeypatch):
    monkeypatch.setattr(sources, "_get", lambda url, **p: _Resp(
        [[1790035200000, "1", "2", "0.5", "118.57", "9"]]))
    assert sources._binance_history("SOL", 10) == [("2026-09-22", 118.57)]


def test_bybit_history_is_returned_oldest_first(monkeypatch):
    monkeypatch.setattr(sources, "_get", lambda url, **p: _Resp({"result": {"list": [
        ["1790121600000", "1", "2", "0.5", "86.0"], ["1790035200000", "1", "2", "0.5", "85.0"]]}}))
    assert sources._bybit_history("BTC", 10) == [("2026-09-22", 85.0), ("2026-09-23", 86.0)]


def test_kraken_history_uses_kraken_names_and_the_window(monkeypatch):
    seen = {}
    yesterday = dt.datetime.combine(dt.date.today() - dt.timedelta(days=1), dt.time(),
                                    dt.timezone.utc)
    old = yesterday - dt.timedelta(days=100)

    def fake(url, **params):
        seen.update(params)
        return _Resp({"error": [], "result": {"XXBTZUSD": [
            [int(old.timestamp()), "o", "h", "l", "1.0"],
            [int(yesterday.timestamp()), "o", "h", "l", "86000.1"]], "last": 1}})
    monkeypatch.setattr(sources, "_get", fake)
    assert sources._kraken_history("BTC", 10) == [(yesterday.date().isoformat(), 86000.1)]
    assert seen["pair"] == "XBTUSD"


def test_coin_list_falls_back_to_coinpaprika(monkeypatch):
    def down():
        raise ConnectionError("x")
    monkeypatch.setattr(sources, "_coingecko_coins", down)
    monkeypatch.setattr(sources, "_coinpaprika_coins", lambda: [("BTC", "btc-bitcoin", "Bitcoin", 1)])
    assert sources.coin_list() == ([("BTC", "btc-bitcoin", "Bitcoin", 1)], "CoinPaprika")


def test_cached_coins_are_cached_and_deduplicated(conn, monkeypatch):
    calls = []

    def coins():
        calls.append(1)
        return [("BTC", "bitcoin", "Bitcoin", 1), ("PEPE", "pepe", "Pepe", 30),
                ("PEPE", "other-pepe", "Other", 200)]
    monkeypatch.setattr(sources, "_coingecko_coins", coins)
    rows = sources.cached_coins(conn)
    sources.cached_coins(conn)
    assert calls == [1]
    assert [r[0] for r in rows] == ["BTC", "PEPE"] and rows[1][1] == "pepe"
    assert sources.coin_name(conn, "PEPE") == "Pepe"


def test_cached_coins_fall_back_to_the_builtin_list(conn, monkeypatch):
    monkeypatch.setattr(sources, "_coingecko_coins", lambda: None)
    monkeypatch.setattr(sources, "_coinpaprika_coins", lambda: None)
    assert {"BTC", "ETH"} <= sources.cached_coin_symbols(conn)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_sources.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'sources'`.

- [ ] **Step 3: Add the `coin_list` table** (`db.py`, append inside `SCHEMA` before its closing `"""`)

```sql

-- The ~250 largest coins by market cap (sources.cached_coins), refreshed daily:
-- how the resolver recognises "PEPE" as a coin.
CREATE TABLE IF NOT EXISTS coin_list (
    symbol   TEXT PRIMARY KEY,
    coin_id  TEXT,
    name     TEXT,
    rank     INTEGER
);
```

- [ ] **Step 4: Implement `sources.py`**

```python
"""Independent free sources for each kind of market data, tried in order until one
answers -- so a lookup still arrives when one site is down (spec §4).

Every source here was checked keyless and answering on 2026-09-23. Checked and left
out: Stooq (now behind a bot check), Euronext's chart data (encrypted), CoinGecko
history beyond a year (needs a key).

Each chain returns (data, source_name), or (None, None) when every source failed.
Callers name the source only when it isn't the first in its chain.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Callable

import requests

import crypto
import db

_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
_TIMEOUT = 20
COIN_LIST_SIZE = 250
COIN_LIST_TTL_SECONDS = 24 * 3600
_COIN_LIST_KEY = "coin_list_fetched"
# Coins pegged to a currency or to gold: no free-moving price to have a direction.
STABLECOINS = {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "PYUSD", "USDD", "BUSD",
               "USDS", "USD1", "USDP", "GUSD", "FRAX", "LUSD", "EURC", "XAUT", "PAXG",
               "USDTB", "USDX"}
_KRAKEN_NAMES = {"BTC": "XBT", "DOGE": "XDG"}
_DAY_MS = 86_400_000


def first_available(attempts: list[tuple[str, Callable]]):
    for name, fetch in attempts:
        try:
            data = fetch()
        except Exception as e:  # any failure of one source just means: try the next
            print(f"[sources] {name} недоступен: {type(e).__name__}: {str(e)[:120]}")
            continue
        if data:
            return data, name
    return None, None


def _get(url: str, **params) -> requests.Response:
    resp = requests.get(url, params=params or None, headers=_HEADERS, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp


def _ms_to_date(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).date().isoformat()


def _since(days: int) -> dt.date:
    return dt.date.today() - dt.timedelta(days=days)


# --------------------------------------------------------------- price history
def _yahoo_history(symbol: str, days: int):
    import yfinance as yf
    hist = yf.Ticker(symbol).history(start=_since(days).isoformat(), auto_adjust=True)
    closes = hist["Close"].dropna()
    return [(idx.date().isoformat(), float(v)) for idx, v in closes.items()]


def _nasdaq_history(symbol: str, days: int):
    data = _get(f"https://api.nasdaq.com/api/quote/{symbol}/historical", assetclass="stocks",
                fromdate=_since(days).isoformat(), limit=9999).json()
    rows = (((data or {}).get("data") or {}).get("tradesTable") or {}).get("rows") or []
    return sorted((dt.datetime.strptime(r["date"], "%m/%d/%Y").date().isoformat(),
                   float(r["close"].replace("$", "").replace(",", ""))) for r in rows)


def _binance_history(symbol: str, days: int):
    start_ms = int(dt.datetime.combine(_since(days), dt.time(), dt.timezone.utc).timestamp() * 1000)
    out = []
    while True:
        rows = _get("https://api.binance.com/api/v3/klines", symbol=f"{symbol}USDT",
                    interval="1d", startTime=start_ms, limit=1000).json()
        if not rows:
            break
        out += [(_ms_to_date(int(r[0])), float(r[4])) for r in rows]
        if len(rows) < 1000:
            break
        start_ms = int(rows[-1][0]) + _DAY_MS
    return out


def _bybit_history(symbol: str, days: int):
    end_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * _DAY_MS
    out: dict[str, float] = {}
    while end_ms > start_ms:
        data = _get("https://api.bybit.com/v5/market/kline", category="spot",
                    symbol=f"{symbol}USDT", interval="D", start=start_ms, end=end_ms,
                    limit=1000).json()
        rows = ((data or {}).get("result") or {}).get("list") or []   # newest first
        if not rows:
            break
        for r in rows:
            out[_ms_to_date(int(r[0]))] = float(r[4])
        oldest = int(rows[-1][0])
        if len(rows) < 1000 or oldest <= start_ms:
            break
        end_ms = oldest - 1
    return sorted(out.items())


def _kraken_history(symbol: str, days: int):
    data = _get("https://api.kraken.com/0/public/OHLC",
                pair=f"{_KRAKEN_NAMES.get(symbol, symbol)}USD", interval=1440).json()
    if data.get("error"):
        raise ValueError(str(data["error"]))
    series = next((v for k, v in (data.get("result") or {}).items() if k != "last"), [])
    cutoff = _since(days).isoformat()
    bars = [(_ms_to_date(int(r[0]) * 1000), float(r[4])) for r in series]
    return [b for b in bars if b[0] >= cutoff]


def price_history(asset, days: int = 800):
    """Daily closes [(iso_date, close)], oldest first."""
    if asset.is_isin or not asset.yahoo:
        return None, None
    if asset.kind == "crypto":
        attempts = [("Yahoo", lambda: _yahoo_history(asset.yahoo, days)),
                    ("Binance", lambda: _binance_history(asset.symbol, days)),
                    ("Bybit", lambda: _bybit_history(asset.symbol, days)),
                    ("Kraken", lambda: _kraken_history(asset.symbol, days))]
    else:
        attempts = [("Yahoo", lambda: _yahoo_history(asset.yahoo, days))]
        if not asset.exchange:
            attempts.append(("Nasdaq", lambda: _nasdaq_history(asset.symbol, days)))
    return first_available(attempts)


# --------------------------------------------------------------- current price
def _last(bars):
    return bars[-1][1] if bars else None


def _tradingview_close(asset):
    import tradingview
    snap = tradingview.fetch_snapshot(asset.tradingview or asset.symbol)
    return float(snap["close"]) if snap and snap.get("close") else None


def _binance_price(symbol: str):
    return float(_get("https://api.binance.com/api/v3/ticker/price",
                      symbol=f"{symbol}USDT").json()["price"])


def current_price(asset):
    if asset.is_isin:
        return None, None
    attempts = [("Yahoo", lambda: _last(_yahoo_history(asset.yahoo, 10))),
                ("TradingView", lambda: _tradingview_close(asset))]
    if asset.kind == "crypto":
        attempts += [("CoinGecko", lambda: crypto.price_usd(None, asset.symbol)),
                     ("Binance", lambda: _binance_price(asset.symbol))]
    elif not asset.exchange:
        attempts.append(("Nasdaq", lambda: _last(_nasdaq_history(asset.symbol, 10))))
    return first_available(attempts)


# ------------------------------------------------------------------- coin list
def _coingecko_coins():
    rows = _get("https://api.coingecko.com/api/v3/coins/markets", vs_currency="usd",
                order="market_cap_desc", per_page=COIN_LIST_SIZE, page=1).json()
    return [(r["symbol"].upper(), r["id"], r["name"], r.get("market_cap_rank")) for r in rows]


def _coinpaprika_coins():
    rows = _get("https://api.coinpaprika.com/v1/tickers", limit=COIN_LIST_SIZE).json()
    return [(r["symbol"].upper(), r["id"], r["name"], r.get("rank")) for r in rows]


def coin_list():
    return first_available([("CoinGecko", lambda: _coingecko_coins()),
                            ("CoinPaprika", lambda: _coinpaprika_coins())])


def cached_coins(conn) -> list[tuple]:
    """(symbol, coin_id, name, rank) by rank, refreshed daily; the built-in list when
    no source has ever answered."""
    if db.get_cached_value(conn, _COIN_LIST_KEY, COIN_LIST_TTL_SECONDS) is None:
        coins, _source = coin_list()
        if coins:
            best: dict[str, tuple] = {}
            for sym, coin_id, name, rank in sorted(coins, key=lambda r: (r[3] is None, r[3] or 0)):
                best.setdefault(sym, (sym, coin_id, name, rank))
            conn.execute("DELETE FROM coin_list")
            conn.executemany("INSERT INTO coin_list (symbol, coin_id, name, rank) VALUES (?,?,?,?)",
                             list(best.values()))
            db.save_cached_value(conn, _COIN_LIST_KEY, 1.0)
    rows = conn.execute("SELECT symbol, coin_id, name, rank FROM coin_list "
                        "ORDER BY rank IS NULL, rank").fetchall()
    return rows or [(s, crypto.COINGECKO_IDS.get(s), None, None) for s in sorted(crypto.SYMBOLS)]


def cached_coin_symbols(conn) -> set[str]:
    return {r[0] for r in cached_coins(conn)} | set(crypto.SYMBOLS)


def coin_name(conn, symbol: str) -> str | None:
    row = conn.execute("SELECT name FROM coin_list WHERE symbol = ?", (symbol.upper(),)).fetchone()
    return row[0] if row and row[0] else None
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_sources.py`
Expected: all pass.

- [ ] **Step 6: Full suite, then commit**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider` (all pass)
```bash
git add sources.py db.py tests/test_sources.py
git commit -m "feat(sources): price and coin-list chains with keyless fallbacks

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: `sources.py`, indicators, analyst targets and news

**Files:**
- Modify: `sources.py`
- Modify: `research.py` (`recent_news` becomes a thin wrapper over `sources._yahoo_news`)
- Test: `tests/test_sources.py` (append)

**Interfaces:**
- Consumes: `tradingview.fetch_snapshot` and `tradingview.analyze` (existing; `analyze`
  takes a raw field dict and returns the view dict `format_view` renders);
  `sources.price_history` (Task 2).
- Produces:
  - `sources.indicators(asset, closes=None) -> (view dict, source)`, where source is
    `"TradingView"` or `"расчёт по ценам"`;
  - `sources._rsi(closes, n=14) -> float`;
  - `sources.nasdaq_analyst(symbol) -> raw analyst dict | None`, in the same shape as
    `research._fetch_analyst_data`: `{"price_targets": {...}, "recommendations": [...], "upgrades_downgrades": []}`;
  - `sources.news(asset, name=None) -> (list[{title, publisher, published, url}], source)`;
  - `sources._yahoo_news(symbol)`, `sources._google_news(query)`,
    `sources._crypto_feed_news(symbol, name)`, `sources.NEWS_LIMIT = 8`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_sources.py`)

```python
# ------------------------------------------------------ indicators, analysts, news
import tradingview  # noqa: E402

RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>Bitcoin climbs as ETF inflows return</title><link>https://example.test/a</link>
<pubDate>Tue, 22 Sep 2026 10:00:00 GMT</pubDate><source url="https://x">Wire A</source></item>
<item><title>Solana validators upgrade</title><link>https://example.test/b</link>
<pubDate>Mon, 21 Sep 2026 09:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_indicators_fall_back_to_a_local_calculation(monkeypatch):
    monkeypatch.setattr(tradingview, "fetch_snapshot", lambda q, session=None: None)
    closes = [100.0 + i for i in range(260)]
    view, src = sources.indicators(NVDA, closes)
    assert src == "расчёт по ценам"
    assert view["ma_state"] == "цена выше и 50-, и 200-дневной средней"
    assert view["rsi"] == 100.0 and view["gauge"] is None


def test_indicators_prefer_tradingview(monkeypatch):
    monkeypatch.setattr(tradingview, "fetch_snapshot",
                        lambda q, session=None: {"Recommend.All": 0.5, "RSI": 55.0, "close": 10.0})
    view, src = sources.indicators(NVDA, [1.0] * 10)
    assert src == "TradingView" and view["gauge"] == 0.5


def test_rsi_is_about_50_for_alternating_moves():
    assert 45 < sources._rsi([100.0, 101.0] * 30) < 55


def test_nasdaq_analyst_converts_to_the_yahoo_shape(monkeypatch):
    monkeypatch.setattr(sources, "_get", lambda url, **p: _Resp({"data": {"consensusOverview": {
        "lowPriceTarget": 245.0, "highPriceTarget": 400.0, "priceTarget": 334.9,
        "buy": 15, "sell": 4, "hold": 9}}}))
    raw = sources.nasdaq_analyst("AAPL")
    assert raw["price_targets"] == {"mean": 334.9, "low": 245.0, "high": 400.0}
    assert raw["recommendations"][0] == {"strongBuy": 0, "buy": 15, "hold": 9, "sell": 4,
                                         "strongSell": 0}


def test_nasdaq_analyst_without_coverage_is_none(monkeypatch):
    monkeypatch.setattr(sources, "_get", lambda url, **p: _Resp(
        {"data": {"consensusOverview": {"priceTarget": None}}}))
    assert sources.nasdaq_analyst("ZZZZ") is None


def test_google_news_parses_rss(monkeypatch):
    monkeypatch.setattr(sources, "_get", lambda url, **p: _Resp(content=RSS))
    items = sources._google_news("Bitcoin crypto")
    assert items[0] == {"title": "Bitcoin climbs as ETF inflows return", "publisher": "Wire A",
                        "published": "2026-09-22", "url": "https://example.test/a"}
    assert items[1]["publisher"] == "Google News"


def test_crypto_feeds_keep_only_matching_headlines_once(monkeypatch):
    monkeypatch.setattr(sources, "_get", lambda url, **p: _Resp(content=RSS))
    items = sources._crypto_feed_news("BTC", "Bitcoin")
    assert [i["title"] for i in items] == ["Bitcoin climbs as ETF inflows return"]
    assert items[0]["publisher"] == "CoinDesk"


def test_news_chain_falls_back_for_crypto(monkeypatch):
    def down(q):
        raise ConnectionError("x")
    monkeypatch.setattr(sources, "_yahoo_news", lambda s: [])
    monkeypatch.setattr(sources, "_google_news", down)
    monkeypatch.setattr(sources, "_crypto_feed_news", lambda s, n: [{"title": "t"}])
    assert sources.news(BTC, "Bitcoin") == ([{"title": "t"}], "CoinDesk/Cointelegraph")


def test_stocks_have_no_crypto_feed_fallback(monkeypatch):
    monkeypatch.setattr(sources, "_yahoo_news", lambda s: [])
    monkeypatch.setattr(sources, "_google_news", lambda q: [])
    monkeypatch.setattr(sources, "_crypto_feed_news", lambda s, n: [{"title": "t"}])
    assert sources.news(NVDA) == (None, None)
    assert sources.news(ISIN) == (None, None)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_sources.py`
Expected: the new tests FAIL with `AttributeError: module 'sources' has no attribute 'indicators'`.

- [ ] **Step 3: Implement** (append to `sources.py`; add `import email.utils`, `import html`,
  `import re` and `import xml.etree.ElementTree as ET` to its imports)

```python
# ------------------------------------------------------------------ indicators
def _rsi(closes: list[float], n: int = 14) -> float:
    """Wilder's RSI over the whole series."""
    deltas = [b - a for a, b in zip(closes, closes[1:])]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_g, avg_l = sum(gains[:n]) / n, sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        avg_g = (avg_g * (n - 1) + g) / n
        avg_l = (avg_l * (n - 1) + l) / n
    return 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)


def _pct(closes: list[float], back: int) -> float | None:
    return (closes[-1] / closes[-1 - back] - 1) * 100 if len(closes) > back else None


def _local_snapshot(closes: list[float]) -> dict | None:
    """A TradingView-shaped field dict computed from our own closes, so
    tradingview.analyze/format_view render it unchanged."""
    if len(closes) < 200:
        return None
    return {"close": closes[-1], "SMA50": sum(closes[-50:]) / 50,
            "SMA200": sum(closes[-200:]) / 200, "RSI": _rsi(closes),
            "Perf.1M": _pct(closes, 21), "Perf.3M": _pct(closes, 63),
            "Perf.Y": _pct(closes, 252), "_symbol": None}


def indicators(asset, closes: list[float] | None = None):
    import tradingview

    def local():
        c = closes
        if c is None:
            bars, _src = price_history(asset, 400)
            c = [v for _d, v in bars or []]
        snap = _local_snapshot(c)
        return tradingview.analyze(snap) if snap else None
    return first_available([
        ("TradingView", lambda: tradingview.analyze(
            tradingview.fetch_snapshot(asset.tradingview or asset.symbol))),
        ("расчёт по ценам", local)])


# ------------------------------------------------------------- analyst targets
def nasdaq_analyst(symbol: str) -> dict | None:
    data = _get(f"https://api.nasdaq.com/api/analyst/{symbol}/targetprice").json()
    ov = (((data or {}).get("data") or {}).get("consensusOverview")) or {}
    if not ov.get("priceTarget"):
        return None
    return {
        "price_targets": {"mean": float(ov["priceTarget"]), "low": ov.get("lowPriceTarget"),
                          "high": ov.get("highPriceTarget")},
        "recommendations": [{"strongBuy": 0, "buy": int(ov.get("buy") or 0),
                             "hold": int(ov.get("hold") or 0), "sell": int(ov.get("sell") or 0),
                             "strongSell": 0}],
        "upgrades_downgrades": [],
    }


# ------------------------------------------------------------------------- news
NEWS_LIMIT = 8
CRYPTO_FEEDS = (("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
                ("Cointelegraph", "https://cointelegraph.com/rss"))


def _yahoo_news(symbol: str) -> list[dict]:
    import yfinance as yf
    out = []
    for item in (yf.Ticker(symbol).news or [])[:NEWS_LIMIT]:
        content = item.get("content", item)
        provider = content.get("provider")
        publisher = (provider.get("displayName") if isinstance(provider, dict)
                     else content.get("publisher")) or "?"
        url = content.get("canonicalUrl") or content.get("clickThroughUrl") or {}
        out.append({"title": content.get("title") or "", "publisher": publisher,
                    "published": (content.get("pubDate") or "")[:10],
                    "url": url.get("url") if isinstance(url, dict) else (url or "")})
    return out


def _rss_date(text: str) -> str:
    try:
        return email.utils.parsedate_to_datetime(text).date().isoformat()
    except (TypeError, ValueError):
        return ""


def _rss_items(url: str, **params) -> list[dict]:
    root = ET.fromstring(_get(url, **params).content)
    out = []
    for item in root.iter("item"):
        source = item.find("source")
        out.append({"title": html.unescape((item.findtext("title") or "").strip()),
                    "publisher": source.text.strip() if source is not None and source.text else None,
                    "published": _rss_date(item.findtext("pubDate") or ""),
                    "url": (item.findtext("link") or "").strip()})
    return out


def _google_news(query: str) -> list[dict]:
    items = _rss_items("https://news.google.com/rss/search", q=query, hl="en-US", gl="US",
                       ceid="US:en")
    for i in items:
        i["publisher"] = i["publisher"] or "Google News"
    return items[:NEWS_LIMIT]


def _crypto_feed_news(symbol: str, name: str | None) -> list[dict]:
    words = [w for w in {symbol.lower(), (name or "").lower()} if w]
    out, seen = [], set()
    for publisher, url in CRYPTO_FEEDS:
        try:
            items = _rss_items(url)
        except (requests.RequestException, ET.ParseError) as e:
            print(f"[sources] {publisher} недоступен: {type(e).__name__}")
            continue
        for it in items:
            title = it["title"].lower()
            if it["title"] in seen or not any(re.search(rf"\b{re.escape(w)}\b", title) for w in words):
                continue
            seen.add(it["title"])
            it["publisher"] = publisher
            out.append(it)
    return sorted(out, key=lambda i: i["published"], reverse=True)[:NEWS_LIMIT]


def news(asset, name: str | None = None):
    if asset.is_isin or not asset.yahoo:
        return None, None
    query = f"{name or asset.symbol} {'crypto' if asset.kind == 'crypto' else 'stock'}"
    attempts = [("Yahoo", lambda: _yahoo_news(asset.yahoo)),
                ("Google News", lambda: _google_news(query))]
    if asset.kind == "crypto":
        attempts.append(("CoinDesk/Cointelegraph", lambda: _crypto_feed_news(asset.symbol, name)))
    return first_available(attempts)
```

In `research.py`, replace the body of `recent_news` (keep its name and docstring) with:
```python
    import sources
    try:
        return sources._yahoo_news(ticker.replace(".", "-"))
    except Exception:
        return []
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add sources.py research.py tests/test_sources.py
git commit -m "feat(sources): indicator, analyst and news chains

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: `outlook.py`, situations, tables and the walk-forward check

**Files:**
- Create: `outlook.py`
- Modify: `db.py` (add `price_bars` and `outlook_table` to `SCHEMA`)
- Test: `tests/test_outlook.py`

**Interfaces:**
- Produces:
  - constants `HORIZON = {"stock": 21, "crypto": 30}`, `LOOKBACK = 252`,
    `MIN_HISTORY = 273`, `MIN_OOS = 50`, `MIN_OWN = 30`, `POOLED = "*"`;
  - `situation_series(closes: pd.Series, h: int) -> pd.Series` (keys, NaN when
    unavailable);
  - `situation(closes: list[float], h: int) -> str | None`;
  - `situation_from_tv(snapshot: dict, h: int) -> str | None`, returning e.g.
    `"up|strong_up|*"`;
  - `pooled(key: str) -> str`;
  - `observations(bars: list[tuple[str, float]], h: int) -> list[tuple[int, str, bool]]`;
  - `build_table(observations) -> dict[key, {"n","up","base_rate","oos_n","oos_brier_s","oos_brier_base"}]`,
    containing both the full keys and the pooled `*` keys;
  - `has_edge(row) -> bool`;
  - `save_table(conn, name, rows)`;
  - `load_table(conn, name) -> tuple[dict, str | None]` (rows, built_at);
  - `save_bars(conn, symbol, bars)`;
  - `load_bars(conn, symbol) -> list[tuple[str, float]]`.

- [ ] **Step 1: Write the failing tests** (`tests/test_outlook.py`)

```python
"""outlook.py: situations, frequency tables and the walk-forward check. Offline --
synthetic price paths."""
from __future__ import annotations

import pandas as pd
import pytest

import outlook


def _path(n, daily=0.001, wiggle=0.01, tail=()):
    """A steady drift with a regular zig-zag (volatility never zero), then `tail`
    extra daily returns."""
    closes, p = [], 100.0
    for i in range(n):
        p *= 1 + daily + (wiggle if i % 2 else -wiggle)
        closes.append(p)
    for r in tail:
        p *= 1 + r
        closes.append(p)
    return closes


# ------------------------------------------------------------------ situations
def test_steady_uptrend_is_up_flat_normal():
    assert outlook.situation(_path(400), 21) == "up|flat|normal"


def test_steady_downtrend_is_down():
    assert outlook.situation(_path(400, daily=-0.001), 21).startswith("down|")


def test_a_big_month_is_a_strong_move():
    assert outlook.situation(_path(400, tail=[0.015] * 21), 21).split("|")[1] == "strong_up"
    assert outlook.situation(_path(400, tail=[-0.015] * 21), 21).split("|")[1] == "strong_down"


def test_a_volatility_spike_is_high_volatility():
    spike = [0.05 if i % 2 else -0.05 for i in range(21)]
    assert outlook.situation(_path(400, tail=spike), 21).endswith("|high")


def test_too_little_history_has_no_situation():
    assert outlook.situation(_path(outlook.MIN_HISTORY - 1), 21) is None
    assert outlook.situation(_path(outlook.MIN_HISTORY), 21) is not None


def test_situation_from_tradingview_is_pooled_over_volatility():
    snap = {"close": 110.0, "SMA50": 105.0, "SMA200": 100.0, "Perf.1M": 20.0, "Volatility.M": 2.0}
    assert outlook.situation_from_tv(snap, 21) == "up|strong_up|*"
    assert outlook.situation_from_tv({**snap, "Perf.1M": 1.0}, 21) == "up|flat|*"
    assert outlook.situation_from_tv({**snap, "SMA200": None}, 21) is None


def test_pooled_key():
    assert outlook.pooled("down|flat|high") == "down|flat|*"


# ---------------------------------------------------------------- observations
def test_observations_are_monthly_and_labelled():
    dates = [d.date().isoformat() for d in pd.bdate_range("2015-01-01", periods=600)]
    closes = _path(600, daily=0.002, wiggle=0.0005)          # rises every day
    obs = outlook.observations(list(zip(dates, closes)), 21)
    expected = len(range(outlook.MIN_HISTORY - 1, 600 - 21, 21))
    assert len(obs) == expected and all(up for _y, _k, up in obs)
    assert obs[0][0] == int(dates[outlook.MIN_HISTORY - 1][:4])


# ---------------------------------------------------------- table, walk-forward
def _obs(key, per_year, up_fn, years=range(2010, 2022)):
    return [(y, key, up_fn(i)) for y in years for i in range(per_year)]


def test_a_planted_effect_has_an_edge_and_noise_does_not():
    noise = _obs("mixed|flat|normal", 100, lambda i: i % 2 == 0)
    rows = outlook.build_table(noise)
    assert not outlook.has_edge(rows["mixed|flat|normal"])     # its rate is the base rate

    planted = _obs("up|strong_up|normal", 30, lambda i: True)
    rows = outlook.build_table(noise + planted)
    row = rows["up|strong_up|normal"]
    assert outlook.has_edge(row) and row["n"] == 30 * 12 and row["up"] == 30 * 12


def test_a_thin_situation_has_no_edge():
    rows = outlook.build_table(_obs("mixed|flat|normal", 100, lambda i: i % 2 == 0)
                               + _obs("down|strong_down|high", 3, lambda i: True))
    assert rows["down|strong_down|high"]["oos_n"] < outlook.MIN_OOS
    assert not outlook.has_edge(rows["down|strong_down|high"])


def test_table_includes_pooled_rows():
    rows = outlook.build_table(_obs("up|flat|normal", 5, lambda i: True)
                               + _obs("up|flat|high", 5, lambda i: False))
    assert rows["up|flat|*"]["n"] == rows["up|flat|normal"]["n"] + rows["up|flat|high"]["n"]


def test_tables_and_bars_round_trip(conn):
    rows = outlook.build_table(_obs("up|flat|normal", 5, lambda i: True))
    outlook.save_table(conn, "stock", rows)
    loaded, built_at = outlook.load_table(conn, "stock")
    assert loaded["up|flat|normal"]["n"] == 60 and built_at
    outlook.save_bars(conn, "AAA", [("2026-01-02", 1.0), ("2026-01-05", 2.0)])
    assert outlook.load_bars(conn, "AAA") == [("2026-01-02", 1.0), ("2026-01-05", 2.0)]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_outlook.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'outlook'`.

- [ ] **Step 3: Add the tables** (`db.py`, append inside `SCHEMA`)

```sql

-- Daily closes for the outlook universes (outlook.py), keyed by Yahoo symbol.
CREATE TABLE IF NOT EXISTS price_bars (
    symbol  TEXT NOT NULL,
    date    TEXT NOT NULL,
    close   REAL NOT NULL,
    PRIMARY KEY (symbol, date)
);

-- One row per situation per table ("stock" / "crypto"): how often the price was
-- higher a month later, and the walk-forward check of that number.
CREATE TABLE IF NOT EXISTS outlook_table (
    table_name      TEXT NOT NULL,
    situation       TEXT NOT NULL,
    n               INTEGER NOT NULL,
    up              INTEGER NOT NULL,
    base_rate       REAL,
    oos_n           INTEGER NOT NULL,
    oos_brier_s     REAL,
    oos_brier_base  REAL,
    built_at        TEXT NOT NULL,
    PRIMARY KEY (table_name, situation)
);
```

- [ ] **Step 4: Implement `outlook.py`**

```python
"""The one-month outlook: how often the price was higher a month later in situations
like today's (spec §2). A historical frequency with its sample size and a walk-forward
check -- not a fitted model and not a promise.

A situation is three coarse facts from daily closes -- trend (vs the 50/200-day
averages), last month's move (against the asset's own normal monthly swing) and
volatility (vs its own past year) -- 18 in all. Tables count them across many assets
and years, one observation per asset per month so overlapping days don't inflate n.
"""
from __future__ import annotations

import datetime as dt
import math

import pandas as pd

HORIZON = {"stock": 21, "crypto": 30}
LOOKBACK = 252
MIN_HISTORY = LOOKBACK + 21            # 273 observations
VOL_WINDOW = 21
HIGH_VOL_QUANTILE = 0.75
MIN_OOS = 50
MIN_OWN = 30
OOS_START_OFFSET_YEARS = 5
POOLED = "*"


# ------------------------------------------------------------------ situations
def situation_series(closes: pd.Series, h: int) -> pd.Series:
    c = pd.Series(closes, dtype=float).reset_index(drop=True)
    sma50, sma200 = c.rolling(50).mean(), c.rolling(200).mean()
    trend = pd.Series("mixed", index=c.index)
    trend[(c > sma50) & (sma50 > sma200)] = "up"
    trend[(c < sma50) & (sma50 < sma200)] = "down"
    rets = c.pct_change()
    sigma_m = rets.rolling(LOOKBACK).std(ddof=0) * math.sqrt(h)
    r = c / c.shift(h) - 1
    move = pd.Series("flat", index=c.index)
    move[r > sigma_m] = "strong_up"
    move[r < -sigma_m] = "strong_down"
    vol = rets.rolling(VOL_WINDOW).std(ddof=0)
    threshold = vol.rolling(LOOKBACK).quantile(HIGH_VOL_QUANTILE)
    vol_state = pd.Series("normal", index=c.index)
    # A hair of tolerance: equal rolling values can differ by float noise.
    vol_state[vol > threshold * (1 + 1e-9)] = "high"
    keys = trend + "|" + move + "|" + vol_state
    valid = ((c.index >= MIN_HISTORY - 1) & sma200.notna() & sigma_m.notna()
             & threshold.notna())
    return keys.where(valid)


def situation(closes: list[float], h: int) -> str | None:
    if len(closes) < MIN_HISTORY:
        return None
    key = situation_series(pd.Series(closes), h).iloc[-1]
    return None if pd.isna(key) else key


def situation_from_tv(snapshot: dict, h: int) -> str | None:
    fields = [snapshot.get(k) for k in ("close", "SMA50", "SMA200", "Perf.1M", "Volatility.M")]
    if any(v is None for v in fields):
        return None
    close, sma50, sma200, perf, vol_m = (float(v) for v in fields)
    trend = ("up" if close > sma50 > sma200 else "down" if close < sma50 < sma200 else "mixed")
    sigma_m = vol_m / 100 * math.sqrt(h)
    r = perf / 100
    move = "strong_up" if r > sigma_m else "strong_down" if r < -sigma_m else "flat"
    return f"{trend}|{move}|{POOLED}"


def pooled(key: str) -> str:
    return key.rsplit("|", 1)[0] + "|" + POOLED


# ------------------------------------------------------------------- tables
def observations(bars: list[tuple[str, float]], h: int) -> list[tuple[int, str, bool]]:
    """(year, situation, price higher h observations later), sampled every h
    observations so no two overlap."""
    if len(bars) < MIN_HISTORY + h:
        return []
    closes = pd.Series([c for _d, c in bars], dtype=float)
    keys = situation_series(closes, h)
    out = []
    for t in range(MIN_HISTORY - 1, len(bars) - h, h):
        key = keys.iloc[t]
        if pd.isna(key):
            continue
        out.append((int(bars[t][0][:4]), key, bool(closes.iloc[t + h] > closes.iloc[t])))
    return out


def _walk_forward(obs: list[tuple[int, str, bool]]) -> dict:
    years = sorted({y for y, _k, _u in obs})
    if not years:
        return {}
    acc: dict[str, dict] = {}
    for year in range(years[0] + OOS_START_OFFSET_YEARS, dt.date.today().year):
        train = [(k, u) for y, k, u in obs if y < year]
        test = [(k, u) for y, k, u in obs if y == year]
        if not train or not test:
            continue
        base = sum(u for _k, u in train) / len(train)
        counts: dict[str, list[int]] = {}
        for k, u in train:
            c = counts.setdefault(k, [0, 0])
            c[0] += 1
            c[1] += u
        for k, u in test:
            n, ups = counts.get(k, (0, 0))
            p = ups / n if n else base
            a = acc.setdefault(k, {"oos_n": 0, "oos_brier_s": 0.0, "oos_brier_base": 0.0})
            a["oos_n"] += 1
            a["oos_brier_s"] += (p - u) ** 2
            a["oos_brier_base"] += (base - u) ** 2
    for a in acc.values():
        a["oos_brier_s"] /= a["oos_n"]
        a["oos_brier_base"] /= a["oos_n"]
    return acc


def _table(obs: list[tuple[int, str, bool]]) -> dict:
    counts: dict[str, list[int]] = {}
    for _y, k, u in obs:
        c = counts.setdefault(k, [0, 0])
        c[0] += 1
        c[1] += u
    base = sum(u for _y, _k, u in obs) / len(obs) if obs else None
    wf = _walk_forward(obs)
    empty = {"oos_n": 0, "oos_brier_s": None, "oos_brier_base": None}
    return {k: {"n": n, "up": ups, "base_rate": base, **wf.get(k, empty)}
            for k, (n, ups) in counts.items()}


def build_table(obs: list[tuple[int, str, bool]]) -> dict:
    """Full situations plus volatility-pooled ones (for the TradingView fallback)."""
    rows = _table(obs)
    rows.update(_table([(y, pooled(k), u) for y, k, u in obs]))
    return rows


def has_edge(row: dict) -> bool:
    return (row.get("oos_n", 0) >= MIN_OOS and row.get("oos_brier_s") is not None
            and row["oos_brier_s"] < row["oos_brier_base"])


# ------------------------------------------------------------------- storage
def save_table(conn, name: str, rows: dict) -> None:
    built_at = dt.datetime.now().isoformat(timespec="seconds")
    conn.execute("DELETE FROM outlook_table WHERE table_name = ?", (name,))
    conn.executemany(
        "INSERT INTO outlook_table (table_name, situation, n, up, base_rate, oos_n, "
        "oos_brier_s, oos_brier_base, built_at) VALUES (?,?,?,?,?,?,?,?,?)",
        [(name, k, r["n"], r["up"], r["base_rate"], r["oos_n"], r["oos_brier_s"],
          r["oos_brier_base"], built_at) for k, r in rows.items()])
    conn.commit()


def load_table(conn, name: str) -> tuple[dict, str | None]:
    rows = conn.execute(
        "SELECT situation, n, up, base_rate, oos_n, oos_brier_s, oos_brier_base, built_at "
        "FROM outlook_table WHERE table_name = ?", (name,)).fetchall()
    table = {k: {"n": n, "up": up, "base_rate": b, "oos_n": on, "oos_brier_s": bs,
                 "oos_brier_base": bb} for k, n, up, b, on, bs, bb, _at in rows}
    return table, (rows[0][7] if rows else None)


def save_bars(conn, symbol: str, bars: list[tuple[str, float]]) -> None:
    conn.executemany("INSERT OR REPLACE INTO price_bars (symbol, date, close) VALUES (?,?,?)",
                     [(symbol, d, c) for d, c in bars])
    conn.commit()


def load_bars(conn, symbol: str) -> list[tuple[str, float]]:
    return [(d, c) for d, c in conn.execute(
        "SELECT date, close FROM price_bars WHERE symbol = ? ORDER BY date", (symbol,))]
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_outlook.py`
Expected: all pass. If `test_steady_uptrend_is_up_flat_normal` fails on the move: with
±1% zig-zag and 0.1% drift, the 21-day return is ≈ 2.1% against σ_m ≈ 4.6%, so the
move is `flat`. Recheck `sigma_m` uses `std(ddof=0) * sqrt(h)`.

- [ ] **Step 6: Full suite, then commit**

```bash
git add outlook.py db.py tests/test_outlook.py
git commit -m "feat(outlook): situations, frequency tables and walk-forward check

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: `outlook.py`, refresh, lookup, message and daily hook

**Files:**
- Modify: `outlook.py`
- Modify: `tradingview.py` (add `"Volatility.M"` to `FIELDS`)
- Modify: `run_daily.sh` (weekly-refresh line)
- Test: `tests/test_outlook.py` (append)

**Interfaces:**
- Consumes:
  - `sources.price_history(asset, days)`, `sources.cached_coins(conn)` and
    `sources.STABLECOINS` (Task 2);
  - `assets.stock_asset` / `assets.crypto_asset` (Task 1);
  - `universe.load()["tickers"]`;
  - `tradingview.fetch_snapshot(query)`.
- Produces:
  - `outlook.refresh(conn, kinds=("stock", "crypto"))`;
  - `outlook.refresh_if_stale(conn)`;
  - `outlook.lookup(conn, asset) -> dict`. Keys: `status` in
    `ok|no_table|no_prices|short_history`; for `ok` also `table`, `situation`,
    `pooled`, `source`, `n`, `p`, `base_rate`, `edge`, `european`, and optionally
    `own_n` / `own_p`;
  - `outlook.format_outlook(result: dict, symbol: str) -> str`;
  - `outlook.describe(situation_key) -> str`;
  - CLI `python outlook.py --rebuild | --refresh-if-stale`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_outlook.py`)

```python
# ------------------------------------------------------- refresh, lookup, message
import datetime as dt  # noqa: E402

import assets  # noqa: E402


def _bars(closes, start="2010-01-01"):
    return [(d.date().isoformat(), c)
            for d, c in zip(pd.bdate_range(start, periods=len(closes)), closes)]


@pytest.fixture
def market(monkeypatch):
    series = {"AAA": _bars(_path(4000)), "BBB": _bars(_path(4000, daily=-0.0005))}
    calls = []

    def history(asset, days=800):
        calls.append((asset.yahoo, days))
        bars = series.get(asset.yahoo)
        return (bars, "Yahoo") if bars else (None, None)
    monkeypatch.setattr(outlook, "_universe", lambda conn, kind: ["AAA", "BBB"] if kind == "stock" else [])
    monkeypatch.setattr(outlook.sources, "price_history", history)
    monkeypatch.setattr(outlook.tradingview, "fetch_snapshot", lambda q, session=None: None)
    return calls


def test_refresh_stores_bars_and_builds_the_table(conn, market):
    outlook.refresh(conn, ["stock"])
    table, built_at = outlook.load_table(conn, "stock")
    assert table and built_at and len(outlook.load_bars(conn, "AAA")) == 4000


def test_refresh_if_stale_does_nothing_when_fresh(conn, market):
    outlook.refresh(conn, ["stock", "crypto"])
    before = len(market)
    outlook.refresh_if_stale(conn)
    assert len(market) == before


def test_a_split_triggers_a_full_refetch(conn, market, monkeypatch):
    outlook.save_bars(conn, "AAA", [(d, c * 2) for d, c in _bars(_path(4000))])   # stale scale
    outlook.refresh(conn, ["stock"])
    aaa_days = [days for sym, days in market if sym == "AAA"]
    assert aaa_days[-1] == outlook.FULL_HISTORY_DAYS
    assert outlook.load_bars(conn, "AAA")[-1][1] == pytest.approx(_path(4000)[-1])


def test_lookup_without_a_table(conn, market):
    assert outlook.lookup(conn, assets.stock_asset("AAA"))["status"] == "no_table"


def test_lookup_places_the_asset_and_reads_the_table(conn, market):
    outlook.refresh(conn, ["stock"])
    res = outlook.lookup(conn, assets.stock_asset("AAA"))
    assert res["status"] == "ok" and res["table"] == "stock" and not res["pooled"]
    assert res["situation"].count("|") == 2 and res["n"] > 0 and 0 < res["base_rate"] < 1


def test_lookup_falls_back_to_tradingview(conn, market, monkeypatch):
    outlook.refresh(conn, ["stock"])
    monkeypatch.setattr(outlook.tradingview, "fetch_snapshot", lambda q, session=None: {
        "close": 110.0, "SMA50": 105.0, "SMA200": 100.0, "Perf.1M": 1.0, "Volatility.M": 2.0})
    res = outlook.lookup(conn, assets.stock_asset("SAP.DE"))
    assert res["status"] == "ok" and res["pooled"] and res["source"] == "TradingView"
    assert res["situation"] == "up|flat|*" and res["european"]


def test_lookup_without_any_prices(conn, market):
    outlook.refresh(conn, ["stock"])
    assert outlook.lookup(conn, assets.stock_asset("ZZZ"))["status"] == "no_prices"
    assert outlook.lookup(conn, assets.resolve("DE0007164600"))["status"] == "no_prices"


def test_lookup_with_too_little_history(conn, market, monkeypatch):
    outlook.refresh(conn, ["stock"])
    monkeypatch.setattr(outlook.sources, "price_history",
                        lambda asset, days=800: (_bars(_path(100)), "Yahoo"))
    assert outlook.lookup(conn, assets.stock_asset("NEW"))["status"] == "short_history"


OK = {"status": "ok", "table": "stock", "situation": "up|strong_up|normal", "pooled": False,
      "source": "Yahoo", "n": 4210, "p": 0.61, "base_rate": 0.56, "edge": True,
      "european": False}


def test_message_with_an_edge():
    text = outlook.format_outlook(OK, "NVDA")
    assert text.splitlines()[0] == ("📈 Прогноз на месяц: рост в 61% похожих ситуаций "
                                    "(n = 4 210) · обычно 56% · проверено на истории")
    assert "ситуация: тренд вверх · месяц сильный рост · волатильность обычная" in text
    assert text.splitlines()[-1].strip().startswith("частота в прошлом, не гарантия")


def test_message_without_an_edge_and_with_own_history():
    text = outlook.format_outlook({**OK, "edge": False, "own_n": 36, "own_p": 0.64}, "NVDA")
    assert text.splitlines()[0] == "📈 Прогноз на месяц: нет преимущества над базовой частотой (обычно 56%)"
    assert "у самой NVDA в такой ситуации: 64% (n = 36)" in text


def test_message_notes():
    text = outlook.format_outlook({**OK, "european": True, "source": "TradingView",
                                   "pooled": True, "situation": "up|flat|*"}, "SAP.DE")
    assert "(без учёта волатильности)" in text and "таблица по акциям США" in text
    assert "цены: TradingView" in text


@pytest.mark.parametrize("status,words", [("no_table", "ещё не готов"),
                                          ("no_prices", "нет свежих цен"),
                                          ("short_history", "мало истории")])
def test_message_for_each_status(status, words):
    assert words in outlook.format_outlook({"status": status}, "X")
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_outlook.py`
Expected: the new tests FAIL with `AttributeError: module 'outlook' has no attribute '_universe'`.

- [ ] **Step 3: Implement**

In `tradingview.py`, add `"Volatility.M"` to `FIELDS` right after `"Volatility.D"`.

In `outlook.py`, add `import sys`, `import assets`, `import sources`, `import tradingview`
to the imports, add the constants below after `POOLED`, and append the functions:

```python
TABLE_MAX_AGE_DAYS = 7
FULL_HISTORY_DAYS = 15 * 365 + 10
TOP_UP_OVERLAP_DAYS = 30
SPLIT_TOLERANCE = 0.01          # an overlap day off by more than 1% means history was rescaled
LOOKUP_DAYS = 800
CRYPTO_UNIVERSE_TOP = 50
CRYPTO_MIN_HISTORY_DAYS = 730
_TREND = {"up": "тренд вверх", "down": "тренд вниз", "mixed": "тренд смешанный"}
_MOVE = {"strong_up": "месяц сильный рост", "strong_down": "месяц сильное падение",
         "flat": "месяц без резких движений"}
_VOL = {"high": "волатильность высокая", "normal": "волатильность обычная",
        POOLED: "(без учёта волатильности)"}


def _universe(conn, kind: str) -> list[str]:
    """Yahoo symbols of the assets a table is built from."""
    if kind == "stock":
        import universe
        return sorted({t.replace(".", "-") for t in universe.load()["tickers"]})
    coins = [r for r in sources.cached_coins(conn)
             if r[0] not in sources.STABLECOINS and r[3] is not None]
    return [f"{sym}-USD" for sym, *_rest in sorted(coins, key=lambda r: r[3])[:CRYPTO_UNIVERSE_TOP]]


def _asset_for(kind: str, yahoo_symbol: str):
    return (assets.stock_asset(yahoo_symbol) if kind == "stock"
            else assets.crypto_asset(yahoo_symbol[:-len("-USD")]))


def _top_up(conn, asset) -> None:
    """Fetch only the new days -- or everything again when the overlap shows the
    history was rescaled (a split or dividend adjustment)."""
    stored = load_bars(conn, asset.yahoo)
    if not stored:
        bars, _src = sources.price_history(asset, FULL_HISTORY_DAYS)
        if bars:
            save_bars(conn, asset.yahoo, bars)
        return
    last = dt.date.fromisoformat(stored[-1][0])
    days = (dt.date.today() - last).days + TOP_UP_OVERLAP_DAYS
    bars, _src = sources.price_history(asset, days)
    if not bars:
        return
    old = dict(stored)
    rescaled = any(d in old and abs(c / old[d] - 1) > SPLIT_TOLERANCE for d, c in bars)
    if rescaled:
        bars, _src = sources.price_history(asset, FULL_HISTORY_DAYS)
        if not bars:
            return
        conn.execute("DELETE FROM price_bars WHERE symbol = ?", (asset.yahoo,))
    save_bars(conn, asset.yahoo, bars)


def refresh(conn, kinds=("stock", "crypto")) -> None:
    for kind in kinds:
        obs = []
        for symbol in _universe(conn, kind):
            asset = _asset_for(kind, symbol)
            try:
                _top_up(conn, asset)
            except Exception as e:  # one symbol failing must not stop the table
                print(f"[outlook] {symbol}: {type(e).__name__}: {e}", file=sys.stderr)
            bars = load_bars(conn, asset.yahoo)
            if kind == "crypto" and len(bars) < CRYPTO_MIN_HISTORY_DAYS:
                continue
            obs += observations(bars, HORIZON[kind])
        if obs:
            save_table(conn, kind, build_table(obs))
            print(f"[outlook] {kind}: {len(obs)} observations")


def refresh_if_stale(conn) -> None:
    stale = []
    for kind in ("stock", "crypto"):
        _rows, built_at = load_table(conn, kind)
        if (built_at is None or dt.datetime.now() - dt.datetime.fromisoformat(built_at)
                > dt.timedelta(days=TABLE_MAX_AGE_DAYS)):
            stale.append(kind)
    if stale:
        refresh(conn, stale)


def lookup(conn, asset) -> dict:
    if asset.is_isin:
        return {"status": "no_prices"}
    kind, h = asset.kind, HORIZON[asset.kind]
    table, _built_at = load_table(conn, kind)
    if not table:
        return {"status": "no_table"}
    bars, source = sources.price_history(asset, LOOKUP_DAYS)
    key = None
    if bars and len(bars) >= MIN_HISTORY:
        key = situation([c for _d, c in bars], h)
    is_pooled = False
    if key is None:
        snap = tradingview.fetch_snapshot(asset.tradingview or asset.symbol)
        key = situation_from_tv(snap, h) if snap else None
        if key is not None:
            is_pooled, source = True, "TradingView"
    if key is None:
        return {"status": "short_history" if bars else "no_prices"}
    row = table.get(key, {})
    result = {"status": "ok", "table": kind, "situation": key, "pooled": is_pooled,
              "source": source, "n": row.get("n", 0),
              "p": row["up"] / row["n"] if row.get("n") else None,
              "base_rate": next(iter(table.values()))["base_rate"],
              "edge": has_edge(row), "european": kind == "stock" and asset.exchange is not None}
    if not is_pooled:
        own = [up for _y, k, up in observations(load_bars(conn, asset.yahoo), h) if k == key]
        if len(own) >= MIN_OWN:
            result["own_n"], result["own_p"] = len(own), sum(own) / len(own)
    return result


def describe(key: str) -> str:
    trend, move, vol = key.split("|")
    return f"{_TREND[trend]} · {_MOVE[move]} · {_VOL[vol]}"


def _pct0(x: float) -> str:
    return f"{x * 100:.0f}%"


def format_outlook(result: dict, symbol: str) -> str:
    status = result.get("status")
    if status == "no_table":
        return "📈 Прогноз на месяц: ещё не готов (таблица строится раз в неделю)"
    if status == "no_prices":
        return "📈 Прогноз на месяц: нет свежих цен"
    if status == "short_history":
        return "📈 Прогноз на месяц: мало истории для прогноза"
    usual = f"обычно {_pct0(result['base_rate'])}"
    if result["edge"] and result["p"] is not None:
        n = f"{result['n']:,}".replace(",", " ")
        head = (f"📈 Прогноз на месяц: рост в {_pct0(result['p'])} похожих ситуаций "
                f"(n = {n}) · {usual} · проверено на истории")
    else:
        head = f"📈 Прогноз на месяц: нет преимущества над базовой частотой ({usual})"
    lines = [head, f"   ситуация: {describe(result['situation'])}"]
    if result.get("own_n"):
        lines.append(f"   у самой {symbol} в такой ситуации: {_pct0(result['own_p'])} "
                     f"(n = {result['own_n']})")
    notes = []
    if result.get("european"):
        notes.append("таблица по акциям США")
    if result.get("table") == "crypto":
        notes.append("по ~30 крупнейшим монетам")
    if result.get("source") not in (None, "Yahoo"):
        notes.append(f"цены: {result['source']}")
    lines.append("   частота в прошлом, не гарантия" + ("".join(f" · {n}" for n in notes)))
    return "\n".join(lines)


def main() -> int:
    import argparse
    import db
    from pathlib import Path
    ap = argparse.ArgumentParser(description=__doc__)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--rebuild", action="store_true", help="refetch and rebuild both tables now")
    group.add_argument("--refresh-if-stale", action="store_true",
                       help="rebuild a table only when it is older than 7 days")
    args = ap.parse_args()
    conn = db.connect(Path(__file__).parent / "data" / "disclosures.db")
    if args.rebuild:
        refresh(conn)
    else:
        refresh_if_stale(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

In `run_daily.sh`, add after the `python bot.py --once ...` line:
```bash

# The one-month outlook's frequency tables (outlook.py): rebuilt when older than a
# week. Its own failure must not fail the daily run.
python outlook.py --refresh-if-stale || echo "[outlook] refresh failed"
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_outlook.py`
Expected: all pass.

- [ ] **Step 5: Full suite, then commit**

```bash
git add outlook.py tradingview.py run_daily.sh tests/test_outlook.py
git commit -m "feat(outlook): weekly refresh, lookup and the outlook message

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: `crypto_research.py`, the crypto dossier

**Files:**
- Create: `crypto_research.py`
- Modify: `cluster/crypto.py` (public alias `daily_etf_flows = _daily_etf_flows`,
  re-exported from `cluster/__init__.py`)
- Test: `tests/test_crypto_research.py`

**Interfaces:**
- Consumes:
  - `sources.price_history`, `sources.current_price`, `sources.indicators`,
    `sources.news`, `sources.coin_name`;
  - `outlook.lookup` and `outlook.format_outlook`;
  - `research.political_trades(conn, ticker)` (existing, imported lazily);
  - `cluster.daily_etf_flows(conn) -> {coin: [(as_of, flow_usd, funds)]}`;
  - `cluster.find_onchain_signals(conn, ignore_alert_state=True)`;
  - `tradingview.format_view(view)` and `tradingview.format_signal_note(view)`;
  - `telegram_notify._b` / `telegram_notify._esc`.
- Produces:
  - `crypto_research.build(conn, asset) -> dict` with `kind == "crypto"`, `found`,
    `ticker`, `name`, `current`, `changes`, `trend`, `volatility_pct`, `tradingview`,
    `news`, `sources`, `treasury`, `etf_flows`, `political`, `onchain`, `outlook`;
  - `crypto_research.format_report(rep) -> str` (terminal);
  - `crypto_research.format_brief(rep) -> str` (the Claude prompt input);
  - `crypto_research.format_condensed(rep) -> str` (Telegram HTML fallback).

- [ ] **Step 1: Write the failing tests** (`tests/test_crypto_research.py`)

```python
"""The crypto dossier. Offline: every source and the outlook are stubbed."""
from __future__ import annotations

import datetime as dt

import pytest

import assets
import crypto_research
import crypto_treasury as ct
import db
from conftest import add_house_txn

BTC = assets.crypto_asset("BTC")
TODAY = dt.date.today()


@pytest.fixture
def offline(monkeypatch):
    closes = [50_000.0 * (1.001 ** i) for i in range(400)]
    bars = [((TODAY - dt.timedelta(days=399 - i)).isoformat(), c) for i, c in enumerate(closes)]
    monkeypatch.setattr(crypto_research.sources, "price_history", lambda a, days=800: (bars, "Binance"))
    monkeypatch.setattr(crypto_research.sources, "current_price", lambda a: (None, None))
    monkeypatch.setattr(crypto_research.sources, "indicators", lambda a, closes=None: (None, None))
    monkeypatch.setattr(crypto_research.sources, "news", lambda a, name=None: (
        [{"title": "Bitcoin climbs", "publisher": "CoinDesk", "published": "2026-09-22", "url": "u"}],
        "CoinDesk/Cointelegraph"))
    monkeypatch.setattr(crypto_research.sources, "coin_name", lambda conn, s: "Bitcoin")
    monkeypatch.setattr(crypto_research.outlook, "lookup", lambda conn, a: {"status": "no_table"})
    return closes


def test_build_collects_price_trend_and_the_bots_own_data(conn, offline):
    db.save_crypto_treasury_txn(conn, ct.TreasuryTxn(
        "acc", "Strategy Inc", "MSTR", "1", "BTC", "P", 950, 79670.0, 75.7e6,
        (TODAY - dt.timedelta(days=2)).isoformat(), "8-K", "https://sec.test/x"))
    add_house_txn(conn, "CRYPTO:BTC", "Hon. Someone", "$50,001 - $100,000")
    rep = crypto_research.build(conn, BTC)
    assert rep["kind"] == "crypto" and rep["found"] and rep["name"] == "Bitcoin"
    assert rep["current"] == pytest.approx(offline[-1])
    assert rep["changes"]["30 дней"] == pytest.approx((1.001 ** 30 - 1) * 100)
    assert rep["trend"] == "up" and rep["sources"]["prices"] == "Binance"
    assert rep["treasury"][0][1] == "Strategy Inc" and rep["political"]


def test_report_texts(conn, offline):
    rep = crypto_research.build(conn, BTC)
    report = crypto_research.format_report(rep)
    assert "BTC — Bitcoin" in report and "$BTC" in report and "📈 Прогноз на месяц" in report
    assert "цены: Binance" in report and "новости: CoinDesk/Cointelegraph" in report
    brief = crypto_research.format_brief(rep)
    assert brief.startswith("АКТИВ: BTC (Bitcoin, криптовалюта)") and "---NEWS---" in brief
    html = crypto_research.format_condensed(rep)
    assert html.startswith("<b>BTC — Bitcoin</b>") and "📈 Прогноз на месяц" in html


def test_nothing_found_when_no_source_has_a_price(conn, offline, monkeypatch):
    monkeypatch.setattr(crypto_research.sources, "price_history", lambda a, days=800: (None, None))
    rep = crypto_research.build(conn, BTC)
    assert not rep["found"] and "цена: недоступно сейчас" in crypto_research.format_report(rep)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_crypto_research.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'crypto_research'`.

- [ ] **Step 3: Implement**

In `cluster/crypto.py`, after `_daily_etf_flows`, add:
```python
daily_etf_flows = _daily_etf_flows   # public name for the crypto dossier (crypto_research.py)
```
and add `daily_etf_flows,` to the `from .crypto import (...)` block in `cluster/__init__.py`.

Create `crypto_research.py`:
```python
"""The crypto dossier (spec §3): price and trend, TradingView's rating, what the bot
itself tracks for the coin (company treasury trades, spot-ETF flows, Congress buys,
exchange flows), headlines and the one-month outlook.

No Buy/Hold/Avoid score: opinion.py's factors -- insiders, financials, analysts --
don't exist for a coin, so the outlook is the main number here.
"""
from __future__ import annotations

import statistics

import cluster
import datefmt
import outlook
import sources
import termstyle
import tradingview

_CHANGE_WINDOWS = ((7, "7 дней"), (30, "30 дней"), (365, "1 год"))
_TREND_TEXT = {"up": "выше 50- и 200-дневной средней", "down": "ниже 50- и 200-дневной средней",
               "mixed": "между 50- и 200-дневной средней"}


def _trend(closes: list[float]) -> str | None:
    if len(closes) < 200:
        return None
    close, sma50, sma200 = closes[-1], sum(closes[-50:]) / 50, sum(closes[-200:]) / 200
    return "up" if close > sma50 > sma200 else "down" if close < sma50 < sma200 else "mixed"


def _treasury(conn, symbol: str) -> list:
    return conn.execute(
        "SELECT filed_date, company, ticker, side, units, total_usd, avg_price_usd, source_url "
        "FROM crypto_treasury_txns WHERE coin = ? ORDER BY filed_date DESC LIMIT 5",
        (symbol,)).fetchall()


def build(conn, asset) -> dict:
    import research
    name = sources.coin_name(conn, asset.symbol)
    bars, price_src = sources.price_history(asset, 400)
    closes = [c for _d, c in bars or []]
    current = closes[-1] if closes else None
    if current is None:
        current, price_src = sources.current_price(asset)
    rets = [b / a - 1 for a, b in zip(closes[-31:], closes[-30:])]
    view, view_src = sources.indicators(asset, closes or None)
    headlines, news_src = sources.news(asset, name)
    ticker = asset.signal_ticker
    return {
        "kind": "crypto",
        "asset": asset,
        "ticker": asset.symbol,
        "name": name,
        "found": current is not None,
        "current": current,
        "changes": {label: (closes[-1] / closes[-1 - n] - 1) * 100
                    for n, label in _CHANGE_WINDOWS if len(closes) > n},
        "trend": _trend(closes),
        "volatility_pct": statistics.pstdev(rets) * 100 if len(rets) >= 2 else None,
        "tradingview": view,
        "news": headlines or [],
        "sources": {"prices": price_src, "indicators": view_src, "news": news_src},
        "treasury": _treasury(conn, asset.symbol),
        "etf_flows": cluster.daily_etf_flows(conn).get(asset.symbol, [])[-5:],
        "political": research.political_trades(conn, ticker),
        "onchain": [s for s in cluster.find_onchain_signals(conn, ignore_alert_state=True)
                    if s.ticker == ticker],
        "outlook": outlook.lookup(conn, asset),
    }


def _title(rep: dict) -> str:
    return rep["ticker"] + (f" — {rep['name']}" if rep["name"] else "")


def _source_notes(rep: dict) -> list[str]:
    notes = []
    for key, label, first in (("prices", "цены", "Yahoo"), ("indicators", "индикаторы", "TradingView"),
                              ("news", "новости", "Yahoo")):
        src = rep["sources"].get(key)
        if src and src != first:
            notes.append(f"{label}: {src}")
    return notes


def _price_line(rep: dict) -> str:
    if rep["current"] is None:
        return "цена: недоступно сейчас"
    bits = [f"цена ${rep['current']:,.2f}"]
    bits += [f"{label} {pct:+.1f}%" for label, pct in rep["changes"].items()]
    return " · ".join(bits)


def _bot_lines(rep: dict) -> list[str]:
    lines = []
    for filed, company, co_ticker, side, units, total, avg, url in rep["treasury"]:
        verb = "купила" if side == "P" else "продала"
        money = f" (${total:,.0f})" if total else ""
        lines.append(f"🪙 {datefmt.fmt(filed)} {company} ({co_ticker or '?'}) {verb} {units:,.4g} {rep['ticker']}{money}")
    for as_of, flow, funds in rep["etf_flows"]:
        lines.append(f"🏦 {datefmt.fmt(as_of)} ETF {', '.join(funds)}: {flow / 1e6:+,.0f} млн $")
    for date, member, ttype, amount, url, chamber in rep["political"][:5]:
        lines.append(f"{'🟢' if ttype == 'P' else '🔴'} {datefmt.fmt(date)} {member} [{chamber}] {amount}")
    for s in rep["onchain"]:
        lines.append(f"⛓ {'вывод с бирж' if s.bullish else 'завод на биржи'}: {s.units:,.0f} {rep['ticker']}")
    return lines


def format_report(rep: dict) -> str:
    L = ["\n" + termstyle.header(_title(rep)),
         f"  Криптовалюта. Нужна акция с тикером {rep['ticker']}? Напишите ${rep['ticker']}.",
         "  " + _price_line(rep)]
    if rep["trend"]:
        L.append(f"  тренд: цена {_TREND_TEXT[rep['trend']]}")
    if rep["volatility_pct"] is not None:
        L.append(f"  волатильность: {rep['volatility_pct']:.1f}% в день (30 дней)")
    L.append("\n" + outlook.format_outlook(rep["outlook"], rep["ticker"]))
    if rep["tradingview"]:
        L.append(tradingview.format_view(rep["tradingview"]))
    L.append("\n" + termstyle.section("Что бот сам видит по монете"))
    L += ["  " + line for line in _bot_lines(rep)] or ["  Ничего за последнее время."]
    L.append("\n" + termstyle.section("Новости"))
    L += [f"  {n['published']}  [{n['publisher']}]  {n['title'][:70]}" for n in rep["news"]] \
        or ["  недоступно сейчас"]
    notes = _source_notes(rep)
    if notes:
        L.append("\n  Источники: " + " · ".join(notes))
    return "\n".join(L)


def format_brief(rep: dict) -> str:
    """What run_claude_analysis.sh hands Claude for a coin."""
    L = [f"АКТИВ: {rep['ticker']} ({rep['name'] or rep['ticker']}, криптовалюта)",
         _price_line(rep)]
    if rep["trend"]:
        L.append(f"тренд: цена {_TREND_TEXT[rep['trend']]}")
    note = tradingview.format_signal_note(rep["tradingview"])
    if note:
        L.append(note)
    L += _bot_lines(rep)
    L.append(outlook.format_outlook(rep["outlook"], rep["ticker"]))
    L.append("---NEWS---")
    L += [f"{n['published']} [{n['publisher']}] {n['title']}" for n in rep["news"]]
    return "\n".join(L)


def format_condensed(rep: dict) -> str:
    """Telegram HTML fallback reply when the Claude pass fails."""
    import telegram_notify
    L = [telegram_notify._b(_title(rep), True), telegram_notify._esc(_price_line(rep))]
    if rep["trend"]:
        L.append(f"тренд: цена {_TREND_TEXT[rep['trend']]}")
    L += [telegram_notify._esc(line) for line in _bot_lines(rep)]
    L.append(telegram_notify._esc(outlook.format_outlook(rep["outlook"], rep["ticker"])))
    return "\n".join(L)
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_crypto_research.py`
Expected: all pass.

- [ ] **Step 5: Full suite, then commit**

```bash
git add crypto_research.py cluster/crypto.py cluster/__init__.py tests/test_crypto_research.py
git commit -m "feat(crypto_research): the crypto dossier

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: `research.py`, resolve, dispatch, the outlook and fallbacks for stocks

**Files:**
- Modify: `research.py` (`build`, `format_report`, `main`; new `format_brief`; analyst,
  news and current-price fallbacks)
- Test: `tests/test_research.py` (append)

**Interfaces:**
- Consumes: `assets.resolve` (Task 1); `sources.cached_coin_symbols`,
  `sources.current_price`, `sources.indicators`, `sources.news`,
  `sources.nasdaq_analyst`, `sources.first_available` (Tasks 2–3); `outlook.lookup`
  and `outlook.format_outlook` (Task 5); `crypto_research.*` (Task 6).
- Produces:
  - `research.build(conn, text) -> dict`, raising `ValueError` when `text` isn't a
    ticker. A stock report now also has `kind == "stock"`, `asset`, `outlook`,
    `sources`, `found`;
  - `research.format_report(rep)` renders crypto via `crypto_research.format_report`;
  - `research.format_brief(rep) -> str` (first line `АКТИВ: …`, then opinion,
    entry/target, outlook, `---NEWS---` and headlines).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_research.py`)

```python
# ---------------------------------------------------- any-asset lookup (plan Task 7)
import assets  # noqa: E402
import sources  # noqa: E402


@pytest.fixture
def offline_stock(monkeypatch):
    """Every network step of a stock build stubbed out."""
    import cik_map
    import crypto_research

    class _Cik:
        def cik(self, ticker):
            return None
    monkeypatch.setattr(cik_map, "CikMap", _Cik)
    monkeypatch.setattr(research, "price_context", lambda t: {"windows": [], "current": None})
    monkeypatch.setattr(research, "_fetch_analyst_data", lambda t: {
        "price_targets": {}, "recommendations": [], "upgrades_downgrades": []})
    monkeypatch.setattr(research, "recent_filings", lambda t, cik: [])
    monkeypatch.setattr(research, "_yf_info", lambda t: {})
    for fn in ("financials", "ownership", "short_interest", "earnings_calendar"):
        monkeypatch.setattr(research, fn, lambda *a: None)
    monkeypatch.setattr(research, "share_count_history", lambda cik: None)
    monkeypatch.setattr(research.annual_report, "build", lambda cik, session=None: None)
    monkeypatch.setattr(research.marketcap, "facts", lambda conn, t, source=None: {})
    monkeypatch.setattr(research.marketcap, "market_cap_eur", lambda conn, t, source=None: None)
    monkeypatch.setattr(sources, "cached_coin_symbols", lambda conn: {"BTC"})
    monkeypatch.setattr(sources, "current_price", lambda a: (123.0, "TradingView"))
    monkeypatch.setattr(sources, "indicators", lambda a, closes=None: (None, None))
    monkeypatch.setattr(sources, "news", lambda a, name=None: (
        [{"title": "Head", "publisher": "Google News", "published": "2026-09-22", "url": "u"}],
        "Google News"))
    monkeypatch.setattr(sources, "nasdaq_analyst", lambda s: {
        "price_targets": {"mean": 150.0}, "recommendations": [
            {"strongBuy": 0, "buy": 10, "hold": 2, "sell": 0, "strongSell": 0}],
        "upgrades_downgrades": []})
    monkeypatch.setattr(research.outlook, "lookup", lambda conn, a: {"status": "no_table"})
    monkeypatch.setattr(crypto_research, "build", lambda conn, a: {"kind": "crypto", "ticker": a.symbol})


def test_build_dispatches_crypto_to_the_crypto_dossier(conn, offline_stock):
    assert research.build(conn, "BTC") == {"kind": "crypto", "ticker": "BTC"}


def test_build_rejects_text_that_is_not_a_ticker(conn, offline_stock):
    with pytest.raises(ValueError):
        research.build(conn, "!!!")


def test_stock_build_uses_the_fallback_chains(conn, offline_stock):
    rep = research.build(conn, "NVDA")
    assert rep["kind"] == "stock" and rep["found"]
    assert rep["prices"]["current"] == 123.0 and rep["sources"]["prices"] == "TradingView"
    assert rep["analyst"]["target_mean"] == 150.0 and rep["sources"]["analyst"] == "Nasdaq"
    assert rep["news"][0]["title"] == "Head" and rep["sources"]["news"] == "Google News"
    assert rep["outlook"] == {"status": "no_table"}


def test_stock_report_shows_the_outlook_and_the_sources(conn, offline_stock):
    text = research.format_report(research.build(conn, "NVDA"))
    assert "📈 Прогноз на месяц" in text
    assert "цены: TradingView" in text and "аналитики: Nasdaq" in text and "новости: Google News" in text


def test_brief_for_a_stock(conn, offline_stock):
    brief = research.format_brief(research.build(conn, "$NVDA"))
    assert brief.startswith("АКТИВ: NVDA (акция)") and "📈 Прогноз на месяц" in brief
    assert brief.index("---NEWS---") < brief.index("Head")


def test_isin_build_still_works_offline(conn):
    rep = research.build(conn, "DE0007190001")
    assert rep["is_isin"] and rep["outlook"] == {"status": "no_prices"} and rep["news"] == []
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_research.py`
Expected: the new tests FAIL (e.g. `KeyError: 'kind'` or no `ValueError` raised).

- [ ] **Step 3: Implement** (`research.py`)

Add `import assets`, `import outlook` and `import sources` to the project imports.

`price_context` and `_fetch_analyst_data` currently rewrite their argument with
`ticker.replace(".", "-")`, which would turn `EQNR.OL` into `EQNR-OL`. Remove that
rewrite from both (use the argument as-is); their callers now pass `asset.yahoo`,
which is already in Yahoo's form (`BRK-B`, `EQNR.OL`).

Delete `recent_news` (Task 3 made it a wrapper). After this task nothing calls it; check
with `grep -rn "recent_news" --include=*.py .` that only the definition matches before
deleting it.

Rename the existing `build(conn, ticker)` to `_build_stock(conn, asset)` and make these
changes inside it:

1. Its first line becomes `ticker = asset.symbol` (instead of
   `ticker = ticker.strip().upper()`), and `is_isin = asset.is_isin` (instead of
   `marketcap._looks_like_isin(ticker)`).
2. Replace `prices = … price_context(ticker)` and the `analyst = …` lines with:
```python
    prices = {"windows": [], "current": None} if is_isin else price_context(asset.yahoo)
    source_notes = {}
    if prices.get("current") is None and not is_isin:
        prices["current"], source_notes["prices"] = sources.current_price(asset)
    analyst = None
    if not is_isin:
        raw, source_notes["analyst"] = _analyst_raw(asset)
        analyst = analyst_view(raw, prices.get("current")) if raw else None
```
3. Replace `tv = tradingview.analyze(tradingview.fetch_snapshot(ticker))` with:
```python
    tv, source_notes["indicators"] = sources.indicators(asset)
```
4. Replace `"news": [] if is_isin else recent_news(ticker),` in the `rep` dict with
   `"news": headlines or [],`, and before the dict add:
```python
    headlines, source_notes["news"] = sources.news(asset, sec_name)
```
5. Add these keys to the `rep` dict:
```python
        "kind": "stock",
        "asset": asset,
        "outlook": outlook.lookup(conn, asset),
        "sources": source_notes,
```
6. Before `return rep`, add:
```python
    rep["found"] = bool(prices.get("current") is not None or rep["insiders"]["buys"]
                        or rep["insiders"]["sells"] or european or rep["stakes"]
                        or rep["political"] or rep["tradingview"])
```

Then add:
```python
def _analyst_raw(asset):
    """Yahoo's analyst data, falling back to Nasdaq's consensus (US listings only)."""
    def yahoo():
        raw = _fetch_analyst_data(asset.yahoo)
        return raw if (raw["price_targets"] or raw["recommendations"]) else None
    attempts = [("Yahoo", yahoo)]
    if not asset.exchange:
        attempts.append(("Nasdaq", lambda: sources.nasdaq_analyst(asset.symbol)))
    return sources.first_available(attempts)


def build(conn, text: str) -> dict:
    """Dossier for any stock, ETF, coin or ISIN (see assets.resolve). Raises
    ValueError when `text` isn't shaped like a ticker at all."""
    asset = assets.resolve(text, coins=lambda: sources.cached_coin_symbols(conn))
    if asset is None:
        raise ValueError(f"not a ticker: {text!r}")
    if asset.kind == "crypto":
        import crypto_research
        return crypto_research.build(conn, asset)
    return _build_stock(conn, asset)


_SOURCE_FIRST = {"prices": ("цены", "Yahoo"), "analyst": ("аналитики", "Yahoo"),
                 "indicators": ("индикаторы", "TradingView"), "news": ("новости", "Yahoo")}


def _source_notes(rep: dict) -> list[str]:
    return [f"{label}: {rep['sources'][k]}" for k, (label, first) in _SOURCE_FIRST.items()
            if rep.get("sources", {}).get(k) and rep["sources"][k] != first]


def format_brief(rep: dict) -> str:
    """What run_claude_analysis.sh hands Claude: the asset, the deterministic parts
    and the headlines."""
    if rep.get("kind") == "crypto":
        import crypto_research
        return crypto_research.format_brief(rep)
    L = [f"АКТИВ: {rep['ticker']} (акция" + (f", {rep['name']}" if rep.get("name") else "") + ")"]
    if rep.get("opinion"):
        L.append(opinion.format_opinion(rep["opinion"]).strip())
    entry_target = format_entry_target(rep)
    if entry_target:
        L.append(entry_target)
    L.append(outlook.format_outlook(rep["outlook"], rep["ticker"]))
    L.append("---NEWS---")
    L += [f"{n['published']} [{n['publisher']}] {n['title']}" for n in rep["news"]]
    return "\n".join(L)
```

In `format_report`:
- first line of the function: `if rep.get("kind") == "crypto": import crypto_research; return crypto_research.format_report(rep)`
  (as two lines);
- after the `entry_target` block add:
```python
    if rep.get("outlook"):
        L.append("\n" + outlook.format_outlook(rep["outlook"], t))
```
- just before the closing `L.append("\n" + "-" * 72)` add:
```python
    notes = _source_notes(rep)
    if notes:
        L.append("\n  Источники: " + " · ".join(notes))
```
- change the news section title `"Новости (агрегатор Yahoo Finance)"` to `"Новости"`.

In `main()`: change the argument help to `"ticker, coin or ISIN, e.g. NVDA, BTC, EQNR.OL, $BTC"`,
and wrap the print:
```python
    try:
        print(format_report(build(conn, args.ticker)))
    except ValueError:
        print("Не похоже на тикер. Примеры: NVDA, BTC, EQNR.OL, $BTC (акция), BTC-USD (монета).")
        raise SystemExit(2)
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: all pass. The pre-existing ISIN tests pass unchanged, because an ISIN
resolves to a stock with `is_isin=True` and never reaches the coin list.

- [ ] **Step 5: Commit**

```bash
git add research.py tests/test_research.py
git commit -m "feat(research): any-asset build with the outlook and source fallbacks

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Telegram, the Claude prompt, the menu and the README

**Files:**
- Modify: `telegram_bot.py` (`_handle_message`, `HELP_TEXT`, a new `LOOKUP_HINT`)
- Modify: `telegram_notify.py` (`format_condensed`: crypto dispatch + the outlook line)
- Modify: `claude_analysis_prompt.txt` (step a, and step c's composition rules)
- Modify: `menu.py` (`show_research`, main menu label)
- Modify: `README.md` ("Досье по тикеру или монете" and "Прогноз на месяц")
- Test: `tests/test_telegram_bot.py` (append; plus an autouse stub fixture)

**Interfaces:**
- Consumes: `assets.resolve`, `Asset.key` / `.kind` / `.is_isin` / `.symbol`;
  `sources.cached_coin_symbols`, `sources.current_price`; `research.build`,
  `research.format_brief`; `crypto_research.format_condensed`;
  `outlook.format_outlook`.
- Produces: the user-facing behaviour, with no new public functions.

- [ ] **Step 1: Write the failing tests**

At the top of `tests/test_telegram_bot.py` (after the imports), add an autouse fixture
so the existing tests stay offline:
```python
@pytest.fixture(autouse=True)
def _offline_lookup(monkeypatch):
    import sources
    monkeypatch.setattr(sources, "cached_coin_symbols", lambda conn: {"BTC", "ETH", "SOL"})
    monkeypatch.setattr(sources, "current_price", lambda asset: (100.0, "Yahoo"))
```
In `test_handle_message_enqueues_before_running`, change the expected queue from
`["AAPL"]` to `["$AAPL"]` (the queue now stores `Asset.key`).

Append:
```python
# ------------------------------------------------------------- any-asset lookup
def test_a_coin_is_queued_as_a_coin(conn, monkeypatch):
    import db
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stderr=""))
    tb._handle_message(conn, "btc")
    tb._handle_message(conn, "$BTC")
    assert [t for _, t in db.pending_analysis(conn)] == ["CRYPTO:BTC", "$BTC"]


def test_an_unknown_stock_is_not_queued(conn, monkeypatch):
    import db
    import sources
    sent = []
    monkeypatch.setattr(sources, "current_price", lambda asset: (None, None))
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    tb._handle_message(conn, "ZZZZQ")
    assert db.pending_analysis(conn) == [] and "Не нашёл такой тикер: ZZZZQ" in sent[0]


def test_not_a_ticker_gets_the_hint(conn, monkeypatch):
    sent = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    tb._handle_message(conn, "#$%")
    assert "BTC" in sent[0] and "EQNR.OL" in sent[0]


def test_crypto_fallback_reply_uses_the_crypto_format(conn, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stderr="x"))
    monkeypatch.setattr("research.build", lambda conn, text: {
        "kind": "crypto", "ticker": "BTC", "name": "Bitcoin", "current": 1.0, "changes": {},
        "trend": None, "treasury": [], "etf_flows": [], "political": [], "onchain": [],
        "outlook": {"status": "no_table"}})
    sent = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    tb._handle_message(conn, "BTC")
    assert sent[0].startswith("<b>BTC — Bitcoin</b>") and "Прогноз на месяц" in sent[0]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_telegram_bot.py`
Expected: the new tests FAIL (the queue holds `BTC`, and unknown stocks get queued).

- [ ] **Step 3: Implement**

`telegram_bot.py`: add `import assets` and `import sources` to the imports. Add the
constant below after `POSITIONS_USAGE`, and add `+ "\n" + LOOKUP_HINT` at the end of
`HELP_TEXT`:
```python
LOOKUP_HINT = ("Любой тикер или монета: NVDA, BTC, SOL, EQNR.OL, VOLV-B.ST. "
               "$BTC — акция с таким тикером, BTC-USD — монета.")
```
In `_handle_message`, replace the block from `ticker = _extract_ticker(text)` down to
`db.enqueue_analysis(conn, ticker)` with:
```python
    asset = assets.resolve(text, coins=lambda: sources.cached_coin_symbols(conn))
    if asset is None:
        telegram_notify.send_text(f"Не похоже на тикер: {telegram_notify._esc(text[:40])}. "
                                  + LOOKUP_HINT)
        return
    if asset.kind == "stock" and not asset.is_isin and sources.current_price(asset)[0] is None:
        telegram_notify.send_text(f"Не нашёл такой тикер: {asset.symbol}. " + LOOKUP_HINT)
        return
    ticker = asset.key
    db.enqueue_analysis(conn, ticker)
```
(the rest of the function keeps using `ticker`, now the asset key).

`telegram_notify.format_condensed`: at the top of the function body add
```python
    if rep.get("kind") == "crypto":
        import crypto_research
        return crypto_research.format_condensed(rep)
```
and after the `entry_target` block add
```python
    if rep.get("outlook"):
        import outlook
        L.append(_esc(outlook.format_outlook(rep["outlook"], t)))
```

`claude_analysis_prompt.txt`: replace step a's code block with
```
      python3 -c "
import db, research
conn = db.connect('data/disclosures.db')
print(research.format_brief(research.build(conn, '<TICKER>')))
"
```
Then change these composition rules in step c:
- "Start with an HTML bold ticker header" becomes: "Start with an HTML bold header
  of the symbol from the first line (`АКТИВ: …`), e.g. <b>NVDA</b> or
  <b>BTC — Bitcoin</b>".
- Add the bullet: "Include the `📈 Прогноз на месяц` block verbatim. Present it as a
  historical frequency, never as a promise or your own forecast."
- Add the bullet: "For a coin (`криптовалюта` on the first line) there is no opinion
  score. Include the price/trend lines and the bot's own lines (🪙 🏦 🟢 ⛓) instead."

`menu.py`: in `show_research`, change the prompt to
`"Тикер, монета или ISIN (NVDA, BTC, EQNR.OL, $BTC): "` and wrap the print:
```python
    try:
        print(research.format_report(research.build(conn, key)))
    except ValueError:
        print("Не похоже на тикер. Примеры: NVDA, BTC, EQNR.OL, $BTC (акция), BTC-USD (монета).")
```
Change the main menu line `2) Досье по тикеру` to `2) Досье по тикеру или монете`.

`README.md`: rename the "Досье по тикеру" section to "Досье по тикеру или монете".
Add a paragraph on what can be typed (the resolver rules), and a "Прогноз на месяц"
subsection covering:
- the 18 situations;
- the two tables (≈170 US large caps over ~15 years; ~30 largest coins);
- the walk-forward check and the "нет преимущества" line;
- the weekly rebuild (`python outlook.py --rebuild` to force it);
- that it's a historical frequency, not a guarantee.

Also add a table of the source chains from the spec §4, and a line saying that a
section whose sources all failed shows "недоступно сейчас".

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Smoke run on a scratch copy of the live DB** (read-only for the live file)

```bash
SP=$(mktemp -d); cp /Users/sevastians/Desktop/disclosure-bot/data/disclosures.db "$SP/v.db"
.venv/bin/python - "$SP" <<'EOF'
import sys, db, research, outlook
conn = db.connect(sys.argv[1] + "/v.db")
outlook.refresh(conn)                       # first build: a few minutes
for text in ("BTC", "NVDA", "EQNR.OL", "$BTC"):
    print(research.format_brief(research.build(conn, text)).splitlines()[:4])
EOF
```
Expected:
- the refresh prints `[outlook] stock: … observations` and `[outlook] crypto: … observations`;
- each brief starts with `АКТИВ:` and includes a `📈 Прогноз на месяц` line;
- `$BTC` is the ETF (акция), and `BTC` is the coin.

- [ ] **Step 6: Commit**

```bash
git add telegram_bot.py telegram_notify.py claude_analysis_prompt.txt menu.py README.md tests/test_telegram_bot.py
git commit -m "feat: any-asset lookup in Telegram, the Claude pass and the menu

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
