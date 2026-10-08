"""cfd/live.py: the experimental live CFD signals (spec 2026-10-08-cfd-live-crypto-breakout.md).

What goes live is the breakout BO-D with exit E0 -- exactly what the research tested, through the same
code (setups.bo_d, exits.LadderState) -- on 13 coins, on daily bars. TP1..TP4 are checkpoints, not exits:
nothing closes early, the position leaves at the trailing stop only. The bot never places an order; it
sends messages, and keeps a row per signal in `cfd_signals` so that the next daily run goes on from where
this one stopped.

This file holds, in order: the settings and the table (kv_cache and cfd_signals), the tracker (feed the
completed bars after `last_bar` to a stored signal, one at a time), the scanner (is the last completed bar
a signal bar) with the sizing, and the daily pass that ties them to Telegram.
"""
from __future__ import annotations

import datetime as dt
import math
import sys
from collections.abc import Callable
from dataclasses import dataclass, fields

import db
import fx
from cfd import data, exits
from cfd import indicators as ind
from cfd import instruments as ins
from cfd import setups
from cfd.data import Bar

# ------------------------------------------------------------------ settings (kv_cache)
KV_KEYS = {"balance": "cfd_balance_eur", "risk": "cfd_risk_pct", "maxrisk": "cfd_max_open_risk_pct",
           "paused": "cfd_paused"}
DEFAULT_RISK_PCT = 1.0
DEFAULT_MAX_OPEN_RISK_PCT = 3.0
_FOREVER = 100 * 365 * 24 * 3600        # a setting does not expire


@dataclass(frozen=True)
class Settings:
    balance_eur: float | None = None     # None: no balance set
    risk_pct: float = DEFAULT_RISK_PCT
    max_open_risk_pct: float = DEFAULT_MAX_OPEN_RISK_PCT
    paused: bool = False


def get_settings(conn) -> Settings:
    def value(name):
        return db.get_cached_value(conn, KV_KEYS[name], _FOREVER)

    balance, risk, maxrisk, paused = (value(n) for n in ("balance", "risk", "maxrisk", "paused"))
    return Settings(balance_eur=balance if balance else None,
                    risk_pct=DEFAULT_RISK_PCT if risk is None else float(risk),
                    max_open_risk_pct=DEFAULT_MAX_OPEN_RISK_PCT if maxrisk is None else float(maxrisk),
                    paused=bool(paused))


def set_setting(conn, name: str, value) -> None:
    """Keep one setting: «balance» (None: no balance), «risk», «maxrisk» or «paused» (a bool)."""
    if name not in KV_KEYS:
        raise ValueError(f"unknown setting {name!r}")
    db.save_cached_value(conn, KV_KEYS[name], 0.0 if value is None else float(value))


# ------------------------------------------------------------------ the table
@dataclass
class Signal:
    """A row of cfd_signals."""
    id: int
    coin: str
    symbol: str
    side: str
    signal_date: str
    entry: float
    stop0: float
    stop: float
    r: float
    checkpoint: int
    last_bar: str
    status: str
    closed_date: str | None
    exit_price: float | None
    result_r: float | None
    risk_pct: float | None
    risk_eur: float | None
    qty: float | None
    created: str


_COLUMNS = [f.name for f in fields(Signal)]
_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM cfd_signals"


def _signals(conn, where: str = "", params=()) -> list[Signal]:
    return [Signal(*row) for row in conn.execute(f"{_SELECT} {where} ORDER BY id", params)]


def insert_signal(conn, *, coin: str, symbol: str, side: str, signal_date: str, entry: float,
                  stop0: float, r: float, last_bar: str, risk_pct: float | None = None,
                  risk_eur: float | None = None, qty: float | None = None) -> int:
    """A new open signal, its stop at the initial stop. Raises sqlite3.IntegrityError for a coin that
    already has a signal on that date."""
    cur = conn.execute(
        """INSERT INTO cfd_signals (coin, symbol, side, signal_date, entry, stop0, stop, r, checkpoint,
                                    last_bar, status, risk_pct, risk_eur, qty, created)
           VALUES (?,?,?,?,?,?,?,?,0,?,'open',?,?,?,?)""",
        (coin, symbol, side, signal_date, entry, stop0, stop0, r, last_bar, risk_pct, risk_eur, qty,
         dt.datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    return cur.lastrowid


def get_signal(conn, signal_id: int) -> Signal | None:
    rows = _signals(conn, "WHERE id = ?", (signal_id,))
    return rows[0] if rows else None


def open_signals(conn) -> list[Signal]:
    return _signals(conn, "WHERE status = 'open'")


def closed_signals(conn) -> list[Signal]:
    return _signals(conn, "WHERE status = 'closed'")


def all_signals(conn) -> list[Signal]:
    return _signals(conn)


def has_signal(conn, coin: str, signal_date: str) -> bool:
    return conn.execute("SELECT 1 FROM cfd_signals WHERE coin = ? AND signal_date = ?",
                        (coin, signal_date)).fetchone() is not None


_UPDATABLE = {"stop", "checkpoint", "last_bar", "status", "closed_date", "exit_price", "result_r"}


def update_signal(conn, signal_id: int, **changes) -> None:
    """Change the named columns of a row (the progress of a signal), and nothing else."""
    bad = set(changes) - _UPDATABLE
    if bad:
        raise ValueError(f"cannot update {sorted(bad)}")
    if not changes:
        return
    sets = ", ".join(f"{name} = ?" for name in changes)
    conn.execute(f"UPDATE cfd_signals SET {sets} WHERE id = ?", (*changes.values(), signal_id))
    conn.commit()


# ================================================================ the data
COINS: tuple[ins.Instrument, ...] = (ins.by_symbol("BTC-USD"), ins.by_symbol("ETH-USD")) + ins.ALT_COINS
CHECKPOINTS = 4                  # TP1..TP4 = +1R..+4R: marks on the way, never exits
ATR_LEN = 14


def coin_of(inst: ins.Instrument) -> str:
    """SOL of SOLUSD: the name a coin goes by in the messages and in the table."""
    return inst.name.removesuffix("USD")


class Feed:
    """The daily bars of the coins for one pass, fetched once per symbol through cfd.data's seam
    (`fetch(symbol, interval) -> raw rows`; the default is Yahoo). `bars(symbol)` are the completed
    ones, cleaned at the coins' 60 % wick threshold; `forming_open(symbol)` is the open of the bar of
    `today` (UTC), still forming, or None -- the reference entry of a signal made at yesterday's close."""

    def __init__(self, fetch: data.Fetch | None = None, today: dt.date | None = None):
        self._fetch = fetch
        self.today = today or data._today()
        self._loaded: dict[str, tuple[list[Bar], float | None]] = {}

    def _load(self, symbol: str) -> tuple[list[Bar], float | None]:
        if symbol not in self._loaded:
            rows = (self._fetch or data.yahoo_fetch)(symbol, data.DAILY)
            cleaned = data.clean_rows(rows, data.DAILY, max_wick=data.MAX_WICK_CRYPTO).bars
            done = [b for b in cleaned if b.ts.date() < self.today]
            forming = next((b.open for b in cleaned if b.ts.date() == self.today), None)
            self._loaded[symbol] = (done, forming)
        return self._loaded[symbol]

    def bars(self, symbol: str) -> list[Bar]:
        return self._load(symbol)[0]

    def forming_open(self, symbol: str) -> float | None:
        return self._load(symbol)[1]


# ================================================================ the tracker
@dataclass(frozen=True)
class Notice:
    """One event to tell the user about. `kind` is "entry", "checkpoint" or "close"; `signal` the row as
    it is at that moment. A checkpoint carries its number `k`, its price `level` and the `stop` in force
    after the bar; a close the `price` it was closed at, its `result_r` after the costs, whether the stop
    had `trailed` away from the initial one and the `date` of the bar. An entry carries, when the sizing
    asks for it, `risk_pct` (the percent the message names), `qty_per_1000` (the quantity for a €1 000
    balance, with no balance set) and `over_limit` (the open risk including this signal, and the limit it
    is above)."""
    kind: str
    signal: Signal
    k: int | None = None
    level: float | None = None
    stop: float | None = None
    price: float | None = None
    result_r: float | None = None
    trailed: bool = False
    date: str | None = None
    qty_per_1000: float | None = None
    over_limit: tuple[float, float] | None = None
    risk_pct: float | None = None


@dataclass(frozen=True)
class BarEvent:
    kind: str                        # "checkpoint" | "close"
    k: int | None = None
    level: float | None = None
    price: float | None = None


def advance(side: str, entry: float, stop0: float, stop: float, checkpoint: int, bar: Bar,
            atr: float | None) -> tuple[list[BarEvent], float, bool]:
    """One completed bar of an open signal: (events in order, the stop after the bar, closed).

    The position is exits.LadderState under E0 -- the backtest's own code, resumed at `stop`. Inside the
    bar the stop goes before a new checkpoint (the backtest's bar order), unless the bar opened beyond the
    checkpoint, which was then reached at the open. The trailing stop moves at the end of the bar and is
    effective from the next one."""
    state = exits.LadderState(side, entry, stop0, exits.E0, stop=stop)
    fills = state.step(bar, atr)
    sign = 1 if side == "long" else -1
    best = bar.high if side == "long" else bar.low
    events: list[BarEvent] = []
    for k in range(checkpoint + 1, CHECKPOINTS + 1):
        level = state.level(k)
        if sign * best < sign * level:
            break
        if state.closed and sign * bar.open < sign * level:
            break
        events.append(BarEvent("checkpoint", k=k, level=level))
    if state.closed:
        events.append(BarEvent("close", price=fills[0].price))
    return events, state.stop, state.closed


def result_r(side: str, entry: float, stop0: float, exit_price: float, entry_date: dt.date,
             exit_date: dt.date, costs: ins.Costs) -> float:
    """The result of a closed signal in R after the pre-registered costs, as exits.simulate computes it:
    the move over R, less the round trip and a financing charge per night as a percent of the entry."""
    distance = abs(entry - stop0)
    sign = 1 if side == "long" else -1
    gross = sign * (exit_price - entry) / distance
    nights = max(0, (exit_date - entry_date).days)
    cost_pct = costs.round_trip_pct + nights * costs.financing_pct(side)
    return gross - cost_pct / 100.0 * entry / distance


Send = Callable[[Notice], bool]


def track_signal(conn, sig: Signal, bars: list[Bar], send: Send) -> int:
    """Feed an open signal the completed `bars` after its `last_bar`, in order, and tell each event with
    `send(notice) -> bool`. The row is saved only after the message of what it records went out: a
    checkpoint is saved when sent; the stop and `last_bar` when the bar is done; the close, with its exit,
    when its message is sent. A refused send ends the work for this signal and leaves the row as it was,
    so the next run repeats that event and nothing before it. `sig` is updated in place. Returns the
    number of messages sent."""
    if sig.status != "open":
        return 0
    atr = ind.atr(bars, ATR_LEN)
    costs = ins.by_symbol(sig.symbol).costs
    entry_date = dt.date.fromisoformat(sig.signal_date) + dt.timedelta(days=1)
    sent = 0
    for i, bar in enumerate(bars):
        day = bar.ts.date().isoformat()
        if day <= sig.last_bar:
            continue
        events, stop, closed = advance(sig.side, sig.entry, sig.stop0, sig.stop, sig.checkpoint, bar, atr[i])
        for ev in events:
            if ev.kind == "checkpoint":
                notice = Notice("checkpoint", sig, k=ev.k, level=ev.level, stop=stop, date=day)
                if not send(notice):
                    return sent
                sent += 1
                update_signal(conn, sig.id, checkpoint=ev.k)
                sig.checkpoint = ev.k
                continue
            sign = 1 if sig.side == "long" else -1
            res = result_r(sig.side, sig.entry, sig.stop0, ev.price, entry_date,
                           bar.ts.date(), costs)
            notice = Notice("close", sig, price=ev.price, result_r=res, date=day,
                            trailed=sign * (sig.stop - sig.stop0) > 0)
            if not send(notice):
                return sent
            sent += 1
            update_signal(conn, sig.id, status="closed", closed_date=day, exit_price=ev.price,
                          result_r=res, last_bar=day)
            sig.status, sig.closed_date, sig.exit_price, sig.result_r, sig.last_bar = (
                "closed", day, ev.price, res, day)
            return sent
        update_signal(conn, sig.id, stop=stop, last_bar=day)
        sig.stop, sig.last_bar = stop, day
    return sent


def track_all(conn, send: Send, *, fetch: data.Fetch | None = None, today: dt.date | None = None,
              feed: Feed | None = None) -> int:
    """Track every open signal. One coin failing (its data, its send) is logged and the others go on.
    Returns the number of messages sent."""
    feed = feed or Feed(fetch, today)
    sent = 0
    for sig in open_signals(conn):
        try:
            sent += track_signal(conn, sig, feed.bars(sig.symbol), send)
        except Exception as e:
            print(f"[CFD] {sig.coin}: tracking failed: {type(e).__name__}: {e}", file=sys.stderr)
    return sent


# ================================================================ sizing
def qty_step(price: float) -> float:
    """The smallest step of a quantity, by the price of the coin: 0.0001 above 100, whole coins under 1,
    hundredths from 1 to 100."""
    if price > 100:
        return 0.0001
    if price < 1:
        return 1.0
    return 0.01


def size_qty(risk_eur: float, r_usd: float, price: float, conn=None) -> float:
    """How many coins risk `risk_eur` over a stop `r_usd` away (every coin is quoted in dollars; fx.to_eur
    turns R into euro), rounded down to the step of the price."""
    r_eur = fx.to_eur(r_usd, "USD", conn)
    step = qty_step(price)
    qty = math.floor(risk_eur / r_eur / step + 1e-9) * step
    return round(qty, 4)


def open_risk_pct(conn, settings: Settings) -> float:
    """The risk, in percent of the balance, that the open signals carry: each its own risk_pct, and a
    signal made with no balance set (risk_pct NULL) the current risk percent."""
    return sum(s.risk_pct if s.risk_pct is not None else settings.risk_pct for s in open_signals(conn))


# ================================================================ the scanner
MIN_R_ATR, MAX_R_ATR = exits.MIN_R_ATR, exits.MAX_R_ATR       # the backtest's R rules
REFERENCE_BALANCE = 1000.0                                    # the balance a message without one sizes for


def scan_coin(conn, inst: ins.Instrument, feed: Feed, settings: Settings, send: Send) -> bool:
    """Read the last completed bar of a coin: when it is a BO-D signal bar and the coin has no open
    signal and none for that date, make the signal (sized, checked against the open-risk limit), tell it
    with `send`, and keep the row once the message went out. True when a signal was made."""
    coin = coin_of(inst)
    bars = feed.bars(inst.symbol)
    if not bars:
        return False
    last = bars[-1]
    day = last.ts.date()
    if day != feed.today - dt.timedelta(days=1):
        print(f"[CFD] {coin}: the last completed bar is {day}, not yesterday -- not read", file=sys.stderr)
        return False
    signal = next((s for s in setups.bo_d(bars) if s.index == len(bars) - 1), None)
    if signal is None:
        return False
    if any(s.coin == coin for s in open_signals(conn)) or has_signal(conn, coin, day.isoformat()):
        return False
    forming = feed.forming_open(inst.symbol)
    entry = forming if forming else last.close
    distance = (entry - signal.stop) if signal.side == "long" else (signal.stop - entry)
    if distance <= MIN_R_ATR * signal.atr or distance > MAX_R_ATR * signal.atr:
        print(f"[CFD] {coin}: {signal.side} signal skipped, R {distance:.6g} is outside "
              f"{MIN_R_ATR:g}-{MAX_R_ATR:g} ATR", file=sys.stderr)
        return False

    risk_pct = risk_eur = qty = qty_per_1000 = None
    if settings.balance_eur:
        risk_pct = settings.risk_pct
        risk_eur = round(settings.balance_eur * risk_pct / 100.0, 2)
        qty = size_qty(risk_eur, distance, entry, conn)
    else:
        qty_per_1000 = size_qty(REFERENCE_BALANCE * settings.risk_pct / 100.0, distance, entry, conn)
    total = open_risk_pct(conn, settings) + settings.risk_pct
    over = (total, settings.max_open_risk_pct) if total > settings.max_open_risk_pct else None

    draft = Signal(0, coin, inst.symbol, signal.side, day.isoformat(), entry, signal.stop, signal.stop,
                   distance, 0, day.isoformat(), "open", None, None, None, risk_pct, risk_eur, qty, "")
    if not send(Notice("entry", draft, qty_per_1000=qty_per_1000, over_limit=over,
                     risk_pct=settings.risk_pct)):
        return False
    insert_signal(conn, coin=coin, symbol=inst.symbol, side=signal.side, signal_date=day.isoformat(),
                  entry=entry, stop0=signal.stop, r=distance, last_bar=day.isoformat(),
                  risk_pct=risk_pct, risk_eur=risk_eur, qty=qty)
    return True


def scan_all(conn, send: Send, *, fetch: data.Fetch | None = None, today: dt.date | None = None,
             settings: Settings | None = None, feed: Feed | None = None) -> int:
    """Scan the 13 coins. One coin failing is logged and the others go on. Returns the number of
    signals made."""
    feed = feed or Feed(fetch, today)
    settings = settings or get_settings(conn)
    made = 0
    for inst in COINS:
        try:
            made += scan_coin(conn, inst, feed, settings, send)
        except Exception as e:
            print(f"[CFD] {coin_of(inst)}: scan failed: {type(e).__name__}: {e}", file=sys.stderr)
    return made
