"""cfd/data.py: OHLC bars for the CFD research -- the Bar type, the data-hygiene rules of spec
2026-10-05-cfd-signals.md §1.1, completed-bars-only, a file cache for the research run, the
London session, and the one network seam (yfinance).

Everything the engine reads is a list of `Bar`, so the setups, exits and research modules are
pure and run on synthetic bars. The network sits behind `fetch(symbol, interval) -> raw rows`
(the default is `yahoo_fetch`); a raw row is `(ts, open, high, low, close)` where `ts` is an ISO
string, a date or a datetime and a price may be None/NaN. Yahoo's FX history has bad ticks,
hence the hygiene rules: a bar is dropped when any of O/H/L/C is missing or not above zero, when
high < low, or when its high or low is more than 15 % away from its close. Duplicates (Yahoo
repeats the latest bar) keep the last valid row.

`Bar.ts` is always a tz-aware UTC datetime: the start of the hour for hourly bars, midnight UTC
of the bar's own calendar date for daily bars (so `ts.date()` is the date Yahoo shows). Only
completed bars are returned -- Yahoo serves the bar still forming as the last row, and a signal or a
stop on it would differ from the final bar.

pandas and yfinance are imported only inside the yfinance edge, so importing this module (and
every pure module that imports `Bar` from it) needs neither.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc

DAILY = "1d"
HOURLY = "1h"

MAX_WICK = 0.15                  # §1.1: a high or low further than this from the close is a bad tick
MIN_BARS_BEFORE_SIGNAL = 300     # §1.1: an instrument needs this many bars before its first signal
DAILY_START = "2000-01-01"       # Yahoo serves what it has from here (FX from late 2003)
HOURLY_PERIOD = "725d"           # Yahoo keeps 1h bars for 730 days; a margin so the edge isn't refused
CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cfd_cache"   # git-ignored

Fetch = Callable[[str, str], Iterable]


@dataclass(frozen=True)
class Bar:
    ts: dt.datetime
    open: float
    high: float
    low: float
    close: float


@dataclass
class Dropped:
    """How many rows each rule removed, for the report's data section."""
    missing: int = 0        # a value missing, not finite, or not above zero
    inverted: int = 0       # high below low
    wick: int = 0           # high or low more than 15 % from the close
    duplicate: int = 0      # a timestamp seen twice (the last valid row is kept)
    unparsable: int = 0     # a timestamp that is not a date
    incomplete: int = 0     # the bar still forming

    @property
    def total(self) -> int:
        return (self.missing + self.inverted + self.wick + self.duplicate + self.unparsable
                + self.incomplete)


@dataclass
class Loaded:
    bars: list[Bar]
    dropped: Dropped = field(default_factory=Dropped)


# ---------------------------------------------------------------- cleaning
def _num(value) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _parse_ts(value, interval: str) -> dt.datetime | None:
    try:
        if isinstance(value, dt.datetime):
            ts = value
        elif isinstance(value, dt.date):
            ts = dt.datetime(value.year, value.month, value.day)
        elif isinstance(value, str):
            ts = dt.datetime.fromisoformat(value.strip())
        else:
            return None
    except ValueError:
        return None
    if interval == DAILY:       # the bar's own calendar date, whatever offset the source stamped
        return dt.datetime(ts.year, ts.month, ts.day, tzinfo=UTC)
    if ts.tzinfo is None:       # an hourly stamp without an offset is taken as UTC
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def _bad_reason(o, h, low, c) -> str | None:
    if None in (o, h, low, c) or min(o, h, low, c) <= 0:
        return "missing"
    if h < low:
        return "inverted"
    if abs(h - c) / c > MAX_WICK or abs(low - c) / c > MAX_WICK:
        return "wick"
    return None


def clean_rows(rows: Iterable, interval: str) -> Loaded:
    """Raw rows -> sorted, de-duplicated bars with the §1.1 hygiene applied. `interval` is "1d"
    or "1h" and sets how a timestamp is read. Forming bars are not judged here (see load_daily)."""
    dropped = Dropped()
    by_ts: dict[dt.datetime, Bar] = {}
    for row in rows:
        ts = _parse_ts(row[0], interval)
        if ts is None:
            dropped.unparsable += 1
            continue
        o, h, low, c = (_num(v) for v in row[1:5])
        reason = _bad_reason(o, h, low, c)
        if reason:
            setattr(dropped, reason, getattr(dropped, reason) + 1)
            continue
        if ts in by_ts:
            dropped.duplicate += 1
        by_ts[ts] = Bar(ts, o, h, low, c)
    return Loaded([by_ts[ts] for ts in sorted(by_ts)], dropped)


def _today() -> dt.date:
    return dt.datetime.now(UTC).date()


def load_daily(symbol: str, *, fetch: Fetch | None = None, today: dt.date | None = None) -> Loaded:
    """Completed daily bars of a Yahoo symbol with their drop counts. Bars dated today or later
    are still forming and are removed."""
    rows = (fetch or yahoo_fetch)(symbol, DAILY)
    loaded = clean_rows(rows, DAILY)
    cutoff = today or _today()
    done = [b for b in loaded.bars if b.ts.date() < cutoff]
    loaded.dropped.incomplete = len(loaded.bars) - len(done)
    loaded.bars = done
    return loaded


def load_hourly(symbol: str, *, fetch: Fetch | None = None,
                now: dt.datetime | None = None) -> Loaded:
    """Completed hourly bars (UTC) of a Yahoo symbol with their drop counts: a bar counts once
    its hour has ended."""
    rows = (fetch or yahoo_fetch)(symbol, HOURLY)
    loaded = clean_rows(rows, HOURLY)
    cutoff = now or dt.datetime.now(UTC)
    done = [b for b in loaded.bars if b.ts + dt.timedelta(hours=1) <= cutoff]
    loaded.dropped.incomplete = len(loaded.bars) - len(done)
    loaded.bars = done
    return loaded


def daily_bars(symbol: str, *, fetch: Fetch | None = None,
               today: dt.date | None = None) -> list[Bar]:
    return load_daily(symbol, fetch=fetch, today=today).bars


def hourly_bars(symbol: str, *, fetch: Fetch | None = None,
                now: dt.datetime | None = None) -> list[Bar]:
    return load_hourly(symbol, fetch=fetch, now=now).bars


# ---------------------------------------------------------------- the cache
def _cache_path(cache_dir: Path | str, symbol: str, interval: str) -> Path:
    return Path(cache_dir) / f"{re.sub(r'[^A-Za-z0-9.-]', '_', symbol)}_{interval}.json"


def _row_for_json(row) -> list:
    ts = row[0]
    if isinstance(ts, (dt.datetime, dt.date)):
        ts = ts.isoformat()
    return [ts, *(_num(v) for v in row[1:5])]


def _read_cache(path: Path) -> list[tuple] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [tuple(r) for r in payload["rows"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None         # missing or corrupt: the caller fetches again


def cache_stamp(cache_dir: Path | str, symbol: str, interval: str) -> str | None:
    """When the cache file of a symbol was fetched (ISO, UTC), or None."""
    try:
        payload = json.loads(_cache_path(cache_dir, symbol, interval).read_text(encoding="utf-8"))
        return payload["fetched_at"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def cached_fetch(fetch: Fetch, cache_dir: Path | str = CACHE_DIR, *, refresh: bool = False) -> Fetch:
    """A fetch that reads `cache_dir` first. Rows from the network are written to a JSON file per
    (symbol, interval) -- strict JSON, a missing price is null -- so a second research run (or a
    re-run after a hygiene change) needs no network. `refresh=True` fetches again and overwrites.
    Nothing is cached when the fetch fails or comes back empty, so a retry tries the network."""
    def cached(symbol: str, interval: str) -> list[tuple]:
        path = _cache_path(cache_dir, symbol, interval)
        if not refresh:
            rows = _read_cache(path)
            if rows is not None:
                return rows
        rows = [tuple(_row_for_json(r)) for r in fetch(symbol, interval)]
        if rows:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"symbol": symbol, "interval": interval,
                       "fetched_at": dt.datetime.now(UTC).isoformat(timespec="seconds"),
                       "rows": rows}
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, allow_nan=False), encoding="utf-8")
            os.replace(tmp, path)       # an interrupted run never leaves half a file
        return rows
    return cached


# ---------------------------------------------------------------- the yfinance edge
def rows_from_frame(frame, interval: str) -> list[tuple]:
    """The Open/High/Low/Close columns of a yfinance history frame as raw rows: a daily bar is
    stamped with its local calendar date, an hourly one with its UTC start; NaN becomes None."""
    if frame is None or len(frame) == 0:
        return []
    rows = []
    values = frame[["Open", "High", "Low", "Close"]].itertuples(index=False, name=None)
    for idx, (o, h, low, c) in zip(frame.index, values):
        if interval == DAILY:
            ts = idx.date().isoformat()
        else:
            idx = idx.tz_localize("UTC") if idx.tzinfo is None else idx.tz_convert("UTC")
            ts = idx.isoformat()
        rows.append((ts, _num(o), _num(h), _num(low), _num(c)))
    return rows


def yahoo_fetch(symbol: str, interval: str) -> list[tuple]:
    """Raw rows from Yahoo through yfinance, unadjusted (FX, indices and futures have nothing to
    adjust, and a CFD trades the raw price). The network seam of the whole package."""
    import yfinance as yf
    if interval == DAILY:
        span = {"start": DAILY_START}
    elif interval == HOURLY:
        span = {"period": HOURLY_PERIOD}
    else:
        raise ValueError(f"interval must be '1d' or '1h', not {interval!r}")
    frame = yf.Ticker(symbol).history(interval=interval, auto_adjust=False, **span)
    return rows_from_frame(frame, interval)


# ---------------------------------------------------------------- sessions
@dataclass(frozen=True)
class Session:
    """A daily window in a named time zone, applied to a bar's start time: the bar is inside
    when start_hour <= local start time < end_hour (so a 07:00-16:00 session holds the bars
    starting 07:00 ... 15:00)."""
    start_hour: int
    end_hour: int
    tz: str

    def __post_init__(self):
        if not 0 <= self.start_hour < self.end_hour <= 24:
            raise ValueError(f"bad session hours {self.start_hour}-{self.end_hour}")

    @property
    def hours(self) -> float:
        return float(self.end_hour - self.start_hour)

    def _local(self, ts: dt.datetime) -> dt.datetime:
        return ts.astimezone(ZoneInfo(self.tz))

    def contains(self, ts: dt.datetime) -> bool:
        local = self._local(ts)
        minutes = local.hour * 60 + local.minute
        return self.start_hour * 60 <= minutes < self.end_hour * 60

    def local_date(self, ts: dt.datetime) -> dt.date:
        return self._local(ts).date()


LONDON_SESSION = Session(7, 16, "Europe/London")     # the gold config's session (§1.2)
