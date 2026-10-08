"""cfd/rates.py: the interest rates of H5 (CARRY-FX, docs/cfd/PREREGISTRATION_R2.md) -- OECD 3-month
interbank rates, monthly, one FRED series per currency, read from FRED's `fredgraph.csv`.

The publication rule is exact and has no look-ahead: the value for month M is used from the 15th of
month M+1 (the 15th included). A month that has no value ("." or an empty cell in the csv) is simply
absent, so the last value carries forward until a later month publishes; after the last observation
the last value carries forward indefinitely. Before the first usable observation there is no rate.

    rates = load_rates()                               # network on the first run, then the cache
    rates.rate_on("EUR", datetime.date(2024, 3, 20))    # February 2024's value (usable since 03-15)

The network sits behind `fetch(series_id) -> csv text` (the default is `fred_fetch`, through
`requests`, imported only when it is used). Downloaded text is cached under data/cfd_cache/
(git-ignored) as `fred_<series>.json` -- the csv inside a small JSON envelope with the time it was
fetched -- so a second research run is offline; `refresh=True` downloads again. Nothing is cached when
a fetch fails or its reply holds no observation. A series that fails to load is recorded in
`Rates.errors` (and logged) and left out: its rates are None, so no pair that needs it has a state.
"""
from __future__ import annotations

import bisect
import csv
import datetime as dt
import io
import json
import math
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from cfd.data import CACHE_DIR, UTC

FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
_TIMEOUT = 20

# currency -> FRED series: OECD "3-month or 90-day rates and yields: interbank rates", monthly
SERIES: dict[str, str] = {
    "USD": "IR3TIB01USM156N",
    "EUR": "IR3TIB01EZM156N",
    "GBP": "IR3TIB01GBM156N",
    "JPY": "IR3TIB01JPM156N",
    "AUD": "IR3TIB01AUM156N",
    "CAD": "IR3TIB01CAM156N",
    "CHF": "IR3TIB01CHM156N",
    "NZD": "IR3TIB01NZM156N",
}
PUBLICATION_DAY = 15             # the value for month M is usable from this day of month M+1

Fetch = Callable[[str], str]


# ---------------------------------------------------------------- the publication rule
def available_from(month: dt.date) -> dt.date:
    """The first day the value of `month` may be used: the 15th of the following month."""
    year, m = (month.year + 1, 1) if month.month == 12 else (month.year, month.month + 1)
    return dt.date(year, m, PUBLICATION_DAY)


def _as_date(day: dt.date | dt.datetime) -> dt.date:
    return day.date() if isinstance(day, dt.datetime) else day


@dataclass(frozen=True)
class RateSeries:
    """The observations of one currency, (month, value) sorted by month, with the publication rule
    applied by `on`."""
    currency: str
    series_id: str
    observations: tuple[tuple[dt.date, float], ...]
    _usable_from: tuple[dt.date, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        object.__setattr__(self, "_usable_from",
                           tuple(available_from(month) for month, _ in self.observations))

    def on(self, day: dt.date | dt.datetime) -> float | None:
        """The rate in force on `day`: the value of the latest month already published (its 15th of
        the next month reached), skipping months with no value. None before the first one."""
        k = bisect.bisect_right(self._usable_from, _as_date(day)) - 1
        return None if k < 0 else self.observations[k][1]


@dataclass
class Rates:
    """The rates of every currency that loaded, and why the others did not."""
    series: dict[str, RateSeries] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    def rate_on(self, currency: str, day: dt.date | dt.datetime) -> float | None:
        """The rate of `currency` in force on `day` (see `RateSeries.on`); None when the currency
        did not load. A currency outside `SERIES` is a mistake, not a missing rate: KeyError."""
        if currency not in SERIES:
            raise KeyError(currency)
        series = self.series.get(currency)
        return None if series is None else series.on(day)


# ---------------------------------------------------------------- parsing fredgraph.csv
def parse_fredgraph(text: str) -> list[tuple[dt.date, float]]:
    """(month, value) pairs of a fredgraph.csv, sorted by month. The header (`observation_date` or
    `DATE`), rows that are not a date, and values that are "." , empty or not a finite number are
    skipped; negative rates are real and kept. Two rows of one month: the later one wins."""
    by_month: dict[dt.date, float] = {}
    for row in csv.reader(io.StringIO(text.lstrip("﻿"))):
        if len(row) < 2:
            continue
        try:
            day = dt.date.fromisoformat(row[0].strip())
        except ValueError:
            continue
        raw = row[1].strip()
        if raw in ("", "."):
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if math.isfinite(value):
            by_month[dt.date(day.year, day.month, 1)] = value
    return sorted(by_month.items())


# ---------------------------------------------------------------- the network seam
def fred_fetch(series_id: str) -> str:
    """The csv text of a FRED series. The one network call of this module."""
    import requests
    response = requests.get(FRED_URL, params={"id": series_id}, timeout=_TIMEOUT)
    response.raise_for_status()
    return response.text


# ---------------------------------------------------------------- the cache
def _cache_path(cache_dir: Path | str, series_id: str) -> Path:
    return Path(cache_dir) / f"fred_{series_id}.json"


def _read_cache(path: Path) -> str | None:
    try:
        text = json.loads(path.read_text(encoding="utf-8"))["csv"]
    except (OSError, ValueError, KeyError, TypeError):
        return None                     # missing or corrupt: the caller fetches again
    return text if isinstance(text, str) else None


def _write_cache(path: Path, currency: str, series_id: str, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"series": series_id, "currency": currency,
               "fetched_at": dt.datetime.now(UTC).isoformat(timespec="seconds"), "csv": text}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)               # an interrupted run never leaves half a file


def cache_stamp(cache_dir: Path | str, currency: str) -> str | None:
    """When the cache file of a currency's series was fetched (ISO, UTC), or None."""
    try:
        payload = json.loads(_cache_path(cache_dir, SERIES[currency]).read_text(encoding="utf-8"))
        return payload["fetched_at"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


# ---------------------------------------------------------------- loading
def load_series(currency: str, *, fetch: Fetch | None = None, cache_dir: Path | str = CACHE_DIR,
                refresh: bool = False) -> RateSeries:
    """The series of one currency, from the cache when it holds observations, otherwise fetched (and
    cached when the reply holds at least one). Raises when there is nothing to use."""
    series_id = SERIES[currency]
    path = _cache_path(cache_dir, series_id)
    observations: list[tuple[dt.date, float]] = []
    if not refresh:
        cached = _read_cache(path)
        observations = parse_fredgraph(cached) if cached is not None else []
    if not observations:
        text = (fetch or fred_fetch)(series_id)
        observations = parse_fredgraph(text)
        if not observations:
            raise ValueError(f"FRED series {series_id} came back with no observations")
        _write_cache(path, currency, series_id, text)
    return RateSeries(currency, series_id, tuple(observations))


def load_rates(currencies: Iterable[str] = tuple(SERIES), *, fetch: Fetch | None = None,
               cache_dir: Path | str = CACHE_DIR, refresh: bool = False,
               log: Callable[[str], None] | None = None) -> Rates:
    """The rates of the given currencies (all eight by default). A series that fails is logged,
    recorded in `errors` and left out, so one dead series never costs the whole run."""
    result = Rates()
    for currency in currencies:
        try:
            result.series[currency] = load_series(currency, fetch=fetch, cache_dir=cache_dir,
                                                  refresh=refresh)
        except Exception as e:
            message = f"{type(e).__name__}: {str(e)[:120]}"
            result.errors[currency] = message
            if log is not None:
                log(f"{currency}: no rates ({message})")
    return result
