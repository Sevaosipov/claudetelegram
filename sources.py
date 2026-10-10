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
# A coin's price history is deep enough from this many daily bars (or from `days` bars, when
# fewer are asked for): the first source in the chain with that many wins. A coin listed days
# ago has a full year on one exchange and eleven days on another (HYPE: Binance 11, Bybit 421),
# so the chain's order alone must not pick the short one.
CRYPTO_MIN_BARS = 200


def first_available(attempts: list[tuple[str, Callable]], min_size: int | None = None):
    """(data, source name) of the first source that answers. With `min_size`, the first whose
    answer has that many items -- and when none does, the longest answer any gave (the earliest
    source among equals), never a short one while a longer one was on offer."""
    best, best_name = None, None
    for name, fetch in attempts:
        try:
            data = fetch()
        except Exception as e:  # any failure of one source just means: try the next
            print(f"[sources] {name} недоступен: {type(e).__name__}: {str(e)[:120]}",
                  file=sys.stderr)   # stderr: stdout is research.py's report / Claude's brief
            continue
        if not data:
            continue
        if min_size is None or len(data) >= min_size:
            return data, name
        if best is None or len(data) > len(best):
            best, best_name = data, name
    return best, best_name


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
    """Daily closes [(iso_date, close)], oldest first. A coin takes the first source, in the
    order below, that has at least min(days, CRYPTO_MIN_BARS) bars; when none has, the longest
    series any gave. A stock takes the first source that answers."""
    if asset.is_isin or not asset.yahoo:
        return None, None
    if asset.kind == "crypto":
        attempts = [("Yahoo", lambda: _yahoo_crypto_history(asset, days)),
                    ("Binance", lambda: _binance_history(asset.symbol, days)),
                    ("Bybit", lambda: _bybit_history(asset.symbol, days)),
                    ("Kraken", lambda: _kraken_history(asset.symbol, days))]
        return first_available(attempts, min_size=min(days, CRYPTO_MIN_BARS))
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
NEWS_LIMIT = 10
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


# ---- whom to believe. A headline is as good as who wrote it: the company's own filing or press release and
# the wire and newspaper desks first; the sites that turn every price move into an article not at all.
TIER_PRIMARY, TIER_OTHER, TIER_NOISE = 1, 2, 3
_PRIMARY = ("sec.gov", "sec (8-k)", "dow-jones", "business wire", "businesswire", "pr newswire", "prnewswire", "globenewswire",
            "globe newswire", "reuters", "bloomberg", "wall street journal", "wsj", "financial times", "ft.com",
            "associated press", "apnews", "ap news", "cnbc", "barron", "marketwatch", "dow jones", "the economist",
            "new york times", "nytimes", "nikkei", "coindesk", "the block", "theblock")
_NOISE = ("motley fool", "fool.com", "zacks", "benzinga", "investorplace", "simply wall st", "simplywall",
          "24/7 wall st", "247wallst", "tipranks", "insider monkey", "insidermonkey", "gurufocus", "stockstory",
          "marketbeat", "stocktwits", "investing.com", "cryptobriefing", "binance_news", "coinpedia", "thestreet", "invezz", "fxstreet", "stock traders daily",
          "defense world", "etf daily news", "ticker report", "american banking news")
# A press-release wire also carries what law firms pay to post about every falling stock and every merger:
# «shareholder alert», «investigates whether ... a fair deal». Noise, whoever the wire is.
_NOISE_TITLE = re.compile(r"(shareholder|investor|stock) alert|\$hareholder|class action|law (firm|offices)|"
                          r"investigat\w+ (whether|adequacy|the fairness|claims)|reminds (investors|shareholders)|"
                          r"lead plaintiff|obtaining (a )?fair deals?|halper sadeh|kahn swick|levi & korsinsky|rosen law|pomerantz", re.I)
# A wire's one-line note of a broker's rating or price target: true, but there are a dozen a day and each says
# little. Kept, behind the news proper.
_ROUTINE_TITLE = re.compile(r"price target|is maintained at|is (raised|cut|lowered) to (buy|sell|hold|neutral|"
                            r"outperform|underperform|overweight|underweight)|market talk", re.I)
_NAME_FILLER = {"inc", "corp", "corporation", "co", "company", "ltd", "plc", "group", "holdings", "the", "and",
                "sa", "ag", "nv", "stock", "crypto", "class"}
TRUSTED_SITES = ("reuters.com", "bloomberg.com", "wsj.com", "ft.com", "cnbc.com", "apnews.com",
                 "businesswire.com", "prnewswire.com", "globenewswire.com", "marketwatch.com", "barrons.com")
TRUSTED_LABEL = "Reuters/Bloomberg/WSJ/FT/CNBC/AP и пресс-релизы"
SEC_LABEL = "SEC 8-K"
SEC_NEWS_DAYS = 45
MIN_TRUSTED = 3             # with fewer headlines than this from the trusted sources, the general search is added
# What an 8-K says, by its item numbers (exhibits, 9.01, say nothing). The wording of 1.03, 3.01 and 4.02
# is the score's own red flags (model_score.RED_FLAGS): the company's own filing is the surest place to
# read them.
SEC_8K_ITEMS = {
    "1.01": "entered a material agreement", "1.02": "terminated a material agreement",
    "1.03": "bankruptcy or receivership", "2.01": "completed an acquisition or a disposal",
    "2.02": "reported results of operations", "2.03": "took on a new debt obligation",
    "2.04": "debt default or acceleration", "2.05": "restructuring costs", "2.06": "material impairment",
    "3.01": "notice of delisting or of failing a listing rule", "3.02": "unregistered sale of shares",
    "4.01": "change of auditor", "4.02": "restatement: earlier financial statements not to be relied on",
    "5.01": "change of control", "5.02": "officer or director change", "5.07": "shareholder vote results",
    "7.01": "Regulation FD disclosure", "8.01": "other material event",
}
_CIK_MAP = None


def tier(item: dict) -> int:
    """How far a headline's publisher is to be trusted: TIER_PRIMARY (a filing, a press-release wire, a
    wire or newspaper desk), TIER_NOISE (a site that writes an article per price move) or TIER_OTHER."""
    who = f"{item.get('publisher') or ''} {item.get('url') or ''}".lower()
    title = (item.get("title") or "").lower()
    tail = title.rsplit(" - ", 1)[-1] if " - " in title else ""          # Google News: «... - Reuters»
    if any(n in who or n in tail for n in _NOISE) or _NOISE_TITLE.search(title):
        return TIER_NOISE
    if any(p in who or p in tail for p in _PRIMARY):
        return TIER_OTHER if _ROUTINE_TITLE.search(title) else TIER_PRIMARY
    return TIER_OTHER


def _title_key(title: str) -> str:
    base = title.rsplit(" - ", 1)[0] if " - " in title else title
    return re.sub(r"[^a-z0-9а-я]+", " ", base.lower()).strip()


def ranked(items: list[dict], limit: int | None = None) -> list[dict]:
    """The headlines worth reading, best first: the noise dropped, one of each title (the most trusted
    copy), the primary sources before the rest and, within a tier, the newest first."""
    best: dict[str, dict] = {}
    for item in items:
        if not item.get("title") or tier(item) == TIER_NOISE:
            continue
        key = _title_key(item["title"])
        if key not in best or tier(item) < tier(best[key]):
            best[key] = item
    newest = sorted(best.values(), key=lambda i: i.get("published") or "", reverse=True)
    return sorted(newest, key=tier)[:limit or NEWS_LIMIT]


def about(items: list[dict], *names: str | None) -> list[dict]:
    """The headlines that name the asset: its ticker or a word of its name (a search engine also returns
    what merely sits near the query: «RXO stock» brings a story about Lukoil)."""
    words = {w for n in names if n for w in re.findall(r"[a-zа-я0-9]+", n.lower())
             if len(w) >= 2 and w not in _NAME_FILLER}
    if not words:
        return items
    pattern = re.compile(r"\b(" + "|".join(map(re.escape, sorted(words))) + r")\b")
    return [i for i in items if pattern.search((i.get("title") or "").lower())]


def _trusted_google_news(query: str) -> list[dict]:
    """Google News restricted to the trusted desks and the press-release wires, the last 30 days."""
    sites = " OR ".join(f"site:{s}" for s in TRUSTED_SITES)
    items = _rss_items("https://news.google.com/rss/search", q=f"{query} ({sites}) when:30d", hl="en-US",
                       gl="US", ceid="US:en")
    for i in items:
        i["publisher"] = i["publisher"] or "Google News"
    return items[:NEWS_LIMIT]


def _sec_8k_news(symbol: str, today: dt.date | None = None, session=None) -> list[dict]:
    """The company's own 8-K filings of the last SEC_NEWS_DAYS days as headlines, «8-K: reported results of
    operations; officer or director change» -- what it told the SEC itself. [] for a ticker with no CIK."""
    global _CIK_MAP
    import cik_map
    import sec_edgar
    _CIK_MAP = _CIK_MAP or cik_map.CikMap()
    cik = _CIK_MAP.cik(symbol)
    if not cik:
        return []
    number = str(cik).lstrip("0") or "0"
    resp = (session or sec_edgar.new_session()).get(sec_edgar.SUBMISSIONS_URL.format(cik=number), timeout=30)
    resp.raise_for_status()
    recent = resp.json().get("filings", {}).get("recent", {})
    since = ((today or dt.date.today()) - dt.timedelta(days=SEC_NEWS_DAYS)).isoformat()
    out = []
    for form, day, items, acc in zip(recent.get("form", []), recent.get("filingDate", []),
                                     recent.get("items", []), recent.get("accessionNumber", [])):
        if form != "8-K" or day < since:
            continue
        said = [SEC_8K_ITEMS[i] for i in (x.strip() for x in (items or "").split(",")) if i in SEC_8K_ITEMS]
        if said:
            out.append({"title": "8-K: " + "; ".join(said), "publisher": "SEC (8-K)", "published": day,
                        "url": f"https://www.sec.gov/Archives/edgar/data/{number}/{acc.replace('-', '')}/"})
    return out[:NEWS_LIMIT]


TV_NEWS_URL = "https://news-headlines.tradingview.com/v2/headlines"
TV_NEWS_LABEL = "TradingView"
TV_STOCK_EXCHANGES = ("NASDAQ", "NYSE", "AMEX")     # tried in turn: the feed wants EXCHANGE:TICKER


def _tradingview_news(asset) -> list[dict]:
    """TradingView's own news feed for the asset (its public headlines endpoint, no sign-in): Reuters and
    Dow Jones items among others, each with its provider, so the tiers sort them like any other headline.
    A US stock is asked as NASDAQ:, NYSE: and AMEX: until one answers; a coin as BINANCE:<SYMBOL>USDT.
    [] for a listing elsewhere."""
    if asset.kind == "crypto":
        symbols = [f"BINANCE:{asset.symbol}USDT"]
    elif "." in asset.symbol:
        return []
    else:
        symbols = [f"{exchange}:{asset.symbol}" for exchange in TV_STOCK_EXCHANGES]
    for symbol in symbols:
        items = _get(TV_NEWS_URL, client="web", lang="en", symbol=symbol).json().get("items") or []
        if items:
            out = []
            for item in items[:3 * NEWS_LIMIT]:
                stamp = item.get("published")
                day = (dt.datetime.fromtimestamp(stamp, dt.timezone.utc).date().isoformat()
                       if isinstance(stamp, (int, float)) else "")
                path = item.get("storyPath") or ""
                out.append({"title": (item.get("title") or "").strip(), "publisher": item.get("provider") or "TradingView",
                            "published": day, "url": f"https://www.tradingview.com{path}" if path else ""})
            return out
    return []


def news(asset, name: str | None = None):
    """(the headlines on an asset, where they came from), the most trusted first (ranked).

    TradingView's news feed is the main source: it is asked first, and with a US company's own 8-K filings
    (the surest place for a bankruptcy, a delisting or a restatement) it is usually the whole answer. Only
    when the two give fewer than MIN_TRUSTED headlines worth reading are the others asked -- the trusted
    desks and press-release wires through the news search, Yahoo's feed, a coin's trade press -- and the
    general news search last, when even those leave it short. A failing source is skipped.
    (None, None) with nothing, or for an asset with no listing."""
    if asset.is_isin or not asset.yahoo:
        return None, None
    crypto_asset = asset.kind == "crypto"
    query = f"{name or asset.symbol} {'crypto' if crypto_asset else 'stock'}"
    main = [(TV_NEWS_LABEL, lambda: _tradingview_news(asset))]
    if not crypto_asset and "." not in asset.symbol:
        main.append((SEC_LABEL, lambda: _sec_8k_news(asset.symbol)))
    more = [(TRUSTED_LABEL, lambda: about(_trusted_google_news(query), name, asset.symbol)),
            ("Yahoo", lambda: _yahoo_news(asset.yahoo))]
    if crypto_asset:
        more.append(("CoinDesk/Cointelegraph", lambda: _crypto_feed_news(asset.symbol, name)))
    last = [("Google News", lambda: about(_google_news(query), name, asset.symbol))]
    found, used = [], []
    for step in (main, more, last):
        if len(ranked(found)) >= MIN_TRUSTED:
            break
        items, names = _gather(step)
        found, used = found + items, used + names
    best = ranked(found)
    return (best, ", ".join(used)) if best else (None, None)


def _gather(attempts: list[tuple[str, Callable]]) -> tuple[list[dict], list[str]]:
    """(the items of every source that answered, the names of those that gave any)."""
    found, used = [], []
    for label, fetch in attempts:
        try:
            items = fetch() or []
        except Exception as e:
            print(f"[sources] {label} недоступен: {type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
            continue
        if items:
            found += items
            used.append(label)
    return found, used
