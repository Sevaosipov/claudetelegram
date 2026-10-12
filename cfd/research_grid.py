"""cfd/research_grid.py: what «sequence trading» does (the averaging-down grid of the Knockoutforex white paper
the user sent) on the hourly EURUSD and gold history. An illustration with fixed, simple settings -- not a
search: no stop on a trade, a further trade every half a day's range against the basket, the whole basket
closed at a small profit, the broker's margin as the only limit."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from cfd import research_day as rd
from cfd.data import Bar

START = 10_000.0                 # the account; results are read as multiples of it
STOP_OUT = 0.5                   # the broker closes everything when equity falls to half the margin in use


@dataclass
class Outcome:
    name: str
    wins: int = 0
    stop_outs: list = field(default_factory=list)     # (date, equity before, equity after)
    final: float = START
    worst_drawdown: float = 0.0   # of the equity before the sequence, marked to the worst price
    longest_days: int = 0
    deepest_levels: int = 0
    first_year: int = 0
    last_year: int = 0


def run(name: str, bars: list[Bar], *, target: float, grow: float, cost: float, night_pct: float,
        leverage: float, step_adr: float = 0.5) -> Outcome:
    """`target`: the share of equity a sequence aims at, and what the first trade makes over one step;
    `grow`: each further trade's size against the one before; `cost`: the round trip in price units."""
    adr = rd.daily_range(bars)
    out = Outcome(name, first_year=bars[0].ts.year, last_year=bars[-1].ts.year)
    equity, side, basket, step, unit, opened, last_day = START, 1, [], 0.0, 0.0, None, None
    for b in bars:
        if equity <= START * 0.01:
            break                                             # nothing left to trade
        a = adr.get(b.ts.date())
        if not basket:
            if not a or b.ts.weekday() >= 5:
                continue
            step, unit = step_adr * a, target * equity / (step_adr * a)
            basket, opened, last_day, paid = [(b.open, unit)], b.ts, b.ts.date(), cost * unit
            continue
        if b.ts.date() != last_day:                           # a night's financing on what is open
            nights = (b.ts.date() - last_day).days
            paid += nights * night_pct / 100.0 * sum(e * u for e, u in basket)
            last_day = b.ts.date()
        adverse, fav = (b.low, b.high) if side > 0 else (b.high, b.low)
        pnl = lambda p: side * sum(u * (p - e) for e, u in basket) - paid      # noqa: E731
        margin = lambda: sum(e * u for e, u in basket) / leverage              # noqa: E731
        # further trades, each a step beyond the last, while the margin allows
        while side * (basket[-1][0] - adverse) >= step:
            price = basket[-1][0] - side * step
            price = min(price, b.open) if side > 0 else max(price, b.open)     # a gap: at the open
            size = basket[-1][1] * grow
            if (sum(e * u for e, u in basket) + price * size) / leverage > equity + pnl(price):
                break                                                           # no free margin: stalled
            basket.append((price, size))
            paid += cost * size
        out.deepest_levels = max(out.deepest_levels, len(basket))
        worst = equity + pnl(adverse)
        out.worst_drawdown = max(out.worst_drawdown, 1 - worst / equity)
        if worst <= STOP_OUT * margin():                       # the broker's stop-out
            out.stop_outs.append((b.ts.date(), round(equity), round(max(worst, 0.0))))
            equity, basket, side = max(worst, 0.0), [], -side
            continue
        units = sum(u for _e, u in basket)
        goal = sum(e * u for e, u in basket) / units + side * (target * equity + paid) / units
        if side * (fav - goal) >= 0:
            equity += target * equity
            out.wins += 1
            out.longest_days = max(out.longest_days, (b.ts - opened).days)
            basket, side = [], -side
    out.final = equity + (side * sum(u * (bars[-1].close - e) for e, u in basket) - paid if basket else 0.0)
    return out


def report(o: Outcome) -> str:
    years = o.last_year - o.first_year + 1
    first = o.stop_outs[0] if o.stop_outs else None
    return (f"{o.name:<34} won {o.wins:>4}  stop-outs {len(o.stop_outs):>2}  "
            f"first {first[0].isoformat() if first else '   never  '}  final x{o.final / START:>7.2f}  "
            f"worst drawdown {o.worst_drawdown:>4.0%}  longest {o.longest_days:>4}d  deepest {o.deepest_levels:>2} trades  "
            f"({years}y)")


def main(log=print) -> list[Outcome]:
    from cfd import research_gold
    markets = (("EURUSD", rd.hourly(), 0.8e-4, 0.010, 30.0), ("XAUUSD", research_gold.load_gold(), 0.30, 0.020, 20.0))
    outs = []
    for market, bars, cost, night, lev in markets:
        for target in (0.01, 0.0025):
            for grow in (1.0, 1.5):
                o = run(f"{market} target {target:.2%} size x{grow}", bars, target=target, grow=grow, cost=cost,
                        night_pct=night, leverage=lev)
                outs.append(o)
                log(report(o))
                for d, before, after in o.stop_outs[:4]:
                    log(f"    stop-out {d}: {before} -> {after}")
    return outs


if __name__ == "__main__":
    main()
