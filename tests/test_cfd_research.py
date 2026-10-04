"""cfd/research.py: the statistics, the IS/OOS split, one-open-trade-per-instrument, the exit choice,
the gate of spec §1.6 on synthetic Stats and trade lists, the report writer and enabled.json, and
the whole run end to end on synthetic bars behind a fake fetch. Nothing here reaches the network
and nothing is written outside tmp_path."""
from __future__ import annotations

import datetime as dt
import json
import math
import random

import pytest

from cfd import data, exits, research
from cfd import instruments as ins
from cfd.data import Bar
from cfd.setups import Signal

UTC = dt.timezone.utc
DAY = dt.timedelta(days=1)
D0 = dt.datetime(2020, 1, 1, tzinfo=UTC)


def row(r=1.0, *, setup="PB-D", exit_kind="E1", klass="FX", period="OOS", day=1, hold=1.0,
        symbol="EURUSD=X"):
    exit_ts = D0 + day * DAY
    return research.Row(setup, exit_kind, symbol, symbol[:6], klass, period, r,
                        exit_ts - hold * DAY, exit_ts, hold)


def S(n=150, win=0.5, pf=1.15, mean=0.05, t=1.96, total=1.0, dd=1.0, hold=1.0):
    return research.Stats(n, win, pf, mean, t, total, dd, hold)


def make_rows(setup, exit_kind, klass, period, wins, losses, *, symbol=None, day0=0):
    """`wins` trades of +1R and `losses` of -1R, interleaved, one exit a day."""
    out, w, lo, day = [], wins, losses, day0
    while w or lo:
        if w:
            out.append(row(1.0, setup=setup, exit_kind=exit_kind, klass=klass, period=period,
                           day=day, symbol=symbol or "EURUSD=X"))
            w -= 1
            day += 1
        if lo:
            out.append(row(-1.0, setup=setup, exit_kind=exit_kind, klass=klass, period=period,
                           day=day, symbol=symbol or "EURUSD=X"))
            lo -= 1
            day += 1
    return out


# ================================================================== statistics
def test_stats_hand_computed():
    # results 2, -1, 1, -1, 3 exiting on days 1..5, held 1..5 days
    rows = [row(r, day=i + 1, hold=i + 1) for i, r in enumerate([2.0, -1.0, 1.0, -1.0, 3.0])]
    st = research.stats(rows)
    assert st.n == 5
    assert st.win_rate == pytest.approx(0.6)                  # 2, 1, 3
    assert st.profit_factor == pytest.approx(3.0)             # 6 / 2
    assert st.mean_r == pytest.approx(0.8)
    # sample sd: deviations 1.2, -1.8, 0.2, -1.8, 2.2 -> 12.8 / 4 = 3.2 -> sd 1.78885; t = 0.8 / (sd / sqrt 5)
    assert st.t_stat == pytest.approx(1.0)
    assert st.total_r == pytest.approx(4.0)
    # equity 2, 1, 2, 1, 4 from a zero start: the worst peak-to-trough is 1
    assert st.max_drawdown_r == pytest.approx(1.0)
    assert st.mean_hold_days == pytest.approx(3.0)


def test_stats_of_no_trades_is_all_zero():
    st = research.stats([])
    assert (st.n, st.win_rate, st.profit_factor, st.mean_r, st.t_stat, st.total_r,
            st.max_drawdown_r, st.mean_hold_days) == (0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def test_stats_with_no_losers_has_an_infinite_profit_factor():
    st = research.stats([row(1.0, day=1), row(2.0, day=2)])
    assert math.isinf(st.profit_factor)
    assert st.t_stat == pytest.approx(3.0)                    # mean 1.5, sd 0.7071, n 2


def test_stats_with_no_winners_has_zero_profit_factor_and_drawdown_from_the_start():
    st = research.stats([row(-1.0, day=1), row(-1.0, day=2)])
    assert st.profit_factor == 0.0 and st.win_rate == 0.0
    assert st.max_drawdown_r == pytest.approx(2.0)            # the equity curve starts at 0
    assert st.t_stat == 0.0                                    # no spread: the t-statistic is not defined


def test_a_zero_result_is_not_a_win():
    st = research.stats([row(0.0, day=1), row(1.0, day=2)])
    assert st.win_rate == pytest.approx(0.5)


def test_stats_t_needs_two_trades():
    assert research.stats([row(1.0)]).t_stat == 0.0


def test_stats_drawdown_follows_the_exit_date_not_the_list_order():
    a, b, c = row(-1.0, day=1), row(2.0, day=2), row(-3.0, day=3)
    # in exit order: -1, +1, -2 -> peak 1, trough -2: 3. In the order [c, a, b] it would be 4
    assert research.stats([c, a, b]).max_drawdown_r == pytest.approx(3.0)
    assert research.stats([a, b, c]).max_drawdown_r == pytest.approx(3.0)


# ================================================================== periods (spec §1.5)
@pytest.mark.parametrize("klass, day, period", [
    ("FX", dt.date(2005, 12, 31), None),            # before the study window
    ("FX", dt.date(2006, 1, 1), "IS"),
    ("FX", dt.date(2016, 12, 30), "IS"),
    ("FX", dt.date(2016, 12, 31), "IS"),
    ("FX", dt.date(2017, 1, 1), "OOS"),
    ("FX", dt.date(2025, 9, 1), "OOS"),
    ("Metals", dt.date(2017, 1, 1), "OOS"),
    ("Crypto", dt.date(2015, 3, 1), "IS"),          # crypto has no 2006 floor ...
    ("Crypto", dt.date(2017, 6, 1), "IS"),          # ... and stays in-sample through 2019
    ("Crypto", dt.date(2019, 12, 31), "IS"),
    ("Crypto", dt.date(2020, 1, 1), "OOS"),
])
def test_period_is_set_by_the_signal_date(klass, day, period):
    assert research.period_of(klass, day) == period


# ================================================================== one open trade at a time
def flat_bars(n=40):
    return [Bar(D0 + i * DAY, 100.0, 100.5, 99.5, 100.0) for i in range(n)]


def test_take_trades_skips_signals_read_while_a_trade_is_open():
    bars = flat_bars()
    bars[15] = Bar(bars[15].ts, 100.0, 100.5, 94.0, 95.0)         # hits a stop at 96 on bar 15
    atr = [2.0] * len(bars)
    sigs = [Signal(10, "long", 96.0, 2.0),    # enters bar 11, stopped on bar 15
            Signal(12, "long", 96.0, 2.0),    # read while that trade is open: skipped
            Signal(14, "long", 96.0, 2.0),    # likewise
            Signal(15, "long", 96.0, 2.0)]    # read at the close of the exit bar: allowed
    trades, counts = research.take_trades(bars, sigs, "E1", costs=ins.by_symbol("EURUSD=X").costs,
                                          entry="next_open", atr=atr)
    assert [(t.signal_index, t.status) for t in trades] == [(10, "closed")]
    assert counts.signals == 4 and counts.blocked == 2 and counts.closed == 1
    assert counts.open == 1 and counts.skipped == 0           # the signal on 15 is still open at the end


def test_take_trades_a_skipped_entry_takes_no_slot_not_even_for_the_next_signal():
    bars = flat_bars()
    bars[11] = Bar(bars[11].ts, 94.0, 94.5, 93.5, 94.0)           # gaps through the stop: skipped
    sigs = [Signal(10, "long", 96.0, 2.0),     # its entry bar (11) opens through the stop: skipped
            Signal(11, "long", 96.0, 2.0)]     # read on the very next bar: nothing is open, so it enters
    trades, counts = research.take_trades(bars, sigs, "E1", costs=ins.by_symbol("EURUSD=X").costs,
                                          entry="next_open", atr=[2.0] * 40)
    assert counts.skipped == 1 and counts.blocked == 0
    assert counts.open == 1 and counts.closed == 0                 # the second entered and is still open
    assert trades == []


def test_take_trades_stops_once_a_trade_is_still_open_at_the_end_of_the_data():
    bars = flat_bars()
    sigs = [Signal(10, "long", 96.0, 2.0), Signal(30, "long", 96.0, 2.0)]
    trades, counts = research.take_trades(bars, sigs, "E1", costs=ins.by_symbol("EURUSD=X").costs,
                                          entry="next_open", atr=[2.0] * 40)
    assert trades == [] and counts.open == 1 and counts.blocked == 1 and counts.signals == 2


def test_take_trades_a_freed_slot_depends_on_the_exit_so_trade_sets_differ_by_exit():
    # E0 never closes on the flat bars after bar 11; the stop-out below happens on bar 20 only for the
    # ladder because of the stepped stop... here the same signal list yields different trades per exit
    bars = flat_bars(60)
    bars[12] = Bar(bars[12].ts, 100.5, 104.5, 100.2, 104.0)       # TP1 (104) under E1 / E2
    bars[13] = Bar(bars[13].ts, 103.0, 103.5, 99.0, 99.5)         # then back through the entry (100)
    atr = [2.0] * 60
    sigs = [Signal(10, "long", 96.0, 2.0), Signal(14, "long", 96.0, 2.0)]
    costs = ins.by_symbol("EURUSD=X").costs
    e1, c1 = research.take_trades(bars, sigs, "E1", costs=costs, entry="next_open", atr=atr)
    e0, c0 = research.take_trades(bars, sigs, "E0", costs=costs, entry="next_open", atr=atr)
    # bar 13 stops the E1 trade; the signal on 14 then enters and stays open
    assert [t.signal_index for t in e1] == [10] and c1.open == 1
    assert e0 == [] and c0.open == 1 and c0.blocked == 1              # E0 is still in the first trade


# ================================================================== choosing the ladder exit
def test_the_chosen_exit_is_the_ladder_with_the_higher_pooled_out_of_sample_mean_r():
    rows = (make_rows("PB-D", "E1", "FX", "OOS", 55, 45)          # mean +0.10
            + make_rows("PB-D", "E2", "FX", "OOS", 60, 40)        # mean +0.20
            + make_rows("PB-D", "E0", "FX", "OOS", 90, 10))       # E0 is never a candidate
    assert research.choose_exit(rows, "PB-D") == "E2"


def test_in_sample_results_do_not_choose_the_exit():
    rows = (make_rows("PB-D", "E1", "FX", "OOS", 60, 40) + make_rows("PB-D", "E2", "FX", "OOS", 55, 45)
            + make_rows("PB-D", "E2", "FX", "IS", 90, 10) + make_rows("PB-D", "E1", "FX", "IS", 10, 90))
    assert research.choose_exit(rows, "PB-D") == "E1"


def test_a_tie_or_no_trades_chooses_e1():
    tie = make_rows("BO-D", "E1", "FX", "OOS", 55, 45) + make_rows("BO-D", "E2", "FX", "OOS", 55, 45)
    assert research.choose_exit(tie, "BO-D") == "E1"
    assert research.choose_exit([], "BO-D") == "E1"


def test_an_exit_with_no_trades_cannot_win_the_choice():
    only_e2 = make_rows("PB-D", "E2", "FX", "OOS", 40, 60)           # a losing E2, and no E1 at all
    assert research.choose_exit(only_e2, "PB-D") == "E2"
    only_e1 = make_rows("PB-D", "E1", "FX", "OOS", 40, 60)
    assert research.choose_exit(only_e1, "PB-D") == "E1"


def test_gold_has_no_split_so_all_its_trades_choose_the_exit():
    rows = (make_rows("PB-H1-GOLD", "E1", "Metals", "ALL", 50, 50)
            + make_rows("PB-H1-GOLD", "E2", "Metals", "ALL", 52, 48))
    assert research.choose_exit(rows, "PB-H1-GOLD") == "E2"


# ================================================================== the gate: setup level
def level1(is_stats=None, oos=None):
    return research.gate_daily("PB-D", "E2", is_stats or S(mean=0.01),
                               oos or S(), {c: S(n=0, pf=0.0, mean=0.0, t=0.0) for c in ins.GATE_CLASSES})


def test_gate_level_one_passes_exactly_on_its_thresholds():
    g = level1()
    assert [c.rule for c in g.checks] == [
        "in-sample mean R", "out-of-sample trades", "out-of-sample profit factor",
        "out-of-sample mean R", "out-of-sample t-statistic"]
    assert [c.threshold for c in g.checks] == ["> 0", ">= 150", ">= 1.15", "> 0", ">= 1.96"]
    assert all(c.passed for c in g.checks) and g.passed


@pytest.mark.parametrize("is_stats, oos, failing", [
    (S(mean=0.0), None, "in-sample mean R"),                         # strictly greater than zero
    (S(mean=-0.01), None, "in-sample mean R"),
    (None, S(n=149), "out-of-sample trades"),
    (None, S(pf=1.1499), "out-of-sample profit factor"),
    (None, S(mean=0.0), "out-of-sample mean R"),
    (None, S(mean=-0.01), "out-of-sample mean R"),
    (None, S(t=1.9599), "out-of-sample t-statistic"),
])
def test_gate_level_one_fails_one_line_at_a_time(is_stats, oos, failing):
    g = level1(is_stats, oos)
    assert not g.passed
    assert [c.rule for c in g.checks if not c.passed] == [failing]


def test_a_failed_setup_enables_no_class_and_its_class_lines_are_not_evaluated():
    classes = {c: S(n=40, pf=2.0, mean=0.3) for c in ins.GATE_CLASSES}
    g = research.gate_daily("PB-D", "E2", S(mean=0.01), S(n=100), classes)
    assert not g.passed and g.enabled_classes == [] and g.class_checks == {}


# ================================================================== the gate: class level
def test_class_gate_needs_30_trades_a_profit_factor_of_1_10_and_a_positive_mean():
    classes = {
        "FX": S(n=30, pf=1.10, mean=0.01),            # exactly on every threshold: enabled
        "Metals": S(n=29, pf=2.0, mean=0.5),          # too few trades
        "Energy": S(n=60, pf=1.0999, mean=0.5),       # profit factor just short
        "Indices": S(n=60, pf=1.5, mean=0.0),         # mean not above zero
        "Crypto": S(n=60, pf=1.5, mean=0.1),          # fine
    }
    g = research.gate_daily("BO-D", "E1", S(mean=0.01), S(), classes)
    assert g.passed
    assert g.enabled_classes == ["FX", "Crypto"]                       # in the order of GATE_CLASSES
    assert [(c.rule, c.threshold) for c in g.class_checks["FX"]] == [
        ("out-of-sample trades", ">= 30"), ("out-of-sample profit factor", ">= 1.10"),
        ("out-of-sample mean R", "> 0")]
    assert [c.passed for c in g.class_checks["Metals"]] == [False, True, True]
    assert [c.passed for c in g.class_checks["Energy"]] == [True, False, True]
    assert [c.passed for c in g.class_checks["Indices"]] == [True, True, False]


def test_a_class_with_no_trades_is_not_enabled():
    g = research.gate_daily("PB-D", "E2", S(mean=0.01), S(), {"FX": S(n=40, pf=1.5, mean=0.2)})
    assert g.enabled_classes == ["FX"]
    assert [c.passed for c in g.class_checks["Energy"]] == [False, False, False]


# ================================================================== the gate: gold
def test_gold_gate_needs_the_ladder_to_pay_and_e0_to_match_the_users_own_range():
    ok = research.gate_gold("E1", S(pf=1.10, mean=0.01), S(pf=1.05))
    assert ok.passed and ok.enabled_classes == ["Metals"]
    assert [c.rule for c in ok.checks] == [
        "profit factor, chosen exit", "mean R, chosen exit", "E0 profit factor, lower bound",
        "E0 profit factor, upper bound"]
    assert [c.threshold for c in ok.checks] == [">= 1.10", "> 0", ">= 1.05", "<= 1.35"]
    assert research.gate_gold("E2", S(pf=1.5, mean=0.1), S(pf=1.35)).passed        # the top of the range


@pytest.mark.parametrize("chosen, e0, failing", [
    (S(pf=1.0999, mean=0.1), S(pf=1.2), "profit factor, chosen exit"),
    (S(pf=1.5, mean=0.0), S(pf=1.2), "mean R, chosen exit"),
    (S(pf=1.5, mean=-0.1), S(pf=1.2), "mean R, chosen exit"),
    (S(pf=1.5, mean=0.1), S(pf=1.0499), "E0 profit factor, lower bound"),
    (S(pf=1.5, mean=0.1), S(pf=1.3501), "E0 profit factor, upper bound"),
])
def test_gold_gate_fails_one_line_at_a_time(chosen, e0, failing):
    g = research.gate_gold("E1", chosen, e0)
    assert not g.passed and g.enabled_classes == []
    assert [c.rule for c in g.checks if not c.passed] == [failing]


def test_gold_gate_with_no_trades_fails():
    g = research.gate_gold("E1", S(n=0, pf=0.0, mean=0.0, t=0.0), S(n=0, pf=0.0, mean=0.0, t=0.0))
    assert not g.passed


# ================================================================== decide(): synthetic trade lists
def passing_and_failing_rows():
    rows = []
    # PB-D: E1 is flat out of sample, E2 pays; pooled OOS 200 trades; class mix below
    for exit_kind, mix in (("E1", {"FX": (50, 50), "Metals": (20, 20)}),
                           ("E2", {"FX": (70, 30), "Metals": (28, 12), "Energy": (12, 18),
                                   "Indices": (14, 6), "Crypto": (7, 3)})):
        for klass, (w, lo) in mix.items():
            rows += make_rows("PB-D", exit_kind, klass, "OOS", w, lo, symbol=f"{klass}-X")
    rows += make_rows("PB-D", "E2", "FX", "IS", 30, 20) + make_rows("PB-D", "E1", "FX", "IS", 25, 25)
    # BO-D: too few out-of-sample trades to pass level one
    rows += make_rows("BO-D", "E1", "FX", "OOS", 60, 40) + make_rows("BO-D", "E2", "FX", "OOS", 50, 50)
    rows += make_rows("BO-D", "E1", "FX", "IS", 60, 40)
    # PB-H1-GOLD: E1 pays (PF 1.5), E2 less, E0 inside the user's range (PF 33/27 = 1.22)
    rows += make_rows("PB-H1-GOLD", "E1", "Metals", "ALL", 36, 24)
    rows += make_rows("PB-H1-GOLD", "E2", "Metals", "ALL", 31, 29)
    rows += make_rows("PB-H1-GOLD", "E0", "Metals", "ALL", 33, 27)
    return rows


def test_decide_applies_both_levels_and_the_gold_rule_to_a_synthetic_trade_list():
    d = research.decide(passing_and_failing_rows())
    assert d.chosen == {"PB-D": "E2", "BO-D": "E1", "PB-H1-GOLD": "E1"}
    assert d.gates["PB-D"].passed and not d.gates["BO-D"].passed and d.gates["PB-H1-GOLD"].passed
    # Energy has a negative out-of-sample record; Indices and Crypto have fewer than 30 trades
    assert d.gates["PB-D"].enabled_classes == ["FX", "Metals"]
    assert d.enabled == [("PB-D", "FX"), ("PB-D", "Metals"), ("PB-H1-GOLD", "Metals")]
    # and the numbers behind it
    assert d.stats["PB-D"]["OOS"].n == 200
    assert d.stats["PB-D"]["IS"].mean_r == pytest.approx(0.2)         # 30 wins, 20 losses


def test_decide_with_nothing_passing_enables_nothing_but_still_names_the_exits():
    rows = (make_rows("PB-D", "E1", "FX", "OOS", 40, 60) + make_rows("PB-D", "E2", "FX", "OOS", 45, 55)
            + make_rows("BO-D", "E1", "FX", "OOS", 40, 60)
            + make_rows("PB-H1-GOLD", "E1", "Metals", "ALL", 20, 40))
    d = research.decide(rows)
    assert d.enabled == []
    assert d.chosen == {"PB-D": "E2", "BO-D": "E1", "PB-H1-GOLD": "E1"}
    assert not any(g.passed for g in d.gates.values())


def test_decide_on_no_trades_at_all():
    d = research.decide([])
    assert d.enabled == [] and set(d.chosen) == {"PB-D", "BO-D", "PB-H1-GOLD"}


def test_enabled_payload_is_the_machine_readable_outcome():
    d = research.decide(passing_and_failing_rows())
    now = dt.datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    payload = research.enabled_payload(d, now)
    assert set(payload) == {"exit", "enabled", "run_at"}
    assert payload["exit"] == {"PB-D": "E2", "BO-D": "E1", "PB-H1-GOLD": "E1"}
    assert payload["enabled"] == [["PB-D", "FX"], ["PB-D", "Metals"], ["PB-H1-GOLD", "Metals"]]
    assert payload["run_at"] == "2026-10-05T12:00:00+00:00"
    assert json.loads(json.dumps(payload)) == payload


# ================================================================== the report
def fake_results(rows):
    res = research.Results()
    res.rows = list(rows)
    res.counts[("PB-D", "E2")] = research.Counts(signals=10, closed=8, skipped=1, open=1, blocked=0)
    res.coverage.append(research.Coverage("EURUSD=X", "EURUSD", "1d", 5000, dt.date(2005, 1, 3),
                                          dt.date(2025, 10, 3), data.Dropped(missing=2, wick=1)))
    res.coverage.append(research.Coverage("GC=F", "XAUUSD", "1h", 9000, dt.date(2024, 1, 3),
                                          dt.date(2025, 10, 3), data.Dropped()))
    return res


NOW = dt.datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def section(text, title):
    """The lines under the exact heading `## title`, up to the next `## ` heading."""
    lines = text.splitlines()
    start = lines.index(f"## {title}") + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end])


def test_stats_cells_are_formatted_for_the_tables():
    st = research.Stats(5, 0.6, 3.0, 0.8, 1.0, 4.0, 1.0, 3.0)
    assert research.stats_cells(st) == ["5", "60.0%", "3.00", "+0.800", "1.00", "+4.0", "1.0", "3.0"]
    assert research.stats_cells(research.stats([])) == ["0"] + ["–"] * 7
    inf = research.Stats(2, 1.0, math.inf, 1.5, 3.0, 3.0, 0.0, 1.0)
    assert research.stats_cells(inf)[2] == "inf"


def test_report_names_the_outcome_the_gate_and_every_setup_exit_period():
    res = fake_results(passing_and_failing_rows())
    d = research.decide(res.rows)
    text = research.render_report(res, d, NOW)
    assert text.startswith("# CFD backtest report")
    assert "2026-10-05" in text
    assert "PB-D / FX" in text and "PB-H1-GOLD / Metals" in text            # what is enabled
    assert "no CFD signal goes live" not in text
    for setup in ("PB-D", "BO-D", "PB-H1-GOLD"):
        assert f"## {setup}" in text.splitlines()
    # a gate table with PASS and FAIL lines
    assert "PASS" in text and "FAIL" in text
    assert "out-of-sample t-statistic" in text and ">= 1.96" in text
    # the table row of the chosen exit, out of sample, with its numbers (200 trades, PF 1.89)
    pb_oos = [ln for ln in text.splitlines() if ln.startswith("| E2 * | OOS |")]
    assert any(ln.split("|")[3].strip() == "200" for ln in pb_oos)
    # the carry strategy note, in one line
    assert sum("CARRY-D" in ln for ln in text.splitlines()) == 1
    assert "information-only" in text


def test_report_says_so_when_nothing_passed():
    rows = make_rows("PB-D", "E1", "FX", "OOS", 40, 60) + make_rows("BO-D", "E1", "FX", "OOS", 40, 60)
    res = fake_results(rows)
    text = research.render_report(res, research.decide(rows), NOW)
    assert "Nothing passed the gate: no CFD signal goes live." in text


def test_report_lists_each_exit_each_period_and_each_class():
    res = fake_results(passing_and_failing_rows())
    text = research.render_report(res, research.decide(res.rows), NOW)
    # per setup x exit x period
    for exit_kind in ("E0", "E1", "E2"):
        for period in ("IS", "OOS"):
            assert f"| {exit_kind}" in text and f"| {period} |" in text
    # per class: the classes of the gate appear as table rows for PB-D
    body = section(text, "PB-D")
    for klass in ins.GATE_CLASSES:
        assert f"| {klass} |" in body
    # every exit has a row for both periods in the pooled table
    for exit_kind in ("E0", "E1", "E2 *"):
        for period in ("IS", "OOS"):
            assert f"| {exit_kind} | {period} |" in body


def test_report_has_the_data_coverage_and_the_trade_accounting():
    res = fake_results(passing_and_failing_rows())
    text = research.render_report(res, research.decide(res.rows), NOW)
    assert "EURUSD" in text and "5000" in text and "2005-01-03" in text and "2025-10-03" in text
    assert "XAUUSD" in text and "9000" in text
    assert "| PB-D | E2 | 10 | 8 | 1 | 1 | 0 |" in text         # signals, closed, skipped, open, blocked
    # what the hygiene rules removed: itemised when something was, a plain 0 when nothing was
    assert "3 (missing 2, inverted 0, wick 1, duplicate 0, unparsable 0, forming 0)" in text
    assert "| 2025-10-03 | 0 |" in text


def test_the_report_is_deterministic():
    res = fake_results(passing_and_failing_rows())
    d = research.decide(res.rows)
    assert research.render_report(res, d, NOW) == research.render_report(res, d, NOW)


def test_write_outputs_creates_the_folder_and_both_files(tmp_path):
    out = tmp_path / "docs" / "cfd"
    d = research.decide(passing_and_failing_rows())
    paths = research.write_outputs("# report\n", research.enabled_payload(d, NOW), out)
    assert (out / "BACKTEST_REPORT.md").read_text() == "# report\n"
    loaded = json.loads((out / "enabled.json").read_text())
    assert loaded["enabled"] == [["PB-D", "FX"], ["PB-D", "Metals"], ["PB-H1-GOLD", "Metals"]]
    assert {p.name for p in paths} == {"BACKTEST_REPORT.md", "enabled.json"}


def test_the_default_output_folder_is_docs_cfd_and_the_cache_is_git_ignored_data():
    assert research.OUT_DIR.parts[-2:] == ("docs", "cfd")
    assert data.CACHE_DIR.parts[-2:] == ("data", "cfd_cache")


# ================================================================== the wiring of each setup
def synthetic_bars(symbol, interval="1d", **kw):
    rows = synthetic_fetch(**kw)(symbol, interval)
    return data.clean_rows(rows, interval).bars


def test_each_setup_is_wired_to_its_entry_costs_and_session(monkeypatch):
    seen = []
    real = exits.simulate

    def spy(bars, sig, kind, **kw):
        seen.append((kind, kw))
        return real(bars, sig, kind, **kw)
    monkeypatch.setattr(exits, "simulate", spy)
    gspc = ins.by_symbol("^GSPC")
    research.backtest_daily(gspc, synthetic_bars("^GSPC"))
    assert {k for k, _ in seen} == {"E0", "E1", "E2"} and len(seen) > 30
    for _, kw in seen:
        assert kw["entry"] == "next_open" and kw.get("session") is None
        assert kw["costs"] == gspc.costs and kw.get("e0_target_r") is None
    seen.clear()
    research.backtest_gold(synthetic_bars("GC=F", "1h", hourly_days=300))
    assert {k for k, _ in seen} == {"E0", "E1", "E2"} and seen
    for _, kw in seen:
        assert kw["entry"] == "close" and kw["session"] == data.LONDON_SESSION
        assert kw["costs"] == ins.gold_intraday_costs()          # the XAUUSD round trip, no financing
        assert kw["e0_target_r"] == 5.0


HOUR = dt.timedelta(hours=1)
GOLD_QUIET = (417.2, 418.0, 416.5, 417.4)


def gold_series(after):
    """Hourly gold: 320 bars climbing 1 an hour (every true range 2), a 4-bar pullback, and a trigger
    bar at 10:00 UTC on 2024-01-17 (index 323: close 417, stop 412.905, R 4.0946), then `after`."""
    rows, c = [], 100.0
    for _ in range(320):
        o, c = c, c + 1
        rows.append((o, c + 0.5, o - 0.5, c))
    lv = c
    rows += [(lv, lv + 0.2, lv - 2.5, lv - 2.0), (lv - 2.0, lv - 1.8, lv - 4.5, lv - 4.0),
             (lv - 4.0, lv - 3.8, lv - 6.0, lv - 5.5), (lv - 5.5, lv - 2.5, lv - 5.8, lv - 3.0)]
    rows += after
    first = dt.datetime(2024, 1, 17, 10, tzinfo=UTC) - 323 * HOUR
    return [Bar(first + i * HOUR, *r) for i, r in enumerate(rows)]


def gold_rows(after):
    bars = gold_series(after)
    rows, _ = research.backtest_gold(bars)
    trigger = bars[323].ts
    return {r.exit_kind: r for r in rows if r.signal_ts == trigger}


R_GOLD = 417.0 - (414.0 - 0.5 * (((((2.0 * 13 + 2.7) / 14 * 13 + 2.7) / 14 * 13 + 2.2) / 14 * 13 + 3.3) / 14))
GOLD_COST = 0.030 / 100 * 417.0 / R_GOLD


def test_gold_e0_has_its_hard_five_r_target_in_the_backtest():
    by_exit = gold_rows([(417.0, 438.5, 416.9, 438.0)] + [(438.0, 438.5, 437.5, 438.0)] * 3)
    assert R_GOLD == pytest.approx(4.0946337)
    # the target (417 + 5 R) is reached on the first bar after the trigger: +5R minus the XAUUSD round trip
    assert by_exit["E0"].result_r == pytest.approx(5.0 - GOLD_COST)
    assert by_exit["E0"].hold_days < 1.0


def test_gold_trades_are_flat_at_the_session_end_under_every_exit():
    after = [GOLD_QUIET] * 5 + [(417.5, 418.5, 416.5, 418.0)] + [GOLD_QUIET]     # 11:00..15:00, 16:00, 17:00
    by_exit = gold_rows(after)
    assert set(by_exit) == {"E0", "E1", "E2"}
    # nothing reached: the position leaves at the close of the 16:00 bar (418.0)
    want = (418.0 - 417.0) / R_GOLD - GOLD_COST
    for exit_kind, row in by_exit.items():
        assert row.result_r == pytest.approx(want), exit_kind
        assert row.period == "ALL" and row.klass == "Metals"
        assert row.exit_ts == bars_ts(after, 329)


def bars_ts(after, index):
    return gold_series(after)[index].ts


# ================================================================== what feeds the gate
def test_class_decisions_use_only_out_of_sample_trades():
    rows = passing_and_failing_rows()
    # a glowing in-sample Energy record must not enable Energy: its out-of-sample trades lose
    rows += make_rows("PB-D", "E2", "Energy", "IS", 60, 0, symbol="Energy-X")
    d = research.decide(rows)
    assert d.gates["PB-D"].passed
    assert d.gates["PB-D"].enabled_classes == ["FX", "Metals"]
    assert [c.passed for c in d.gates["PB-D"].class_checks["Energy"]] == [True, False, False]


def test_signals_before_the_study_window_are_not_taken():
    # daily bars from mid-2003: with 300 bars of warm-up the first signals land in 2004-2005,
    # before 2006-01-01, and must not become trades (nor count as signals)
    from cfd import setups
    day, rows, price = dt.date(2003, 6, 2), [], 100.0
    rng = random.Random(4)
    while day < dt.date(2009, 1, 1):
        if day.weekday() < 5:
            o = price
            price *= 1 + rng.gauss(0.0006, 0.011)
            rows.append((day.isoformat(), o, max(o, price) * 1.002, min(o, price) * 0.998, price))
        day += DAY
    bars = data.clean_rows(rows, "1d").bars
    all_signals = setups.pb_d(bars)
    early = [s for s in all_signals if bars[s.index].ts.date() < dt.date(2006, 1, 1)]
    assert early, "the premise: this series has signals before 2006"
    out, counts = research.backtest_daily(ins.by_symbol("EURUSD=X"), bars)
    assert out and all(r.signal_ts.date() >= dt.date(2006, 1, 1) for r in out)
    assert counts[("PB-D", "E1")].signals == len(all_signals) - len(early)


# ================================================================== end to end on synthetic bars
SUBSET = tuple(ins.by_symbol(s) for s in ("EURUSD=X", "EURGBP=X", "GC=F", "^GSPC", "BTC-USD"))


def synthetic_fetch(calls=None, *, hourly_days=70):
    """Random-walk Yahoo-shaped rows for any symbol: daily from 2005 to 2022 (crypto from 2014), and
    hourly gold for the last `hourly_days` days before 2025-01-10."""
    def fetch(symbol, interval):
        if calls is not None:
            calls.append((symbol, interval))
        rng = random.Random(f"{symbol}-{interval}")
        price, rows = 100.0, []
        if interval == "1d":
            day = dt.date(2014, 9, 17) if symbol == "BTC-USD" else dt.date(2005, 1, 3)
            while day <= dt.date(2022, 12, 30):
                if symbol == "BTC-USD" or day.weekday() < 5:
                    o = price
                    price = max(1.0, price * (1 + rng.gauss(0.0004, 0.011)))
                    hi = max(o, price) * (1 + abs(rng.gauss(0, 0.003)))
                    lo = min(o, price) * (1 - abs(rng.gauss(0, 0.003)))
                    rows.append((day.isoformat(), o, hi, lo, price))
                day += DAY
        else:
            ts = dt.datetime(2025, 1, 10, tzinfo=UTC) - dt.timedelta(days=hourly_days)
            end = dt.datetime(2025, 1, 10, tzinfo=UTC)
            while ts < end:
                if ts.weekday() < 5:
                    o = price
                    price *= 1 + rng.gauss(0.0001, 0.002)
                    hi = max(o, price) * (1 + abs(rng.gauss(0, 0.0007)))
                    lo = min(o, price) * (1 - abs(rng.gauss(0, 0.0007)))
                    rows.append((ts.isoformat(), o, hi, lo, price))
                ts += dt.timedelta(hours=1)
        return rows
    return fetch


NOW_RUN = dt.datetime(2025, 1, 10, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def outcome(tmp_path_factory):
    base = tmp_path_factory.mktemp("cfdrun")
    return research.run(fetch=synthetic_fetch(), now=NOW_RUN, cache_dir=base / "cache",
                        out_dir=base / "out", universe=SUBSET), base


def test_run_end_to_end_writes_the_report_and_enabled_json(outcome):
    out, base = outcome
    report = (base / "out" / "BACKTEST_REPORT.md").read_text()
    payload = json.loads((base / "out" / "enabled.json").read_text())
    assert report.startswith("# CFD backtest report")
    assert set(payload) == {"exit", "enabled", "run_at"}
    assert set(payload["exit"]) == {"PB-D", "BO-D", "PB-H1-GOLD"}
    assert all(v in ("E1", "E2") for v in payload["exit"].values())
    assert payload["run_at"] == NOW_RUN.isoformat()
    assert payload["enabled"] == [[s, c] for s, c in out.decision.enabled]
    for pair in payload["enabled"]:
        assert pair[0] in payload["exit"] and pair[1] in ins.GATE_CLASSES


def test_run_produces_trades_for_every_setup_and_exit_in_both_periods(outcome):
    out, _ = outcome
    rows = out.results.rows
    for setup in ("PB-D", "BO-D"):
        for exit_kind in ("E0", "E1", "E2"):
            periods = {r.period for r in rows if r.setup == setup and r.exit_kind == exit_kind}
            assert {"IS", "OOS"} <= periods, (setup, exit_kind, periods)
    gold = {(r.exit_kind, r.period) for r in rows if r.setup == "PB-H1-GOLD"}
    assert gold and {p for _, p in gold} == {"ALL"}


def test_run_applies_the_period_split_by_signal_date(outcome):
    out, _ = outcome
    for r in out.results.rows:
        if r.setup == "PB-H1-GOLD":
            continue
        day = r.signal_ts.date()
        want = research.period_of(r.klass, day)
        assert r.period == want and want is not None
    crypto = [r for r in out.results.rows if r.klass == "Crypto" and r.setup != "PB-H1-GOLD"]
    assert any(r.period == "IS" and r.signal_ts.year >= 2017 for r in crypto)
    assert not any(r.period == "OOS" and r.signal_ts.year < 2020 for r in crypto)


def test_run_trades_are_all_closed_and_costed(outcome):
    out, _ = outcome
    assert out.results.rows
    assert all(math.isfinite(r.result_r) for r in out.results.rows)
    assert all(r.exit_ts >= r.signal_ts for r in out.results.rows)


def test_run_chosen_exit_and_gate_match_a_recomputation_from_the_rows(outcome):
    out, _ = outcome
    again = research.decide(out.results.rows)
    assert again.chosen == out.decision.chosen
    assert again.enabled == out.decision.enabled
    for setup in again.gates:
        assert again.gates[setup].passed == out.decision.gates[setup].passed


def test_run_the_report_has_every_instrument_in_its_data_section(outcome):
    out, base = outcome
    text = (base / "out" / "BACKTEST_REPORT.md").read_text()
    for inst in SUBSET:
        assert inst.name in text
    assert "GC=F" in text and "1h" in text
    assert "CARRY-D" in text


def test_run_is_deterministic_and_reads_the_cache_the_second_time(tmp_path):
    calls: list = []
    kwargs = dict(now=NOW_RUN, cache_dir=tmp_path / "cache", universe=SUBSET[:2])
    a = research.run(fetch=synthetic_fetch(calls), out_dir=tmp_path / "a", **kwargs)
    first_calls = len(calls)
    assert first_calls == len(SUBSET[:2]) + 1                    # each daily series, plus gold hourly
    b = research.run(fetch=synthetic_fetch(calls), out_dir=tmp_path / "b", **kwargs)
    assert len(calls) == first_calls                              # all from the cache
    report_a = (tmp_path / "a" / "BACKTEST_REPORT.md").read_text()
    assert report_a == (tmp_path / "b" / "BACKTEST_REPORT.md").read_text()
    assert a.decision.enabled == b.decision.enabled
    research.run(fetch=synthetic_fetch(calls), out_dir=tmp_path / "c", refresh=True, **kwargs)
    assert len(calls) == 2 * first_calls                          # --refresh goes back to the source


def test_run_survives_an_instrument_whose_data_fails(tmp_path):
    good = synthetic_fetch()

    def flaky(symbol, interval):
        if symbol == "EURGBP=X":
            raise RuntimeError("Yahoo is down")
        return good(symbol, interval)
    out = research.run(fetch=flaky, now=NOW_RUN, cache_dir=tmp_path / "cache", out_dir=tmp_path / "out",
                       universe=SUBSET[:3])
    text = (tmp_path / "out" / "BACKTEST_REPORT.md").read_text()
    assert "EURGBP" in text and "no data" in text
    assert {r.symbol for r in out.results.rows} >= {"EURUSD=X"}
    assert "EURGBP=X" not in {r.symbol for r in out.results.rows}


def test_run_without_gold_hourly_data_reports_the_gold_setup_as_not_enabled(tmp_path):
    good = synthetic_fetch()

    def no_hourly(symbol, interval):
        return [] if interval == "1h" else good(symbol, interval)
    out = research.run(fetch=no_hourly, now=NOW_RUN, cache_dir=tmp_path / "cache",
                       out_dir=tmp_path / "out", universe=SUBSET[:3])
    assert not out.decision.gates["PB-H1-GOLD"].passed
    assert all(s != "PB-H1-GOLD" for s, _ in out.decision.enabled)
    assert "no data" in (tmp_path / "out" / "BACKTEST_REPORT.md").read_text()


# ================================================================== the command line
def test_cli_without_run_does_nothing_and_asks_for_it(capsys, tmp_path):
    calls: list = []
    rc = research.main([], fetch=synthetic_fetch(calls), out_dir=tmp_path / "out",
                       cache_dir=tmp_path / "cache")
    assert rc == 2 and calls == []
    assert "--run" in capsys.readouterr().out
    assert not (tmp_path / "out").exists()


def test_cli_run_prints_the_exit_the_gate_and_the_enabled_classes(capsys, tmp_path):
    rc = research.main(["--run"], fetch=synthetic_fetch(), now=NOW_RUN, out_dir=tmp_path / "out",
                       cache_dir=tmp_path / "cache", universe=SUBSET)
    out = capsys.readouterr().out
    assert rc == 0
    for setup in ("PB-D", "BO-D", "PB-H1-GOLD"):
        line = next(ln for ln in out.splitlines() if ln.startswith(setup))
        assert "exit E" in line and ("gate PASS" in line or "gate FAIL" in line)
    assert "enabled:" in out
    assert (tmp_path / "out" / "enabled.json").exists()


def test_cli_refresh_flag_refetches(tmp_path):
    calls: list = []
    common = dict(fetch=synthetic_fetch(calls), now=NOW_RUN, out_dir=tmp_path / "out",
                  cache_dir=tmp_path / "cache", universe=SUBSET[:1])
    research.main(["--run"], **common)
    n = len(calls)
    research.main(["--run"], **common)
    assert len(calls) == n
    research.main(["--run", "--refresh"], **common)
    assert len(calls) == 2 * n
