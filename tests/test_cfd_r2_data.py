"""cfd/data.py, round 2, amendment A3 of docs/cfd/PREREGISTRATION_R2.md: the rule that drops a bar whose high
or low is more than 15 % from its close was written for bad FX ticks, and alt-coins move that far on real days
(the breakout days under test), so for crypto symbols the threshold is 60 %. Every other hygiene rule and every
non-crypto symbol is unchanged -- and so is round 1's loading path: BTC and ETH stay at 15 % in
`cfd.research`, whose results and report are not to move."""
from __future__ import annotations

import datetime as dt
import random

import pytest

from cfd import data, research
from cfd import instruments as ins

UTC = dt.timezone.utc
DAY = dt.timedelta(days=1)


def row(day, o, h, low, c):
    return (day, o, h, low, c)


GOOD = ("2024-01-02", 100.0, 101.0, 99.0, 100.5)


def clean(rows, **kw):
    return data.clean_rows(rows, "1d", **kw)


def test_the_thresholds_are_15_percent_and_for_crypto_60_percent():
    assert data.MAX_WICK == 0.15
    assert data.MAX_WICK_CRYPTO == 0.60


# ------------------------------------------------------------------ clean_rows with a threshold
def test_a_wick_between_15_and_60_percent_is_kept_at_the_crypto_threshold_and_dropped_at_the_default():
    rows = [GOOD,
            row("2024-01-03", 100.0, 130.0, 99.0, 100.0),          # a high 30 % over the close
            row("2024-01-04", 100.0, 101.0, 55.0, 100.0)]          # a low 45 % under it
    kept = clean(rows, max_wick=data.MAX_WICK_CRYPTO)
    assert [b.ts.date().isoformat() for b in kept.bars] == ["2024-01-02", "2024-01-03", "2024-01-04"]
    assert kept.dropped.wick == 0
    for default in (clean(rows), clean(rows, max_wick=None), clean(rows, max_wick=data.MAX_WICK)):
        assert [b.ts.date().isoformat() for b in default.bars] == ["2024-01-02"]
        assert default.dropped.wick == 2


def test_a_wick_of_exactly_60_percent_is_kept_it_must_be_more_than_that():
    high = clean([row("2024-01-03", 100.0, 160.0, 99.0, 100.0)], max_wick=0.60)
    low = clean([row("2024-01-03", 100.0, 101.0, 40.0, 100.0)], max_wick=0.60)
    assert len(high.bars) == len(low.bars) == 1 and high.dropped.wick == low.dropped.wick == 0


@pytest.mark.parametrize("bad", [
    row("2024-01-03", 100.0, 160.1, 99.0, 100.0),                 # a high just over 60 % above the close
    row("2024-01-03", 100.0, 101.0, 39.9, 100.0),                 # a low just over 60 % below it
    row("2024-01-03", 100.0, 250.0, 99.0, 100.0),
])
def test_a_wick_over_60_percent_is_dropped_at_the_crypto_threshold(bad):
    res = clean([GOOD, bad], max_wick=data.MAX_WICK_CRYPTO)
    assert len(res.bars) == 1 and res.dropped.wick == 1


def test_the_other_hygiene_rules_are_unchanged_at_the_crypto_threshold():
    rows = [GOOD,
            row("2024-01-03", None, 101.0, 99.0, 100.0),                       # a value missing
            row("2024-01-04", 100.0, 101.0, 0.0, 100.0),                       # not above zero
            row("2024-01-05", 100.0, 99.0, 101.0, 100.0),                      # high below low
            row("2024-01-06", 140.0, 110.0, 95.0, 100.0),                      # a stray open: not a rule
            row("2024-01-02", 100.0, 101.5, 99.5, 101.0),                      # a duplicate: the last row wins
            ("not-a-date", 1.0, 1.0, 1.0, 1.0)]
    res = clean(rows, max_wick=data.MAX_WICK_CRYPTO)
    assert [b.ts.date().isoformat() for b in res.bars] == ["2024-01-02", "2024-01-06"]
    assert res.bars[0].close == 101.0
    d = res.dropped
    assert (d.missing, d.inverted, d.wick, d.duplicate, d.unparsable) == (2, 1, 0, 1, 1)


def test_the_default_behaviour_of_clean_rows_is_the_15_percent_rule_exactly_as_before():
    rows = [("2024-01-03", 100.0, 115.0, 85.0, 100.0), ("2024-01-04", 100.0, 115.1, 85.0, 100.0)]
    res = clean(rows)
    assert len(res.bars) == 1 and res.dropped.wick == 1                          # exactly 15 % is kept


def test_the_default_reads_the_module_constant_when_it_is_called(monkeypatch):
    rows = [row("2024-01-03", 100.0, 130.0, 99.0, 100.0)]
    assert clean(rows).dropped.wick == 1
    monkeypatch.setattr(data, "MAX_WICK", 0.35)
    assert clean(rows).dropped.wick == 0


# ------------------------------------------------------------------ load_daily and daily_bars
def fetch_with(rows):
    return lambda symbol, interval: list(rows)


ROWS = [GOOD, row("2024-01-03", 100.0, 130.0, 99.0, 100.0), row("2024-01-04", 100.0, 101.0, 99.0, 100.2)]
TODAY = dt.date(2024, 2, 1)


def test_load_daily_passes_the_threshold_to_the_cleaning():
    default = data.load_daily("X", fetch=fetch_with(ROWS), today=TODAY)
    assert len(default.bars) == 2 and default.dropped.wick == 1
    crypto = data.load_daily("X", fetch=fetch_with(ROWS), today=TODAY, max_wick=data.MAX_WICK_CRYPTO)
    assert len(crypto.bars) == 3 and crypto.dropped.wick == 0


def test_daily_bars_passes_the_threshold_too():
    assert len(data.daily_bars("X", fetch=fetch_with(ROWS), today=TODAY)) == 2
    assert len(data.daily_bars("X", fetch=fetch_with(ROWS), today=TODAY, max_wick=0.6)) == 3


def test_the_threshold_is_not_part_of_what_the_cache_stores(tmp_path):
    # the cache holds raw rows, so a coin read through it is cleaned at whatever threshold the load asks for
    calls: list = []

    def fetch(symbol, interval):
        calls.append(symbol)
        return list(ROWS)
    cached = data.cached_fetch(fetch, tmp_path)
    assert len(data.load_daily("SOL-USD", fetch=cached, today=TODAY).bars) == 2
    assert len(data.load_daily("SOL-USD", fetch=cached, today=TODAY, max_wick=0.6).bars) == 3
    assert calls == ["SOL-USD"]


# ------------------------------------------------------------------ round 1 is untouched
def walk_rows(symbol, wick_row):
    rng = random.Random(symbol)
    price, rows, day = 100.0, [], dt.date(2015, 1, 5)
    while day <= dt.date(2016, 12, 30):
        o = price
        price = max(1.0, price * (1 + rng.gauss(0.0004, 0.011)))
        rows.append((day.isoformat(), o, max(o, price) * 1.003, min(o, price) * 0.997, price))
        day += DAY
    d, o, h, low, c = rows[wick_row]
    rows[wick_row] = (d, o, c * 1.30, low, c)                         # one bar with a high 30 % over its close
    return rows


def test_round_ones_run_still_cleans_bitcoin_and_ether_at_15_percent(tmp_path):
    def fetch(symbol, interval):
        return walk_rows(symbol, 200) if interval == "1d" else []
    universe = (ins.by_symbol("BTC-USD"), ins.by_symbol("ETH-USD"))
    out = research.run(fetch=fetch, now=dt.datetime(2017, 1, 10, tzinfo=UTC), cache_dir=tmp_path / "cache",
                       out_dir=tmp_path / "out", universe=universe, write=False, log=lambda m: None)
    daily = {c.symbol: c for c in out.results.coverage if c.interval == "1d"}
    assert set(daily) == {"BTC-USD", "ETH-USD"}
    for symbol, coverage in daily.items():
        assert coverage.dropped.wick == 1, symbol            # the 30 % bar is dropped: round 1's 15 % rule
    assert not (tmp_path / "out").exists()


def test_round_ones_module_never_asks_for_the_crypto_threshold():
    import inspect
    assert "max_wick" not in inspect.getsource(research) and "MAX_WICK_CRYPTO" not in inspect.getsource(research)
