"""cfd/setups.py, round 3: FX-REV (`fx_rev`, H6) on crafted bars with hand-computed values. Where one
comparison needs its boundary tested the indicator is stubbed to a number worked out by hand; the
indicators themselves are tested in test_cfd_indicators.py and test_cfd_r3_indicators.py."""
from __future__ import annotations

import datetime as dt
import math

import pytest

from cfd import indicators as ind
from cfd import setups
from cfd.data import Bar

UTC = dt.timezone.utc
DAY = dt.timedelta(days=1)
D0 = dt.datetime(2024, 1, 1, tzinfo=UTC)


def mk(rows, start=D0):
    return [Bar(start + i * DAY, o, h, low, c) for i, (o, h, low, c) in enumerate(rows)]


def mirror(rows, k):
    return [(k - o, k - low, k - h, k - c) for (o, h, low, c) in rows]


def flat(n, price=100.0, half_range=0.5):
    return [(price, price + half_range, price - half_range, price)] * n


def climb(n, start=100.0, step=0.5, wick=0.25):
    rows, c = [], start
    for _ in range(n):
        o, c = c, c + step
        rows.append((o, c + wick, o - wick, c))
    return rows


def test_the_parameters_are_the_preregistered_ones():
    assert setups.REV_SMA_LEN == 20 and setups.REV_SD_LEN == 20
    assert setups.REV_BAND_SD == 2.0 and setups.REV_STOP_ATR == 2.0
    assert setups.REV_ADX_MAX == 20.0
    assert setups.ADX_LEN == 14 and setups.ATR_LEN == 14


# 40 flat bars (open = close = 100, high 100.5, low 99.5: every true range 1.0, ADX 0), then bar 40
# opens 100 and closes 97 with high 100.5 and low 96.5:
#   TR = max(4, |100.5 - 100|, |96.5 - 100|) = 4  -> ATR(14) = (1 * 13 + 4) / 14 = 17/14
#   SMA20 = (19 * 100 + 97) / 20 = 99.85
#   population variance = (19 * 0.15^2 + 2.85^2) / 20 = (0.4275 + 8.1225) / 20 = 0.4275,  SD20 = 0.653835
#   lower band = 99.85 - 2 * 0.653835 = 98.54233 > 97 -> long
#   ADX(14) at bar 40: -DM = 99.5 - 96.5 = 3, +DM = 0; -DI = 100 * 3 / 17, +DI = 0, DX = 100 %;
#   ADX = (0 * 13 + 1) / 14 * 100 = 7.14 < 20
#   stop = 97 - 2 * 17/14 = 94.571429
DROP = flat(40) + [(100.0, 100.5, 96.5, 97.0)]
ATR_DROP = 17 / 14


def test_a_close_under_the_lower_band_in_a_range_signals_a_long_with_its_stop_2_atr_below():
    bars = mk(DROP)
    assert ind.adx(bars)[40] == pytest.approx(100 / 14)
    assert ind.sma(ind.closes(bars), 20)[40] == pytest.approx(99.85)
    assert ind.stdev(ind.closes(bars), 20)[40] == pytest.approx(math.sqrt(0.4275))
    sigs = setups.fx_rev(bars, min_bars=30)
    assert [(s.index, s.side) for s in sigs] == [(40, "long")]
    assert sigs[0].atr == pytest.approx(ATR_DROP)
    assert sigs[0].stop == pytest.approx(97.0 - 2.0 * ATR_DROP)
    assert sigs[0].stop == pytest.approx(94.5714286)


def test_the_mirror_image_signals_a_short_with_its_stop_2_atr_above():
    # prices through 200: close 103 > SMA20 100.15 + 2 * 0.653835 = 101.457
    sigs = setups.fx_rev(mk(mirror(DROP, 200.0)), min_bars=30)
    assert [(s.index, s.side) for s in sigs] == [(40, "short")]
    assert sigs[0].atr == pytest.approx(ATR_DROP)
    assert sigs[0].stop == pytest.approx(103.0 + 2.0 * ATR_DROP)


def test_a_flat_series_makes_no_signal():
    assert setups.fx_rev(mk(flat(60)), min_bars=30) == []


def test_exactly_on_the_band_is_not_a_signal_and_the_comparison_is_strict(monkeypatch):
    # SMA20 stubbed to 100 and SD20 to 2.0: the bands are 96 and 104 exactly
    n = 45
    monkeypatch.setattr(setups.ind, "sma", lambda values, length: [100.0] * len(values))
    monkeypatch.setattr(setups.ind, "stdev", lambda values, length: [2.0] * len(values))
    monkeypatch.setattr(setups.ind, "adx", lambda bars, length: [10.0] * len(bars))

    def with_last_close(c):
        rows = flat(n - 1) + [(100.0, 100.5, 95.0, c)]
        rows[-1] = (100.0, max(100.5, c + 0.5), min(95.0, c - 0.5), c)
        return mk(rows)
    assert setups.fx_rev(with_last_close(97.0), min_bars=30) == []      # inside the bands
    assert setups.fx_rev(with_last_close(103.0), min_bars=30) == []
    assert setups.fx_rev(with_last_close(96.0), min_bars=30) == []
    assert setups.fx_rev(with_last_close(104.0), min_bars=30) == []
    assert [(s.index, s.side) for s in setups.fx_rev(with_last_close(95.99), min_bars=30)] == [(n - 1, "long")]
    assert [(s.index, s.side) for s in setups.fx_rev(with_last_close(104.01), min_bars=30)] == [(n - 1, "short")]


def test_adx_must_be_strictly_under_20(monkeypatch):
    bars = mk(DROP)
    monkeypatch.setattr(setups.ind, "adx", lambda b, length: [19.99] * len(b))
    assert [s.index for s in setups.fx_rev(bars, min_bars=30)] == [40]
    monkeypatch.setattr(setups.ind, "adx", lambda b, length: [20.0] * len(b))
    assert setups.fx_rev(bars, min_bars=30) == []
    monkeypatch.setattr(setups.ind, "adx", lambda b, length: [25.0] * len(b))
    assert setups.fx_rev(bars, min_bars=30) == []


def test_a_crash_after_a_steady_climb_is_a_trend_and_makes_no_signal():
    # 40 bars climbing 0.5 a bar, then a drop of 12: far under SMA20 - 2 * SD20, but ADX(14) is high
    rows = climb(40)
    last = rows[-1][3]
    rows.append((last, last + 0.25, last - 12.25, last - 12.0))
    bars = mk(rows)
    closes = ind.closes(bars)
    assert closes[40] < ind.sma(closes, 20)[40] - 2.0 * ind.stdev(closes, 20)[40]
    assert ind.adx(bars)[40] >= 20.0
    assert setups.fx_rev(bars, min_bars=30) == []


def test_no_signal_before_min_bars_and_none_while_an_indicator_is_missing():
    bars = mk(DROP)
    assert [s.index for s in setups.fx_rev(bars, min_bars=40)] == [40]
    assert setups.fx_rev(bars, min_bars=41) == []
    # ADX needs 27 bars, SMA20 / SD20 20, ATR 14: a drop at bar 22 has no ADX yet
    early = mk(flat(22) + [(100.0, 100.5, 96.5, 97.0)])
    assert setups.fx_rev(early, min_bars=0) == []


def test_the_default_needs_300_bars_of_history():
    assert setups.fx_rev(mk(DROP)) == []
    long_ = mk(flat(300) + [(100.0, 100.5, 96.5, 97.0)])
    assert [s.index for s in setups.fx_rev(long_)] == [300]


def test_a_signal_never_depends_on_later_bars():
    bars = mk(DROP + [(97.0, 97.5, 96.0, 96.5)] * 5)
    assert setups.fx_rev(bars, min_bars=30)[0] == setups.fx_rev(bars[:41], min_bars=30)[0]
    assert setups.fx_rev(bars[:40], min_bars=30) == []
