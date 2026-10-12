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
