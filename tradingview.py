"""Direct technical readings from TradingView, no key and no login.

TradingView's embeddable "Technical Analysis" and "Symbol Info" widgets are backed
by `scanner.tradingview.com`, which answers a plain GET with a list of requested
fields for one symbol. This module asks it for the readings that widget shows: the
oscillator/moving-average gauge, RSI, MACD, ADX, price against the 50- and 200-day
averages, trailing performance, and the handful of fundamentals TradingView
carries (P/E, EPS, beta).

WHAT THE GAUGE IS, AND ISN'T

`Recommend.All` is TradingView's technical-analysis gauge on a -1..+1 scale, which
they label "Strong Sell" through "Strong Buy". It is a mechanical tally of ~26
momentum and trend indicators, it moves during the trading day, it is entirely
backward-looking, and it has no established predictive value. It is reported here
the way a P/E ratio is reported -- a number the market looks at, computed by
someone else, shown to every TradingView user identically -- not as a
recommendation and not as this project's view. `analyze()` returns TradingView's
own label alongside the number so the reader sees exactly what the widget says,
with the caveat attached in the report.

STABILITY

These endpoints are undocumented. They have been stable for years (the widely-used
`tradingview-ta` library depends on the same ones) but could change without notice,
like the House PDF parser. Every failure path returns None; the dossier omits the
section rather than erroring.
"""
from __future__ import annotations

import requests

import termstyle

SCANNER_URL = "https://scanner.tradingview.com/symbol"
SEARCH_URL = "https://symbol-search.tradingview.com/symbol_search/"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Origin": "https://www.tradingview.com"}
_TIMEOUT = 15

FIELDS = (
    "Recommend.All", "Recommend.MA", "Recommend.Other",
    "RSI", "MACD.macd", "MACD.signal", "ADX",
    "close", "SMA50", "SMA200",
    "Perf.1M", "Perf.3M", "Perf.YTD", "Perf.Y",
    "Volatility.D", "price_earnings_ttm", "earnings_per_share_basic_ttm",
    "beta_1_year",
)

# TradingView's own thresholds for the -1..+1 gauge, symmetric about zero:
# |x| >= 0.5 is "Strong", |x| >= 0.1 is a direction, the middle is "Neutral".

# When symbol search returns the same company on several venues, the primary
# listing carries the full indicator set; regional exchanges (Dusseldorf, Munich,
# US OTC) often return nothing from the free scanner. Prefer these.
_PRIMARY_EXCHANGES = ("NASDAQ", "NYSE", "AMEX", "XETR", "LSE", "EURONEXT", "SIX",
                       "OMXSTO", "OSL", "TSX", "ASX", "HKEX", "FWB")


def _looks_like_isin(text: str) -> bool:
    t = (text or "").strip()
    return len(t) == 12 and t[:2].isalpha() and t[2:].isalnum()


def resolve_symbol(query: str, session: requests.Session | None = None) -> str | None:
    """A bare ticker or an ISIN -> "EXCHANGE:SYMBOL", or None.

    TradingView's symbol search resolves both, so this is the one place in the
    research tool where an ISIN gets you real market data rather than a skip.
    """
    session = session or requests.Session()
    try:
        resp = session.get(SEARCH_URL, params={"text": query.strip(), "type": "stock"},
                           headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        results = resp.json()
    except (requests.RequestException, ValueError):
        return None
    def clean(row):
        exch, sym = row.get("exchange"), row.get("symbol")
        if not exch or not sym:
            return None
        # The search marks up matched substrings with <em> tags.
        return f"{exch}:{sym.replace('<em>', '').replace('</em>', '')}"

    candidates = [c for c in (clean(r) for r in (results or [])) if c]
    if not candidates:
        return None
    # Primary listings first; within that, search order.
    candidates.sort(key=lambda c: c.split(":")[0] not in _PRIMARY_EXCHANGES)
    return candidates[0]


def fetch_snapshot(query: str, session: requests.Session | None = None) -> dict | None:
    """Raw scanner fields for a ticker or ISIN, or None if it can't be resolved or
    fetched. The returned dict is field name -> value, exactly as TradingView gives
    it (numbers, or None for a field with no value)."""
    session = session or requests.Session()
    symbol = query.strip().upper()
    if _looks_like_isin(symbol) or ":" not in symbol:
        resolved = resolve_symbol(symbol, session)
        if not resolved:
            return None
        symbol = resolved
    try:
        resp = session.get(SCANNER_URL,
                           params={"symbol": symbol, "fields": ",".join(FIELDS), "no_404": "true"},
                           headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(data, dict) or not data:
        return None
    data["_symbol"] = symbol
    return data


def _gauge_label(value: float | None) -> str | None:
    if value is None:
        return None
    if value >= 0.5:
        return "Strong Buy"
    if value >= 0.1:
        return "Buy"
    if value > -0.1:
        return "Neutral"
    if value > -0.5:
        return "Sell"
    return "Strong Sell"


def analyze(raw: dict | None) -> dict | None:
    """Turn a scanner snapshot into a structured view, or None if there is nothing
    usable in it.

    Every derived reading is a plain statement of indicator state (RSI is above 70,
    price is below both averages, MACD is below its signal line). The gauge carries
    TradingView's own label and nothing more."""
    if not raw:
        return None
    g = raw.get

    rsi = g("RSI")
    macd, signal = g("MACD.macd"), g("MACD.signal")
    close, sma50, sma200 = g("close"), g("SMA50"), g("SMA200")
    rec_all = g("Recommend.All")

    if rec_all is None and rsi is None and close is None:
        return None

    rsi_state = None
    if rsi is not None:
        rsi_state = ("перекуплен (RSI > 70)" if rsi >= 70
                     else "перепродан (RSI < 30)" if rsi <= 30 else None)

    macd_state = None
    if macd is not None and signal is not None:
        macd_state = "MACD выше сигнальной" if macd > signal else "MACD ниже сигнальной"

    ma_state = None
    if close is not None and sma50 is not None and sma200 is not None:
        if close > sma50 and close > sma200:
            ma_state = "цена выше и 50-, и 200-дневной средней"
        elif close < sma50 and close < sma200:
            ma_state = "цена ниже и 50-, и 200-дневной средней"
        else:
            ma_state = "цена между 50- и 200-дневной средней"

    return {
        "symbol": raw.get("_symbol"),
        "gauge": rec_all,
        "gauge_label": _gauge_label(rec_all),
        "gauge_ma": g("Recommend.MA"),
        "gauge_osc": g("Recommend.Other"),
        "rsi": rsi,
        "rsi_state": rsi_state,
        "macd_state": macd_state,
        "ma_state": ma_state,
        "adx": g("ADX"),
        "volatility_d": g("Volatility.D"),
        "perf": {k: g(f"Perf.{k}") for k in ("1M", "3M", "YTD", "Y")},
        "pe_ttm": g("price_earnings_ttm"),
        "eps_ttm": g("earnings_per_share_basic_ttm"),
        "beta": g("beta_1_year"),
    }


def format_signal_note(view: dict | None) -> str | None:
    """A single compact line for attaching to an already-dense signal message --
    the short form of format_view(), sized to fit as one extra line rather than
    stand as its own dossier section. None on no usable reading, same as every
    other function here."""
    if not view or view["gauge"] is None:
        return None
    bits = [f"{view['gauge']:+.2f} ({view['gauge_label']})"]
    if view["rsi_state"]:
        bits.append(view["rsi_state"])
    return ("TradingView " + " · ".join(bits)
            + " — механический индикатор, не мнение бота")


def annotate_signals(signals: list, session: requests.Session | None = None) -> list:
    """Attach a compact TradingView read to each signal's `market_note`
    attribute (None on any failure to resolve or fetch -- a missing note is not
    an error, same policy as every other network step in this project).

    Meant to run on a short, already-filtered list of signals only: each one
    costs one or two live requests to TradingView's scanner, so running this
    over an unfiltered signal list would multiply a daily run's network cost by
    however many hundred signals came out of the finders.
    """
    session = session or requests.Session()
    for sig in signals:
        ticker = getattr(sig, "ticker", None)
        note = None
        if ticker:
            try:
                note = format_signal_note(analyze(fetch_snapshot(ticker, session)))
            except Exception:
                note = None
        sig.market_note = note
    return signals


def format_view(view: dict | None) -> str:
    if not view:
        return ""
    L = ["\n" + termstyle.section("TradingView (технические индикаторы)")]
    if view["gauge"] is not None:
        parts = [f"Технический рейтинг TradingView: {view['gauge']:+.2f} "
                 f"по их шкале −1…+1 (они называют это «{view['gauge_label']}»)"]
        if view["gauge_ma"] is not None and view["gauge_osc"] is not None:
            parts.append(f"    скользящие средние {view['gauge_ma']:+.2f} · "
                         f"осцилляторы {view['gauge_osc']:+.2f}")
        L += [f"  {parts[0]}"] + parts[1:]
    readings = [r for r in (view["rsi_state"], view["macd_state"], view["ma_state"]) if r]
    if view["rsi"] is not None:
        readings.insert(0, f"RSI {view['rsi']:.0f}")
    if view["adx"] is not None:
        readings.append(f"ADX {view['adx']:.0f}")
    if readings:
        L.append("  " + " · ".join(readings))
    p = view["perf"]
    perf = [f"{k} {v:+.1f}%" for k, v in p.items() if v is not None]
    if perf:
        L.append("  Доходность: " + " · ".join(perf))
    fund = []
    if view["pe_ttm"] is not None:
        fund.append(f"P/E {view['pe_ttm']:.1f}")
    if view["eps_ttm"] is not None:
        fund.append(f"EPS ttm {view['eps_ttm']:.2f}")
    if view["beta"] is not None:
        fund.append(f"β {view['beta']:.2f}")
    if view["volatility_d"] is not None:
        fund.append(f"дневная волатильность {view['volatility_d']:.1f}%")
    if fund:
        L.append("  " + " · ".join(fund))
    L.append("  Технический рейтинг TradingView, а не мнение бота.")
    return "\n".join(L)
