"""cfd/setups.py: each setup's trigger and stop on crafted series, both directions.

Long cases are written out; the short side is the same series reflected (o, h, l, c) ->
(K - o, K - l, K - h, K - c), which turns every long condition into its mirror, so one crafted
series proves both directions. Where a single condition needs its boundary tested (ADX 20, the
1.0 ATR dip, EMA rising), the indicator is stubbed to a number worked out by hand -- the
indicators themselves are tested in test_cfd_indicators.py."""
from __future__ import annotations

import dataclasses
import datetime as dt
import random

import pytest

from cfd import indicators as ind
from cfd import setups
from cfd.data import LONDON_SESSION, Bar, Session

UTC = dt.timezone.utc
DAY = dt.timedelta(days=1)
HOUR = dt.timedelta(hours=1)
D0 = dt.datetime(2024, 1, 1, tzinfo=UTC)


def mk(rows, start=D0, step=DAY):
    """Bars from (open, high, low, close) rows."""
    return [Bar(start + i * step, o, h, low, c) for i, (o, h, low, c) in enumerate(rows)]


def mirror(rows, k):
    return [(k - o, k - low, k - h, k - c) for (o, h, low, c) in rows]


def uptrend(n, start=100.0):
    """n bars, each opening at the previous close and closing 1 higher, with 0.5 wicks: every true
    range is 2, so ATR is exactly 2 and ADX is 100."""
    rows, c = [], start
    for _ in range(n):
        o, c = c, c + 1
        rows.append((o, c + 0.5, o - 0.5, c))
    return rows


def pullback_tail(level):
    """A 4-bar pullback from `level` and a trigger bar that closes back above the previous high."""
    return [
        (level, level + 0.2, level - 2.5, level - 2.0),
        (level - 2.0, level - 1.8, level - 4.5, level - 4.0),
        (level - 4.0, level - 3.8, level - 6.0, level - 5.5),
        (level - 5.5, level - 2.5, level - 5.8, level - 3.0),     # the trigger
    ]


PB_ROWS = uptrend(60) + pullback_tail(160.0)        # the trigger is bar 63
K = 400.0


def indices(signals):
    return [s.index for s in signals]


def pb_both(rows, **kw):
    """PB-D signals of the series and of its mirror image."""
    return setups.pb_d(mk(rows), **kw), setups.pb_d(mk(mirror(rows, K)), **kw)


def hand_atr_at_63():
    # TR is 2 on bars 0-59 (ATR 2); then bars 60..63 have TR 2.7, 2.7, 2.2, 3.3 and ATR is
    # Wilder's (prev*13 + TR)/14
    atr = 2.0
    for tr in (2.7, 2.7, 2.2, 3.3):
        atr = (atr * 13 + tr) / 14
    return atr


def test_a_signal_is_a_frozen_record_of_index_side_stop_atr():
    s = setups.Signal(5, "long", 99.0, 1.5)
    assert [f.name for f in dataclasses.fields(s)] == ["index", "side", "stop", "atr"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.stop = 1.0  # type: ignore[misc]


@pytest.fixture
def neutral(monkeypatch):
    """ATR 2, ADX 50 and a rising EMA far below the price on every bar, so a test about one
    window or one comparison isn't also a test about the indicators."""
    monkeypatch.setattr(setups.ind, "atr", lambda bars, length=14: [2.0] * len(bars))
    monkeypatch.setattr(setups.ind, "adx", lambda bars, length=14: [50.0] * len(bars))
    monkeypatch.setattr(setups.ind, "ema", lambda values, length: [50.0 + 0.1 * i
                                                                   for i in range(len(values))])


# ================================================================== PB-D
def test_pb_d_long_signal_on_a_pullback_in_an_uptrend():
    longs, _ = pb_both(PB_ROWS, min_bars=60)
    assert len(longs) == 1
    s = longs[0]
    atr = hand_atr_at_63()
    assert (s.index, s.side) == (63, "long")
    assert s.atr == pytest.approx(atr)
    # stop = lowest low of the last 5 bars (154.0, bar 62) - 0.5 ATR
    assert s.stop == pytest.approx(154.0 - 0.5 * atr)
    assert s.stop == pytest.approx(152.9053662536)


def test_pb_d_short_signal_is_the_mirror_image():
    _, shorts = pb_both(PB_ROWS, min_bars=60)
    assert len(shorts) == 1
    s = shorts[0]
    atr = hand_atr_at_63()
    assert (s.index, s.side) == (63, "short")
    assert s.atr == pytest.approx(atr)
    # stop = highest high of the last 5 bars (K - 154.0) + 0.5 ATR
    assert s.stop == pytest.approx(K - 154.0 + 0.5 * atr)


def test_pb_d_pure_uptrend_signals_start_once_the_ema_and_its_lag_exist():
    # EMA50 exists from bar 49; "five bars earlier" needs bar 54 -- no signal before it
    sigs = setups.pb_d(mk(uptrend(70)), min_bars=0)
    assert sigs and min(indices(sigs)) == 54


def test_pb_d_trigger_close_must_be_strictly_above_the_previous_high():
    # the previous bar's high is 156.2
    def rows(close, high=157.5):
        return uptrend(60) + pullback_tail(160.0)[:3] + [(154.5, high, 154.2, close)]
    for close in (156.2, 156.0):                       # equal, and below
        assert pb_both(rows(close), min_bars=60) == ([], [])
    longs, shorts = pb_both(rows(156.3), min_bars=60)
    assert indices(longs) == [63] and indices(shorts) == [63]


def test_pb_d_trigger_close_must_not_be_below_the_open():
    # close above the previous high but below its own open: no trigger
    red = uptrend(60) + pullback_tail(160.0)[:3] + [(157.2, 157.5, 154.2, 157.0)]
    assert pb_both(red, min_bars=60) == ([], [])
    # close == open (a doji) still triggers: the rule is close >= open
    doji = uptrend(60) + pullback_tail(160.0)[:3] + [(157.0, 157.5, 154.2, 157.0)]
    longs, shorts = pb_both(doji, min_bars=60)
    assert indices(longs) == [63] and indices(shorts) == [63]


def test_pb_d_dip_must_be_at_least_one_atr_below_the_swing_high(monkeypatch):
    # swing high 160.5, pullback low 154.0: the dip is exactly 6.5. With ATR 6.5 it qualifies
    # (<=), with 6.6 it does not; the stop is 154.0 - 0.5 * ATR
    adx = lambda bars, length=14: [50.0] * len(bars)          # noqa: E731
    monkeypatch.setattr(setups.ind, "adx", adx)
    monkeypatch.setattr(setups.ind, "atr", lambda bars, length=14: [6.5] * len(bars))
    longs, shorts = pb_both(PB_ROWS, min_bars=60)
    assert indices(longs) == [63] and indices(shorts) == [63]
    assert longs[0].stop == pytest.approx(154.0 - 0.5 * 6.5)
    monkeypatch.setattr(setups.ind, "atr", lambda bars, length=14: [6.6] * len(bars))
    assert pb_both(PB_ROWS, min_bars=60) == ([], [])


def test_pb_d_adx_must_be_at_least_20(monkeypatch):
    monkeypatch.setattr(setups.ind, "adx", lambda bars, length=14: [20.0] * len(bars))
    longs, shorts = pb_both(PB_ROWS, min_bars=60)
    assert indices(longs) == [63] and indices(shorts) == [63]
    monkeypatch.setattr(setups.ind, "adx", lambda bars, length=14: [19.999] * len(bars))
    assert pb_both(PB_ROWS, min_bars=60) == ([], [])
    monkeypatch.setattr(setups.ind, "adx", lambda bars, length=14: [None] * len(bars))
    assert pb_both(PB_ROWS, min_bars=60) == ([], [])


def test_pb_d_needs_the_ema_to_be_rising_and_the_close_above_it(monkeypatch):
    def with_ema(values):
        monkeypatch.setattr(setups.ind, "ema", lambda vals, length: list(values(len(vals))))
    n = len(PB_ROWS)
    with_ema(lambda m: [100.0 + 0.1 * i for i in range(m)])     # rising, below the close of 157
    assert indices(setups.pb_d(mk(PB_ROWS), min_bars=60)) == [63]
    with_ema(lambda m: [100.0] * m)                              # flat: not "greater than"
    assert setups.pb_d(mk(PB_ROWS), min_bars=60) == []
    with_ema(lambda m: [100.0 - 0.1 * i for i in range(m)])     # falling
    assert setups.pb_d(mk(PB_ROWS), min_bars=60) == []
    with_ema(lambda m: [200.0 + 0.1 * i for i in range(m)])     # rising but above the close
    assert setups.pb_d(mk(PB_ROWS), min_bars=60) == []
    with_ema(lambda m: [None] * m)
    assert setups.pb_d(mk(PB_ROWS), min_bars=60) == []
    assert n == 64


def test_pb_d_short_needs_the_ema_falling_and_the_close_below_it(monkeypatch):
    mirrored = mk(mirror(PB_ROWS, K))
    monkeypatch.setattr(setups.ind, "ema", lambda v, length: [300.0 - 0.1 * i for i in range(len(v))])
    assert indices(setups.pb_d(mirrored, min_bars=60)) == [63]
    monkeypatch.setattr(setups.ind, "ema", lambda v, length: [300.0] * len(v))
    assert setups.pb_d(mirrored, min_bars=60) == []


def test_pb_d_the_swing_high_looks_back_ten_bars_including_the_current(neutral):
    # flat bars (range 1, ATR stubbed to 2); a trigger bar closing just above the previous high.
    # Dip needs pullback low <= swing high - 2: only a spike inside the last 10 bars can provide it
    def rows(spike_back):
        r = [(100.0, 100.5, 99.5, 100.0)] * 20
        r[19 - spike_back] = (100.0, 103.0, 99.5, 100.0)
        r[19] = (100.0, 100.8, 99.5, 100.6)                    # close 100.6 > previous high
        return r
    # (the spike bar itself is the previous bar when spike_back == 1, so the trigger needs the
    #  spike's own high to be beaten: keep spike_back >= 2)
    assert indices(setups.pb_d(mk(rows(9)), min_bars=1)) == [19]       # bar t-9: in the window
    assert setups.pb_d(mk(rows(10)), min_bars=1) == []                  # bar t-10: out of it


def test_pb_d_the_pullback_low_looks_back_five_bars_including_the_current(neutral):
    # a low spike inside the last 5 bars makes the dip (swing high 100.8 - 2 = 98.8)
    def rows(spike_back):
        r = [(100.0, 100.5, 99.5, 100.0)] * 20
        r[19 - spike_back] = (100.0, 100.5, 98.0, 100.0)
        r[19] = (100.0, 100.8, 99.5, 100.6)
        return r
    sigs = setups.pb_d(mk(rows(4)), min_bars=1)                          # bar t-4: in the window
    assert indices(sigs) == [19]
    assert sigs[0].stop == pytest.approx(98.0 - 0.5 * 2.0)              # the stop uses that low
    assert setups.pb_d(mk(rows(5)), min_bars=1) == []                    # bar t-5: out of it


def test_pb_d_default_needs_300_bars_before_the_first_signal():
    bars = mk(uptrend(330))
    everything = setups.pb_d(bars, min_bars=0)
    assert indices(everything)[0] == 54
    by_default = setups.pb_d(bars)
    assert by_default and min(indices(by_default)) == 300
    assert by_default == [s for s in everything if s.index >= 300]


def test_pb_d_with_too_little_history_is_empty():
    assert setups.pb_d(mk(uptrend(40)), min_bars=0) == []
    assert setups.pb_d([], min_bars=0) == []


def _random_walk(n, seed):
    rng = random.Random(seed)
    price, rows = 100.0, []
    for _ in range(n):
        o = price
        price *= 1 + rng.gauss(0.0004, 0.01)
        rows.append((o, max(o, price) * (1 + abs(rng.gauss(0, 0.003))),
                     min(o, price) * (1 - abs(rng.gauss(0, 0.003))), price))
    return rows


def test_pb_d_signals_are_ordered_one_per_bar_and_never_see_the_future():
    bars = mk(_random_walk(700, 3))
    full = setups.pb_d(bars, min_bars=60)
    assert full, "the random series should produce some signals"
    assert indices(full) == sorted(set(indices(full)))
    for k in (200, 333, 500):
        assert setups.pb_d(bars[:k], min_bars=60) == [s for s in full if s.index < k]


# ================================================================== BO-D
FLAT = [(100.0, 100.5, 99.5, 100.0)] * 320
BREAKOUT = (100.0, 103.5, 99.8, 103.0)
KB = 200.0


def bo_both(rows, **kw):
    return setups.bo_d(mk(rows), **kw), setups.bo_d(mk(mirror(rows, KB)), **kw)


def test_bo_d_long_breakout_stop_is_close_minus_two_and_a_half_atr():
    longs, _ = bo_both(FLAT + [BREAKOUT])
    assert len(longs) == 1
    s = longs[0]
    # TR of the breakout bar = max(3.7, |103.5-100|, |99.8-100|) = 3.7; ATR = (1*13 + 3.7)/14
    atr = (1.0 * 13 + 3.7) / 14
    assert (s.index, s.side) == (320, "long")
    assert s.atr == pytest.approx(atr)
    assert s.stop == pytest.approx(103.0 - 2.5 * atr)
    assert s.stop == pytest.approx(100.0178571429)


def test_bo_d_short_breakout_is_the_mirror_image():
    _, shorts = bo_both(FLAT + [BREAKOUT])
    atr = (1.0 * 13 + 3.7) / 14
    assert len(shorts) == 1
    s = shorts[0]
    assert (s.index, s.side) == (320, "short")
    assert s.stop == pytest.approx(97.0 + 2.5 * atr)


def test_bo_d_close_must_be_strictly_above_the_previous_55_bar_high():
    equal = (100.0, 100.5, 99.9, 100.5)               # close == the 55-bar high of 100.5
    assert bo_both(FLAT + [equal]) == ([], [])
    above = (100.0, 100.7, 99.9, 100.6)
    longs, shorts = bo_both(FLAT + [above])
    assert indices(longs) == [320] and indices(shorts) == [320]


def test_bo_d_a_high_above_the_channel_without_a_close_above_it_is_no_breakout():
    wick = (100.0, 105.0, 99.8, 100.4)
    assert bo_both(FLAT + [wick]) == ([], [])


def test_bo_d_the_channel_is_the_previous_55_bars_the_signal_bar_excluded():
    def rows(spike_at):
        r = list(FLAT)
        r[spike_at] = (100.0, 104.0, 99.5, 100.0)      # a high above the breakout close of 103
        return r + [BREAKOUT]
    # the breakout is bar 320; its previous 55 bars are 265..319
    assert bo_both(rows(265)) == ([], [])              # t-55: inside the channel, blocks it
    longs, shorts = bo_both(rows(264))                 # t-56: outside, no longer blocks
    assert indices(longs) == [320] and indices(shorts) == [320]


def test_bo_d_close_must_be_above_the_200_day_average(monkeypatch):
    longs, _ = bo_both(FLAT + [BREAKOUT])
    assert indices(longs) == [320]
    monkeypatch.setattr(setups.ind, "sma", lambda v, length: [103.0] * len(v))     # equal: no
    assert setups.bo_d(mk(FLAT + [BREAKOUT])) == []
    monkeypatch.setattr(setups.ind, "sma", lambda v, length: [102.99] * len(v))
    assert indices(setups.bo_d(mk(FLAT + [BREAKOUT]))) == [320]
    monkeypatch.setattr(setups.ind, "sma", lambda v, length: [None] * len(v))
    assert setups.bo_d(mk(FLAT + [BREAKOUT])) == []


def test_bo_d_breakout_below_a_high_200_day_average_is_no_signal():
    # 200 bars around 150, then 120 bars around 100, then a breakout to 103: above its 55-bar
    # channel but far below the 200-day average (about 130)
    rows = [(150.0, 150.5, 149.5, 150.0)] * 200 + [(100.0, 100.5, 99.5, 100.0)] * 120
    rows = rows + [BREAKOUT]
    bars = mk(rows)
    assert ind.sma(ind.closes(bars), 200)[-1] > 103.0          # (the premise)
    assert 320 not in indices(setups.bo_d(bars))


def test_bo_d_the_average_is_exactly_200_bars_long():
    # a synthetic outlier close of 797: if it is inside the 200-bar window the average is
    # (797 + 198*100 + 103)/200 = 103.5, above the breakout close of 103, which blocks the signal;
    # one bar further back it is outside and the average is (199*100 + 103)/200 = 100.015
    def rows(outlier_back):
        r = list(FLAT)
        r[320 - outlier_back] = (797.0, 797.5, 796.5, 797.0)
        return r + [BREAKOUT]
    assert setups.bo_d(mk(rows(199))) == []                       # bar 121: the oldest in the window
    assert indices(setups.bo_d(mk(rows(200)))) == [320]           # bar 120: just out of it


def test_bo_d_default_needs_300_bars_before_the_first_signal():
    rows = [(100.0, 100.5, 99.5, 100.0)] * 250 + [BREAKOUT] + [(103.0, 103.5, 102.5, 103.0)] * 5
    assert setups.bo_d(mk(rows)) == []                          # bar 250 is before bar 300
    assert indices(setups.bo_d(mk(rows), min_bars=200)) == [250]


def test_bo_d_with_too_little_history_is_empty():
    assert setups.bo_d(mk(FLAT[:100] + [BREAKOUT]), min_bars=0) == []      # no 200-day average
    assert setups.bo_d([], min_bars=0) == []


def test_bo_d_signals_are_ordered_and_never_see_the_future():
    bars = mk(_random_walk(900, 5))
    full = setups.bo_d(bars, min_bars=210)
    assert full
    assert indices(full) == sorted(set(indices(full)))
    for k in (400, 650):
        assert setups.bo_d(bars[:k], min_bars=210) == [s for s in full if s.index < k]


# ================================================================== PB-H1-GOLD
def gold_bars(last_ts, *, n_base=320, reflect=False):
    """Hourly bars ending at `last_ts`: a steady climb (TR 2 on every bar) from 100, then the
    same 4-bar pullback and trigger as PB_ROWS, at the level where the climb ended (420)."""
    base = uptrend(n_base)
    level = base[-1][3]
    rows = base + pullback_tail(level)
    if reflect:
        rows = mirror(rows, 1000.0)
    n = len(rows)
    return [Bar(last_ts - (n - 1 - i) * HOUR, o, h, low, c)
            for i, (o, h, low, c) in enumerate(rows)], level


def utc(y, m, d, h):
    return dt.datetime(y, m, d, h, tzinfo=UTC)


def test_htf_ema_uses_the_previous_completed_four_hour_bar():
    # one day of hourly bars; the close of each 4-hour block is the close of its last hour (the
    # other hours close at 5 so a wrong pick shows). Block closes: 10, 12, 11, 14, 13, 15.
    closes = {3: 10.0, 7: 12.0, 11: 11.0, 15: 14.0, 19: 13.0, 23: 15.0}
    day = utc(2024, 1, 3, 0)
    bars = [Bar(day + h * HOUR, 5.0, 20.0, 0.5, closes.get(h, 5.0)) for h in range(24)]
    htf, old = setups.htf_emas(bars, length=2)
    # EMA(2) of the block closes (alpha 2/3, seeded with the mean of the first two):
    #   block:  0     1     2     3     4         5
    #   EMA:   None  11    11    13    13        14.3333
    # an hourly bar in block k sees EMA[k-1] and EMA[k-2]
    expected = {
        0: (None, None), 3: (None, None),            # block 0
        4: (None, None), 7: (None, None),            # block 1: EMA[0] doesn't exist yet
        8: (11.0, None), 11: (11.0, None),           # block 2: EMA[1], EMA[0]
        12: (11.0, 11.0), 15: (11.0, 11.0),          # block 3: EMA[2], EMA[1]
        16: (13.0, 11.0), 19: (13.0, 11.0),          # block 4: EMA[3], EMA[2]
        20: (13.0, 13.0), 23: (13.0, 13.0),          # block 5: EMA[4], EMA[3]
    }
    for h, want in expected.items():
        got = (htf[h], old[h])
        assert got == (pytest.approx(want[0]) if want[0] is not None else None,
                       pytest.approx(want[1]) if want[1] is not None else None), h
    assert len(htf) == len(old) == 24


def test_htf_ema_default_is_the_ema_50_of_the_4h_closes():
    # 30 days of continuous hourly bars starting at 00:00 UTC, so block k is bars 4k..4k+3 and its
    # close is bar 4k+3's close: derived here by index arithmetic, not by timestamps
    rng = random.Random(9)
    price, bars = 100.0, []
    for i in range(30 * 24):
        price *= 1 + rng.uniform(-0.004, 0.004)
        bars.append(Bar(utc(2024, 1, 1, 0) + i * HOUR, price, price * 1.001, price * 0.999, price))
    block_closes = [bars[4 * k + 3].close for k in range(len(bars) // 4)]
    ema = ind.ema(block_closes, 50)
    htf, old = setups.htf_emas(bars)
    assert ema[49] is not None and ema[48] is None
    for i in range(len(bars)):
        k = i // 4
        want = ema[k - 1] if k >= 1 else None
        want_old = ema[k - 2] if k >= 2 else None
        assert htf[i] == (pytest.approx(want) if want is not None else None), i
        assert old[i] == (pytest.approx(want_old) if want_old is not None else None), i
    # the first block with an EMA behind it is block 50 (it sees EMA[49], the first EMA value),
    # and the first with two is block 51
    assert htf[4 * 49 + 3] is None and htf[4 * 50] is not None
    assert old[4 * 50 + 3] is None and old[4 * 51] is not None


def test_htf_ema_follows_the_blocks_that_exist_when_the_market_was_shut():
    # hours 8-11 are missing (no bars): the "previous completed 4h bar" is the previous block that
    # has data. Block closes in order: 10 (00-03), 12 (04-07), 14 (12-15), 13 (16-19), 15 (20-23)
    closes = {3: 10.0, 7: 12.0, 15: 14.0, 19: 13.0, 23: 15.0}
    day = utc(2024, 1, 3, 0)
    hours = [h for h in range(24) if not 8 <= h <= 11]
    bars = [Bar(day + h * HOUR, 5.0, 20.0, 0.5, closes.get(h, 5.0)) for h in hours]
    htf, old = setups.htf_emas(bars, length=2)
    at = {b.ts.hour: (htf[i], old[i]) for i, b in enumerate(bars)}
    # EMA(2): None, 11, 13, 13, 14.3333 over the five blocks
    assert at[12] == (pytest.approx(11.0), None)                       # block 2 sees EMA[1], EMA[0]
    assert at[16] == (pytest.approx(13.0), pytest.approx(11.0))
    assert at[23] == (pytest.approx(13.0), pytest.approx(13.0))


@pytest.mark.parametrize("trigger_utc, in_session", [
    (utc(2024, 1, 17, 6), False),     # January, London = UTC: 06:00 is before 07:00
    (utc(2024, 1, 17, 7), True),
    (utc(2024, 1, 17, 10), True),
    (utc(2024, 1, 17, 15), True),     # the 15:00 bar closes at 16:00: its start is inside
    (utc(2024, 1, 17, 16), False),    # a bar starting at 16:00 is outside
    (utc(2024, 7, 17, 5), False),     # July, London = UTC+1: 05:00 UTC is 06:00 London
    (utc(2024, 7, 17, 6), True),      # 07:00 London
    (utc(2024, 7, 17, 14), True),     # 15:00 London
    (utc(2024, 7, 17, 15), False),    # 16:00 London
])
def test_gold_long_signal_only_for_a_bar_starting_inside_the_london_session(trigger_utc, in_session):
    bars, level = gold_bars(trigger_utc)
    last = len(bars) - 1
    got = [s for s in setups.pb_h1_gold(bars) if s.index == last]
    if not in_session:
        assert got == []
        return
    atr = hand_atr_at_63()                     # the same bar shapes as PB-D's case: ATR 2.18927
    (s,) = got
    assert s.side == "long"
    assert s.atr == pytest.approx(atr)
    # stop = pullback low (level - 6.0) - 0.5 ATR
    assert s.stop == pytest.approx(level - 6.0 - 0.5 * atr)
    assert s.stop == pytest.approx(412.9053662536)


def test_gold_short_signal_is_the_mirror_image():
    bars, level = gold_bars(utc(2024, 1, 17, 10), reflect=True)
    last = len(bars) - 1
    (s,) = [s for s in setups.pb_h1_gold(bars) if s.index == last]
    assert s.side == "short"
    assert s.stop == pytest.approx(1000.0 - (level - 6.0) + 0.5 * hand_atr_at_63())


def test_gold_every_signal_is_in_session_and_after_the_first_300_bars():
    bars, _ = gold_bars(utc(2024, 1, 17, 10))
    sigs = setups.pb_h1_gold(bars)
    assert sigs and min(indices(sigs)) >= 300
    assert all(LONDON_SESSION.contains(bars[s.index].ts) for s in sigs)


def test_gold_trend_needs_close_above_a_rising_htf_ema(monkeypatch):
    bars, _ = gold_bars(utc(2024, 1, 17, 10))
    n = len(bars)
    last = n - 1                                   # the close there is 417.0

    def with_htf(ema, old):
        monkeypatch.setattr(setups, "htf_emas", lambda b, *a, **k: ([ema] * len(b), [old] * len(b)))
        return [s.index for s in setups.pb_h1_gold(bars)]
    assert last in with_htf(400.0, 399.0)           # close above a rising EMA
    assert last not in with_htf(400.0, 400.0)       # flat: not rising
    assert last not in with_htf(400.0, 401.0)       # falling
    assert last not in with_htf(417.0, 416.0)       # close == EMA: not above
    assert last not in with_htf(500.0, 499.0)       # close below the EMA
    assert last not in with_htf(None, None)
    assert last not in with_htf(400.0, None)        # no EMA a block earlier yet


def test_gold_short_trend_needs_close_below_a_falling_htf_ema(monkeypatch):
    bars, _ = gold_bars(utc(2024, 1, 17, 10), reflect=True)     # last close 1000 - 417 = 583
    last = len(bars) - 1

    def with_htf(ema, old):
        monkeypatch.setattr(setups, "htf_emas", lambda b, *a, **k: ([ema] * len(b), [old] * len(b)))
        return [s.index for s in setups.pb_h1_gold(bars)]
    assert last in with_htf(600.0, 601.0)
    assert last not in with_htf(600.0, 600.0)
    assert last not in with_htf(600.0, 599.0)


def test_gold_needs_the_4h_ema_history_before_any_signal():
    bars, _ = gold_bars(utc(2024, 1, 17, 10), n_base=60)        # 64 hourly bars: 16 blocks
    assert setups.pb_h1_gold(bars, min_bars=0) == []


def test_gold_session_can_be_replaced():
    bars, _ = gold_bars(utc(2024, 1, 17, 20))                   # trigger bar at 20:00 UTC
    last = len(bars) - 1
    assert last not in indices(setups.pb_h1_gold(bars))
    allday = Session(0, 24, "UTC")
    assert last in indices(setups.pb_h1_gold(bars, session=allday))
