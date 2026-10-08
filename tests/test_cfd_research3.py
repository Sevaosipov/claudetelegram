"""cfd/research3.py: round 3 (forex) -- H6 FX-REV and H7 CARRY-BASKET exactly as docs/cfd/PREREGISTRATION_R3.md
fixes them. What is tested: the wiring of H6 (setup, exit, costs, periods) both by spying on the simulator and by
hand-computed results on crafted bars with the real setup, H7's months and their periods, the gate arithmetic of
each hypothesis (t >= 2.45), the round3 block of enabled.json and its merge, the report, and the whole run behind
fake Yahoo and FRED fetches. Nothing here reaches the network and nothing is written outside tmp_path."""
from __future__ import annotations

import datetime as dt
import json
import math
import random

import pytest

from cfd import basket as bk
from cfd import data, exits, rates, research, research2, research3, setups
from cfd import indicators as ind
from cfd import instruments as ins
from cfd.data import Bar
from cfd.setups import Signal

UTC = dt.timezone.utc
DAY = dt.timedelta(days=1)
D = dt.date
NOW = dt.datetime(2026, 10, 8, 12, 0, tzinfo=UTC)

FX_REV, CARRY_BASKET = "FX-REV", "CARRY-BASKET"


def approx(x):
    return pytest.approx(x, rel=1e-9, abs=1e-9)


def daily(rows, start):
    return [Bar(start + i * DAY, float(o), float(h), float(low), float(c))
            for i, (o, h, low, c) in enumerate(rows)]


def utc(y, m, d):
    return dt.datetime(y, m, d, tzinfo=UTC)


def mirror(rows, k):
    return [(k - o, k - low, k - h, k - c) for (o, h, low, c) in rows]


def flat(n, price=100.0, half_range=0.5):
    return [(price, price + half_range, price - half_range, price)] * n


def pair(name):
    return next(i for i in ins.H6_UNIVERSE if i.name == name)


def S(n=150, win=0.5, pf=1.15, mean=0.05, t=2.5, total=1.0, dd=1.0, hold=1.0):
    return research.Stats(n, win, pf, mean, t, total, dd, hold)


def M(n=120, win=0.5, pf=1.15, mean=0.002, t=2.5, total=0.2, dd=5.0):
    return bk.MonthStats(n, win, pf, mean, t, total, dd)


def row(r=1.0, *, exit_kind="E0c", period="OOS", day=1, hold=1.0, symbol="EURCHF=X", name="EURCHF"):
    exit_ts = utc(2020, 1, 1) + day * DAY
    return research.Row(FX_REV, exit_kind, symbol, name, "FX", period, r, exit_ts - hold * DAY, exit_ts, hold)


def make_rows(period, wins, losses, *, day0=0, symbol="EURCHF=X", name="EURCHF"):
    out, w, lo, day = [], wins, losses, day0
    while w or lo:
        if w:
            out.append(row(1.0, period=period, day=day, symbol=symbol, name=name))
            w -= 1
            day += 1
        if lo:
            out.append(row(-1.0, period=period, day=day, symbol=symbol, name=name))
            lo -= 1
            day += 1
    return out


def make_months(period, wins, losses, *, year=2018, win_net=0.01, loss_net=-0.005):
    """Months of the given period: `wins` of win_net and `losses` of loss_net, interleaved, one a month."""
    b = bk.Basket(("USD", "EUR", "GBP"), ("JPY", "CHF", "CAD"))
    nets = []
    w, lo = wins, losses
    while w or lo:
        if w:
            nets.append(win_net)
            w -= 1
        if lo:
            nets.append(loss_net)
            lo -= 1
    out = []
    for k, net in enumerate(nets):
        start = D(year + k // 12, k % 12 + 1, 1)
        out.append(bk.Month(start, start + dt.timedelta(days=28), b, 28, 0, net + 0.002, 0.002, net))
    return out


def periods_of(months, period):
    return {m.start: period for m in months}


def section(text, title):
    lines = text.splitlines()
    start = lines.index(f"## {title}") + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end])


# ================================================================== the pre-registered numbers
def test_the_hypotheses_and_their_numbers_are_the_preregistered_ones():
    assert research3.HYPOTHESES == (FX_REV, CARRY_BASKET)
    assert research3.LABELS == {FX_REV: "H6", CARRY_BASKET: "H7"}
    assert research3.MIN_OOS == {FX_REV: 100, CARRY_BASKET: 100}
    assert research3.GATE_MIN_PF == 1.10
    assert research3.GATE_MIN_T == 2.45
    assert research3.REV_TIME_STOP == 16
    assert research3.JUDGED == {FX_REV: "E0c", CARRY_BASKET: "monthly"}
    assert research3.OUT_DIR == research.OUT_DIR and research3.OUT_DIR.parts[-2:] == ("docs", "cfd")
    assert research3.REPORT_NAME == "BACKTEST_REPORT_R3.md" and research3.BLOCK == "round3"
    assert data.MAX_WICK == 0.15


def test_the_default_universes_are_the_preregistered_ones():
    assert [i.symbol for i in research3.UNIVERSES[FX_REV]] == [
        "EURCHF=X", "EURGBP=X", "AUDNZD=X", "AUDCAD=X", "NZDCAD=X", "USDCAD=X", "EURNOK=X", "EURSEK=X"]
    assert [i.symbol for i in research3.UNIVERSES[CARRY_BASKET]] == [
        "EURUSD=X", "GBPUSD=X", "AUDUSD=X", "NZDUSD=X", "USDJPY=X", "USDCAD=X", "USDCHF=X"]


# ================================================================== H6: what it runs
@pytest.fixture
def spy(monkeypatch):
    seen = []
    real = exits.simulate

    def simulate(bars, sig, kind, **kw):
        seen.append((kind, kw))
        return real(bars, sig, kind, **kw)
    monkeypatch.setattr(exits, "simulate", simulate)
    return seen


def quiet_bars(n=40, start=utc(2020, 1, 1)):
    return daily(flat(n), start)


def test_h6_runs_one_exit_e0c_with_a_16_bar_time_stop_the_sma20_condition_and_no_trail(monkeypatch, spy):
    monkeypatch.setattr(research3.setups, "fx_rev", lambda bars: [Signal(30, "long", 96.0, 1.0)])
    bars = quiet_bars()
    rows, counts = research3.backtest_fx_rev(pair("EURCHF"), bars)
    assert [k for k, _ in spy] == ["E0c"]
    kw = spy[0][1]
    assert kw["time_stop_bars"] == 16 and callable(kw["exit_when"])
    assert kw["costs"] == pair("EURCHF").costs and kw["entry"] == "next_open"
    assert "trail_atr" not in kw and "max_r_atr" not in kw and kw.get("e0_target_r") is None
    assert set(counts) == {(FX_REV, "E0c")}


def test_h6_the_condition_is_a_close_at_or_beyond_sma20_equal_counts():
    f = research3.beyond_sma([5.0, 5.0, 4.99, 5.01, 5.0], [5.0, 5.0, 5.0, 5.0, None])
    assert f(0, "long") and f(0, "short")                    # equal: both count
    assert not f(2, "long") and f(2, "short")                # under the average: only a short is done
    assert f(3, "long") and not f(3, "short")                # over it: only a long is done
    assert not f(4, "long") and not f(4, "short")            # no average yet, no condition


def test_h6_reads_the_sma20_of_the_closes(monkeypatch, spy):
    monkeypatch.setattr(research3.setups, "fx_rev", lambda bars: [Signal(30, "long", 96.0, 1.0)])
    bars = daily(flat(30) + [(100.0, 100.5, 96.5, 97.0)] + flat(9), utc(2020, 1, 1))
    research3.backtest_fx_rev(pair("EURCHF"), bars)
    f = spy[0][1]["exit_when"]
    closes, sma = ind.closes(bars), ind.sma(ind.closes(bars), 20)
    for j in (25, 30, 31, 35):
        assert f(j, "long") == (closes[j] >= sma[j]) and f(j, "short") == (closes[j] <= sma[j])
    assert f(10, "long") is False and f(10, "short") is False          # SMA20 is not there yet


# ---- H6 on crafted bars with the real setup (300 flat bars, then the drop at index 340)
DROP = (100.0, 100.5, 96.5, 97.0)       # TR 4 -> ATR(14) 17/14; SMA20 99.85, SD20 0.653835; ADX 7.14; stop 94.5714
R_ENTRY = 41 / 14                       # entry 97.5 - stop (97 - 2 * 17/14) = 0.5 + 34/14


def crafted(after, start):
    return daily(flat(340) + [DROP] + after, start)


def start_for(signal_day):
    return dt.datetime(signal_day.year, signal_day.month, signal_day.day, tzinfo=UTC) - 340 * DAY


SMA_EXIT = [(97.5, 98.8, 97.2, 98.5),       # 341: the entry bar (open 97.5); close 98.5 < SMA20 99.775
            (98.5, 100.5, 98.0, 100.0),     # 342: close 100.0 >= SMA20 99.775
            (100.2, 100.7, 99.8, 100.0)] + flat(10)    # 343: the exit, at its open 100.2


def test_h6_long_leaves_at_the_open_after_the_first_close_at_or_above_sma20_hand_computed():
    bars = crafted(SMA_EXIT, start_for(D(2020, 6, 1)))
    assert [(s.index, s.side) for s in setups.fx_rev(bars)] == [(340, "long")]
    rows, counts = research3.backtest_fx_rev(pair("EURCHF"), bars)
    assert len(rows) == 1 and (counts[(FX_REV, "E0c")].signals, counts[(FX_REV, "E0c")].closed) == (1, 1)
    # entry 97.5, R = 41/14, exit 100.2 (the open of bar 343), 2 nights
    # gross = 2.7 / R = 37.8/41 = 0.9219512; cost = (0.030 + 2 * 0.010) % * 97.5 / R = 0.0005 * 97.5 * 14/41 = 0.0166463
    assert rows[0].result_r == approx(37.8 / 41 - 0.0005 * 97.5 * 14 / 41)
    assert rows[0].result_r == pytest.approx(0.9053049, abs=1e-6)
    assert (rows[0].setup, rows[0].exit_kind, rows[0].symbol, rows[0].period) == (FX_REV, "E0c", "EURCHF=X", "OOS")
    assert rows[0].hold_days == 2.0 and rows[0].signal_ts == bars[340].ts and rows[0].exit_ts == bars[343].ts


def test_h6_short_is_the_mirror_image_and_leaves_on_a_close_at_or_below_sma20():
    bars = crafted(SMA_EXIT, start_for(D(2020, 6, 1)))
    mirrored = daily(mirror([(b.open, b.high, b.low, b.close) for b in bars], 200.0), bars[0].ts)
    assert [(s.index, s.side) for s in setups.fx_rev(mirrored)] == [(340, "short")]
    rows, _ = research3.backtest_fx_rev(pair("EURCHF"), mirrored)
    # the same R and gross; the entry is 102.5 (the mirror of 97.5), so the cost is 0.0005 * 102.5 * 14/41
    assert len(rows) == 1 and rows[0].result_r == approx(37.8 / 41 - 0.0005 * 102.5 * 14 / 41)


def test_h6_the_stop_fills_at_its_level_before_anything_else_in_the_entry_bar():
    bars = crafted([(97.5, 97.8, 94.0, 94.5)] + flat(10), start_for(D(2020, 6, 1)))
    rows, _ = research3.backtest_fx_rev(pair("EURCHF"), bars)
    # stop 94.5714 hit in the entry bar: -1R gross, no night, a round trip of 0.030 % -> 0.0003 * 97.5 * 14/41
    assert rows[0].result_r == approx(-1.0 - 0.0003 * 97.5 * 14 / 41)
    assert rows[0].hold_days == 0.0


def test_h6_a_trade_that_goes_nowhere_leaves_at_the_open_of_the_16th_bar_after_entry():
    stay = [(97.5, 97.8, 97.2, 97.6)] * 20
    bars = crafted(stay, start_for(D(2020, 6, 1)))
    rows, _ = research3.backtest_fx_rev(pair("EURCHF"), bars)
    # entry bar 341 (open 97.5), out at the open of bar 357 (97.5): gross 0, 16 nights: (0.030 + 0.16) %
    assert rows[0].exit_ts == bars[357].ts and rows[0].hold_days == 16.0
    assert rows[0].result_r == approx(-0.0019 * 97.5 * 14 / 41)


def test_h6_the_condition_beats_the_time_stop_when_it_comes_first():
    bars = crafted(SMA_EXIT, start_for(D(2020, 6, 1)))
    rows, _ = research3.backtest_fx_rev(pair("EURCHF"), bars)
    assert rows[0].exit_ts == bars[343].ts           # not bar 357


def test_h6_each_pair_pays_its_own_round_trip_and_ten_thousandths_a_night():
    bars = crafted(SMA_EXIT, start_for(D(2020, 6, 1)))
    gross = 37.8 / 41

    def result(name):
        return research3.backtest_fx_rev(pair(name), bars)[0][0].result_r
    for name, round_trip in (("EURCHF", 0.030), ("AUDNZD", 0.030), ("USDCAD", 0.015),
                             ("EURNOK", 0.060), ("EURSEK", 0.060)):
        assert result(name) == approx(gross - (round_trip + 2 * 0.010) / 100 * 97.5 * 14 / 41), name


def test_h6_skips_an_entry_gapped_next_to_its_stop():
    # the entry bar opens at 95.0: R = 95.0 - 94.5714 = 0.4286 > 0.25 ATR (0.3036), taken; at 94.8 it is 0.2286 -> skipped
    taken = crafted([(95.0, 95.5, 94.9, 95.0)] + flat(5), start_for(D(2020, 6, 1)))
    skipped = crafted([(94.8, 95.5, 94.7, 95.0)] + flat(5), start_for(D(2020, 6, 1)))
    c_taken = research3.backtest_fx_rev(pair("EURCHF"), taken)[1][(FX_REV, "E0c")]
    assert c_taken.skipped == 0 and c_taken.closed >= 1
    rows, counts = research3.backtest_fx_rev(pair("EURCHF"), skipped)
    c = counts[(FX_REV, "E0c")]
    assert c.skipped == 1 and all(r.signal_ts != skipped[340].ts for r in rows)    # the signal at 340 took no trade


def test_h6_periods_follow_the_signal_date_and_warm_up_is_dropped():
    def period(day):
        rows, _ = research3.backtest_fx_rev(pair("EURCHF"), crafted(SMA_EXIT, start_for(day)))
        return rows[0].period if rows else None
    assert period(D(2003, 6, 1)) is None                 # before 2006-01-01: warm-up
    assert period(D(2005, 12, 31)) is None
    assert period(D(2006, 1, 1)) == "IS"
    assert period(D(2016, 12, 31)) == "IS"
    assert period(D(2017, 1, 1)) == "OOS"
    assert period(D(2024, 5, 1)) == "OOS"


def test_h6_one_trade_at_a_time_per_pair():
    # a second reversion signal inside the open trade is blocked (read while the trade is open)
    bars = crafted([(97.5, 98.8, 97.2, 98.5), (98.5, 98.9, 95.9, 96.0), (96.0, 96.5, 95.5, 96.2)]
                   + [(96.2, 96.6, 95.8, 96.0)] * 25, start_for(D(2020, 6, 1)))
    rows, counts = research3.backtest_fx_rev(pair("EURCHF"), bars)
    c = counts[(FX_REV, "E0c")]
    assert c.signals >= 2 and c.blocked >= 1 and len(rows) == c.closed


# ================================================================== H7: months and periods
def usd_pair_bars(days, price=1.0):
    return [Bar(utc(d.year, d.month, d.day), price, price, price, price) for d in days]


def all_rates(rate_by_ccy):
    return rates.Rates(series={c: rates.RateSeries(c, rates.SERIES[c], ((D(2005, 1, 1), r),))
                               for c, r in rate_by_ccy.items()})


RATE_MAP = {"USD": 2.0, "EUR": 0.5, "GBP": 1.0, "JPY": -0.1, "AUD": 1.5, "CAD": 1.2, "CHF": -0.5, "NZD": 1.7}


def test_h7_prices_each_currency_in_usd_direct_or_inverted():
    bars = {"EURUSD=X": usd_pair_bars([D(2020, 1, 2)], 1.25), "USDJPY=X": usd_pair_bars([D(2020, 1, 2)], 100.0)}
    v = research3.basket_values(bars)
    assert set(v) == {"EUR", "JPY"} and v["EUR"] == [(D(2020, 1, 2), 1.25)]
    assert v["JPY"] == [(D(2020, 1, 2), 0.01)]


def test_h7_a_pair_that_did_not_load_is_a_currency_left_out():
    bars = {"EURUSD=X": usd_pair_bars([D(2020, 1, 2)]), "USDJPY=X": []}
    assert set(research3.basket_values(bars)) == {"EUR"}


def test_h7_months_are_split_by_their_first_day():
    days = [D(2005, 12, 1), D(2006, 1, 2), D(2016, 12, 1), D(2017, 1, 2), D(2017, 2, 1)]
    bars = {pair_symbol: usd_pair_bars(days) for pair_symbol, _ in ins.H7_PAIRS.values()}
    months, period_of = research3.backtest_basket(bars, all_rates(RATE_MAP))
    assert [m.start for m in months] == days[:4]
    assert [period_of[m.start] for m in months] == [None, "IS", "IS", "OOS"]    # Dec 2005 is warm-up
    assert all(m.basket.longs == ("USD", "NZD", "AUD") for m in months)


def test_h7_period_boundaries():
    assert research.period_of("FX", D(2016, 12, 31)) == "IS" and research.period_of("FX", D(2017, 1, 1)) == "OOS"
    assert research.period_of("FX", D(2005, 12, 31)) is None and research.period_of("FX", D(2006, 1, 1)) == "IS"


# ================================================================== the gate
def gate_fx(is_stats=None, oos=None):
    return research3.gate_fx_rev(is_stats or S(mean=0.05), oos or S())


def gate_bk(is_stats=None, oos=None):
    return research3.gate_carry_basket(is_stats or M(n=130, mean=0.001), oos or M())


def test_the_gate_lines_and_thresholds_of_h6():
    g = gate_fx()
    assert g.passed and g.verdict == "PASS" and g.exit_kind == "E0c" and g.name == FX_REV
    assert [(c.rule, c.threshold) for c in g.checks] == [
        ("out-of-sample trades", ">= 100"), ("out-of-sample profit factor", ">= 1.10"),
        ("out-of-sample mean R", "> 0"), ("out-of-sample t-statistic", ">= 2.45"),
        ("in-sample mean R", "> 0")]


def test_the_gate_lines_and_thresholds_of_h7():
    g = gate_bk()
    assert g.passed and g.exit_kind == "monthly" and g.name == CARRY_BASKET
    assert [(c.rule, c.threshold) for c in g.checks] == [
        ("out-of-sample months", ">= 100"), ("out-of-sample profit factor", ">= 1.10"),
        ("out-of-sample mean month", "> 0"), ("out-of-sample t-statistic", ">= 2.45"),
        ("in-sample mean month", "> 0")]
    assert g.checks[2].value == "+0.2000%" and g.checks[4].value == "+0.1000% (130 months)"


@pytest.mark.parametrize("oos, failing", [
    (dict(n=99), "out-of-sample trades"),
    (dict(pf=1.0999), "out-of-sample profit factor"),
    (dict(mean=0.0, t=3.0), "out-of-sample mean R"),
    (dict(mean=-0.01, t=3.0), "out-of-sample mean R"),
    (dict(t=2.4499), "out-of-sample t-statistic"),
    (dict(t=2.33), "out-of-sample t-statistic"),            # round 2's bar is not round 3's
])
def test_h6_gate_fails_one_line_at_a_time(oos, failing):
    g = gate_fx(oos=S(**oos))
    assert not g.passed and g.verdict == "FAIL"
    assert [c.rule for c in g.failures()] == [failing]
    assert failing in g.summary() and g.summary().startswith("FAIL (")


@pytest.mark.parametrize("oos, failing", [
    (dict(n=99), "out-of-sample months"),
    (dict(pf=1.0999), "out-of-sample profit factor"),
    (dict(mean=0.0, t=3.0), "out-of-sample mean month"),
    (dict(mean=-0.001, t=3.0), "out-of-sample mean month"),
    (dict(t=2.4499), "out-of-sample t-statistic"),
])
def test_h7_gate_fails_one_line_at_a_time(oos, failing):
    g = gate_bk(oos=M(**oos))
    assert [c.rule for c in g.failures()] == [failing]


def test_the_gates_pass_exactly_on_their_thresholds():
    assert gate_fx(oos=S(n=100, pf=1.10, t=2.45, mean=1e-9)).passed
    assert gate_bk(oos=M(n=100, pf=1.10, t=2.45, mean=1e-9)).passed


def test_an_infinite_profit_factor_passes_its_line():
    assert gate_fx(oos=S(pf=math.inf)).passed and gate_bk(oos=M(pf=math.inf)).passed


def test_the_in_sample_mean_is_always_required_with_no_minimum_of_trades():
    assert not gate_fx(is_stats=S(n=500, mean=-0.001)).passed
    assert not gate_fx(is_stats=S(n=500, mean=0.0)).passed
    assert not gate_fx(is_stats=S(n=3, mean=-0.2)).passed             # round 2 would have only reported this
    assert not gate_fx(is_stats=research.stats([])).passed            # no in-sample trades is a mean of 0
    assert not gate_bk(is_stats=M(n=130, mean=-0.0001)).passed
    assert not gate_bk(is_stats=bk.month_stats([])).passed
    assert gate_fx(is_stats=S(n=3, mean=0.2)).passed


def test_the_gate_has_no_class_or_pair_level():
    g = gate_fx()
    assert {c.scope for c in g.checks} == {FX_REV} and len(g.checks) == 5


# ================================================================== decide and the payload
def passing_inputs():
    rows = make_rows("IS", 60, 40, day0=0) + make_rows("OOS", 70, 30, day0=1000)
    months = make_months("IS", 80, 40, year=2007, win_net=0.01, loss_net=-0.005) \
        + make_months("OOS", 80, 40, year=2017, win_net=0.01, loss_net=-0.005)
    period = {m.start: ("IS" if m.start.year < 2017 else "OOS") for m in months}
    return rows, months, period


def test_decide_enables_the_hypotheses_that_pass_each_on_its_own_sample():
    rows, months, period = passing_inputs()
    d = research3.decide(rows, months, period)
    assert d.enabled == [FX_REV, CARRY_BASKET]
    assert d.rev["OOS"].n == 100 and d.rev["IS"].n == 100
    assert d.basket["OOS"].n == 120 and d.basket["IS"].n == 120
    assert d.judged == {FX_REV: "E0c", CARRY_BASKET: "monthly"}
    assert d.gates[FX_REV].passed and d.gates[CARRY_BASKET].passed


def test_decide_one_can_pass_while_the_other_fails():
    rows, months, period = passing_inputs()
    assert research3.decide(rows, [], {}).enabled == [FX_REV]
    assert research3.decide([], months, period).enabled == [CARRY_BASKET]
    d = research3.decide([], [], {})
    assert d.enabled == [] and d.rev["OOS"].n == 0 and d.basket["OOS"].n == 0
    assert not d.gates[FX_REV].passed and not d.gates[CARRY_BASKET].passed


def test_decide_uses_only_the_pooled_e0c_trades_of_fx_rev_and_out_of_sample_for_the_gate():
    rows, months, period = passing_inputs()
    stray = [research.Row("OTHER", "E0c", "EURCHF=X", "EURCHF", "FX", "OOS", -1.0, utc(2020, 1, 1),
                          utc(2020, 1, 2), 1.0)] * 500 + [row(-1.0, exit_kind="EL", day=5000)] * 500
    d = research3.decide(rows + stray, months, period)
    assert d.rev["OOS"].n == 100 and d.gates[FX_REV].passed


def test_decide_leaves_out_months_that_belong_to_neither_period():
    rows, months, period = passing_inputs()
    warm = make_months("IS", 50, 0, year=1990)
    period_with = {**period, **{m.start: None for m in warm}}
    d = research3.decide(rows, months + warm, period_with)
    assert d.basket["IS"].n == 120 and d.basket["OOS"].n == 120


def test_decide_a_pass_is_for_the_whole_universe_no_per_pair_picking():
    rows = make_rows("IS", 60, 40) + make_rows("OOS", 70, 30, day0=1000)
    one_pair = [r for r in rows]
    d = research3.decide(one_pair, [], {})
    assert d.enabled == [FX_REV]                      # the pooled sample decides; a pair is not a gate line
    assert len(d.gates[FX_REV].checks) == 5


def test_the_round3_payload_is_the_machine_readable_outcome():
    rows, months, period = passing_inputs()
    payload = research3.round3_payload(research3.decide(rows, months[:3], period), NOW)
    assert list(payload) == ["enabled", "exit", "run_at"]
    assert payload["enabled"] == [FX_REV]
    assert payload["exit"] == {FX_REV: "E0c", CARRY_BASKET: "monthly"}      # both, passed or not
    assert payload["run_at"] == "2026-10-08T12:00:00+00:00"
    assert json.loads(json.dumps(payload)) == payload


# ================================================================== enabled.json
ROUNDS12_JSON = {
    "exit": {"PB-D": "E1", "BO-D": "E1", "PB-H1-GOLD": "E1"}, "enabled": [],
    "run_at": "2026-10-04T22:42:04+00:00",
    "round2": {"enabled": [], "exit": {"CR-BO": "E2", "IDX-DIP": "EL", "CARRY-FX": "EL"},
               "run_at": "2026-10-08T15:54:54+00:00"}}


def write_rounds12(path):
    path.write_text(json.dumps(ROUNDS12_JSON, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def block(enabled=(), at=NOW):
    return {"enabled": list(enabled), "exit": {FX_REV: "E0c", CARRY_BASKET: "monthly"}, "run_at": at.isoformat()}


def test_merging_adds_the_round3_block_and_leaves_rounds_one_and_two_byte_for_byte(tmp_path):
    path = tmp_path / "enabled.json"
    write_rounds12(path)
    before = path.read_text()
    merged = research3.merge_enabled(path, block([FX_REV]))
    after = path.read_text()
    loaded = json.loads(after)
    assert list(loaded) == ["exit", "enabled", "run_at", "round2", "round3"]
    assert {k: loaded[k] for k in ROUNDS12_JSON} == ROUNDS12_JSON and loaded["round3"] == block([FX_REV])
    assert merged == loaded
    head = before.rstrip().removesuffix("}").rstrip()
    assert after.startswith(head + ",\n  \"round3\": ")           # the text of rounds 1-2 is untouched
    assert after.endswith("\n") and not after.endswith("\n\n")


def test_merging_again_replaces_the_round3_block_in_place(tmp_path):
    path = tmp_path / "enabled.json"
    write_rounds12(path)
    research3.merge_enabled(path, block([FX_REV]))
    second = block([], NOW + DAY)
    research3.merge_enabled(path, second)
    loaded = json.loads(path.read_text())
    assert list(loaded) == ["exit", "enabled", "run_at", "round2", "round3"] and loaded["round3"] == second
    assert path.read_text().count('"round3"') == 1
    assert {k: loaded[k] for k in ROUNDS12_JSON} == ROUNDS12_JSON


def test_merging_into_a_missing_file_writes_just_the_round3_block(tmp_path):
    path = tmp_path / "docs" / "cfd" / "enabled.json"
    research3.merge_enabled(path, block())
    assert json.loads(path.read_text()) == {"round3": block()}


@pytest.mark.parametrize("junk", ["{not json", "[]", '"text"', "null"])
def test_merging_refuses_a_file_it_cannot_merge_into_and_leaves_it_alone(tmp_path, junk):
    path = tmp_path / "enabled.json"
    path.write_text(junk, encoding="utf-8")
    with pytest.raises(ValueError):
        research3.merge_enabled(path, block())
    assert path.read_text() == junk and not list(tmp_path.glob("*.tmp"))


def test_write_outputs_writes_the_report_and_merges_without_touching_the_other_reports(tmp_path):
    out = tmp_path / "docs" / "cfd"
    out.mkdir(parents=True)
    write_rounds12(out / "enabled.json")
    (out / "BACKTEST_REPORT.md").write_text("round one report\n")
    (out / "BACKTEST_REPORT_R2.md").write_text("round two report\n")
    paths = research3.write_outputs("# report\n", block([CARRY_BASKET]), out)
    assert (out / "BACKTEST_REPORT_R3.md").read_text() == "# report\n"
    assert (out / "BACKTEST_REPORT.md").read_text() == "round one report\n"
    assert (out / "BACKTEST_REPORT_R2.md").read_text() == "round two report\n"
    assert json.loads((out / "enabled.json").read_text())["round3"] == block([CARRY_BASKET])
    assert {p.name for p in paths} == {"BACKTEST_REPORT_R3.md", "enabled.json"}


def test_write_outputs_does_not_write_the_report_when_the_merge_would_fail(tmp_path):
    (tmp_path / "enabled.json").write_text("{broken")
    with pytest.raises(ValueError):
        research3.write_outputs("# report\n", block(), tmp_path)
    assert not (tmp_path / "BACKTEST_REPORT_R3.md").exists()


# ================================================================== the report
def fake_results(rows, months, period):
    res = research3.Results3()
    res.rows = list(rows)
    res.months = list(months)
    res.period_of_month = dict(period)
    res.counts[(FX_REV, "E0c")] = research.Counts(signals=10, closed=8, skipped=1, open=1, blocked=0)
    res.coverage.append(research.Coverage("EURCHF=X", "EURCHF", "1d", 5000, D(2003, 12, 1), D(2026, 10, 7),
                                          data.Dropped(missing=2, wick=1)))
    res.rate_coverage.append(research2.RateCoverage("EUR", "IR3TIB01EZM156N", 400, D(1994, 1, 1),
                                                    D(2026, 8, 1), "2026-10-08"))
    res.rate_coverage.append(research2.RateCoverage("JPY", "IR3TIB01JPM156N", 0, None, None, None,
                                                    "ConnectionError: down"))
    return res


def report(rows=None, months=None, period=None):
    if rows is None and months is None:
        rows, months, period = passing_inputs()
    res = fake_results(rows or [], months or [], period or {})
    d = research3.decide(res.rows, res.months, res.period_of_month)
    return research3.render_report(res, d, NOW), d


def test_the_report_names_the_outcome_the_gate_and_both_hypotheses():
    text, d = report()
    assert text.startswith("# CFD backtest report, round 3 (forex)")
    assert "2026-10-08" in text and "docs/cfd/PREREGISTRATION_R3.md" in text
    assert "Enabled for live signals: FX-REV, CARRY-BASKET." in section(text, "Outcome")
    gate = section(text, "Gate")
    assert "### FX-REV (judged on E0c)" in gate and "### CARRY-BASKET (judged on monthly)" in gate
    assert ">= 2.45" in gate and gate.count("PASS") == 10 and "FAIL" not in gate
    assert "| out-of-sample trades | 100 | >= 100 | PASS |" in gate
    assert "| out-of-sample months | 120 | >= 100 | PASS |" in gate


def test_the_report_says_so_when_nothing_passed():
    text, _ = report([], [], {})
    assert "No hypothesis passed the gate" in section(text, "Outcome")
    assert "Enabled for live signals" not in text and "FAIL" in section(text, "Gate")


def test_the_report_has_the_in_sample_and_out_of_sample_table_of_each_hypothesis():
    text, d = report()
    h6 = section(text, "FX-REV")
    assert "| IS | 100 |" in h6 and "| OOS | 100 |" in h6
    h7 = section(text, "CARRY-BASKET")
    assert "| IS | 120 |" in h7 and "| OOS | 120 |" in h7 and "Worst peak-to-trough" in h7
    # either period: 80 months of +1.0 % and 40 of -0.5 % -> mean (0.8 - 0.2) / 120 = +0.500 %, PF 0.8 / 0.2 = 4.00,
    # total +60.0 %
    assert "+0.500%" in h7 and "4.00" in h7 and "+60.0%" in h7


def test_the_report_has_per_pair_results_for_h6_and_per_year_results_for_h7_for_information():
    rows = make_rows("IS", 30, 20) + make_rows("OOS", 40, 20, day0=500) \
        + make_rows("OOS", 5, 5, symbol="EURNOK=X", name="EURNOK", day0=900)
    _, months, period = passing_inputs()
    text, _ = report(rows, months, period)
    h6 = section(text, "FX-REV")
    assert "By pair (for information, not a gate)" in h6
    assert "| EURCHF | IS | 50 |" in h6 and "| EURNOK | OOS | 10 |" in h6
    assert "AUDNZD" not in h6                              # a pair without trades has no row
    h7 = section(text, "CARRY-BASKET")
    assert "By year (for information, not a gate)" in h7
    for year in ("2007", "2008", "2017", "2018", "2026"):
        assert f"| {year} |" in h7


def test_the_report_has_the_trade_accounting_the_data_and_the_rates():
    text, _ = report()
    assert "| FX-REV | E0c | 10 | 8 | 1 | 1 | 0 |" in section(text, "Trade accounting")
    data_ = section(text, "Data")
    assert "EURCHF=X" in data_ and "5000" in data_
    assert "no rates: ConnectionError: down" in section(text, "Rates")


def test_the_report_notes_state_how_the_pre_registration_was_read():
    notes = section(report()[0], "Notes")
    assert "16th bar" in notes and "compounded" in notes and "2.45" in notes
    assert "no further round" in notes and "per-pair picking" in notes


def test_the_report_is_deterministic():
    assert report()[0] == report()[0]


# ================================================================== the run, behind fake fetches
def synthetic_fetch(calls=None):
    """Random-walk Yahoo-shaped rows for any symbol, weekdays from 2005 to 2022."""
    def fetch(symbol, interval):
        if calls is not None:
            calls.append((symbol, interval))
        rng = random.Random(f"{symbol}-{interval}")
        price, out = 100.0, []
        day = D(2005, 1, 3)
        while day <= D(2022, 12, 30):
            if day.weekday() < 5:
                o = price
                price = max(1.0, price * (1 + rng.gauss(0.0001, 0.006)))
                hi = max(o, price) * (1 + abs(rng.gauss(0, 0.001)))
                lo = min(o, price) * (1 - abs(rng.gauss(0, 0.001)))
                out.append((day.isoformat(), o, hi, lo, price))
            day += DAY
        return out
    return fetch


PHASE = {"USD": 0, "EUR": 20, "GBP": 40, "JPY": 60, "AUD": 10, "CAD": 30, "CHF": 50, "NZD": 70}


def fake_fred(calls=None, fail=()):
    def fetch(series_id):
        if calls is not None:
            calls.append(series_id)
        if series_id in fail:
            raise RuntimeError("FRED is down")
        currency = next(c for c, s in rates.SERIES.items() if s == series_id)
        lines = [f"observation_date,{series_id}"]
        for m in range(12 * 29):
            value = 2.0 + 1.5 * math.sin(2 * math.pi * (m + PHASE[currency]) / 96)
            lines.append(f"{1994 + m // 12}-{m % 12 + 1:02d}-01,{value:.4f}")
        return "\n".join(lines) + "\n"
    return fetch


NOW_RUN = dt.datetime(2023, 1, 10, 12, 0, tzinfo=UTC)
SMALL = {FX_REV: ins.H6_UNIVERSE[:3] + ins.H6_UNIVERSE[5:6], CARRY_BASKET: ins.H7_UNIVERSE}


@pytest.fixture(scope="module")
def outcome(tmp_path_factory):
    base = tmp_path_factory.mktemp("cfd3run")
    out = base / "out"
    out.mkdir()
    write_rounds12(out / "enabled.json")
    return research3.run(fetch=synthetic_fetch(), rates_fetch=fake_fred(), now=NOW_RUN,
                         cache_dir=base / "cache", out_dir=out, universes=SMALL), base


def test_run_end_to_end_writes_the_report_and_merges_enabled_json(outcome):
    out, base = outcome
    text = (base / "out" / "BACKTEST_REPORT_R3.md").read_text()
    payload = json.loads((base / "out" / "enabled.json").read_text())
    assert text.startswith("# CFD backtest report, round 3 (forex)") and text == out.report
    assert list(payload) == ["exit", "enabled", "run_at", "round2", "round3"]
    assert {k: payload[k] for k in ROUNDS12_JSON} == ROUNDS12_JSON
    r3 = payload["round3"]
    assert set(r3) == {"enabled", "exit", "run_at"} and r3["run_at"] == NOW_RUN.isoformat()
    assert r3 == out.payload and r3["exit"] == {FX_REV: "E0c", CARRY_BASKET: "monthly"}


def test_run_produces_trades_and_months_in_both_periods(outcome):
    out, _ = outcome
    assert {r.period for r in out.results.rows} == {"IS", "OOS"}
    assert out.decision.rev["IS"].n > 0 and out.decision.rev["OOS"].n > 0
    assert out.decision.basket["IS"].n > 0 and out.decision.basket["OOS"].n > 0
    assert {r.setup for r in out.results.rows} == {FX_REV} and {r.exit_kind for r in out.results.rows} == {"E0c"}
    assert {r.symbol for r in out.results.rows} <= {i.symbol for i in SMALL[FX_REV]}


def test_run_applies_each_period_split_by_the_date(outcome):
    out, _ = outcome
    for r in out.results.rows:
        assert r.period == ("IS" if r.signal_ts.date() <= D(2016, 12, 31) else "OOS")
        assert r.signal_ts.date() >= D(2006, 1, 1)
    for m in out.results.months:
        expected = research.period_of("FX", m.start)
        assert out.results.period_of_month[m.start] == expected


def test_run_trades_are_closed_costed_and_inside_the_universe(outcome):
    out, _ = outcome
    assert all(r.exit_ts >= r.signal_ts and 0 <= r.hold_days <= 30 for r in out.results.rows)
    c = out.results.counts[(FX_REV, "E0c")]
    assert c.closed == len(out.results.rows) and c.signals >= c.closed


def test_run_decision_matches_a_recomputation_from_the_rows_and_months(outcome):
    out, _ = outcome
    again = research3.decide(out.results.rows, out.results.months, out.results.period_of_month)
    assert again.enabled == out.decision.enabled
    assert {k: v.passed for k, v in again.gates.items()} == {k: v.passed for k, v in out.decision.gates.items()}
    assert again.rev == out.decision.rev and again.basket == out.decision.basket


def test_run_h7_months_are_the_basket_of_the_loaded_bars_and_rates(outcome, tmp_path):
    out, _ = outcome
    fetch = synthetic_fetch()
    bars = {i.symbol: data.daily_bars(i.symbol, fetch=fetch, today=NOW_RUN.date()) for i in ins.H7_UNIVERSE}
    r = rates.load_rates(fetch=fake_fred(), cache_dir=tmp_path)
    expected, _ = research3.backtest_basket(bars, r)
    assert out.results.months == expected and len(expected) > 150


def test_run_the_report_lists_every_pair_and_every_rate_series_it_used_once(outcome):
    out, _ = outcome
    symbols = [c.symbol for c in out.results.coverage]
    assert sorted(symbols) == sorted({i.symbol for i in SMALL[FX_REV]} | {i.symbol for i in SMALL[CARRY_BASKET]})
    assert len(symbols) == len(set(symbols))                       # USDCAD serves both and is listed once
    assert {c.currency for c in out.results.rate_coverage} == set(rates.SERIES)
    assert all(rates.SERIES[c] in out.report for c in rates.SERIES)


def test_run_is_deterministic_and_reads_the_caches_the_second_time(tmp_path):
    calls, fcalls = [], []
    kw = dict(now=NOW_RUN, cache_dir=tmp_path / "cache", out_dir=tmp_path / "out", universes=SMALL)
    a = research3.run(fetch=synthetic_fetch(calls), rates_fetch=fake_fred(fcalls), **kw)
    n, m = len(calls), len(fcalls)
    assert n == len({i.symbol for u in SMALL.values() for i in u}) and m == 8
    b = research3.run(fetch=synthetic_fetch(calls), rates_fetch=fake_fred(fcalls), **kw)
    assert (len(calls), len(fcalls)) == (n, m)
    assert a.report.replace("[", "") == b.report.replace("[", "") and a.payload == b.payload


def test_run_survives_a_pair_whose_data_fails_and_a_currency_whose_rates_fail(tmp_path):
    good = synthetic_fetch()

    def flaky(symbol, interval):
        if symbol in ("EURCHF=X", "USDJPY=X"):
            raise RuntimeError("rate limited")
        return good(symbol, interval)
    logs = []
    out = research3.run(fetch=flaky, rates_fetch=fake_fred(fail={rates.SERIES["EUR"]}), now=NOW_RUN,
                        cache_dir=tmp_path / "c", out_dir=tmp_path / "o", universes=SMALL, log=logs.append)
    assert any("EURCHF=X" in m for m in logs) and any("EUR" in m and "no rates" in m for m in logs)
    assert {c.symbol: c.bars for c in out.results.coverage}["EURCHF=X"] == 0
    assert "no data" in out.report and "no rates: " in out.report
    assert out.decision.basket["OOS"].n > 0                         # six currencies are left
    assert all("EUR" not in m.basket.legs for m in out.results.months)
    assert all(r.symbol != "EURCHF=X" for r in out.results.rows)


def test_run_without_any_rates_the_basket_has_no_months_and_fails(tmp_path):
    out = research3.run(fetch=synthetic_fetch(), rates_fetch=fake_fred(fail=set(rates.SERIES.values())),
                        now=NOW_RUN, cache_dir=tmp_path / "c", out_dir=tmp_path / "o", universes=SMALL)
    assert out.results.months == [] and not out.decision.gates[CARRY_BASKET].passed
    assert out.decision.rev["OOS"].n > 0                           # H6 needs no rates


def test_run_without_the_h7_universe_does_not_fetch_any_rates(tmp_path):
    fcalls = []
    out = research3.run(fetch=synthetic_fetch(), rates_fetch=fake_fred(fcalls), now=NOW_RUN,
                        cache_dir=tmp_path / "c", out_dir=tmp_path / "o",
                        universes={FX_REV: ins.H6_UNIVERSE[:1]})
    assert fcalls == [] and out.results.rate_coverage == [] and out.results.months == []


def test_run_with_a_missing_hypothesis_runs_it_on_nothing(tmp_path):
    out = research3.run(fetch=synthetic_fetch(), rates_fetch=fake_fred(), now=NOW_RUN,
                        cache_dir=tmp_path / "c", out_dir=tmp_path / "o", universes={CARRY_BASKET: ins.H7_UNIVERSE})
    assert out.results.rows == [] and out.decision.rev["OOS"].n == 0 and out.decision.basket["OOS"].n > 0


def test_run_with_write_false_writes_nothing(tmp_path):
    research3.run(fetch=synthetic_fetch(), rates_fetch=fake_fred(), now=NOW_RUN, cache_dir=tmp_path / "c",
                  out_dir=tmp_path / "o", universes={FX_REV: ins.H6_UNIVERSE[:1]}, write=False)
    assert not (tmp_path / "o").exists()


def test_run_cleans_every_pair_at_15_percent(monkeypatch, tmp_path):
    asked = {}
    real = data.load_daily

    def spy_load(symbol, **kw):
        asked[symbol] = kw.get("max_wick")
        return real(symbol, **kw)
    monkeypatch.setattr(data, "load_daily", spy_load)
    research3.run(fetch=synthetic_fetch(), rates_fetch=fake_fred(), now=NOW_RUN, cache_dir=tmp_path / "c",
                  out_dir=tmp_path / "o", universes={FX_REV: ins.H6_UNIVERSE[:2],
                                                     CARRY_BASKET: ins.H7_UNIVERSE[:2]}, write=False)
    assert set(asked.values()) == {0.15}


# ================================================================== the command line
def run_main(argv, tmp_path, *, fetch=None, rates_fetch=None, universes=SMALL):
    return research3.main(argv, fetch=fetch or synthetic_fetch(), rates_fetch=rates_fetch or fake_fred(),
                          now=NOW_RUN, out_dir=tmp_path / "out", cache_dir=tmp_path / "cache",
                          universes=universes)


def test_cli_without_run_does_nothing_and_asks_for_it(capsys, tmp_path):
    calls: list = []
    rc = research3.main([], fetch=synthetic_fetch(calls), rates_fetch=fake_fred(calls),
                        out_dir=tmp_path / "out", cache_dir=tmp_path / "cache")
    assert rc == 2 and calls == []
    assert "--run" in capsys.readouterr().out
    assert not (tmp_path / "out").exists()


def test_cli_run_prints_per_hypothesis_the_gate_and_the_key_numbers_in_and_out_of_sample(capsys, tmp_path):
    rc = run_main(["--run"], tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    h6 = next(ln for ln in out.splitlines() if ln.startswith("H6 FX-REV"))
    assert ("gate PASS" in h6 or "gate FAIL" in h6)
    for piece in ("out-of-sample", "trades", "PF", "mean R", "t ", "in-sample"):
        assert piece in h6
    h7 = next(ln for ln in out.splitlines() if ln.startswith("H7 CARRY-BASKET"))
    assert ("gate PASS" in h7 or "gate FAIL" in h7)
    for piece in ("out-of-sample", "months", "PF", "mean month", "t ", "worst peak-to-trough", "in-sample"):
        assert piece in h7
    failed = [ln for ln in out.splitlines() if ln.startswith("  failed: ")]
    verdicts = [("gate FAIL" in ln) for ln in (h6, h7)]
    assert len(failed) == sum(verdicts) and all("needs" in ln for ln in failed)
    assert "enabled:" in out and "written:" in out
    assert (tmp_path / "out" / "BACKTEST_REPORT_R3.md").exists()
    assert json.loads((tmp_path / "out" / "enabled.json").read_text())["round3"]


def test_cli_warns_when_a_series_or_a_currency_has_no_data(capsys, tmp_path):
    good = synthetic_fetch()

    def flaky(symbol, interval):
        if symbol == "AUDNZD=X":
            raise RuntimeError("rate limited")
        return good(symbol, interval)
    rc = run_main(["--run"], tmp_path, fetch=flaky, rates_fetch=fake_fred(fail={rates.SERIES["EUR"]}))
    out = capsys.readouterr().out
    assert rc == 0
    assert "WARNING: no data for AUDNZD=X (1d)" in out
    assert "WARNING: no rates for EUR" in out
    rc = run_main(["--run"], tmp_path / "again")
    assert "WARNING" not in capsys.readouterr().out


def test_cli_refresh_flag_refetches(tmp_path):
    calls: list = []
    fred_calls: list = []
    common = dict(fetch=synthetic_fetch(calls), rates_fetch=fake_fred(fred_calls), now=NOW_RUN,
                  out_dir=tmp_path / "out", cache_dir=tmp_path / "cache",
                  universes={FX_REV: ins.H6_UNIVERSE[:1], CARRY_BASKET: ins.H7_UNIVERSE[:1]})
    research3.main(["--run"], **common)
    n, m = len(calls), len(fred_calls)
    research3.main(["--run"], **common)
    assert (len(calls), len(fred_calls)) == (n, m)
    research3.main(["--run", "--refresh"], **common)
    assert (len(calls), len(fred_calls)) == (2 * n, 2 * m)


def test_the_module_never_imports_the_network_libraries_at_import_time():
    import subprocess
    import sys
    code = ("import sys; import cfd.research3; "
            "print('requests' in sys.modules, 'yfinance' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=str(research3.OUT_DIR.parent.parent))
    assert out.stdout.split() == ["False", "False"], out.stderr
