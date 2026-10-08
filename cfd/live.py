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
from dataclasses import dataclass, fields

import db

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
