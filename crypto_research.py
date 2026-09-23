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
    # Prices come from the history chain (Yahoo first, when it agrees with an exchange
    # spot); the exchanges-first current-price chain only fills in when that fails.
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
