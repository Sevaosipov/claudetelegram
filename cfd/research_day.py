"""cfd/research_day.py: the EURUSD day-trading research (docs/cfd/PREREGISTRATION_DAY_EURUSD.md) -- the day
league's ideas and their variants on 15-minute bars built from FXCM's public one-minute candles, chosen on a
design period and judged once on a hold-out period."""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import io
import time
from pathlib import Path

from cfd import research4 as r4
from cfd.data import Bar

UTC = dt.timezone.utc
M1_DIR = r4.CACHE_DIR / "fxcm_m1"
FIRST_YEAR = 2021


def load_m1(pair: str = "EURUSD", first_year: int = FIRST_YEAR, last: dt.date | None = None,
            cache_dir: Path = M1_DIR, pause: float = 0.4, log=None) -> list[tuple]:
    """FXCM's one-minute candles of `pair`: [(ts UTC, bid open, high, low, close, ask open)], oldest first.
    A file per week of the year, kept once fetched (the running week is not asked for)."""
    last = last or dt.date.today()
    rows: dict[dt.datetime, tuple] = {}
    for year in range(first_year, last.year + 1):
        for week in range(1, 54):
            if year == last.year and week >= last.isocalendar()[1]:
                break
            path = cache_dir / f"{pair}_{year}_{week:02d}.csv.gz"
            if not path.exists():
                body = r4._get(f"https://candledata.fxcorporate.com/m1/{pair}/{year}/{week}.csv.gz")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body or b"")
                if log:
                    log(f"{path.name} {len(body or b'')}")
                time.sleep(pause)
            body = path.read_bytes()
            if not body:
                continue
            text = gzip.decompress(body).decode("utf-8", errors="replace").lstrip("﻿")
            for row in csv.reader(io.StringIO(text)):
                if len(row) < 6 or not row[0][:1].isdigit():
                    continue
                try:
                    ts = dt.datetime.strptime(row[0].split(".")[0], "%m/%d/%Y %H:%M:%S").replace(tzinfo=UTC)
                    o, h, low, c, ask = (float(x) for x in row[1:6])
                except ValueError:
                    continue
                if low > 0 and h >= max(o, c) and low <= min(o, c):
                    rows[ts] = (ts, o, h, low, c, ask)
    return [rows[ts] for ts in sorted(rows)]


def to_m15(m1: list[tuple]) -> tuple[list[Bar], dict[dt.datetime, float]]:
    """15-minute bid bars from one-minute rows, and each bar's spread at its open (ask - bid)."""
    bars: list[Bar] = []
    spread: dict[dt.datetime, float] = {}
    cur = None
    for ts, o, h, low, c, ask in m1:
        start = ts.replace(minute=ts.minute - ts.minute % 15, second=0, microsecond=0)
        if cur is None or cur[0] != start:
            if cur:
                bars.append(Bar(*cur))
            cur = [start, o, h, low, c]
            spread[start] = ask - o
        else:
            cur[2], cur[3], cur[4] = max(cur[2], h), min(cur[3], low), c
    if cur:
        bars.append(Bar(*cur))
    return bars, spread
