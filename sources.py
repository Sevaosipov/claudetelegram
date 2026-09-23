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
import email.utils
import html
import re
import sys
import xml.etree.ElementTree as ET
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
# Yahoo's crypto series is kept only when its last close is within 10% of an exchange's
# spot price: Yahoo files a clashing coin under a numbered ticker, so SYM-USD can be
# another token (M-USD closed at 0.00029 while MemeCore traded at 1.22).
CRYPTO_SPOT_TOLERANCE = 0.10


def first_available(attempts: list[tuple[str, Callable]]):
    for name, fetch in attempts:
        try:
            data = fetch()
        except Exception as e:  # any failure of one source just means: try the next
            print(f"[sources] {name} недоступен: {type(e).__name__}: {str(e)[:120]}",
                  file=sys.stderr)   # stderr: stdout is research.py's report / Claude's brief
            continue
        if data:
            return data, name
    return None, None


def source_note(label: str, used: str | None, first: str,
                empty: str = "недоступно сейчас") -> str | None:
    """How a message names a section's source (spec §4): nothing when the chain's
    first source answered, `empty` when none did, else "цены: Binance (Yahoo недоступен)"."""
    if used == first:
        return None
    if used is None:
        return f"{label}: {empty}"
    return f"{label}: {used} ({first} недоступен)"


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


def _yahoo_crypto_history(asset, days: int):
    """Yahoo first for its depth (the table's 5-year walk-forward needs it), but only
    when the series agrees with an exchange spot -- see CRYPTO_SPOT_TOLERANCE."""
    bars = _yahoo_history(asset.yahoo, days)
    if bars:
        spot = _crypto_spot(asset.symbol)
        if spot and abs(bars[-1][1] / spot - 1) > CRYPTO_SPOT_TOLERANCE:
            raise ValueError(f"Yahoo {asset.yahoo} is a different coin")
    return bars


def price_history(asset, days: int = 800):
    """Daily closes [(iso_date, close)], oldest first."""
    if asset.is_isin or not asset.yahoo:
        return None, None
    if asset.kind == "crypto":
        attempts = [("Yahoo", lambda: _yahoo_crypto_history(asset, days)),
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


def _bybit_price(symbol: str):
    data = _get("https://api.bybit.com/v5/market/tickers", category="spot",
                symbol=f"{symbol}USDT").json()
    return float(data["result"]["list"][0]["lastPrice"])


def _crypto_spot(symbol: str) -> float | None:
    """An exchange's spot price, quietly: None when none answers. Never raises."""
    for fetch in (lambda: _binance_price(symbol), lambda: _bybit_price(symbol),
                  lambda: crypto.price_usd(None, symbol)):
        try:
            price = float(fetch())
        except Exception:  # any failure just means: ask the next exchange
            continue
        if price > 0:
            return price
    return None


def current_price(asset):
    if asset.is_isin:
        return None, None
    if asset.kind == "crypto":
        # Exchanges first: Yahoo's SYM-USD can be a different coin (CRYPTO_SPOT_TOLERANCE).
        attempts = [("Binance", lambda: _binance_price(asset.symbol)),
                    ("Bybit", lambda: _bybit_price(asset.symbol)),
                    ("CoinGecko", lambda: crypto.price_usd(None, asset.symbol)),
                    ("TradingView", lambda: _tradingview_close(asset)),
                    ("Yahoo", lambda: _last(_yahoo_history(asset.yahoo, 10)))]
        return first_available(attempts)
    attempts = [("Yahoo", lambda: _last(_yahoo_history(asset.yahoo, 10))),
                ("TradingView", lambda: _tradingview_close(asset))]
    if not asset.exchange:
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


def stock_universe_symbols() -> set[str]:
    """S&P 100 + Nasdaq-100 tickers (universe.py): these win a clash with a coin.
    An empty set when the list can't be had. Never raises."""
    try:
        import universe
        return set(universe.load()["tickers"])
    except Exception:  # no list just means no override
        return set()


def coin_name(conn, symbol: str) -> str | None:
    row = conn.execute("SELECT name FROM coin_list WHERE symbol = ?", (symbol.upper(),)).fetchone()
    return row[0] if row and row[0] else None


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
def _price_or_none(value) -> float | None:
    """Nasdaq's numbers arrive as numbers or as text ("$1,334.90", "N/A")."""
    try:
        return float(str(value).replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return None


def nasdaq_analyst(symbol: str) -> dict | None:
    data = _get(f"https://api.nasdaq.com/api/analyst/{symbol}/targetprice").json()
    ov = (((data or {}).get("data") or {}).get("consensusOverview")) or {}
    mean = _price_or_none(ov.get("priceTarget"))
    if not mean:
        return None
    return {
        "price_targets": {"mean": mean, "low": _price_or_none(ov.get("lowPriceTarget")),
                          "high": _price_or_none(ov.get("highPriceTarget"))},
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
            print(f"[sources] {publisher} недоступен: {type(e).__name__}", file=sys.stderr)
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
