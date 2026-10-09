"""signal_context.py: what a weekly buy signal says besides its score -- what the company does and how it
is valued, and how many euros of it to buy.

Neither changes which signals are picked: the score (model_score.py) is as it was, and these are facts
put next to it.

  * about(): the company's industry, its size and a few ratios (P/E, P/S, revenue growth, margin) from
    Yahoo, as one line. A coin has none.
  * amount_eur(): the position's size from the user's own settings -- the share of the account risked on
    one signal over the signal's stop, and no more than a share of the account -- and the value of the
    Trading 212 account the sync stored. The settings are the user's (/size); the bot never places an order.
"""
from __future__ import annotations

import datetime as dt
import math
import sys

import crypto
import db
import positions
import t212_account

# ------------------------------------------------------------------ the size of a position
KV_KEYS = {"risk": "size_risk_pct", "max": "size_max_pct"}
DEFAULTS = {"risk": 1.0, "max": 10.0}           # percent of the account
LIMITS = {"risk": (0.1, 5.0), "max": (1.0, 100.0)}
_FOREVER = 100 * 365 * 24 * 3600
USAGE = {
    "risk": "/size risk 1 — риск на одну покупку, % счёта: от 0,1 до 5",
    "max": "/size max 10 — не больше этой доли счёта в одной покупке, %: от 1 до 100",
}


def settings(conn) -> dict:
    out = {}
    for name, key in KV_KEYS.items():
        value = db.get_cached_value(conn, key, _FOREVER)
        out[name] = DEFAULTS[name] if value is None else float(value)
    return out


def amount_eur(conn, stop_pct: float | None, today: dt.date) -> float | None:
    """How many euros of a signal to buy: the account's value times the risk percent, over the stop's
    distance (a stop hit then costs that share of the account), and no more than the maximum share.
    None with no stop or no fresh account value in euros."""
    account = t212_account.account_value(conn, today, max_age_days=t212_account.STALE_DAYS)
    if not stop_pct or account is None or not account[0] or (account[1] or "EUR") != "EUR":
        return None
    s = settings(conn)
    return min(account[0] * s["risk"] / 100.0 / stop_pct, account[0] * s["max"] / 100.0)


def _pct(x: float) -> str:
    return f"{x:g}".replace(".", ",")


def settings_text(conn, today: dt.date | None = None) -> str:
    """«Риск 1% счёта на покупку, не больше 10% счёта в одной. Сейчас: счёт €192 — при стопе −10% это €19.»"""
    import telegram_notify
    s = settings(conn)
    text = f"Риск {_pct(s['risk'])}% счёта на покупку, не больше {_pct(s['max'])}% счёта в одной."
    today = today or dt.date.today()
    account = t212_account.account_value(conn, today, max_age_days=t212_account.STALE_DAYS)
    example = amount_eur(conn, 0.10, today)
    if account and example is not None:
        text += (f" Сейчас: счёт {telegram_notify.money(account[0])} — при стопе −10% это "
                 f"{telegram_notify.money(example)}.")
    return text


def handle_command(conn, text: str) -> str:
    """The answer to «/size ...»: no argument -- the settings; «risk X» or «max X» -- validate, keep and
    answer with the settings; anything else -- the usage."""
    args = text.split()[1:]
    if not args:
        return "\n".join([settings_text(conn), *USAGE.values()])
    name = args[0].lower()
    if name not in KV_KEYS or len(args) != 2:
        return "\n".join(USAGE.values())
    try:
        value = float(args[1].replace(",", ".").rstrip("%"))
    except ValueError:
        return USAGE[name]
    low, high = LIMITS[name]
    if not math.isfinite(value) or not low <= value <= high:
        return USAGE[name]
    db.save_cached_value(conn, KV_KEYS[name], value)
    return settings_text(conn)


# ------------------------------------------------------------------ the company
def yahoo_info(symbol: str) -> dict:
    """Yahoo's summary of a company (yfinance Ticker.info). The network seam."""
    import yfinance as yf
    return yf.Ticker(symbol).info or {}


def _num(info: dict, key: str) -> float | None:
    value = info.get(key)
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None


def _cap(x: float, currency: str | None) -> str:
    sign = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency or "USD", "")
    for size, name in ((1e12, "трлн"), (1e9, "млрд"), (1e6, "млн")):
        if x >= size:
            number = f"{x / size:.1f}".replace(".", ",")
            return f"{sign}{number} {name}" + ("" if sign else f" {currency}")
    return f"{sign}{x:,.0f}".replace(",", " ")


def _ratio(x: float) -> str:
    return (f"{x:.0f}" if x >= 20 else f"{x:.1f}").replace(".", ",")


def _signed(x: float) -> str:
    return f"{x * 100:+.0f}%".replace("-", "−")


def about_line(info: dict) -> str | None:
    """«Trucking (Industrials) · кап. $2,8 млрд · P/E 31 (прогноз 18) · P/S 0,6 · выручка −4% г/г ·
    маржа 1,2%» from Yahoo's summary; what it does not have is left out, a loss is said instead of a
    P/E, and None when there is nothing."""
    parts = []
    industry, sector = info.get("industry"), info.get("sector")
    if industry or sector:
        parts.append(f"{industry} ({sector})" if industry and sector else str(industry or sector))
    cap = _num(info, "marketCap")
    if cap:
        parts.append(f"кап. {_cap(cap, info.get('currency'))}")
    pe, forward, eps = _num(info, "trailingPE"), _num(info, "forwardPE"), _num(info, "trailingEps")
    if pe and pe > 0:
        parts.append(f"P/E {_ratio(pe)}" + (f" (прогноз {_ratio(forward)})" if forward and forward > 0 else ""))
    elif eps is not None and eps < 0:
        parts.append("убыточна" + (f" (прогноз P/E {_ratio(forward)})" if forward and forward > 0 else ""))
    elif forward and forward > 0:
        parts.append(f"прогноз P/E {_ratio(forward)}")
    ps = _num(info, "priceToSalesTrailing12Months")
    # Yahoo divides a price in one currency by sales reported in another (Equinor: NOK over USD)
    same_currency = info.get("financialCurrency") in (None, info.get("currency"))
    if ps and ps > 0 and same_currency:
        parts.append(f"P/S {_ratio(ps)}")
    growth = _num(info, "revenueGrowth")
    if growth is not None:
        parts.append(f"выручка {_signed(growth)} г/г")
    margin = _num(info, "profitMargins")
    if margin is not None:
        parts.append(f"маржа {margin * 100:.1f}%".replace(".", ",").replace("-", "−"))
    return " · ".join(parts) if parts else None


def about(ticker: str, source: str | None = None, *, info_fn=None) -> str | None:
    """about_line of a stock's listing; None for a coin, a ticker with no Yahoo symbol or a failed
    fetch (logged)."""
    if crypto.is_crypto(ticker):
        return None
    symbol = positions.yahoo_symbol(ticker, source)
    if not symbol:
        return None
    try:
        return about_line((info_fn or yahoo_info)(symbol))
    except Exception as e:
        print(f"[signal_context] {ticker}: no company summary: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def enrich(conn, pick: dict, today: dt.date, *, info_fn=None) -> dict:
    """A pick (signals_weekly.pick_record) with what its message adds: `about` (the company line) and
    `amount_eur` (the size), each only when there is one."""
    line = about(pick["ticker"], pick.get("source"), info_fn=info_fn)
    if line:
        pick["about"] = line
    amount = amount_eur(conn, pick.get("stop_pct"), today)
    if amount is not None:
        pick["amount_eur"] = round(amount, 2)
    return pick
