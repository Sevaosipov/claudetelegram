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
COIN_LIST_RETRY_SECONDS = 3600
_COIN_LIST_KEY = "coin_list_fetched"
_COIN_LIST_FAILED_KEY = "coin_list_failed"
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
    no source has ever answered. When all sources fail, remember the failure for 1 hour
    to avoid repeated HTTP timeouts."""
    # Skip refresh if a recent failure is cached
    if db.get_cached_value(conn, _COIN_LIST_FAILED_KEY, COIN_LIST_RETRY_SECONDS) is None:
        # Refresh needed only if both the success and failure caches are expired
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
            else:
                # Remember the failure for 1 hour
                db.save_cached_value(conn, _COIN_LIST_FAILED_KEY, 1.0)
    rows = conn.execute("SELECT symbol, coin_id, name, rank FROM coin_list "
                        "ORDER BY rank IS NULL, rank").fetchall()
    return rows or [(s, crypto.COINGECKO_IDS.get(s), None, None) for s in sorted(crypto.SYMBOLS)]


def cached_coin_symbols(conn) -> set[str]:
    return {r[0] for r in cached_coins(conn)} | set(crypto.SYMBOLS)


def coin_name(conn, symbol: str) -> str | None:
    row = conn.execute("SELECT name FROM coin_list WHERE symbol = ?", (symbol.upper(),)).fetchone()
    return row[0] if row and row[0] else None
