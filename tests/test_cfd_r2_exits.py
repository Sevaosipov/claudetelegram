"""cfd/exits.py, round 2: the four-take-profit ladder EL (quarters at +0.5R, +1.0R, +1.5R, +2.0R; the stop
stays at its initial level until TP2, moves to the entry after TP2 and to TP1 after TP3), the one-part
exit E0c (a stop, optionally trailing by a configurable multiple of ATR), the time stop ("the open of
the Nth bar after entry"), the exit-on-condition hook ("the next open after the condition is true"),
and the configurable upper bound of the R rule -- plus a regression that pins round 1's E0, E1 and E2
to the numbers the untouched round-1 code produced.

State-machine scenarios run twice, as written (a long: entry 100, stop 96, so R = 4 and the EL
take-profits are 102, 104, 106, 108) and reflected through K = 200 (a short: entry 100, stop 104), and
the two must agree event for event, as in test_cfd_exits.py."""
from __future__ import annotations

import dataclasses
import datetime as dt
import random

import pytest

from cfd import exits
from cfd import indicators as ind
from cfd import instruments as ins
from cfd import setups
from cfd.data import LONDON_SESSION, Bar
from cfd.setups import Signal

UTC = dt.timezone.utc
DAY = dt.timedelta(days=1)
D0 = dt.datetime(2024, 1, 1, tzinfo=UTC)
K = 200.0


def b(o, h, low, c, ts=D0):
    return Bar(ts, float(o), float(h), float(low), float(c))


def flip(bar):
    return Bar(bar.ts, K - bar.open, K - bar.low, K - bar.high, K - bar.close)


def run_both(kind, bars, *, atr=2.0, atrs=None, **state_kw):
    """Step `bars` through a fresh long state and a fresh short state (the reflected bars); assert the
    short events are the long ones reflected; return (long events, long state)."""
    out = {}
    for side in ("long", "short"):
        state = (exits.LadderState("long", 100.0, 96.0, kind, **state_kw) if side == "long"
                 else exits.LadderState("short", 100.0, 104.0, kind, **state_kw))
        events = []
        for i, bar in enumerate(bars):
            a = atrs[i] if atrs is not None else atr
            events += state.step(bar if side == "long" else flip(bar), a)
        out[side] = (events, state)
    long_events, long_state = out["long"]
    short_events, short_state = out["short"]
    assert len(long_events) == len(short_events)
    for le, se in zip(long_events, short_events):
        assert (le.kind, le.label, le.fraction, le.parts, le.stage, le.gap) == \
               (se.kind, se.label, se.fraction, se.parts, se.stage, se.gap)
        assert se.price == pytest.approx(K - le.price)
        assert (le.stop is None) == (se.stop is None)
        if le.stop is not None:
            assert se.stop == pytest.approx(K - le.stop)
    assert (long_state.stage, long_state.remaining, long_state.closed) == \
           (short_state.stage, short_state.remaining, short_state.closed)
    assert short_state.stop == pytest.approx(K - long_state.stop)
    return long_events, long_state


def gross_r(events, entry=100.0, r=4.0):
    return sum(e.fraction * (e.price - entry) / r for e in events)


# a clean climb: each bar fills the next EL target and its low stays above the stop that fill sets
EL_CLIMB = [
    b(100.5, 102.2, 100.2, 102.0),     # TP1 at 102 (+0.5R): the stop stays at 96
    b(102.0, 104.3, 101.5, 104.0),     # TP2 at 104 (+1R): the stop to the entry (100)
    b(104.0, 106.2, 103.0, 106.0),     # TP3 at 106 (+1.5R): the stop to TP1 (102)
    b(106.0, 108.4, 105.0, 108.0),     # TP4 at 108 (+2R): closed
]


# ================================================================== construction
def test_round_two_kinds_are_known_and_round_ones_are_unchanged():
    assert exits.EXIT_KINDS == ("E0", "E1", "E2")          # research.py iterates these: frozen
    assert (exits.E0C, exits.EL) == ("E0c", "EL")
    assert exits.ROUND2_EXIT_KINDS == ("E0c", "EL")
    for kind in exits.EXIT_KINDS + exits.ROUND2_EXIT_KINDS:
        assert exits.LadderState("long", 100.0, 96.0, kind).kind == kind
    with pytest.raises(ValueError):
        exits.LadderState("long", 100.0, 96.0, "E9")


def test_el_state_starts_whole_at_the_initial_stop():
    s = exits.LadderState("long", 100.0, 96.0, "EL")
    assert (s.stage, s.remaining, s.stop, s.closed, s.trail) == (0, 1.0, 96.0, False, None)
    assert [s.level(m) for m in (0.5, 1.0, 1.5, 2.0)] == [102.0, 104.0, 106.0, 108.0]


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_a_trail_multiple_must_be_positive(bad):
    with pytest.raises(ValueError):
        exits.LadderState("long", 100.0, 96.0, "E0c", trail_atr=bad)


# ================================================================== EL: the take-profits and the stop schedule
def test_el_a_quarter_at_each_of_half_one_one_and_a_half_and_two_r():
    events, state = run_both("EL", EL_CLIMB)
    assert [(e.kind, e.label, e.price, e.fraction, e.stage, e.stop) for e in events] == [
        ("tp", "TP1", 102.0, 0.25, 1, 96.0),         # the stop is still the initial one
        ("tp", "TP2", 104.0, 0.25, 2, 100.0),        # the stop to the entry
        ("tp", "TP3", 106.0, 0.25, 3, 102.0),        # the stop to TP1
        ("tp", "TP4", 108.0, 0.25, 4, None),         # the last quarter: closed
    ]
    assert all(e.parts == (e.label,) and not e.gap for e in events)
    assert state.closed and state.remaining == 0.0
    assert gross_r(events) == pytest.approx(1.25)    # 0.25 * (0.5 + 1 + 1.5 + 2)


def test_el_after_tp1_the_stop_is_still_the_initial_stop():
    # a bar that dips to 96.5 would stop the remainder at the entry under E1's schedule; here the stop
    # is still 96, so it holds -- and only the dip through 96 stops three quarters out, at -1R
    bars = [EL_CLIMB[0], b(101.5, 101.8, 96.5, 97.0), b(97.0, 97.5, 95.5, 96.0)]
    events, state = run_both("EL", bars)
    assert [e.label for e in events] == ["TP1", "STOP"]
    stop = events[1]
    assert (stop.kind, stop.price, stop.fraction, stop.stage, stop.parts, stop.gap) == (
        "stop", 96.0, 0.75, 1, ("TP2", "TP3", "TP4"), False)
    assert gross_r(events) == pytest.approx(0.25 * 0.5 + 0.75 * -1.0)       # -0.625


def test_el_after_tp2_the_stop_moves_to_the_entry():
    events, state = run_both("EL", EL_CLIMB[:2] + [b(103.5, 103.8, 99.5, 100.2)])
    stop = events[-1]
    assert (stop.kind, stop.price, stop.fraction, stop.stage, stop.parts) == (
        "stop", 100.0, 0.5, 2, ("TP3", "TP4"))
    assert gross_r(events) == pytest.approx(0.25 * 0.5 + 0.25 * 1.0)         # half the position at 0


def test_el_after_tp3_the_stop_moves_to_tp1():
    events, _ = run_both("EL", EL_CLIMB[:3] + [b(105.5, 105.8, 101.5, 102.5)])
    stop = events[-1]
    assert (stop.kind, stop.price, stop.fraction, stop.stage, stop.parts) == (
        "stop", 102.0, 0.25, 3, ("TP4",))
    assert gross_r(events) == pytest.approx(0.25 * (0.5 + 1.0 + 1.5) + 0.25 * 0.5)       # 0.875


def test_el_the_stop_is_checked_before_the_first_target_within_a_bar():
    # range 95..103 covers the initial stop (96) and TP1 (102): conservative -> the stop, all of it
    events, state = run_both("EL", [b(100.0, 103.0, 95.0, 99.0)])
    (e,) = events
    assert (e.kind, e.price, e.fraction, e.stage, e.parts, e.gap) == (
        "stop", 96.0, 1.0, 0, ("TP1", "TP2", "TP3", "TP4"), False)
    assert gross_r(events) == pytest.approx(-1.0)


def test_el_a_bar_that_reaches_tp2_and_trades_back_through_the_entry_stops_the_rest_there():
    # TP1 (102) and TP2 (104) are reached; the stop moves to the entry (100) and the bar's low of
    # 99.0 takes it before TP3 -- the stop is checked again after each fill
    events, state = run_both("EL", [b(100.5, 104.5, 99.0, 100.5)])
    assert [(e.kind, e.label, e.price, e.fraction) for e in events] == [
        ("tp", "TP1", 102.0, 0.25), ("tp", "TP2", 104.0, 0.25), ("stop", "STOP", 100.0, 0.5)]
    assert gross_r(events) == pytest.approx(0.375)


def test_el_a_bar_that_reaches_tp3_and_trades_back_through_tp1_stops_the_last_quarter_there():
    events, _ = run_both("EL", EL_CLIMB[:2] + [b(104.0, 106.5, 101.5, 105.0)])
    assert [(e.label, e.price, e.fraction) for e in events[2:]] == [("TP3", 106.0, 0.25), ("STOP", 102.0, 0.25)]


def test_el_a_bar_that_opens_beyond_targets_fills_them_at_the_open():
    # opens at 107: above TP1, TP2 and TP3 -> all three fill at 107; TP4 (108) is not reached
    events, state = run_both("EL", [b(107.0, 107.5, 106.2, 107.2)])
    assert [(e.label, e.price, e.gap) for e in events] == [
        ("TP1", 107.0, True), ("TP2", 107.0, True), ("TP3", 107.0, True)]
    assert (state.stage, state.remaining, state.stop) == (3, 0.25, 102.0)
    assert gross_r(events) == pytest.approx(0.25 * 3 * 7 / 4)


def test_el_a_stop_gapped_through_fills_at_the_open():
    events, _ = run_both("EL", [b(94.0, 95.0, 93.0, 94.5)])
    (e,) = events
    assert (e.kind, e.price, e.gap, e.fraction) == ("stop", 94.0, True, 1.0)
    assert gross_r(events) == pytest.approx(-1.5)


def test_el_a_gap_through_the_initial_stop_after_tp1():
    events, _ = run_both("EL", [EL_CLIMB[0], b(94.0, 95.0, 93.0, 94.5)])
    stop = events[-1]
    assert (stop.kind, stop.price, stop.gap, stop.fraction) == ("stop", 94.0, True, 0.75)
    assert gross_r(events) == pytest.approx(0.25 * 0.5 + 0.75 * -1.5)


def test_el_a_gap_through_the_stop_at_the_entry_after_tp2():
    events, _ = run_both("EL", EL_CLIMB[:2] + [b(99.0, 100.5, 98.5, 100.0)])
    stop = events[-1]
    assert (stop.kind, stop.price, stop.gap, stop.fraction, stop.stage) == ("stop", 99.0, True, 0.5, 2)
    assert gross_r(events) == pytest.approx(0.125 + 0.25 + 0.5 * -0.25)


def test_el_a_bar_opening_at_a_target_after_the_stop_moved_does_not_gap_the_stop():
    # TP1 fills at the open (105 is above 102 and 104 -> TP1, TP2 at the open), the stop moves to the
    # entry, and the bar's low of 99 reaches it by trading down, not by a gap at the open
    events, _ = run_both("EL", [b(105.0, 105.5, 99.0, 100.0)])
    assert [(e.label, e.price, e.gap) for e in events] == [
        ("TP1", 105.0, True), ("TP2", 105.0, True), ("STOP", 100.0, False)]
    assert gross_r(events) == pytest.approx(0.25 * 5 / 4 * 2)


def test_el_has_no_trailing_stop_and_ignores_a_hard_target():
    # a bar to 101.9 trails a stop under E0; under EL the stop stays at 96
    events, state = run_both("EL", [b(100.0, 101.9, 99.0, 101.0), b(101, 101.9, 99, 100)], target_r=5.0)
    assert events == [] and state.stop == 96.0 and state.trail is None


def test_el_runs_the_remainder_to_the_close_at_a_price():
    s = exits.LadderState("long", 100.0, 96.0, "EL")
    s.step(EL_CLIMB[0])
    (e,) = s.close_at(103.0, "time")
    assert (e.kind, e.label, e.price, e.fraction, e.stage, e.stop, e.gap, e.parts) == (
        "time", "TIME", 103.0, 0.75, 1, None, False, ("TP2", "TP3", "TP4"))
    assert s.closed and s.close_at(99.0, "time") == []


def test_el_a_state_copied_midway_continues_exactly_like_the_original():
    bars = EL_CLIMB[:2] + [b(104.0, 105.0, 102.5, 104.5), b(104.5, 106.4, 104.0, 106.0),
                           b(106.0, 106.5, 101.0, 102.0)]
    whole = exits.LadderState("long", 100.0, 96.0, "EL")
    events = []
    for bar in bars:
        events += whole.step(bar, 2.0)
    for cut in range(1, len(bars)):
        first = exits.LadderState("long", 100.0, 96.0, "EL")
        head = []
        for bar in bars[:cut]:
            head += first.step(bar, 2.0)
        resumed = dataclasses.replace(first)
        tail = []
        for bar in bars[cut:]:
            tail += resumed.step(bar, 2.0)
        assert head + tail == events and resumed == whole


# ================================================================== E0c: one part, a stop, an optional trail
def test_e0c_without_a_trail_keeps_its_initial_stop_whatever_the_price_does():
    events, state = run_both("E0c", [b(100.5, 110.0, 100.2, 109.0)])
    assert events == [] and state.stop == 96.0
    # ... and leaves at that stop when it is reached
    events, state = run_both("E0c", [b(100.5, 110.0, 100.2, 109.0), b(109, 109.5, 95.5, 96.0)])
    (e,) = events
    assert (e.kind, e.label, e.price, e.fraction, e.parts, e.stage) == ("stop", "STOP", 96.0, 1.0, ("ALL",), 0)
    assert gross_r(events) == pytest.approx(-1.0)


def test_e0c_has_no_take_profit_and_no_hard_target():
    events, state = run_both("E0c", [b(100.5, 150.0, 100.2, 149.0)])
    assert events == [] and not state.closed


def test_e0c_trails_six_atr_below_the_best_price_and_never_loosens():
    bars = [b(100.5, 103.0, 100.0, 102.5),      # high 103 -> 103 - 12 = 91, below the initial stop: stays 96
            b(102.5, 110.0, 102.0, 109.0),      # high 110 -> 98 (effective next bar)
            b(109.0, 109.8, 105.0, 106.0),      # high 109.8 -> 97.8 < 98: the stop stays at 98
            b(106.0, 107.0, 97.5, 98.2)]        # low 97.5 <= 98: stopped at 98
    for n, want in ((1, 96.0), (2, 98.0), (3, 98.0)):
        events, state = run_both("E0c", bars[:n], trail_atr=6.0)
        assert events == [] and state.stop == pytest.approx(want), n
    events, state = run_both("E0c", bars, trail_atr=6.0)
    (e,) = events
    assert (e.kind, e.price, e.fraction) == ("stop", 98.0, 1.0)
    assert gross_r(events) == pytest.approx(-0.5)


def test_e0c_a_new_trailing_stop_applies_from_the_next_bar_not_the_same_one():
    events, state = run_both("E0c", [b(100.0, 110.0, 97.0, 105.0)], trail_atr=6.0)
    assert events == [] and state.stop == 98.0       # the low of 97 is only tested against 96


def test_e0c_the_trail_multiple_is_what_is_given_not_three():
    # the same bars under E0 (3 ATR -> 104) and E0c (6 ATR -> 98): the trailing distance differs
    bars = [b(100.5, 110.0, 100.2, 109.0)]
    _, e0 = run_both("E0", bars)
    _, e0c = run_both("E0c", bars, trail_atr=6.0)
    assert (e0.stop, e0c.stop) == (104.0, 98.0)


def test_e0c_without_an_atr_does_not_trail():
    events, state = run_both("E0c", [b(100.5, 110.0, 100.2, 109.0)], atr=None, trail_atr=6.0)
    assert events == [] and state.stop == 96.0


def test_the_trail_multiple_also_sets_e0s_and_e2s_trailing_distance():
    _, e0 = run_both("E0", [b(100.5, 110.0, 100.2, 109.0)], trail_atr=1.0)
    assert e0.stop == 108.0                          # 110 - 1 * 2
    # E2: after TP3 (stop at 108) a bar to 118 trails the runner to 118 - 2 = 116; the next bar
    # trades down to 115.5 and takes it. With the default 3 ATR the runner would stop at 112
    climb = [b(100.5, 104.2, 100.2, 104.0), b(104.0, 108.3, 104.2, 108.0), b(108.0, 112.2, 108.1, 112.0)]
    bars = climb + [b(112.0, 118.0, 111.0, 117.0), b(117.0, 117.5, 115.5, 116.5)]
    events, _ = run_both("E2", bars, trail_atr=1.0)
    stop = events[-1]
    assert (stop.kind, stop.price, stop.fraction, stop.stage) == ("stop", 116.0, 0.25, 3)
    events, _ = run_both("E2", bars)
    assert events[-1].price == 112.0


def test_e0c_closes_at_a_price_when_told_to_leave():
    s = exits.LadderState("long", 100.0, 96.0, "E0c")
    (e,) = s.close_at(101.0, "signal")
    assert (e.kind, e.label, e.price, e.fraction, e.parts) == ("signal", "SIGNAL", 101.0, 1.0, ("ALL",))


# ================================================================== simulate: the new exits end to end
def daily(rows, start=D0):
    return [Bar(start + i * DAY, float(o), float(h), float(low), float(c))
            for i, (o, h, low, c) in enumerate(rows)]


PRE = [(100, 100.5, 99.5, 100)] * 3                        # bars 0-2; the signal is read at bar 2
FX = ins.by_symbol("EURUSD=X").costs                       # round trip 0.015, financing 0.010 a night
LONG = Signal(2, "long", 96.0, 2.0)
SHORT = Signal(2, "short", 104.0, 2.0)                      # entry 100 and ATR 2 as the long's: R = 4


def sim(bars, signal, kind, **kw):
    kw.setdefault("costs", FX)
    kw.setdefault("atr", [2.0] * len(bars))
    return exits.simulate(bars, signal, kind, **kw)


def both_sides(rows):
    long_bars = daily(rows)
    return long_bars, [flip(x) for x in long_bars]


def drift_rows(n, entry_open=100.0):
    """n bars whose opens differ by a cent (so the bar a fill happened on shows in its price) and whose
    range (open -/+ 0.6) stays clear of the stop (96) and the first take-profit (102) of the R = 4 trade."""
    return [(entry_open + 0.01 * i, entry_open + 0.01 * i + 0.6, entry_open + 0.01 * i - 0.6,
             entry_open + 0.01 * i) for i in range(n)]


def test_simulate_runs_the_el_ladder_and_charges_the_nights_held():
    # entry at 100 on bar 3; the four take-profits on bars 4..7: gross 1.25R over 4 nights
    rows = PRE + [(100.0, 101.0, 99.0, 100.5)] + [
        (100.5, 102.2, 100.2, 102.0), (102.0, 104.3, 101.5, 104.0),
        (104.0, 106.2, 103.0, 106.0), (106.0, 108.4, 105.0, 108.0)]
    for bars, sig in zip(both_sides(rows), (LONG, SHORT)):
        t = sim(bars, sig, "EL")
        assert t.status == "closed" and t.exit_kind == "EL"
        assert (t.entry_index, t.exit_index, t.nights) == (3, 7, 4)
        assert [p.label for p in t.parts] == ["TP1", "TP2", "TP3", "TP4"]
        assert t.gross_r == pytest.approx(1.25)
        assert t.cost_r == pytest.approx((0.015 + 4 * 0.010) / 100 * 100 / 4)
        assert t.result_r == pytest.approx(1.25 - 0.01375)


def test_simulate_el_stop_schedule_in_r():
    # TP1 then the initial stop (96): -0.625R gross; TP1, TP2 then the entry: +0.375R
    rows = PRE + [(100.0, 101.0, 99.0, 100.5), (100.5, 102.2, 100.2, 102.0),
                  (101.5, 101.8, 96.5, 97.0), (97.0, 97.5, 95.5, 96.0)]
    t = sim(daily(rows), LONG, "EL")
    assert [(p.label, p.price, p.fraction) for p in t.parts] == [("TP1", 102.0, 0.25), ("STOP", 96.0, 0.75)]
    assert t.gross_r == pytest.approx(-0.625)


# ------------------------------------------------------------------ the time stop
def test_time_stop_leaves_at_the_open_of_the_nth_bar_after_the_entry():
    # entry bar 3 (open 100.0); the 16th bar after it is bar 19: it leaves at that bar's open
    # (100.16) even though the bar's range (it trades down to 95) would have hit the stop at 96
    rows = PRE + drift_rows(16) + [(100.16, 101.0, 95.0, 97.0), (97.0, 97.5, 96.5, 97.0)]
    for bars, sig in zip(both_sides(rows), (LONG, SHORT)):
        t = sim(bars, sig, "EL", time_stop_bars=16)
        assert t.status == "closed"
        assert (t.entry_index, t.exit_index) == (3, 19)
        assert [(p.kind, p.label, p.fraction, p.index) for p in t.parts] == [("time", "TIME", 1.0, 19)]
        assert t.parts[0].price == pytest.approx(bars[19].open)
        assert t.nights == 16
        assert t.cost_r == pytest.approx((0.015 + 16 * 0.010) / 100 * 100 / 4)
        assert t.exit_ts == D0 + 19 * DAY
    long_bars, _ = both_sides(rows)
    t = sim(long_bars, LONG, "EL", time_stop_bars=16)
    # a 0.16 gain over R = 4, less the 16 nights: the whole result in R
    assert t.gross_r == pytest.approx(0.16 / 4)
    assert t.result_r == pytest.approx(0.04 - 0.04375)


def test_time_stop_exit_price_is_that_bars_open_for_long_and_short():
    rows = PRE + drift_rows(16) + [(100.19, 100.9, 99.6, 100.4), (100.4, 100.9, 99.9, 100.4)]
    long_bars, short_bars = both_sides(rows)
    tl = sim(long_bars, LONG, "EL", time_stop_bars=16)
    ts = sim(short_bars, SHORT, "EL", time_stop_bars=16)
    assert tl.parts[0].price == pytest.approx(long_bars[19].open)
    assert ts.parts[0].price == pytest.approx(short_bars[19].open)
    assert tl.gross_r == pytest.approx((long_bars[19].open - 100.0) / 4)
    assert ts.gross_r == pytest.approx(tl.gross_r)


@pytest.mark.parametrize("n, exit_bar", [(1, 4), (11, 14), (15, 18), (16, 19), (17, 20)])
def test_time_stop_counts_bars_after_the_entry_bar_exactly(n, exit_bar):
    rows = PRE + drift_rows(25)                            # bars 3..27, the entry bar being bar 3
    bars = daily(rows)
    t = sim(bars, LONG, "E0c", time_stop_bars=n)
    assert (t.entry_index, t.exit_index) == (3, exit_bar)
    assert t.parts[0].price == bars[exit_bar].open and t.parts[0].kind == "time"
    assert t.nights == n


def test_time_stop_with_the_data_ending_one_bar_early_is_still_open():
    rows = PRE + drift_rows(16)                            # bars 3..18: the 16th bar after entry is 19
    assert sim(daily(rows), LONG, "EL", time_stop_bars=16).status == "open"
    assert sim(daily(rows + [(100.2, 100.8, 99.6, 100.4)]), LONG, "EL", time_stop_bars=16).status == "closed"


def test_time_stop_takes_what_is_left_after_some_take_profits():
    rows = PRE + [(100.0, 101.0, 99.0, 100.5), (100.5, 102.2, 100.2, 102.0)] + [
        (101.0, 101.8, 100.6, 101.0)] * 2 + [(101.5, 101.9, 100.5, 101.0)]
    t = sim(daily(rows), LONG, "EL", time_stop_bars=4)      # bar 3 enters; bar 7 is the 4th after it
    assert [(p.label, p.price, p.fraction) for p in t.parts] == [("TP1", 102.0, 0.25), ("TIME", 101.5, 0.75)]
    assert t.gross_r == pytest.approx(0.25 * 0.5 + 0.75 * 1.5 / 4)


def test_a_stop_before_the_time_stop_wins():
    rows = PRE + drift_rows(5) + [(100.0, 100.5, 95.5, 96.5)] + drift_rows(12)
    t = sim(daily(rows), LONG, "EL", time_stop_bars=16)
    assert [p.kind for p in t.parts] == ["stop"] and t.exit_index == 8
    assert t.parts[0].price == 96.0


def test_time_stop_on_a_bar_that_gaps_through_the_stop_leaves_at_the_open_too():
    rows = PRE + drift_rows(16) + [(94.0, 95.0, 93.0, 94.5)]
    t = sim(daily(rows), LONG, "EL", time_stop_bars=16)
    assert t.parts[0].price == 94.0
    assert t.gross_r == pytest.approx(-1.5)


def test_time_stop_must_be_a_positive_whole_number():
    bars = daily(PRE + drift_rows(5))
    for bad in (0, -3, 2.5, True):
        with pytest.raises(ValueError):
            sim(bars, LONG, "EL", time_stop_bars=bad)


# ------------------------------------------------------------------ the exit condition
def test_the_condition_leaves_at_the_next_open_after_the_bar_where_it_is_true():
    rows = PRE + drift_rows(10)
    seen = []

    def cond(j, side):
        seen.append((j, side))
        return j == 6
    bars = daily(rows)
    t = sim(bars, LONG, "E0c", exit_when=cond)
    assert t.status == "closed" and (t.entry_index, t.exit_index) == (3, 7)
    assert [(p.kind, p.label, p.fraction, p.price) for p in t.parts] == [
        ("signal", "SIGNAL", 1.0, bars[7].open)]
    assert seen == [(j, "long") for j in range(3, 7)]       # asked after the entry bar's close, from bar 3 on
    assert t.nights == 4


def test_the_condition_is_asked_with_the_trades_side():
    seen = []
    long_bars, short_bars = both_sides(PRE + drift_rows(6))
    sim(short_bars, SHORT, "E0c", exit_when=lambda j, side: seen.append(side) or j == 5)
    assert set(seen) == {"short"}


def test_a_condition_true_at_the_signal_bar_is_before_the_entry_and_does_not_count():
    bars = daily(PRE + drift_rows(10))
    t = sim(bars, LONG, "E0c", exit_when=lambda j, side: j <= 2)
    assert t.status == "open" and t.entry_index == 3


def test_a_condition_true_at_the_entry_bars_own_close_leaves_at_the_open_after_it():
    bars = daily(PRE + drift_rows(10))
    t = sim(bars, LONG, "E0c", exit_when=lambda j, side: j == 3)
    assert (t.entry_index, t.exit_index) == (3, 4)
    assert t.parts[0].price == bars[4].open and t.nights == 1


def test_a_stop_hit_during_the_bar_comes_before_the_condition_read_at_its_close():
    rows = PRE + drift_rows(2) + [(100.0, 100.5, 95.5, 96.5)] + drift_rows(6)
    t = sim(daily(rows), LONG, "E0c", exit_when=lambda j, side: j == 5)
    assert [(p.kind, p.price) for p in t.parts] == [("stop", 96.0)] and t.exit_index == 5


def test_a_condition_exit_into_a_gap_through_the_stop_still_leaves_at_the_open():
    rows = PRE + drift_rows(2) + [(100.2, 100.6, 99.8, 100.3), (94.0, 95.0, 93.0, 94.5)] + drift_rows(3)
    t = sim(daily(rows), LONG, "E0c", exit_when=lambda j, side: j == 5)
    assert t.exit_index == 6 and t.parts[0].price == 94.0 and t.parts[0].kind == "signal"
    assert t.gross_r == pytest.approx(-1.5)


def test_the_condition_takes_what_is_left_after_take_profits():
    rows = PRE + [(100.0, 101.0, 99.0, 100.5), (100.5, 102.2, 100.2, 102.0), (101.5, 101.9, 100.6, 101.2),
                  (101.3, 101.9, 100.8, 101.0), (101.4, 101.9, 100.9, 101.0)]
    t = sim(daily(rows), LONG, "EL", exit_when=lambda j, side: j == 5)
    assert [(p.label, p.price, p.fraction) for p in t.parts] == [("TP1", 102.0, 0.25), ("SIGNAL", 101.3, 0.75)]
    assert t.exit_index == 6


def test_the_earlier_of_the_condition_and_the_time_stop_wins():
    bars = daily(PRE + drift_rows(20))
    t = sim(bars, LONG, "E0c", exit_when=lambda j, side: j == 8, time_stop_bars=11)
    assert (t.exit_index, t.parts[0].kind) == (9, "signal")
    t = sim(bars, LONG, "E0c", exit_when=lambda j, side: False, time_stop_bars=11)
    assert (t.exit_index, t.parts[0].kind) == (14, "time")
    t = sim(bars, LONG, "E0c", exit_when=lambda j, side: j == 20, time_stop_bars=11)
    assert (t.exit_index, t.parts[0].kind) == (14, "time")
    # a condition read at the close of bar 13 and the time stop both fall on the open of bar 14:
    # one exit, at that open, reported as the time stop
    t = sim(bars, LONG, "E0c", exit_when=lambda j, side: j == 13, time_stop_bars=11)
    assert t.exit_index == 14 and t.parts[0].price == bars[14].open
    assert [p.kind for p in t.parts] == ["time"]


def test_the_condition_with_the_data_ending_before_the_next_open_is_still_open():
    bars = daily(PRE + drift_rows(4))                        # bars 3..6; true at 6: the next open is not there
    assert sim(bars, LONG, "E0c", exit_when=lambda j, side: j == 6).status == "open"


# ------------------------------------------------------------------ the trailing stop and the R rule
def test_e0c_with_a_six_atr_trail_in_a_daily_trade():
    rows = PRE + [(100.0, 101.0, 99.0, 100.5),
                  (100.5, 110.0, 100.0, 109.0),             # high 110 -> trailing stop 98
                  (109.0, 109.5, 97.5, 98.5)]               # stopped at 98
    long_bars, short_bars = both_sides(rows)
    for bars, sig in ((long_bars, LONG), (short_bars, SHORT)):
        t = sim(bars, sig, "E0c", trail_atr=6.0)
        assert [(p.kind, p.price) for p in t.parts] == [("stop", pytest.approx(98.0 if sig is LONG else K - 98.0))]
        assert t.gross_r == pytest.approx(-0.5) and t.exit_index == 5 and t.nights == 2
    # the same bars under E0's three ATR trail 110 - 6 = 104 and stop there for +1R
    t3 = sim(long_bars, LONG, "E0")
    assert t3.gross_r == pytest.approx(1.0) and t3.parts[0].price == 104.0


def test_e0c_without_a_trail_in_a_daily_trade_leaves_only_at_the_initial_stop():
    rows = PRE + [(100.0, 101.0, 99.0, 100.5), (100.5, 110.0, 100.0, 109.0), (109.0, 109.5, 97.5, 98.5),
                  (98.5, 99.0, 95.0, 95.5)]
    t = sim(daily(rows), LONG, "E0c")
    assert t.parts[0].price == 96.0 and t.exit_index == 6 and t.gross_r == pytest.approx(-1.0)


@pytest.mark.parametrize("entry_open, skipped", [
    (112.0, False),         # R = 16 = 8 ATR: not "greater than 8 ATR"
    (112.1, True),          # R = 16.1 > 16
    (108.1, False),         # R = 12.1 is over 6 ATR but fine under the raised bound
    (96.5, True),           # R = 0.5 = 0.25 ATR: still skipped at the low end
    (94.0, True),           # gapped through the stop
])
def test_the_upper_bound_of_the_r_rule_can_be_raised(entry_open, skipped):
    rows = PRE + [(entry_open, entry_open + 0.5, entry_open - 0.5, entry_open), (entry_open,) * 4]
    t = sim(daily(rows), LONG, "EL", max_r_atr=8.0)
    assert (t.status == "skipped") == skipped
    if skipped and entry_open > 100:
        assert t.note == "R > 8 ATR" and t.r == pytest.approx(entry_open - 96.0)


def test_the_default_upper_bound_stays_six_atr():
    rows = PRE + [(108.1, 108.6, 107.6, 108.1), (108.1,) * 4]
    t = sim(daily(rows), LONG, "EL")
    assert t.status == "skipped" and t.note == "R > 6 ATR"


def test_the_upper_bound_must_be_positive():
    for bad in (0.0, -1.0):
        with pytest.raises(ValueError):
            sim(daily(PRE + drift_rows(3)), LONG, "EL", max_r_atr=bad)


def test_simulate_accepts_the_round_two_kinds_and_refuses_others():
    bars = daily(PRE + drift_rows(3))
    for kind in ("E0c", "EL", "E0", "E1", "E2"):
        sim(bars, LONG, kind)
    with pytest.raises(ValueError):
        sim(bars, LONG, "E3")


def test_simulate_computes_the_atr_itself_for_the_six_atr_trail():
    flat = [(100, 100.5, 99.5, 100)] * 30
    bars = daily(flat + [(100, 112, 100, 111), (111, 111.5, 97, 98)])
    sig = Signal(28, "long", 94.0, 1.0)                      # entry 100: R = 6 = 6 ATR
    auto = exits.simulate(bars, sig, "E0c", costs=FX, trail_atr=6.0)
    explicit = exits.simulate(bars, sig, "E0c", costs=FX, atr=ind.atr(bars), trail_atr=6.0)
    assert auto == explicit and auto.status == "closed"
    assert auto.parts[0].price == pytest.approx(112 - 6 * (13 * 1.0 + 12) / 14)


# ================================================================== round 1 is byte for byte what it was
def _walk(n, seed, drift=0.0006, vol=0.012):
    rng = random.Random(seed)
    price, rows = 100.0, []
    for _ in range(n):
        o = price
        price *= 1 + rng.gauss(drift, vol)
        rows.append((o, max(o, price) * (1 + abs(rng.gauss(0, 0.004))),
                     min(o, price) * (1 - abs(rng.gauss(0, 0.004))), price))
    return rows


# What the untouched round-1 simulate produced over these seeded series (signals of PB-D and BO-D
# on 900 random-walk bars): (signals, closed, skipped, open, total R, gross R, cost R, nights, sum of
# exit bar indices). Recorded before round 2 changed exits.py; any drift in E0, E1 or E2 shows here.
GOLDEN_DAILY = {
    (21, "E0", "EURUSD=X"): (223, 223, 0, 0, 31.80281395, 42.12178377, 10.31896982, 3784, 109735),
    (21, "E0", "^GSPC"): (223, 223, 0, 0, 26.34384384, 42.12178377, 15.77793993, 3784, 109735),
    (21, "E1", "EURUSD=X"): (223, 217, 0, 6, -5.51698921, 7.0, 12.51698921, 5220, 106044),
    (21, "E1", "^GSPC"): (223, 217, 0, 6, -11.22854665, 7.0, 18.22854665, 5220, 106044),
    (21, "E2", "EURUSD=X"): (223, 217, 0, 6, -4.17003767, 8.1514627, 12.32150037, 5094, 105918),
    (21, "E2", "^GSPC"): (223, 217, 0, 6, -10.19330989, 8.1514627, 18.34477259, 5094, 105918),
    (22, "E0", "EURUSD=X"): (261, 258, 0, 3, 31.28340511, 44.00211983, 12.71871471, 4600, 118714),
    (22, "E0", "^GSPC"): (261, 258, 0, 3, 26.59037806, 44.00211983, 17.41174176, 4600, 118714),
    (22, "E1", "EURUSD=X"): (261, 252, 0, 9, 3.18419598, 19.0, 15.81580402, 7097, 116468),
    (22, "E1", "^GSPC"): (261, 252, 0, 9, -2.86793638, 19.0, 21.86793638, 7097, 116468),
    (22, "E2", "EURUSD=X"): (261, 252, 0, 9, 2.74230399, 18.40186752, 15.65956353, 7012, 116383),
    (22, "E2", "^GSPC"): (261, 252, 0, 9, -3.10605328, 18.40186752, 21.5079208, 7012, 116383),
}
GOLDEN_E0_TARGET5 = {21: (223, 33.67667587), 22: (258, 32.10843279)}       # (closed, total R)
# (signals, closed, total R, sum of exit bars)
GOLDEN_GOLD = {
    "E0": (261, 261, 16.90676263, 408687),
    "E1": (261, 261, 21.70650054, 408703),
    "E2": (261, 261, 21.70650054, 408703),
}


@pytest.mark.parametrize("seed", [21, 22])
def test_round_one_daily_exits_give_the_numbers_they_gave_before_round_two(seed):
    bars = daily(_walk(900, seed))
    atr = ind.atr(bars)
    signals = setups.pb_d(bars, min_bars=60) + setups.bo_d(bars, min_bars=210)
    for kind in ("E0", "E1", "E2"):
        for symbol in ("EURUSD=X", "^GSPC"):
            costs = ins.by_symbol(symbol).costs
            closed = skipped = open_ = nights = exit_sum = 0
            total = gross = cost = 0.0
            for sig in signals:
                t = exits.simulate(bars, sig, kind, costs=costs, atr=atr)
                if t.status == "closed":
                    closed += 1
                    total, gross, cost = total + t.result_r, gross + t.gross_r, cost + t.cost_r
                    nights, exit_sum = nights + t.nights, exit_sum + t.exit_index
                elif t.status == "skipped":
                    skipped += 1
                else:
                    open_ += 1
            want = GOLDEN_DAILY[(seed, kind, symbol)]
            assert (len(signals), closed, skipped, open_, nights, exit_sum) == (
                want[0], want[1], want[2], want[3], want[7], want[8]), (kind, symbol)
            assert (total, gross, cost) == pytest.approx(want[4:7], abs=1e-6), (kind, symbol)


@pytest.mark.parametrize("seed", [21, 22])
def test_round_one_e0_hard_target_gives_the_numbers_it_gave_before_round_two(seed):
    bars = daily(_walk(900, seed))
    atr = ind.atr(bars)
    signals = setups.pb_d(bars, min_bars=60) + setups.bo_d(bars, min_bars=210)
    trades = [exits.simulate(bars, s, "E0", costs=FX, atr=atr, e0_target_r=5.0) for s in signals]
    done = [t for t in trades if t.status == "closed"]
    assert (len(done), sum(t.result_r for t in done)) == (
        GOLDEN_E0_TARGET5[seed][0], pytest.approx(GOLDEN_E0_TARGET5[seed][1], abs=1e-6))


def test_round_one_gold_session_exits_give_the_numbers_they_gave_before_round_two():
    rng = random.Random(77)
    price, ts, bars = 400.0, dt.datetime(2024, 1, 2, 0, tzinfo=UTC), []
    while len(bars) < 3000:
        if ts.weekday() < 5:
            o = price
            price *= 1 + rng.gauss(0.0002, 0.002)
            bars.append(Bar(ts, o, max(o, price) * (1 + abs(rng.gauss(0, 0.0007))),
                            min(o, price) * (1 - abs(rng.gauss(0, 0.0007))), price))
        ts += dt.timedelta(hours=1)
    atr = ind.atr(bars)
    signals = setups.pb_h1_gold(bars, min_bars=60)
    for kind, want in GOLDEN_GOLD.items():
        trades = [exits.simulate(bars, s, kind, costs=ins.gold_intraday_costs(), entry="close",
                                 session=LONDON_SESSION, atr=atr,
                                 e0_target_r=5.0 if kind == "E0" else None) for s in signals]
        done = [t for t in trades if t.status == "closed"]
        assert (len(signals), len(done), sum(t.exit_index for t in done)) == (want[0], want[1], want[3])
        assert sum(t.result_r for t in done) == pytest.approx(want[2], abs=1e-6)


def test_the_new_parameters_at_their_defaults_change_nothing_for_round_one_kinds():
    bars = daily(_walk(700, 33))
    atr = ind.atr(bars)
    for sig in setups.pb_d(bars, min_bars=60)[:30] + setups.bo_d(bars, min_bars=210)[:30]:
        for kind in ("E0", "E1", "E2"):
            plain = exits.simulate(bars, sig, kind, costs=FX, atr=atr)
            spelled = exits.simulate(bars, sig, kind, costs=FX, atr=atr, trail_atr=None,
                                     time_stop_bars=None, exit_when=None, max_r_atr=6.0)
            assert plain == spelled


# ================================================================== invariants on random series
def test_random_series_the_round_two_exits_conserve_the_position_and_keep_fills_inside_their_bars():
    bars = daily(_walk(1200, 5))
    atr = ind.atr(bars)
    closes = ind.closes(bars)
    sma5 = ind.sma(closes, 5)
    signals = setups.pb_d(bars, min_bars=60)[:60] + setups.bo_d(bars, min_bars=210)[:30]
    assert len(signals) > 40
    plans = [
        ("EL", dict(time_stop_bars=16)),
        ("E0c", dict(time_stop_bars=11,
                     exit_when=lambda j, side: sma5[j] is not None and closes[j] > sma5[j])),
        ("E0c", dict(trail_atr=6.0, max_r_atr=8.0)),
        ("EL", dict(max_r_atr=8.0, exit_when=lambda j, side: j % 7 == 0)),
    ]
    closed = by_time = 0
    for sig in signals:
        for kind, kw in plans:
            t = exits.simulate(bars, sig, kind, costs=FX, atr=atr, **kw)
            if t.status != "closed":
                continue
            closed += 1
            assert sum(p.fraction for p in t.parts) == pytest.approx(1.0)
            assert t.entry_index <= t.exit_index == max(p.index for p in t.parts)
            for p in t.parts:
                bar = bars[p.index]
                assert bar.low - 1e-9 <= p.price <= bar.high + 1e-9
                if p.kind in ("time", "signal"):
                    assert p.price == bar.open
                if p.kind == "time":
                    by_time += 1
                    assert p.index - t.entry_index == kw["time_stop_bars"]
            assert t.result_r == pytest.approx(t.gross_r - t.cost_r)
            assert t.nights == (t.exit_ts.date() - t.entry_ts.date()).days
            if "time_stop_bars" in kw:
                assert t.exit_index - t.entry_index <= kw["time_stop_bars"]
    assert closed > 100 and by_time > 10
