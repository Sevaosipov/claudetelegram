"""cfd/mt5_bridge.py: the user's MetaTrader 5 terminal as a price source. mt5/BotBridge.mq5, attached to a
chart there, writes bars and quotes to CSV files; this module reads them. Nothing here talks to the broker
and nothing can trade.

`fetch_or(fallback)` is a `fetch(symbol, interval)` for cfd.data: the terminal's bars while its files are
fresh, the fallback (Yahoo) otherwise -- the terminal shut, the market closed, the bridge not installed."""
from __future__ import annotations

import csv
import datetime as dt
import glob
import os
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from cfd.data import Bar

UTC = dt.timezone.utc
NEW_YORK = ZoneInfo("America/New_York")
SERVER_AHEAD_OF_NEW_YORK = dt.timedelta(hours=7)     # IC Markets' server clock: its day ends at New York's 17:00
NAMES = {"EURUSD=X": "EURUSD", "GC=F": "XAUUSD", "GBPUSD=X": "GBPUSD", "USDJPY=X": "USDJPY"}
FRAMES = {"15m": "M15", "1h": "H1", "1d": "D1"}
MAX_AGE = 180                    # seconds: an older `recent` file means the terminal is not running
_MAC = "~/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/users/*/AppData/Roaming/" \
       "MetaQuotes/Terminal/Common/Files/botbridge"


def bridge_dir() -> Path | None:
    """Where the terminal writes: MT5_BRIDGE_DIR, else the common-files folder of MetaTrader 5 for macOS."""
    named = os.environ.get("MT5_BRIDGE_DIR")
    if named:
        return Path(named).expanduser()
    found = sorted(glob.glob(os.path.expanduser(_MAC)))
    return Path(found[0]) if found else None


def server_to_utc(stamp: str) -> dt.datetime:
    """A server-clock stamp («2026.10.12 12:00») in UTC: the server runs seven hours ahead of New York the
    year round (UTC+3 in the US summer, UTC+2 in its winter)."""
    naive = dt.datetime.strptime(stamp.strip(), "%Y.%m.%d %H:%M")
    return (naive - SERVER_AHEAD_OF_NEW_YORK).replace(tzinfo=NEW_YORK).astimezone(UTC)


def _read(path: Path) -> list[tuple]:
    out = []
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            try:
                out.append((server_to_utc(row["time"]), float(row["open"]), float(row["high"]),
                            float(row["low"]), float(row["close"]), int(row.get("spread_points") or 0)))
            except (KeyError, ValueError):
                continue
    return out


def rows(symbol: str, interval: str, *, kind: str = "recent", folder: Path | None = None,
         now: float | None = None) -> list[tuple] | None:
    """Raw rows (ISO UTC time, open, high, low, close) of a Yahoo symbol from the terminal's file; None when
    the bridge has none, or its `recent` file is stale."""
    folder = folder or bridge_dir()
    name, frame = NAMES.get(symbol), FRAMES.get(interval)
    if not folder or not name or not frame:
        return None
    path = folder / f"{name}_{frame}_{kind}.csv"
    try:
        if kind == "recent" and (now or time.time()) - path.stat().st_mtime > MAX_AGE:
            return None
        got = _read(path)
        minutes = {"15m": 15, "1h": 60}.get(interval)
        if kind == "recent" and minutes and got:
            # a fresh file can still hold old bars: the terminal answers from its cache until it has synced
            age = dt.datetime.fromtimestamp(now or time.time(), UTC) - got[-1][0]
            if age > dt.timedelta(minutes=3 * minutes):
                return None
        return [(ts.isoformat(), o, h, low, c) for ts, o, h, low, c, _spread in got]
    except OSError:
        return None


def fetch_or(fallback):
    """A cfd.data fetch: the terminal's fresh bars, else `fallback(symbol, interval)`."""
    def fetch(symbol: str, interval: str):
        got = rows(symbol, interval)
        return got if got else fallback(symbol, interval)
    return fetch


def history(name: str, frame: str, folder: Path | None = None) -> list[Bar]:
    """The bars of the terminal's history file of `name` («XAUUSD») and `frame` («H1»), oldest first."""
    folder = folder or bridge_dir()
    if not folder:
        return []
    try:
        return [Bar(ts, o, h, low, c) for ts, o, h, low, c, _s in _read(folder / f"{name}_{frame}_history.csv")]
    except OSError:
        return []


def spread_points(name: str, frame: str, folder: Path | None = None) -> list[int]:
    folder = folder or bridge_dir()
    try:
        return [s for *_bar, s in _read(folder / f"{name}_{frame}_history.csv")] if folder else []
    except OSError:
        return []


# ---------------------------------------------------------------- the account, read only (BotBridge 1.10)
def _table(name: str, folder: Path | None = None) -> list[dict]:
    folder = folder or bridge_dir()
    if not folder:
        return []
    try:
        with (folder / name).open(newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def _when(stamp: str) -> dt.datetime | None:
    try:
        naive = dt.datetime.strptime(stamp.strip(), "%Y.%m.%d %H:%M:%S")
    except ValueError:
        return None
    return (naive - SERVER_AHEAD_OF_NEW_YORK).replace(tzinfo=NEW_YORK).astimezone(UTC)


def _f(row: dict, key: str) -> float:
    try:
        return float(row.get(key) or 0)
    except ValueError:
        return 0.0


def account(folder: Path | None = None) -> dict | None:
    """{balance, equity, currency, ...} of the terminal's account, or None when the bridge has not written it."""
    rows = _table("account.csv", folder)
    if not rows:
        return None
    row = rows[0]
    return {"balance": _f(row, "balance"), "equity": _f(row, "equity"), "margin": _f(row, "margin"),
            "currency": row.get("currency") or "", "at": _when(row.get("server_time") or "")}


def open_positions(folder: Path | None = None) -> list[dict]:
    return [{"symbol": r.get("symbol", ""), "side": r.get("side", ""), "volume": _f(r, "volume"),
             "price": _f(r, "price_open"), "sl": _f(r, "sl"), "tp": _f(r, "tp"),
             "profit": _f(r, "profit") + _f(r, "swap"), "at": _when(r.get("time") or "")}
            for r in _table("positions.csv", folder)]


def closed_trades(folder: Path | None = None) -> list[dict]:
    """The closing deals of the last 90 days: when, the symbol, the lots and the result after commission and
    swap (the commission of the opening deal of the same position is counted in)."""
    rows = _table("deals.csv", folder)
    opening: dict[str, float] = {}
    for r in rows:
        if r.get("entry") == "in":
            opening[r.get("position", "")] = opening.get(r.get("position", ""), 0.0) + _f(r, "commission")
    out = []
    for r in rows:
        if r.get("entry") != "out":
            continue
        net = _f(r, "profit") + _f(r, "commission") + _f(r, "swap") + opening.pop(r.get("position", ""), 0.0)
        out.append({"symbol": r.get("symbol", ""), "side": "buy" if r.get("side") == "sell" else "sell",
                    "volume": _f(r, "volume"), "price": _f(r, "price"), "net": net, "at": _when(r.get("time") or "")})
    return out


def quotes(folder: Path | None = None) -> dict[str, dict]:
    """symbol -> {bid, ask, point} as the terminal last wrote them."""
    return {r.get("symbol", ""): {"bid": _f(r, "bid"), "ask": _f(r, "ask"), "point": _f(r, "point")}
            for r in _table("quotes.csv", folder)}


# ---------------------------------------------------------------- measured costs
COMMISSION_USD_PER_LOT = 7.0     # IC Markets Raw Spread, the round trip
LOT = {"EURUSD": 100_000, "XAUUSD": 100}


MIN_SAMPLES = 20


def sample_spreads(conn, folder: Path | None = None, now: float | None = None) -> int:
    """Store the live spread of every quoted market (mt5_spreads), when the terminal wrote its quotes within
    MAX_AGE seconds and the market is open (a spread above zero). Returns the rows stored."""
    folder = folder or bridge_dir()
    if not folder:
        return 0
    try:
        if (now or time.time()) - (folder / "quotes.csv").stat().st_mtime > MAX_AGE:
            return 0
    except OSError:
        return 0
    stamp = dt.datetime.fromtimestamp(now or time.time(), UTC).isoformat(timespec="seconds")
    rows = [(stamp, name, q["ask"] - q["bid"]) for name, q in quotes(folder).items() if q["ask"] > q["bid"] > 0]
    conn.executemany("INSERT OR IGNORE INTO mt5_spreads (ts, name, spread) VALUES (?,?,?)", rows)
    conn.commit()
    return len(rows)


def measured_cost(name: str, folder: Path | None = None, *, conn=None, hours=(6, 15)) -> dict | None:
    """What a round trip of `name` costs in price units at the user's broker, in `hours` (UTC; 09:00-18:00 in
    Riga is about 6-15): the median of the spreads sampled from the live quote (mt5_spreads) once there are
    MIN_SAMPLES of them -- until then the median of the terminal's 15-minute bars, which record each bar's
    lowest spread and so understate it (`floor` is then True) -- and the commission added. None when the
    bridge has no quote for it."""
    folder = folder or bridge_dir()
    q = quotes(folder).get(name) if folder else None
    if not q or not q["point"] or name not in LOT:
        return None
    samples = []
    if conn is not None:
        samples = sorted(s for ts, s in conn.execute("SELECT ts, spread FROM mt5_spreads WHERE name = ?", (name,))
                         if hours[0] <= dt.datetime.fromisoformat(ts).hour < hours[1])
    floor = len(samples) < MIN_SAMPLES
    if floor:
        try:
            bars = _read(folder / f"{name}_M15_history.csv")
        except OSError:
            bars = []
        samples = sorted(s * q["point"] for ts, *_rest, s in bars[-20_000:] if hours[0] <= ts.hour < hours[1])
        if len(samples) < 100:
            return None
    spread = samples[len(samples) // 2]
    commission = COMMISSION_USD_PER_LOT / LOT[name]
    return {"spread": spread, "commission": commission, "round_trip": spread + commission,
            "live": q["ask"] - q["bid"], "samples": len(samples), "floor": floor}
