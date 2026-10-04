"""cfd/data.py: the Bar type, the data-hygiene rules of spec §1.1, completed-bars-only, the
cache, the London session and the yfinance edge. Offline: the fetch is always a fake (the
conftest guard blocks the network), and the yfinance seam is tested with a fake Ticker."""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import math

import pandas as pd
import pytest

from cfd import data

UTC = dt.timezone.utc


def _utc(y, m, d, h=0, mi=0):
    return dt.datetime(y, m, d, h, mi, tzinfo=UTC)


class FakeFetch:
    """A fetch seam: records its calls and returns canned raw rows."""

    def __init__(self, rows):
        self.rows = rows
        self.calls: list[tuple[str, str]] = []

    def __call__(self, symbol, interval):
        self.calls.append((symbol, interval))
        return list(self.rows)


# ------------------------------------------------------------------ the Bar type
def test_bar_is_a_frozen_dataclass_with_the_five_fields():
    b = data.Bar(_utc(2024, 1, 2), 1.0, 1.1, 0.9, 1.05)
    assert [f.name for f in dataclasses.fields(b)] == ["ts", "open", "high", "low", "close"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        b.close = 2.0  # type: ignore[misc]


def test_min_bars_before_the_first_signal_is_300():
    assert data.MIN_BARS_BEFORE_SIGNAL == 300


# ------------------------------------------------------------------ hygiene (spec §1.1)
GOOD = ("2024-01-02", 100.0, 101.0, 99.0, 100.5)


def _clean(rows, interval="1d"):
    return data.clean_rows(rows, interval)


def test_a_good_row_becomes_a_utc_midnight_bar_for_its_date():
    res = _clean([GOOD])
    assert res.bars == [data.Bar(_utc(2024, 1, 2), 100.0, 101.0, 99.0, 100.5)]
    assert res.dropped.total == 0


@pytest.mark.parametrize("bad", [
    ("2024-01-03", None, 101.0, 99.0, 100.5),
    ("2024-01-03", 100.0, None, 99.0, 100.5),
    ("2024-01-03", 100.0, 101.0, None, 100.5),
    ("2024-01-03", 100.0, 101.0, 99.0, None),
    ("2024-01-03", float("nan"), 101.0, 99.0, 100.5),
    ("2024-01-03", 100.0, 101.0, 99.0, float("nan")),
    ("2024-01-03", 100.0, float("inf"), 99.0, 100.5),
])
def test_a_bar_with_a_missing_value_is_dropped(bad):
    res = _clean([GOOD, bad])
    assert [b.ts for b in res.bars] == [_utc(2024, 1, 2)]
    assert res.dropped.missing == 1


@pytest.mark.parametrize("bad", [
    ("2024-01-03", 0.0, 101.0, 99.0, 100.5),
    ("2024-01-03", 100.0, 101.0, 99.0, 0.0),
    ("2024-01-03", 100.0, 101.0, -1.0, 100.5),
    ("2024-01-03", -100.0, 101.0, 99.0, 100.5),
])
def test_a_bar_with_a_value_at_or_below_zero_is_dropped(bad):
    res = _clean([GOOD, bad])
    assert len(res.bars) == 1
    assert res.dropped.missing == 1


def test_a_bar_with_high_below_low_is_dropped():
    res = _clean([GOOD, ("2024-01-03", 100.0, 99.0, 101.0, 100.0)])
    assert len(res.bars) == 1
    assert res.dropped.inverted == 1


def test_a_bar_whose_high_is_more_than_15_percent_from_its_close_is_dropped():
    # high 120 vs close 100: 20 % away
    res = _clean([GOOD, ("2024-01-03", 100.0, 120.0, 99.0, 100.0)])
    assert len(res.bars) == 1
    assert res.dropped.wick == 1


def test_a_bar_whose_low_is_more_than_15_percent_from_its_close_is_dropped():
    res = _clean([GOOD, ("2024-01-03", 100.0, 101.0, 80.0, 100.0)])
    assert len(res.bars) == 1
    assert res.dropped.wick == 1


def test_a_wick_of_exactly_15_percent_is_kept_it_must_be_more_than_that():
    res = _clean([("2024-01-03", 100.0, 115.0, 85.0, 100.0)])
    assert len(res.bars) == 1
    assert res.dropped.wick == 0


def test_the_open_is_not_part_of_the_15_percent_rule():
    # the spec's list names only the high and the low against the close: a stray open (140 vs
    # a close of 100, outside its own range) does not drop the bar by itself
    res = _clean([("2024-01-03", 140.0, 110.0, 95.0, 100.0)])
    assert len(res.bars) == 1
    assert res.dropped.total == 0


def test_rows_are_sorted_and_a_duplicate_timestamp_keeps_the_last_row():
    rows = [
        ("2024-01-04", 30.0, 31.0, 29.0, 30.5),
        ("2024-01-02", 10.0, 11.0, 9.5, 10.5),
        ("2024-01-03", 20.0, 21.0, 19.0, 20.5),
        ("2024-01-03", 20.1, 21.5, 19.5, 20.8),   # Yahoo's repeated latest bar: the last wins
    ]
    res = _clean(rows)
    assert [b.ts.date().isoformat() for b in res.bars] == ["2024-01-02", "2024-01-03", "2024-01-04"]
    assert res.bars[1].close == 20.8
    assert res.dropped.duplicate == 1


def test_an_invalid_duplicate_does_not_replace_the_valid_row_before_it():
    rows = [("2024-01-03", 20.0, 21.0, 19.0, 20.5), ("2024-01-03", 20.0, None, 19.0, 20.5)]
    res = _clean(rows)
    assert len(res.bars) == 1 and res.bars[0].high == 21.0
    assert res.dropped.missing == 1 and res.dropped.duplicate == 0


def test_an_unparsable_timestamp_is_dropped_and_counted():
    res = _clean([GOOD, ("not a date", 1.0, 1.1, 0.9, 1.0)])
    assert len(res.bars) == 1
    assert res.dropped.unparsable == 1


def test_a_daily_timestamp_with_a_time_and_offset_keeps_its_local_calendar_date():
    # a bar stamped at New York midnight is that day's bar, not the previous UTC day's
    res = _clean([("2024-01-03 00:00:00-05:00", 1.0, 1.1, 0.9, 1.0)])
    assert res.bars[0].ts == _utc(2024, 1, 3)
    res = _clean([(dt.datetime(2024, 1, 3, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=-5))),
                   1.0, 1.1, 0.9, 1.0)])
    assert res.bars[0].ts == _utc(2024, 1, 3)
    res = _clean([(dt.date(2024, 1, 3), 1.0, 1.1, 0.9, 1.0)])
    assert res.bars[0].ts == _utc(2024, 1, 3)


def test_hourly_timestamps_are_converted_to_utc_and_naive_ones_are_taken_as_utc():
    rows = [
        ("2024-01-03T10:00:00+01:00", 1.0, 1.1, 0.9, 1.0),    # 09:00 UTC
        ("2024-01-03T10:00:00", 1.0, 1.1, 0.9, 1.0),          # naive: 10:00 UTC
        (dt.datetime(2024, 1, 3, 11, 0, tzinfo=dt.timezone(dt.timedelta(hours=-5))),
         1.0, 1.1, 0.9, 1.0),                                 # 16:00 UTC
    ]
    res = _clean(rows, "1h")
    assert [b.ts for b in res.bars] == [_utc(2024, 1, 3, 9), _utc(2024, 1, 3, 10),
                                        _utc(2024, 1, 3, 16)]
    assert all(b.ts.utcoffset() == dt.timedelta(0) for b in res.bars)


# ------------------------------------------------------------------ completed bars only
def test_daily_bars_drop_today_and_later_the_forming_bar():
    rows = [("2024-01-02", 1, 1.1, 0.9, 1.0), ("2024-01-03", 1, 1.1, 0.9, 1.0),
            ("2024-01-04", 1, 1.1, 0.9, 1.0)]
    bars = data.daily_bars("X", fetch=FakeFetch(rows), today=dt.date(2024, 1, 4))
    assert [b.ts.date().isoformat() for b in bars] == ["2024-01-02", "2024-01-03"]


def test_load_daily_counts_the_forming_bar_as_incomplete():
    rows = [("2024-01-02", 1, 1.1, 0.9, 1.0), ("2024-01-03", 1, 1.1, 0.9, 1.0)]
    loaded = data.load_daily("X", fetch=FakeFetch(rows), today=dt.date(2024, 1, 3))
    assert len(loaded.bars) == 1
    assert loaded.dropped.incomplete == 1


def test_hourly_bars_keep_only_bars_that_have_closed():
    rows = [("2024-01-03T09:00:00+00:00", 1, 1.1, 0.9, 1.0),
            ("2024-01-03T10:00:00+00:00", 1, 1.1, 0.9, 1.0),
            ("2024-01-03T11:00:00+00:00", 1, 1.1, 0.9, 1.0)]
    # at 11:00:00 exactly the 10:00 bar has just closed; the 11:00 bar is just starting
    bars = data.hourly_bars("X", fetch=FakeFetch(rows), now=_utc(2024, 1, 3, 11, 0))
    assert [b.ts.hour for b in bars] == [9, 10]
    # at 10:59 the 10:00 bar is still forming
    bars = data.hourly_bars("X", fetch=FakeFetch(rows), now=_utc(2024, 1, 3, 10, 59))
    assert [b.ts.hour for b in bars] == [9]


# ------------------------------------------------------------------ the fetch seam
def test_daily_and_hourly_bars_ask_the_fetch_for_the_right_interval():
    f = FakeFetch([GOOD])
    data.daily_bars("EURUSD=X", fetch=f, today=dt.date(2030, 1, 1))
    data.hourly_bars("GC=F", fetch=f, now=_utc(2030, 1, 1))
    assert f.calls == [("EURUSD=X", "1d"), ("GC=F", "1h")]


def test_the_default_fetch_is_yfinance(monkeypatch):
    calls = []

    def fake_yahoo(symbol, interval):
        calls.append((symbol, interval))
        return [GOOD]
    monkeypatch.setattr(data, "yahoo_fetch", fake_yahoo)
    bars = data.daily_bars("EURUSD=X", today=dt.date(2030, 1, 1))
    assert len(bars) == 1 and calls == [("EURUSD=X", "1d")]


def _frame(index, values):
    return pd.DataFrame(values, index=index, columns=["Open", "High", "Low", "Close"])


def test_rows_from_frame_daily_uses_the_local_calendar_date():
    idx = pd.DatetimeIndex(["2024-01-02 00:00:00-05:00", "2024-01-03 00:00:00-05:00"])
    rows = data.rows_from_frame(_frame(idx, [[1, 2, 0.5, 1.5], [1.5, 2.5, 1.0, 2.0]]), "1d")
    assert rows == [("2024-01-02", 1.0, 2.0, 0.5, 1.5), ("2024-01-03", 1.5, 2.5, 1.0, 2.0)]


def test_rows_from_frame_hourly_is_utc_iso_and_nan_becomes_none():
    idx = pd.DatetimeIndex(["2024-01-03 09:00:00-05:00", "2024-01-03 10:00:00-05:00"])
    rows = data.rows_from_frame(_frame(idx, [[1, 2, 0.5, 1.5], [1.5, float("nan"), 1.0, 2.0]]),
                                "1h")
    assert rows[0] == ("2024-01-03T14:00:00+00:00", 1.0, 2.0, 0.5, 1.5)
    assert rows[1] == ("2024-01-03T15:00:00+00:00", 1.5, None, 1.0, 2.0)


def test_rows_from_frame_takes_a_naive_hourly_index_as_utc():
    idx = pd.DatetimeIndex(["2024-01-03 09:00:00"])
    rows = data.rows_from_frame(_frame(idx, [[1, 2, 0.5, 1.5]]), "1h")
    assert rows[0][0] == "2024-01-03T09:00:00+00:00"


def test_rows_from_frame_of_an_empty_frame_is_empty():
    assert data.rows_from_frame(pd.DataFrame(), "1d") == []
    assert data.rows_from_frame(None, "1d") == []


def test_yahoo_fetch_asks_yfinance_for_unadjusted_bars(monkeypatch):
    seen = {}

    class FakeTicker:
        def __init__(self, symbol):
            seen["symbol"] = symbol

        def history(self, **kwargs):
            seen.setdefault("calls", []).append(kwargs)
            idx = pd.DatetimeIndex(["2024-01-02 00:00:00-05:00"])
            return _frame(idx, [[1, 2, 0.5, 1.5]])

    import yfinance
    monkeypatch.setattr(yfinance, "Ticker", FakeTicker)
    daily = data.yahoo_fetch("EURUSD=X", "1d")
    hourly = data.yahoo_fetch("GC=F", "1h")
    assert daily == [("2024-01-02", 1.0, 2.0, 0.5, 1.5)]
    assert hourly[0][0] == "2024-01-02T05:00:00+00:00"
    d_kwargs, h_kwargs = seen["calls"]
    assert d_kwargs["interval"] == "1d" and d_kwargs["auto_adjust"] is False
    assert d_kwargs["start"] == data.DAILY_START
    assert h_kwargs["interval"] == "1h" and h_kwargs["auto_adjust"] is False
    assert h_kwargs["period"] == data.HOURLY_PERIOD
    with pytest.raises(ValueError):
        data.yahoo_fetch("GC=F", "5m")


# ------------------------------------------------------------------ the cache
def test_cached_fetch_reads_the_network_once_then_the_file(tmp_path):
    f = FakeFetch([GOOD, ("2024-01-03", 1, 1.1, 0.9, None)])
    cached = data.cached_fetch(f, tmp_path)
    first = cached("EURUSD=X", "1d")
    second = cached("EURUSD=X", "1d")
    assert f.calls == [("EURUSD=X", "1d")]
    assert first == second
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text())        # strict JSON: no NaN tokens
    assert payload["symbol"] == "EURUSD=X" and payload["interval"] == "1d"
    assert payload["fetched_at"]
    assert payload["rows"][1][4] is None


def test_cached_fetch_refresh_goes_back_to_the_network(tmp_path):
    f = FakeFetch([GOOD])
    data.cached_fetch(f, tmp_path)("GC=F", "1h")
    data.cached_fetch(f, tmp_path, refresh=True)("GC=F", "1h")
    assert len(f.calls) == 2


def test_cache_keeps_symbols_and_intervals_apart(tmp_path):
    f = FakeFetch([GOOD])
    cached = data.cached_fetch(f, tmp_path)
    for sym, itv in [("^GSPC", "1d"), ("GC=F", "1d"), ("GC=F", "1h"), ("BTC-USD", "1d")]:
        cached(sym, itv)
    assert len(f.calls) == 4
    assert len(list(tmp_path.glob("*.json"))) == 4


def test_a_failing_or_empty_fetch_is_not_cached(tmp_path):
    def boom(symbol, interval):
        raise RuntimeError("down")
    with pytest.raises(RuntimeError):
        data.cached_fetch(boom, tmp_path)("GC=F", "1d")
    assert not list(tmp_path.glob("*.json"))
    f = FakeFetch([])
    data.cached_fetch(f, tmp_path)("GC=F", "1d")
    data.cached_fetch(f, tmp_path)("GC=F", "1d")
    assert len(f.calls) == 2 and not list(tmp_path.glob("*.json"))


def test_cached_fetch_creates_the_directory(tmp_path):
    d = tmp_path / "a" / "cfd_cache"
    data.cached_fetch(FakeFetch([GOOD]), d)("GC=F", "1d")
    assert d.is_dir()


def test_cache_stamp_reads_the_fetch_time(tmp_path):
    assert data.cache_stamp(tmp_path, "GC=F", "1d") is None
    data.cached_fetch(FakeFetch([GOOD]), tmp_path)("GC=F", "1d")
    stamp = data.cache_stamp(tmp_path, "GC=F", "1d")
    assert dt.datetime.fromisoformat(stamp).tzinfo is not None


def test_a_corrupt_cache_file_is_refetched(tmp_path):
    f = FakeFetch([GOOD])
    cached = data.cached_fetch(f, tmp_path)
    cached("GC=F", "1d")
    next(tmp_path.glob("*.json")).write_text("{not json")
    rows = cached("GC=F", "1d")
    assert len(f.calls) == 2 and len(rows) == 1


# ------------------------------------------------------------------ the London session
def test_london_session_is_07_to_16_by_bar_start_in_winter_and_summer():
    s = data.LONDON_SESSION
    # January: London == UTC
    assert not s.contains(_utc(2024, 1, 15, 6))
    assert s.contains(_utc(2024, 1, 15, 7))
    assert s.contains(_utc(2024, 1, 15, 15))        # the 15:00 bar closes at 16:00: in
    assert not s.contains(_utc(2024, 1, 15, 16))    # a bar starting at 16:00 is outside
    # July: BST, London == UTC + 1
    assert not s.contains(_utc(2024, 7, 15, 5))
    assert s.contains(_utc(2024, 7, 15, 6))
    assert s.contains(_utc(2024, 7, 15, 14))
    assert not s.contains(_utc(2024, 7, 15, 15))


def test_session_local_date_is_the_london_calendar_date():
    s = data.LONDON_SESSION
    assert s.local_date(_utc(2024, 7, 15, 23, 30)) == dt.date(2024, 7, 16)   # 00:30 BST next day
    assert s.local_date(_utc(2024, 1, 15, 23, 30)) == dt.date(2024, 1, 15)


def test_session_hours_are_checked():
    with pytest.raises(ValueError):
        data.Session(16, 7, "Europe/London")
    assert math.isclose(data.LONDON_SESSION.hours, 9.0)
