"""cfd/exits.py: the ladder state machine (E0 trailing, E1 fixed ladder, E2 ladder with a runner)
and the trade simulator -- every ladder step, stop move, bar-order rule, gap, session end and cost
in R, on crafted bars with hand-computed results.

Every state-machine scenario runs twice: as written (a long, entry 100, stop 96, so R = 4 and
TP1..TP4 = 104, 108, 112, 116, runner target 120) and reflected through K = 200 (a short, entry
100, stop 104), and the two must agree event for event -- one crafted path proves both sides."""
from __future__ import annotations

import dataclasses
import datetime as dt
import random

import pytest

from cfd import exits
from cfd import indicators as ind
from cfd import instruments as ins
from cfd.data import LONDON_SESSION, Bar, Session
from cfd.setups import Signal

UTC = dt.timezone.utc
DAY = dt.timedelta(days=1)
HOUR = dt.timedelta(hours=1)
D0 = dt.datetime(2024, 1, 1, tzinfo=UTC)
K = 200.0


def b(o, h, low, c, ts=D0):
    return Bar(ts, float(o), float(h), float(low), float(c))


def flip(bar):
    return Bar(bar.ts, K - bar.open, K - bar.low, K - bar.high, K - bar.close)


def run_both(kind, bars, *, atr=2.0, target_r=None, session_end=(), atrs=None):
    """Step `bars` through a fresh long state and a fresh short state (the reflected bars); assert
    the short events are the long ones reflected; return (long events, long state)."""
    out = {}
    for side in ("long", "short"):
        state = (exits.LadderState("long", 100.0, 96.0, kind, target_r=target_r) if side == "long"
                 else exits.LadderState("short", 100.0, 104.0, kind, target_r=target_r))
        events = []
        for i, bar in enumerate(bars):
            a = atrs[i] if atrs is not None else atr
            events += state.step(bar if side == "long" else flip(bar), a,
                                 session_end=i in session_end)
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


# a clean climb: each bar fills the next target and its low stays above the stop that fill sets
CLIMB = [
    b(100.5, 104.2, 100.2, 104.0),     # TP1 at 104 -> stop to the entry 100
    b(104.0, 108.3, 104.2, 108.0),     # TP2 at 108 -> stop to TP1 (104)
    b(108.0, 112.2, 108.1, 112.0),     # TP3 at 112 -> stop to TP2 (108)
    b(112.0, 116.4, 111.0, 116.2),     # TP4 at 116 (E1) -> closed
]


# ================================================================== construction
def test_state_starts_at_the_initial_stop_with_the_whole_position():
    s = exits.LadderState("long", 100.0, 96.0, "E1")
    assert (s.stage, s.remaining, s.stop, s.closed) == (0, 1.0, 96.0, False)
    assert s.r == 4.0
    s2 = exits.LadderState("short", 100.0, 104.0, "E0")
    assert s2.r == 4.0 and s2.stop == 104.0


@pytest.mark.parametrize("args", [
    ("long", 100.0, 100.0, "E1"),        # no distance
    ("long", 100.0, 101.0, "E1"),        # stop above a long's entry
    ("short", 100.0, 99.0, "E1"),        # stop below a short's entry
    ("sideways", 100.0, 96.0, "E1"),
    ("long", 100.0, 96.0, "E9"),
])
def test_state_refuses_a_nonsensical_position(args):
    with pytest.raises(ValueError):
        exits.LadderState(*args)


def test_e0_target_must_be_positive():
    with pytest.raises(ValueError):
        exits.LadderState("long", 100.0, 96.0, "E0", target_r=0.0)


def test_a_closed_state_ignores_further_bars():
    s = exits.LadderState("long", 100.0, 96.0, "E1")
    s.step(b(101, 105, 95, 100))
    assert s.closed
    assert s.step(b(100, 130, 90, 100)) == []


# ================================================================== E1: the fixed ladder
def test_e1_a_quarter_at_each_of_1r_2r_3r_4r_and_the_stop_follows():
    events, state = run_both("E1", CLIMB)
    assert [(e.kind, e.label, e.price, e.fraction, e.stage, e.stop) for e in events] == [
        ("tp", "TP1", 104.0, 0.25, 1, 100.0),        # stop to the entry
        ("tp", "TP2", 108.0, 0.25, 2, 104.0),        # stop to TP1
        ("tp", "TP3", 112.0, 0.25, 3, 108.0),        # stop to TP2
        ("tp", "TP4", 116.0, 0.25, 4, None),         # the last quarter: closed
    ]
    assert all(e.parts == (e.label,) and not e.gap for e in events)
    assert state.closed and state.remaining == 0.0
    assert gross_r(events) == pytest.approx(2.5)     # 0.25 * (1 + 2 + 3 + 4)


def test_e1_a_bar_that_opens_beyond_targets_fills_them_at_the_open():
    # opens at 109: above TP1 (104) and TP2 (108) -> both fill at 109; TP3 (112) is inside the bar
    events, state = run_both("E1", [b(109.0, 113.0, 108.5, 112.5)])
    assert [(e.label, e.price, e.gap) for e in events] == [
        ("TP1", 109.0, True), ("TP2", 109.0, True), ("TP3", 112.0, False)]
    assert (state.stage, state.remaining, state.stop) == (3, 0.25, 108.0)


def test_e1_the_stop_is_checked_before_the_target_within_a_bar():
    # range 95..105 covers both the stop (96) and TP1 (104): conservative -> the stop
    events, state = run_both("E1", [b(101, 105, 95, 100)])
    assert len(events) == 1
    e = events[0]
    assert (e.kind, e.label, e.price, e.fraction, e.stage, e.gap) == ("stop", "STOP", 96.0, 1.0, 0, False)
    assert e.parts == ("TP1", "TP2", "TP3", "TP4")
    assert state.closed and gross_r(events) == pytest.approx(-1.0)


def test_e1_a_stop_gapped_through_fills_at_the_open():
    events, state = run_both("E1", [b(94.0, 95.0, 93.0, 94.5)])
    (e,) = events
    assert (e.kind, e.price, e.gap, e.fraction) == ("stop", 94.0, True, 1.0)
    assert gross_r(events) == pytest.approx(-1.5)       # (94 - 100) / 4


def test_e1_after_tp1_the_stop_sits_at_the_entry():
    events, state = run_both("E1", [CLIMB[0], b(103, 103.5, 99.5, 100.5)])
    assert [e.label for e in events] == ["TP1", "STOP"]
    stop = events[1]
    assert (stop.price, stop.fraction, stop.stage, stop.parts) == (100.0, 0.75, 1, ("TP2", "TP3", "TP4"))
    assert gross_r(events) == pytest.approx(0.25)       # a quarter at +1R, three quarters at 0


def test_e1_after_tp2_the_stop_sits_at_tp1():
    events, _ = run_both("E1", [CLIMB[0], CLIMB[1], b(107, 107.5, 103.9, 104.5)])
    stop = events[-1]
    assert (stop.kind, stop.price, stop.fraction, stop.stage, stop.parts) == (
        "stop", 104.0, 0.5, 2, ("TP3", "TP4"))
    assert gross_r(events) == pytest.approx(0.25 + 0.5 + 0.5)       # +1R, +2R quarters, half at +1R


def test_e1_after_tp3_the_stop_sits_at_tp2():
    events, _ = run_both("E1", CLIMB[:3] + [b(111, 111.5, 107.5, 108.5)])
    stop = events[-1]
    assert (stop.kind, stop.price, stop.fraction, stop.stage, stop.parts) == (
        "stop", 108.0, 0.25, 3, ("TP4",))
    assert gross_r(events) == pytest.approx(0.25 * (1 + 2 + 3) + 0.25 * 2)


def test_e1_the_stop_goes_before_tp4_in_the_same_bar_too():
    # range 107..117 covers the stop at TP2 (108) and TP4 (116): the stop first
    events, _ = run_both("E1", CLIMB[:3] + [b(112, 117, 107, 115)])
    assert events[-1].kind == "stop" and events[-1].price == 108.0
    assert [e.label for e in events] == ["TP1", "TP2", "TP3", "STOP"]


def test_e1_tp1_and_the_new_stop_in_one_bar_stop_first_after_the_fill():
    # TP1 is reached (104.5) and the bar also trades down to 99.5, through the entry the stop has
    # just moved to: the remaining three quarters leave at the entry
    events, _ = run_both("E1", [b(100.5, 104.5, 99.5, 103)])
    assert [(e.kind, e.label, e.price, e.fraction) for e in events] == [
        ("tp", "TP1", 104.0, 0.25), ("stop", "STOP", 100.0, 0.75)]
    assert gross_r(events) == pytest.approx(0.25)


def test_e1_gap_fill_then_stop_in_the_same_bar_is_not_a_gap_stop():
    # opens at 105 (above TP1): TP1 fills at the open; the stop then moves to the entry and the
    # bar's low of 99 reaches it -- reached by trading down, not gapped through at the open
    events, _ = run_both("E1", [b(105.0, 105.5, 99.0, 100.0)])
    assert [(e.label, e.price, e.gap) for e in events] == [("TP1", 105.0, True), ("STOP", 100.0, False)]
    assert gross_r(events) == pytest.approx(0.25 * 5 / 4)


def test_e1_a_bar_that_opens_below_the_stepped_stop_gaps_through_it():
    # after TP1 the stop is the entry (100); the next bar opens at 99
    events, _ = run_both("E1", [CLIMB[0], b(99.0, 100.5, 98.5, 100.0)])
    stop = events[-1]
    assert (stop.kind, stop.price, stop.gap, stop.fraction) == ("stop", 99.0, True, 0.75)
    assert gross_r(events) == pytest.approx(0.25 + 0.75 * (-1 / 4))


def test_e1_ignores_a_target_r_and_the_trailing_stop():
    # a high of 110 would trail a stop to 104 under E0; under E1 the stop stays at 96
    events, state = run_both("E1", [b(100.0, 103.0, 99.0, 101.0), b(101, 103, 99, 100)], target_r=5.0)
    assert events == [] and state.stop == 96.0


# ================================================================== E2: ladder with a runner
def test_e2_the_last_quarter_runs_to_5r_instead_of_4r():
    bars = CLIMB[:3] + [b(112.0, 125.0, 111.0, 124.0)]
    events, state = run_both("E2", bars)
    assert [(e.label, e.price, e.fraction, e.stage) for e in events] == [
        ("TP1", 104.0, 0.25, 1), ("TP2", 108.0, 0.25, 2), ("TP3", 112.0, 0.25, 3),
        ("TP4", 120.0, 0.25, 4)]
    assert state.closed and gross_r(events) == pytest.approx(0.25 * (1 + 2 + 3 + 5))


def test_e2_first_three_quarters_behave_exactly_as_in_e1():
    # the same bars under E1 and E2 give the same events up to TP3, then diverge only at the end
    e1, _ = run_both("E1", CLIMB[:3])
    e2, _ = run_both("E2", CLIMB[:3])
    assert [(e.label, e.price, e.stop) for e in e1] == [(e.label, e.price, e.stop) for e in e2]


def test_e2_the_runner_rides_the_trailing_stop_when_it_is_above_the_stepped_one():
    # after TP3 the stepped stop is TP2 = 108. A bar to 118 trails the stop to 118 - 3*2 = 112
    # (effective next bar); the next bar trades down to 111.5 and takes it
    bars = CLIMB[:3] + [b(112.0, 118.0, 111.0, 117.0), b(117.0, 117.5, 111.5, 112.5)]
    events, state = run_both("E2", bars)
    stop = events[-1]
    assert (stop.kind, stop.price, stop.fraction, stop.stage, stop.parts) == (
        "stop", 112.0, 0.25, 3, ("TP4",))
    assert gross_r(events) == pytest.approx(0.25 * (1 + 2 + 3) + 0.25 * 3)


def test_e2_the_runner_never_goes_below_the_stepped_stop():
    # the trailing stop (112.2 - 6 = 106.2) is below TP2 (108): the stop stays at 108
    bars = CLIMB[:3] + [b(112, 113, 107.5, 108.5)]
    events, state = run_both("E2", bars)
    stop = events[-1]
    assert (stop.kind, stop.price, stop.stage) == ("stop", 108.0, 3)
    assert gross_r(events) == pytest.approx(0.25 * (1 + 2 + 3) + 0.25 * 2)


def test_e2_before_tp3_the_trailing_stop_does_not_replace_the_stepped_stop():
    # after TP2 the stop is TP1 (104). The high of 111.9 trails the baseline stop to 105.9, but the
    # three quarters still left keep the stepped stop: a bar down to 105.0 does not stop them
    bars = [CLIMB[0],
            b(104.0, 111.9, 104.2, 111.5),          # TP2; the baseline trail reaches 105.9
            b(111.0, 111.5, 105.0, 105.5),          # low 105.0 is under 105.9 but above 104: no stop
            b(105.5, 106.0, 103.5, 104.0)]          # now the stepped stop (104) is reached
    events, state = run_both("E2", bars)
    assert [e.label for e in events] == ["TP1", "TP2", "STOP"]
    stop = events[-1]
    assert (stop.price, stop.fraction, stop.stage, stop.parts) == (104.0, 0.5, 2, ("TP3", "TP4"))
    assert gross_r(events) == pytest.approx(0.25 + 0.5 + 0.5)       # same as E1 on these bars


def test_e2_the_baseline_trail_kept_since_entry_floors_the_runner_stop_at_tp3():
    # with a tiny ATR (3 ATR = 0.15) the baseline trail is already above TP2 when TP3 fills (after
    # the TP2 bar: 108.3 - 0.15 = 108.15), so the runner's stop starts there, not at 108
    bars = CLIMB[:2] + [b(108.0, 112.2, 108.5, 112.0),        # TP3; this high lifts the trail to 112.05
                        b(112.3, 113.0, 111.9, 112.5)]        # opens above it, trades down into it
    events, state = run_both("E2", bars, atrs=[0.05] * 4)
    tp3 = events[2]
    assert (tp3.label, tp3.stage) == ("TP3", 3)
    assert tp3.stop == pytest.approx(108.15)
    stop = events[-1]
    assert (stop.kind, stop.parts, stop.stage) == ("stop", ("TP4",), 3)
    assert stop.price == pytest.approx(112.05) and not stop.gap
    assert gross_r(events) == pytest.approx(0.25 * 6 + 0.25 * 12.05 / 4)


def test_e2_the_runner_target_fills_at_the_open_when_the_bar_gaps_over_it():
    bars = CLIMB[:3] + [b(122.0, 123.0, 121.0, 122.5)]
    events, _ = run_both("E2", bars)
    last = events[-1]
    assert (last.label, last.price, last.gap, last.stage) == ("TP4", 122.0, True, 4)
    assert gross_r(events) == pytest.approx(0.25 * 6 + 0.25 * 22 / 4)


def test_e2_without_an_atr_the_runner_stop_is_the_stepped_stop():
    bars = CLIMB[:3] + [b(112.0, 118.0, 111.0, 117.0), b(117.0, 117.5, 108.5, 112.5),
                        b(112.5, 113.0, 107.5, 108.0)]
    events, _ = run_both("E2", bars, atrs=[None] * len(bars))
    assert events[-1].kind == "stop" and events[-1].price == 108.0


# ================================================================== E0: the baseline trailing stop
def test_e0_the_stop_trails_three_atr_below_the_high_and_never_moves_down():
    bars = [b(100.5, 103.0, 100.0, 102.5),      # high 103 -> stop 103 - 6 = 97
            b(102.5, 110.0, 102.0, 109.0),      # high 110 -> stop 104
            b(109.0, 109.8, 105.0, 106.0),      # high 109.8 -> 103.8 < 104: the stop stays
            b(106.0, 107.0, 103.5, 104.0)]      # low 103.5 <= 104: stopped at 104
    events, state = run_both("E0", bars[:1])
    assert events == [] and state.stop == 97.0
    events, state = run_both("E0", bars[:2])
    assert state.stop == 104.0
    events, state = run_both("E0", bars[:3])
    assert events == [] and state.stop == 104.0
    events, state = run_both("E0", bars)
    (e,) = events
    assert (e.kind, e.label, e.price, e.fraction, e.parts, e.stage) == (
        "stop", "STOP", 104.0, 1.0, ("ALL",), 0)
    assert gross_r(events) == pytest.approx(1.0)


def test_e0_a_new_trailing_stop_applies_from_the_next_bar_not_the_same_one():
    # the bar's high of 110 would trail the stop to 104, but its low of 100 is only tested against
    # the stop in force when it began (96)
    events, state = run_both("E0", [b(100.0, 110.0, 100.0, 105.0)])
    assert events == [] and state.stop == 104.0


def test_e0_has_no_profit_target_unless_one_is_given():
    events, state = run_both("E0", [b(100.5, 150.0, 100.2, 149.0)])
    assert events == [] and not state.closed and state.stop == 144.0


def test_e0_hard_target_in_r_fills_at_the_target():
    events, state = run_both("E0", [b(100.5, 121.0, 100.2, 120.0)], target_r=5.0)
    (e,) = events
    assert (e.kind, e.label, e.price, e.fraction, e.parts, e.gap) == (
        "target", "TARGET", 120.0, 1.0, ("ALL",), False)
    assert state.closed and gross_r(events) == pytest.approx(5.0)


def test_e0_hard_target_fills_at_the_open_when_gapped_over():
    events, _ = run_both("E0", [b(122.0, 123.0, 121.5, 122.5)], target_r=5.0)
    assert (events[0].price, events[0].gap) == (122.0, True)
    assert gross_r(events) == pytest.approx(5.5)


def test_e0_stop_wins_over_the_target_in_the_same_bar():
    events, _ = run_both("E0", [b(100.5, 121.0, 95.0, 100.0)], target_r=5.0)
    assert events[0].kind == "stop" and events[0].price == 96.0


# ================================================================== session end
def test_session_end_closes_what_is_left_at_the_close_of_that_bar():
    events, state = run_both("E0", [b(101.0, 102.0, 100.5, 101.5)], session_end={0})
    (e,) = events
    assert (e.kind, e.label, e.price, e.fraction, e.parts) == ("session", "SESSION", 101.5, 1.0, ("ALL",))
    assert state.closed
    assert gross_r(events) == pytest.approx(0.375)


def test_session_end_applies_to_the_ladder_and_closes_only_the_remaining_quarters():
    events, state = run_both("E1", [CLIMB[0], b(104.0, 105.0, 103.0, 104.5)], session_end={1})
    assert [e.label for e in events] == ["TP1", "SESSION"]
    last = events[-1]
    assert (last.price, last.fraction, last.stage, last.parts) == (104.5, 0.75, 1, ("TP2", "TP3", "TP4"))
    assert gross_r(events) == pytest.approx(0.25 + 0.75 * 4.5 / 4)


def test_session_end_bar_still_lets_the_stop_and_targets_act_first():
    # the stop (96) is touched in the session-end bar: it leaves at the stop, not at the close
    events, _ = run_both("E0", [b(101.0, 102.0, 95.0, 99.0)], session_end={0})
    assert [(e.kind, e.price) for e in events] == [("stop", 96.0)]
    # a target inside the session-end bar fills at the target; the rest then closes at the close
    events, _ = run_both("E1", [b(100.5, 104.5, 100.2, 103.0)], session_end={0})
    assert [(e.kind, e.label, e.price) for e in events] == [
        ("tp", "TP1", 104.0), ("session", "SESSION", 103.0)]


def test_close_at_closes_everything_left_at_a_price():
    s = exits.LadderState("long", 100.0, 96.0, "E1")
    s.step(CLIMB[0])
    (e,) = s.close_at(101.0)
    assert (e.kind, e.price, e.fraction, e.stage, e.stop) == ("session", 101.0, 0.75, 1, None)
    assert s.closed and s.close_at(99.0) == []


# ================================================================== resuming (the live tracker)
def test_a_state_copied_midway_continues_exactly_like_the_original():
    bars = CLIMB[:2] + [b(107.0, 107.5, 106.0, 107.2),       # quiet above the stop (104)
                        b(107.2, 113.0, 108.5, 112.5),       # TP3
                        b(112.5, 119.0, 112.0, 118.0),       # E1: TP4; E2: the trail rises to 113
                        b(118.0, 118.5, 111.0, 112.0)]       # E2: stopped at 113
    atrs = [2.0] * len(bars)
    for kind in ("E1", "E2"):
        whole = exits.LadderState("long", 100.0, 96.0, kind)
        events = []
        for bar, a in zip(bars, atrs):
            events += whole.step(bar, a)
        for cut in range(1, len(bars)):
            first = exits.LadderState("long", 100.0, 96.0, kind)
            head = []
            for bar, a in zip(bars[:cut], atrs):
                head += first.step(bar, a)
            resumed = dataclasses.replace(first)
            tail = []
            for bar, a in zip(bars[cut:], atrs[cut:]):
                tail += resumed.step(bar, a)
            assert head + tail == events
            assert resumed == whole


# ================================================================== simulate: entries and R
def daily(rows, start=D0):
    return [Bar(start + i * DAY, float(o), float(h), float(low), float(c))
            for i, (o, h, low, c) in enumerate(rows)]


PRE = [(100, 100.5, 99.5, 100)] * 3                        # bars 0-2; the signal is read at bar 2
FX = ins.by_symbol("EURUSD=X").costs                       # round trip 0.015, financing 0.010 a night
LONG = Signal(2, "long", 96.0, 2.0)
SHORT = Signal(2, "short", 104.0, 2.0)


def both_sides(rows):
    """The same crafted daily path for a long (as written) and a short (reflected)."""
    long_bars = daily(rows)
    short_bars = [flip(x) for x in long_bars]
    return long_bars, short_bars


def sim(bars, signal, kind, **kw):
    kw.setdefault("costs", FX)
    kw.setdefault("atr", [2.0] * len(bars))
    return exits.simulate(bars, signal, kind, **kw)


def test_next_open_entry_is_the_open_of_the_bar_after_the_signal_and_r_is_measured_from_it():
    rows = PRE + [(101.0, 101.5, 100.0, 101.0), (101.5, 106.5, 101.2, 106.0)]
    trade = sim(daily(rows), LONG, "E1")
    # entry 101, the stop stays at the signal's 96: R = 5, so TP1 is 106 (hit on bar 4)
    assert (trade.entry_index, trade.entry_price, trade.stop0, trade.r) == (3, 101.0, 96.0, 5.0)
    assert trade.entry_ts == D0 + 3 * DAY and trade.signal_index == 2
    assert trade.status == "open"                     # three quarters still held at the end of data
    short = sim([flip(x) for x in daily(rows)], SHORT, "E1")
    assert (short.entry_price, short.stop0, short.r) == (K - 101.0, 104.0, 5.0)


def test_close_entry_is_the_close_of_the_signal_bar():
    rows = [(100, 100.5, 99.5, 100), (100, 100.5, 99.5, 100), (100, 100.5, 99.5, 100.4),
            (100.4, 100.9, 99.9, 100.5)]
    trade = sim(daily(rows), LONG, "E0", entry="close")
    assert (trade.entry_index, trade.entry_price, trade.r) == (2, 100.4, pytest.approx(4.4))


@pytest.mark.parametrize("entry_open, skipped", [
    (96.5, True),       # R = 0.5 = 0.25 * ATR(2): "<=" -> skipped
    (96.51, False),     # just above
    (96.0, True),       # entry on the stop level
    (94.0, True),       # gapped through the stop: R would be "negative" -> skipped
    (108.0, False),     # R = 12 = 6 * ATR: not "greater than 6 ATR"
    (108.1, True),      # R = 12.1 > 6 ATR
])
def test_entries_with_r_at_most_a_quarter_atr_or_over_six_atr_are_skipped(entry_open, skipped):
    # the high keeps the open inside its own bar; the bar after changes nothing
    rows = PRE + [(entry_open, entry_open + 0.5, entry_open - 0.5, entry_open), (entry_open,) * 4]
    trade = sim(daily(rows), LONG, "E1")
    assert (trade.status == "skipped") == skipped
    if skipped:
        assert trade.parts == () and trade.result_r == 0.0 and trade.note


@pytest.mark.parametrize("entry_open, skipped", [
    (103.5, True), (103.49, False), (104.0, True), (106.0, True), (92.0, False), (91.9, True),
])
def test_the_same_skip_rule_for_a_short(entry_open, skipped):
    rows = PRE + [(entry_open, entry_open + 0.5, entry_open - 0.5, entry_open), (entry_open,) * 4]
    trade = sim(daily(rows), SHORT, "E1")
    assert (trade.status == "skipped") == skipped


def test_a_signal_on_the_last_bar_with_a_next_open_entry_is_still_open():
    trade = sim(daily(PRE), LONG, "E1")
    assert trade.status == "open" and trade.entry_index is None and trade.parts == ()
    assert not trade.closed


def test_a_trade_that_is_not_finished_when_the_data_ends_is_open_not_closed():
    rows = PRE + [(100, 101, 99, 100.5), (100.5, 101.5, 99.5, 101)]
    trade = sim(daily(rows), LONG, "E0")
    assert trade.status == "open" and trade.entry_index == 3 and not trade.closed
    assert trade.parts == () and trade.result_r == 0.0


def test_simulate_validates_its_arguments():
    bars = daily(PRE + [(100, 101, 99, 100)])
    with pytest.raises(ValueError):
        sim(bars, LONG, "E9")
    with pytest.raises(ValueError):
        sim(bars, LONG, "E1", entry="tomorrow")


def test_simulate_computes_the_atr_itself_when_none_is_given():
    # 30 flat bars (every true range 1, so ATR 1), a signal on bar 28, entry on bar 29; bar 30 rallies
    # to 112 (TR 12, ATR (13*1 + 12)/14), trailing the stop to 112 - 3*ATR = 106.64; bar 31 trades
    # down through it. Without an ATR nothing would trail and the trade would still be open.
    flat = [(100, 100.5, 99.5, 100)] * 30
    bars = daily(flat + [(100, 112, 100, 111), (111, 111.5, 106, 107)])
    sig = Signal(28, "long", 96.0, 1.0)
    auto = exits.simulate(bars, sig, "E0", costs=FX)
    explicit = exits.simulate(bars, sig, "E0", costs=FX, atr=ind.atr(bars))
    assert auto == explicit
    assert auto.status == "closed" and auto.exit_index == 31
    assert auto.parts[0].price == pytest.approx(112 - 3 * (13 * 1.0 + 12) / 14)


# ================================================================== simulate: results and costs in R
def test_e1_result_in_r_is_the_parts_minus_the_costs_of_the_nights_held():
    # entry day 3 at 100, the four targets on days 4..7: gross 2.5R over 4 nights
    rows = PRE + [(100.0, 101.0, 99.0, 100.5)] + [
        (100.5, 104.2, 100.2, 104.0), (104.0, 108.3, 104.2, 108.0),
        (108.0, 112.2, 108.1, 112.0), (112.0, 116.4, 111.0, 116.2)]
    long_bars, short_bars = both_sides(rows)
    for bars, sig in ((long_bars, LONG), (short_bars, SHORT)):
        t = sim(bars, sig, "E1")
        assert t.status == "closed" and t.closed
        assert (t.entry_index, t.exit_index) == (3, 7)
        assert (t.entry_ts, t.exit_ts) == (D0 + 3 * DAY, D0 + 7 * DAY)
        assert t.nights == 4
        assert [p.label for p in t.parts] == ["TP1", "TP2", "TP3", "TP4"]
        assert sum(p.fraction for p in t.parts) == 1.0
        assert t.gross_r == pytest.approx(2.5)
        # (0.015 % + 4 nights * 0.010 %) of the entry price 100, over R = 4
        assert t.cost_r == pytest.approx((0.015 + 4 * 0.010) / 100 * 100 / 4)
        assert t.result_r == pytest.approx(2.5 - 0.01375)
        assert t.holding_days == pytest.approx(4.0)


def test_a_trade_that_ends_on_its_entry_day_pays_no_financing():
    rows = PRE + [(100.0, 101.0, 95.0, 96.5)]            # entry at 100; the stop (96) is hit that day
    t = sim(daily(rows), LONG, "E1")
    assert (t.entry_index, t.exit_index, t.nights) == (3, 3, 0)
    assert t.gross_r == pytest.approx(-1.0)
    assert t.cost_r == pytest.approx(0.015 / 100 * 100 / 4)
    assert t.result_r == pytest.approx(-1.0 - 0.00375)
    assert t.holding_days == 0.0


def test_indices_pay_financing_by_side():
    gspc = ins.by_symbol("^GSPC").costs              # round trip 0.030; long 0.020, short 0.005
    rows = PRE + [(100, 100.5, 99.5, 100), (100, 100.6, 99.6, 100), (100, 100.5, 99.5, 100),
                  (100, 101, 95, 96.5)]              # days 4,5 quiet; stop hit on day 6 -> 3 nights
    long_bars, short_bars = both_sides(rows)
    tl = sim(long_bars, LONG, "E0", costs=gspc)
    ts = sim(short_bars, SHORT, "E0", costs=gspc)
    assert tl.nights == ts.nights == 3
    assert tl.gross_r == pytest.approx(-1.0) and ts.gross_r == pytest.approx(-1.0)
    assert tl.cost_r == pytest.approx((0.030 + 3 * 0.020) / 100 * 100 / 4)
    assert ts.cost_r == pytest.approx((0.030 + 3 * 0.005) / 100 * 100 / 4)
    assert tl.result_r == pytest.approx(-1.0225) and ts.result_r == pytest.approx(-1.01125)


def test_nights_are_the_calendar_day_boundaries_between_entry_and_exit_weekends_included():
    # entry on a Friday (2024-01-05), the stop hit on the Monday: three boundaries
    tue = dt.datetime(2024, 1, 2, tzinfo=UTC)
    bars = daily([(100, 100.5, 99.5, 100)] * 3 + [(100.0, 100.5, 99.5, 100.0)], start=tue)
    bars.append(Bar(dt.datetime(2024, 1, 8, tzinfo=UTC), 100.0, 101.0, 95.0, 96.0))
    t = sim(bars, LONG, "E0")
    assert t.entry_ts.weekday() == 4 and t.exit_ts.weekday() == 0
    assert t.nights == 3


def test_a_stop_at_a_gap_is_charged_at_the_open_fill_in_r():
    rows = PRE + [(100.0, 100.5, 99.5, 100.0), (94.0, 95.0, 93.0, 94.5)]
    t = sim(daily(rows), LONG, "E1")
    assert t.parts[0].gap and t.parts[0].price == 94.0
    assert t.gross_r == pytest.approx(-1.5)
    assert t.nights == 1


def test_the_trade_records_every_part_with_its_bar_and_timestamp():
    rows = PRE + [(100.0, 101.0, 99.0, 100.5), (100.5, 104.5, 99.5, 103.0)]    # TP1 then the stop
    t = sim(daily(rows), LONG, "E1")
    assert [(p.kind, p.label, p.price, p.fraction, p.index, p.ts) for p in t.parts] == [
        ("tp", "TP1", 104.0, 0.25, 4, D0 + 4 * DAY),
        ("stop", "STOP", 100.0, 0.75, 4, D0 + 4 * DAY)]
    assert t.gross_r == pytest.approx(0.25)


def test_e0_on_a_daily_trade_trails_and_exits_at_the_trailing_stop():
    rows = PRE + [(100.0, 101.0, 99.0, 100.5),
                  (100.5, 110.0, 100.0, 109.0),       # high 110 -> trailing stop 104
                  (109.0, 109.5, 103.5, 104.0)]       # stopped at 104
    t = sim(daily(rows), LONG, "E0")
    assert [(p.kind, p.price) for p in t.parts] == [("stop", 104.0)]
    assert t.gross_r == pytest.approx(1.0) and t.exit_index == 5 and t.nights == 2


# ================================================================== simulate: the gold session
GOLD = ins.gold_intraday_costs()


def hourly(rows, first=dt.datetime(2024, 1, 17, 10, tzinfo=UTC)):
    return [Bar(first + i * HOUR, float(o), float(h), float(low), float(c))
            for i, (o, h, low, c) in enumerate(rows)]


# bar 0 (10:00 UTC) is the signal bar: close 400; stop 396 (R = 4); ATR 2.
GOLD_SIGNAL = Signal(0, "long", 396.0, 2.0)
QUIET = (400.4, 401.0, 399.0, 400.5)


def gold_sim(rows, kind, **kw):
    bars = hourly(rows)
    kw.setdefault("atr", [2.0] * len(bars))
    return exits.simulate(bars, GOLD_SIGNAL, kind, costs=GOLD, entry="close",
                          session=LONDON_SESSION, **kw)


def test_gold_is_flat_at_the_close_of_the_first_bar_outside_the_session():
    # bars 1..5 are 11:00..15:00 (inside); bar 6 starts at 16:00 (outside): the exit is its close
    rows = [(400, 400.5, 399.5, 400)] + [QUIET] * 5 + [(400.5, 401.5, 399.5, 401.0)]
    t = gold_sim(rows, "E0")
    assert t.status == "closed"
    assert (t.entry_index, t.exit_index) == (0, 6)
    assert t.entry_price == 400.0
    assert [(p.kind, p.label, p.price, p.fraction) for p in t.parts] == [("session", "SESSION", 401.0, 1.0)]
    assert t.nights == 0
    assert t.gross_r == pytest.approx(0.25)                              # (401 - 400) / 4
    assert t.cost_r == pytest.approx(0.030 / 100 * 400 / 4)              # the XAUUSD round trip, no financing
    assert t.result_r == pytest.approx(0.25 - 0.03)


def test_gold_stop_inside_the_session_exits_at_the_stop():
    rows = [(400, 400.5, 399.5, 400), QUIET, (400.5, 401.0, 395.5, 396.5), QUIET]
    t = gold_sim(rows, "E0")
    assert (t.exit_index, [p.kind for p in t.parts]) == (2, ["stop"])
    assert t.gross_r == pytest.approx(-1.0)


def test_gold_the_session_end_bar_still_lets_the_stop_act_first():
    rows = [(400, 400.5, 399.5, 400)] + [QUIET] * 5 + [(400.5, 401.0, 395.0, 399.0)]
    t = gold_sim(rows, "E0")
    assert t.exit_index == 6 and [(p.kind, p.price) for p in t.parts] == [("stop", 396.0)]


def test_gold_ladder_closes_what_is_left_at_session_end():
    rows = [(400, 400.5, 399.5, 400),
            (400.2, 404.5, 400.1, 404.0)]                 # TP1 at 404, stop to 400
    rows += [(403.5, 404.5, 401.0, 403.8)] * 4            # 12:00..15:00, quiet above the stop
    rows += [(403.8, 404.0, 402.5, 403.0)]                # 16:00: the session ends at this close
    t = gold_sim(rows, "E1")
    assert [(p.label, p.price, p.fraction) for p in t.parts] == [
        ("TP1", 404.0, 0.25), ("SESSION", 403.0, 0.75)]
    assert t.gross_r == pytest.approx(0.25 + 0.75 * 3 / 4)
    assert t.result_r == pytest.approx(0.8125 - 0.03)


def test_gold_e0_has_the_hard_five_r_target_when_asked_for_it():
    rows = [(400, 400.5, 399.5, 400), (400.2, 421.0, 400.1, 420.5), QUIET]
    t = gold_sim(rows, "E0", e0_target_r=5.0)
    assert [(p.kind, p.price) for p in t.parts] == [("target", 420.0)]
    assert t.gross_r == pytest.approx(5.0) and t.result_r == pytest.approx(5.0 - 0.03)
    # without the target the same spike bar closes nothing: the trade just goes on trailing
    assert gold_sim(rows[:2], "E0").status == "open"


def test_gold_when_the_data_skips_the_end_of_the_session_it_leaves_at_the_last_price_seen():
    # after the signal bar (10:00) the next bar is 07:00 the next day: nothing from the 16:00
    # bar -- the trade leaves at the close of the last bar of its own session (the signal bar)
    rows = [(400, 400.5, 399.5, 400)]
    bars = hourly(rows) + [Bar(dt.datetime(2024, 1, 18, 7, tzinfo=UTC), 410.0, 415.0, 409.0, 412.0)]
    t = exits.simulate(bars, GOLD_SIGNAL, "E1", costs=GOLD, entry="close", session=LONDON_SESSION,
                       atr=[2.0, 2.0])
    assert t.status == "closed" and t.exit_index == 0
    assert [(p.kind, p.price, p.fraction) for p in t.parts] == [("session", 400.0, 1.0)]
    assert t.gross_r == 0.0 and t.nights == 0
    assert t.result_r == pytest.approx(-0.03)


def test_a_session_trade_pays_no_financing_even_when_its_utc_dates_differ():
    # a 08:00-16:00 Tokyo session runs 23:00-07:00 UTC: entry and exit fall on different UTC
    # dates, yet the trade is flat the same (local) day -- no nights, no financing
    tokyo = Session(8, 16, "Asia/Tokyo")
    rows = [(400, 400.5, 399.5, 400)] + [QUIET] * 7 + [(400.5, 401.5, 399.5, 401.0)]
    bars = hourly(rows, first=dt.datetime(2024, 1, 16, 23, tzinfo=UTC))     # 08:00 JST on the 17th
    t = exits.simulate(bars, GOLD_SIGNAL, "E0", costs=FX, entry="close", session=tokyo,
                       atr=[2.0] * len(bars))
    assert t.exit_index == 8 and t.parts[0].kind == "session"
    assert t.entry_ts.date() != t.exit_ts.date()
    assert t.nights == 0
    assert t.cost_r == pytest.approx(0.015 / 100 * 400 / 4)         # the round trip alone


def test_gold_a_trade_with_no_bar_after_it_is_open():
    t = gold_sim([(400, 400.5, 399.5, 400)], "E0")
    assert t.status == "open" and t.entry_price == 400.0


def test_gold_summer_session_follows_british_summer_time():
    # July: the 16:00 London bar starts at 15:00 UTC; a trade entered at 10:00 UTC leaves at its close
    first = dt.datetime(2024, 7, 17, 10, tzinfo=UTC)
    rows = [(400, 400.5, 399.5, 400)] + [QUIET] * 4 + [(400.5, 401.5, 399.5, 401.0)]
    bars = hourly(rows, first)
    t = exits.simulate(bars, GOLD_SIGNAL, "E0", costs=GOLD, entry="close", session=LONDON_SESSION,
                       atr=[2.0] * len(bars))
    assert t.exit_index == 5                                   # bar 5 starts at 15:00 UTC = 16:00 BST
    assert t.parts[0].kind == "session"


# ================================================================== invariants on random series
def _walk(n, seed, drift=0.0006):
    rng = random.Random(seed)
    price, rows = 100.0, []
    for _ in range(n):
        o = price
        price *= 1 + rng.gauss(drift, 0.012)
        rows.append((o, max(o, price) * (1 + abs(rng.gauss(0, 0.004))),
                     min(o, price) * (1 - abs(rng.gauss(0, 0.004))), price))
    return rows


def test_random_series_every_exit_conserves_the_position_and_keeps_fills_inside_their_bars():
    from cfd import setups
    bars = daily(_walk(900, 21))
    atr = ind.atr(bars)
    signals = setups.pb_d(bars, min_bars=60) + setups.bo_d(bars, min_bars=210)
    assert len(signals) > 20
    closed = 0
    for sig in signals:
        for kind in ("E0", "E1", "E2"):
            t = exits.simulate(bars, sig, kind, costs=FX, atr=atr)
            if t.status != "closed":
                continue
            closed += 1
            assert sum(p.fraction for p in t.parts) == pytest.approx(1.0)
            assert t.entry_index <= t.exit_index == max(p.index for p in t.parts)
            for p in t.parts:
                bar = bars[p.index]
                assert bar.low - 1e-9 <= p.price <= bar.high + 1e-9
            assert t.result_r == pytest.approx(t.gross_r - t.cost_r)
            assert t.cost_r > 0
            assert t.nights == (t.exit_ts.date() - t.entry_ts.date()).days
    assert closed > 40


def test_random_series_the_mirrored_series_gives_the_mirrored_trades():
    from cfd import setups
    bars = daily(_walk(700, 33))
    flipped = [Bar(x.ts, K - x.open, K - x.low, K - x.high, K - x.close) for x in bars]
    atr = ind.atr(bars)
    for sig in setups.pb_d(bars, min_bars=60)[:25]:
        mirror_sig = Signal(sig.index, "short" if sig.side == "long" else "long", K - sig.stop, sig.atr)
        for kind in ("E0", "E1", "E2"):
            t = exits.simulate(bars, sig, kind, costs=FX, atr=atr)
            tm = exits.simulate(flipped, mirror_sig, kind, costs=FX, atr=atr)
            assert t.status == tm.status
            if t.status == "closed":
                assert t.gross_r == pytest.approx(tm.gross_r)
                assert (t.entry_index, t.exit_index) == (tm.entry_index, tm.exit_index)


def test_simulate_is_deterministic():
    from cfd import setups
    bars = daily(_walk(500, 8))
    sig = setups.pb_d(bars, min_bars=60)[0]
    a = exits.simulate(bars, sig, "E2", costs=FX)
    b2 = exits.simulate(bars, sig, "E2", costs=FX)
    assert a == b2


def test_trade_is_a_frozen_record():
    t = sim(daily(PRE + [(100, 101, 99, 100)]), LONG, "E1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.result_r = 1.0  # type: ignore[misc]
