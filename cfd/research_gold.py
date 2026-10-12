"""cfd/research_gold.py: a gold-only search (docs/cfd/PREREGISTRATION_GOLD.md) on Dukascopy's hourly XAUUSD
candles; chosen on a design period, judged once on a hold-out."""
from __future__ import annotations

import datetime as dt
import lzma
import struct
from pathlib import Path

from cfd import research4 as r4
from cfd.data import Bar

UTC = dt.timezone.utc
FIRST_YEAR = 2010


MT5_FROM = 2016                  # amendment A1: from here the broker's own hourly bars


def _duka_month(path: Path, year: int, month: int) -> list[Bar]:
    raw = lzma.decompress(path.read_bytes())
    start = dt.datetime(year, month, 1, tzinfo=UTC)
    out = []
    for k in range(len(raw) // 24):
        secs, o, c, low, high, volume = struct.unpack(">5if", raw[k * 24:(k + 1) * 24])
        if volume > 0 and low > 0:
            out.append(Bar(start + dt.timedelta(seconds=secs), o / 1e3, high / 1e3, low / 1e3, c / 1e3))
    return out


def load_gold(cache_dir: Path = r4.CACHE_DIR) -> list[Bar]:
    """Hourly XAUUSD in UTC: Dukascopy's bid candles 2010-2015 (the months fetched, the hours with no
    volume left out), then the hourly history of the user's MetaTrader 5 terminal (cfd/mt5_bridge.py)."""
    from cfd import mt5_bridge
    bars: list[Bar] = []
    for year in range(FIRST_YEAR, MT5_FROM):
        for month in range(1, 13):
            path = cache_dir / "duka" / f"XAUUSD_{year}_{month:02d}.bi5"
            if path.exists() and path.stat().st_size:
                bars += _duka_month(path, year, month)
    hourly_from = dt.datetime(MT5_FROM, 1, 18, tzinfo=UTC)          # before it the terminal has one bar a day
    return bars + [b for b in mt5_bridge.history("XAUUSD", "H1") if b.ts >= hourly_from]


# ================================================================ the search (PREREGISTRATION_GOLD.md)
DESIGN = (dt.date(2010, 1, 1), dt.date(2018, 12, 31))
HOLDOUT_FROM = dt.date(2019, 1, 1)
COST_USD, NIGHT_PCT = 0.30, 0.020
MIN_TRADES, MIN_YEARS, MIN_T = 80, 7, 2.5


def run(period: str, only: list[str] | None = None, log=print, sides=("long", "short"), bars=None):
    """The 53 candidates of cfd/research_edge.py on gold, at gold's costs."""
    from cfd import research_edge as edge
    edge.COST_ABS, edge.NIGHT_PCT = COST_USD, NIGHT_PCT
    try:
        return edge.run(period, only, log, bars=bars or load_gold(), design=DESIGN, holdout_from=HOLDOUT_FROM,
                        sides=sides)
    finally:
        edge.COST_ABS = edge.NIGHT_PCT = None


def qualifies(x) -> bool:
    return x.n >= MIN_TRADES and x.years_up >= MIN_YEARS and x.t >= MIN_T
