"""cfd/sizing.py: the lot size of a CFD signal for the user's account (IC Markets, MetaTrader 5): the lots
at which a trade stopped out loses the euros the user set with /risk. Nothing here trades.

    lots = risk in euros / (the stop's distance x the contract's size, in euros)

rounded down to the 0.01 a broker takes. When even 0.01 lot risks more than the setting, 0.01 is shown with
what it risks, so the user sees it is over."""
from __future__ import annotations

import math

import db
import fx
from cfd import instruments as ins

RISK_KEY = "cfd_trade_risk_eur"
_FOREVER = 100 * 365 * 24 * 3600
STEP = 0.01
# Units of the instrument in one lot at IC Markets (MetaTrader 5 «contract size»).
CONTRACT = {ins.FX_MAJOR: 100_000, ins.FX_CROSS: 100_000, ins.METALS: 100, ins.INDICES: 1, ins.CRYPTO: 1}
CONTRACT_BY_NAME = {"XAGUSD": 5_000}


def risk_eur(conn) -> float | None:
    value = db.get_cached_value(conn, RISK_KEY, _FOREVER)
    return float(value) if value else None


def contract(name: str) -> float | None:
    if name in CONTRACT_BY_NAME:
        return CONTRACT_BY_NAME[name]
    try:
        return CONTRACT.get(ins.by_name(name).klass)
    except KeyError:
        return None


def lots(conn, name: str, entry: float, stop: float) -> str | None:
    """The message's «Lots» value for a trade of `name` from `entry` with its stop at `stop`: «0.12», or
    «0.01 = €14» when the smallest lot risks more than the setting; None when /risk is not set or the
    instrument's contract is not known."""
    risk, size = risk_eur(conn), contract(name)
    distance = abs(entry - stop)
    if not risk or not size or not distance:
        return None
    try:
        quote = ins.by_name(name).quote
    except KeyError:
        return None
    per_lot = fx.to_eur(distance * size, quote, conn)             # what one lot loses at the stop, in euros
    if per_lot <= 0:
        return None
    n = math.floor(risk / per_lot / STEP + 1e-9) * STEP
    if n < STEP:
        return f"{STEP:.2f} = €{per_lot * STEP:.0f}"
    return f"{n:.2f}"


def handle_command(conn, text: str) -> str:
    args = text.split()[1:]
    if not args:
        risk = risk_eur(conn)
        return (f"Риск на CFD-сделку: €{risk:g}. В сигналах SK и лиги есть строка Lots." if risk
                else "Риск на CFD-сделку не задан. /risk 10 — и в сигналах появится размер в лотах.")
    if args[0].lower() == "off":
        db.save_cached_value(conn, RISK_KEY, 0.0)
        return "Размер в лотах больше не показываю."
    try:
        value = float(args[0].replace(",", ".").lstrip("€"))
    except ValueError:
        return "Команды: /risk, /risk 10, /risk off."
    if not 0 < value <= 100_000:
        return "Риск — число евро больше нуля, например /risk 10."
    db.save_cached_value(conn, RISK_KEY, value)
    return f"Риск на CFD-сделку: €{value:g}. В новых сигналах будет строка Lots."
