"""cfd/research_gold.py: a gold-only search (docs/cfd/PREREGISTRATION_GOLD.md) on Dukascopy's hourly XAUUSD
candles; chosen on a design period, judged once on a hold-out."""
from __future__ import annotations

import datetime as dt
import lzma
import struct
import time
from pathlib import Path

from cfd import research4 as r4
from cfd.data import Bar

UTC = dt.timezone.utc
FIRST_YEAR = 2010


def load_gold(first_year: int = FIRST_YEAR, today: dt.date | None = None, cache_dir: Path = r4.CACHE_DIR,
              pause: float = 1.5, log=None) -> list[Bar]:
    """Dukascopy's hourly bid candles of XAUUSD in UTC, the hours with no volume left out; a past month is
    kept once fetched, the running month is not asked for."""
    today = today or dt.date.today()
    bars: list[Bar] = []
    for year in range(first_year, today.year + 1):
        for month in range(1, 13):
            if (year, month) >= (today.year, today.month):
                break
            path = cache_dir / "duka" / f"XAUUSD_{year}_{month:02d}.bi5"
            if not path.exists():
                body = r4._get(f"https://datafeed.dukascopy.com/datafeed/XAUUSD/{year}/{month - 1:02d}/"
                               "BID_candles_hour_1.bi5")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body or b"")
                if log:
                    log(f"{path.name} {len(body or b'')}")
                time.sleep(pause)
            body = path.read_bytes()
            if not body:
                continue
            raw = lzma.decompress(body)
            start = dt.datetime(year, month, 1, tzinfo=UTC)
            for k in range(len(raw) // 24):
                secs, o, c, low, high, volume = struct.unpack(">5if", raw[k * 24:(k + 1) * 24])
                if volume > 0 and low > 0:
                    bars.append(Bar(start + dt.timedelta(seconds=secs), o / 1e3, high / 1e3, low / 1e3, c / 1e3))
    return bars
