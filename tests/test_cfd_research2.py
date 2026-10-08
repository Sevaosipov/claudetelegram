"""cfd/research2.py: round 2 of the CFD research -- H3 CR-BO, H4 IDX-DIP and H5 CARRY-FX exactly as
docs/cfd/PREREGISTRATION_R2.md fixes them, with its amendments of 2026-10-05 (A1: H5 holds on the original's
condition, the band being for entries only; A2: H5 uses ATR(20); A3: crypto bars are cleaned at a 60 % wick
threshold). What is tested: the wiring of each hypothesis (setup, exits, costs, periods) both by spying on the
simulator and by hand-computed results on crafted bars with the real setups, the exit choice, the gate arithmetic of each hypothesis, the round2 block of enabled.json
and its merge, the report, and the whole run behind fake Yahoo and FRED fetches. Nothing here reaches
the network and nothing is written outside tmp_path."""
from __future__ import annotations

import datetime as dt
import json
import math
import random

import pytest

from cfd import data, exits, rates, research, research2, setups
from cfd import indicators as ind
from cfd import instruments as ins
from cfd.data import Bar
from cfd.setups import Signal

UTC = dt.timezone.utc
DAY = dt.timedelta(days=1)
D0 = dt.datetime(2020, 1, 1, tzinfo=UTC)
NOW = dt.datetime(2026, 10, 5, 12, 0, tzinfo=UTC)

CR_BO, IDX_DIP, CARRY_FX = "CR-BO", "IDX-DIP", "CARRY-FX"


def daily(rows, start=D0):
    return [Bar(start + i * DAY, float(o), float(h), float(low), float(c))
            for i, (o, h, low, c) in enumerate(rows)]


def row(r=1.0, *, setup=CR_BO, exit_kind="E1", klass="Crypto", period="OOS", day=1, hold=1.0,
        symbol="SOL-USD"):
    exit_ts = D0 + day * DAY
    return research.Row(setup, exit_kind, symbol, symbol[:6], klass, period, r,
                        exit_ts - hold * DAY, exit_ts, hold)


def make_rows(setup, exit_kind, period, wins, losses, *, klass="Crypto", day0=0, symbol="SOL-USD"):
    """`wins` trades of +1R and `losses` of -1R, interleaved, one exit a day."""
    out, w, lo, day = [], wins, losses, day0
    while w or lo:
        if w:
            out.append(row(1.0, setup=setup, exit_kind=exit_kind, klass=klass, period=period, day=day,
                           symbol=symbol))
            w -= 1
            day += 1
        if lo:
            out.append(row(-1.0, setup=setup, exit_kind=exit_kind, klass=klass, period=period, day=day,
                           symbol=symbol))
            lo -= 1
            day += 1
    return out


def S(n=150, win=0.5, pf=1.15, mean=0.05, t=2.5, total=1.0, dd=1.0, hold=1.0):
    return research.Stats(n, win, pf, mean, t, total, dd, hold)


def section(text, title):
    """The lines under the exact heading `## title`, up to the next `## ` heading."""
    lines = text.splitlines()
    start = lines.index(f"## {title}") + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end])


# ================================================================== the pre-registered numbers
def test_the_hypotheses_and_their_numbers_are_the_preregistered_ones():
    assert research2.HYPOTHESES == (CR_BO, IDX_DIP, CARRY_FX)
    assert research2.LABELS == {CR_BO: "H3", IDX_DIP: "H4", CARRY_FX: "H5"}
    assert research2.MIN_OOS_TRADES == {CR_BO: 100, IDX_DIP: 100, CARRY_FX: 40}
    assert research2.GATE_MIN_PF == 1.10
    assert research2.GATE_MIN_T == 2.33
    assert research2.GATE_MIN_IS_TRADES == 30
    assert (research2.IDX_SMA_LEN, research2.IDX_E0C_TIME_STOP, research2.IDX_EL_TIME_STOP) == (5, 11, 16)
    assert (research2.CARRY_TRAIL_ATR, research2.CARRY_MAX_R_ATR) == (6.0, 8.0)
    assert setups.CARRY_ATR_LEN == 20 and setups.ATR_LEN == 14               # amendment A2
    assert data.MAX_WICK == 0.15 and data.MAX_WICK_CRYPTO == 0.60            # amendment A3
    assert research2.OUT_DIR == research.OUT_DIR and research2.OUT_DIR.parts[-2:] == ("docs", "cfd")
    assert research2.REPORT_NAME == "BACKTEST_REPORT_R2.md"


def test_the_default_universes_are_the_preregistered_ones():
    u = research2.UNIVERSES
    assert u[CR_BO] == ins.ALT_COINS and len(u[CR_BO]) == 11
    assert [i.symbol for i in u[IDX_DIP]] == ["^GSPC", "^NDX", "^DJI", "^GDAXI", "^FTSE", "^N225"]
    assert u[CARRY_FX] == ins.FX_UNIVERSE and len(u[CARRY_FX]) == 11


# ================================================================== wiring: what each hypothesis runs
@pytest.fixture
def spy(monkeypatch):
    seen = []
    real = exits.simulate

    def simulate(bars, sig, kind, **kw):
        seen.append((kind, kw))
        return real(bars, sig, kind, **kw)
    monkeypatch.setattr(exits, "simulate", simulate)
    return seen


def quiet_bars(n=40, start=D0):
    return daily([(100.0, 100.5, 99.5, 100.0)] * n, start)


def kw_of(seen, kind):
    return [kw for k, kw in seen if k == kind]


def test_h3_is_bo_d_under_e0_e1_and_e2_exactly_as_round_one_with_the_alt_costs(monkeypatch, spy):
    monkeypatch.setattr(research2.setups, "bo_d", lambda bars: [Signal(10, "long", 96.0, 2.0)])
    coin = ins.by_symbol("SOL-USD")
    bars = quiet_bars()
    rows, counts = research2.backtest_cr_bo(coin, bars)
    assert {k for k, _ in spy} == {"E0", "E1", "E2"}
    for _, kw in spy:
        assert kw["atr"] == ind.atr(bars, 14) and kw["atr"] != ind.atr(bars, 20)    # H3 keeps ATR(14)
        assert kw["entry"] == "next_open" and kw["costs"] == coin.costs
        assert set(kw) == {"costs", "entry", "session", "atr", "e0_target_r"}      # nothing round 2 added
        assert kw["session"] is None and kw["e0_target_r"] is None
    c = coin.costs
    assert (c.round_trip_pct, c.financing_long_pct, c.financing_short_pct) == (0.40, 0.06, 0.06)
    assert set(counts) == {(CR_BO, "E0"), (CR_BO, "E1"), (CR_BO, "E2")}


def test_h4_runs_e0c_with_the_sma5_condition_and_an_11_bar_time_stop_and_el_with_16(monkeypatch, spy):
    monkeypatch.setattr(research2.setups, "idx_dip", lambda bars: [Signal(10, "long", 96.0, 2.0)])
    idx = ins.by_symbol("^GSPC")
    bars = quiet_bars()
    _, counts = research2.backtest_idx_dip(idx, bars)
    assert {k for k, _ in spy} == {"E0c", "EL"}
    assert all(kw["atr"] == ind.atr(bars, 14) and kw["atr"] != ind.atr(bars, 20) for _, kw in spy)   # H4: ATR(14)
    (e0c,) = kw_of(spy, "E0c")
    (el,) = kw_of(spy, "EL")
    assert e0c["time_stop_bars"] == 11 and callable(e0c["exit_when"])
    assert el["time_stop_bars"] == 16 and "exit_when" not in el
    for kw in (e0c, el):
        assert kw["entry"] == "next_open" and kw["costs"] == idx.costs
        assert "trail_atr" not in kw and "max_r_atr" not in kw          # the round-1 R bound of 6 ATR
    assert set(counts) == {(IDX_DIP, "E0c"), (IDX_DIP, "EL")}


def test_h4_condition_is_a_close_above_the_5_day_average(monkeypatch, spy):
    monkeypatch.setattr(research2.setups, "idx_dip", lambda bars: [Signal(10, "long", 96.0, 2.0)])
    closes = [100.0] * 8 + [100.0, 101.0, 100.2, 99.0, 100.2, 100.0, 100.0, 100.0]
    bars = daily([(c, c + 0.5, c - 0.5, c) for c in closes])
    research2.backtest_idx_dip(ins.by_symbol("^GSPC"), bars)
    (e0c,) = kw_of(spy, "E0c")
    cond = e0c["exit_when"]
    sma5 = ind.sma(closes, 5)
    for j in range(4, len(closes)):
        assert cond(j, "long") == (closes[j] > sma5[j]), j
    assert cond(9, "long") and not cond(8, "long")          # 101.0 against (100*4+101)/5 = 100.2; 100 = 100: not above
    assert not cond(10, "long")                             # 100.2 against (100*3+101+100.2)/5 = 100.24


def test_h5_runs_e0c_with_a_six_atr_20_trail_and_el_both_leaving_when_the_hold_condition_drops(monkeypatch, spy):
    inst = ins.by_symbol("EURUSD=X")
    holds = ["flat", "flat", "long", "long", "short", "flat", "long"] + ["flat"] * 33
    monkeypatch.setattr(research2.setups, "carry_holds", lambda b, base, quote: holds)
    # the entry state must not be what the exits read: here it says "long" on every bar
    monkeypatch.setattr(research2.setups, "carry_states", lambda b, base, quote: ["long"] * len(b))
    monkeypatch.setattr(research2.setups, "carry_fx", lambda b, base, quote: [Signal(2, "long", 96.0, 2.0)])
    bars = quiet_bars()
    _, counts = research2.backtest_carry_fx(inst, bars, rates.Rates())
    assert {k for k, _ in spy} == {"E0c", "EL"}
    (e0c,) = kw_of(spy, "E0c")
    (el,) = kw_of(spy, "EL")
    assert e0c["trail_atr"] == 6.0 and e0c["max_r_atr"] == 8.0
    assert el["max_r_atr"] == 8.0 and "trail_atr" not in el
    for kw in (e0c, el):
        assert kw["atr"] == ind.atr(bars, 20) and kw["atr"] != ind.atr(bars, 14)     # amendment A2: ATR(20)
        assert "time_stop_bars" not in kw                       # no time stop
        assert kw["entry"] == "next_open" and kw["costs"] == ins.carry_fx_costs(inst)
        cond = kw["exit_when"]
        # the hold series, read at the close of bar j: anything but the trade's own side leaves
        assert [cond(j, "long") for j in range(7)] == [True, True, False, False, True, True, False]
        assert [cond(j, "short") for j in range(7)] == [True, True, True, True, False, True, True]
    assert set(counts) == {(CARRY_FX, "E0c"), (CARRY_FX, "EL")}


def test_h5_gives_each_bar_the_rates_in_force_on_its_date_of_the_bases_and_quotes_currency(monkeypatch):
    seen = {}

    def carry_holds(bars, base, quote):
        seen["states"] = (list(base), list(quote))
        return ["flat"] * len(bars)

    def carry_fx(bars, base, quote):
        seen["signals"] = (list(base), list(quote))
        return []
    monkeypatch.setattr(research2.setups, "carry_holds", carry_holds)
    monkeypatch.setattr(research2.setups, "carry_fx", carry_fx)
    usd = rates.RateSeries("USD", "x", ((dt.date(2019, 12, 1), 1.5), (dt.date(2020, 1, 1), 1.8)))
    jpy = rates.RateSeries("JPY", "y", ((dt.date(2019, 1, 1), -0.1),))
    r = rates.Rates({"USD": usd, "JPY": jpy})
    bars = daily([(100.0, 100.5, 99.5, 100.0)] * 50, start=dt.datetime(2020, 1, 1, tzinfo=UTC))
    research2.backtest_carry_fx(ins.by_symbol("USDJPY=X"), bars, r)      # base USD, quote JPY
    base, quote = seen["states"]
    # USD: December's 1.5 is published on 2020-01-15, January's 1.8 on 2020-02-15
    assert base[:14] == [None] * 14 and base[14] == 1.5 and base[44] == 1.5 and base[45] == 1.8
    # JPY: January 2019's value has been usable since 2019-02-15
    assert quote == [-0.1] * 50
    assert seen["signals"] == seen["states"]
    # and a pair whose currencies did not load has no rates at all
    research2.backtest_carry_fx(ins.by_symbol("EURGBP=X"), bars, r)
    assert seen["states"] == ([None] * 50, [None] * 50)


# ================================================================== the period split in the rows
def test_rows_are_split_by_the_signal_date_with_the_crypto_boundary_at_2019_12_31(monkeypatch):
    monkeypatch.setattr(research2.setups, "bo_d", lambda bars: [Signal(10, "long", 96.0, 2.0)])
    coin = ins.by_symbol("SOL-USD")
    for signal_day, period in ((dt.date(2019, 12, 31), "IS"), (dt.date(2020, 1, 1), "OOS")):
        start = dt.datetime(signal_day.year, signal_day.month, signal_day.day, tzinfo=UTC) - 10 * DAY
        bars = daily([(100.0, 100.5, 99.5, 100.0)] * 11 + [(100.0, 101, 94, 95.0)] + [(95.0, 95.5, 94.5, 95.0)] * 3,
                     start=start)
        rows, _ = research2.backtest_cr_bo(coin, bars)
        assert rows and {r.period for r in rows} == {period}, signal_day
        assert all(r.setup == CR_BO and r.klass == "Crypto" and r.symbol == "SOL-USD" for r in rows)


def test_indices_and_fx_use_the_2006_to_2016_and_2017_on_split(monkeypatch):
    monkeypatch.setattr(research2.setups, "idx_dip", lambda bars: [Signal(10, "long", 96.0, 2.0)])
    idx = ins.by_symbol("^GSPC")
    for signal_day, period in ((dt.date(2016, 12, 31), "IS"), (dt.date(2017, 1, 1), "OOS"),
                               (dt.date(2005, 12, 31), None)):
        start = dt.datetime(signal_day.year, signal_day.month, signal_day.day, tzinfo=UTC) - 10 * DAY
        bars = daily([(100.0, 100.5, 99.5, 100.0)] * 11 + [(100.0, 101, 94, 95.0)] + [(95.0, 95.5, 94.5, 95.0)] * 3,
                     start=start)
        rows, counts = research2.backtest_idx_dip(idx, bars)
        if period is None:                       # before the study window: not a signal at all
            assert rows == [] and counts[(IDX_DIP, "EL")].signals == 0
        else:
            assert rows and {r.period for r in rows} == {period}, signal_day


# ================================================================== hand-computed, real setups and exits
# ---- H4: 300 bars climbing 0.5 a bar, two drops of 3 (RSI(2) 5.26 at bar 301: the signal), then
#   bar 302: open 244.5 (the entry), bar 303 closes above its 5-day average, bar 304 opens at 248.1
# ATR(14) at 301 = 527/392, the stop 244 - 2.5 * ATR = 240.6390306, R = 244.5 - stop = 3.8609694 (2.87 ATR)
# E0c: SMA5 at 302 is 247.4 (the close 246.5 is under it); at 303 it is 247.1 and the close 248.0 is over it
#      -> leaves at the open of bar 304 (248.1): +0.9324083R gross, 2 nights, cost (0.030 + 2 * 0.020) %
# EL:  TP1 (+0.5R = 246.4305) on bar 302, TP2 (+1R = 248.3610) on bar 304 (stop to the entry), then nothing
#      until the time stop at the open of bar 318 (248.0): 0.25 * 0.5 + 0.25 * 1 + 0.5 * 3.5 / R gross,
#      16 nights
def climb(n, start=100.0, step=0.5, wick=0.25):
    rows, c = [], start
    for _ in range(n):
        o, c = c, c + step
        rows.append((o, c + wick, o - wick, c))
    return rows


H4_ROWS = (climb(300) + [(250.0, 250.25, 246.75, 247.0), (247.0, 247.25, 243.75, 244.0)]
           + [(244.5, 246.6, 244.2, 246.5), (246.5, 248.2, 246.0, 248.0), (248.1, 248.6, 247.5, 248.3)]
           + [(248.0, 248.9, 247.2, 248.0)] * 26)
H4_ATR = ((1.0 * 13 + 3.5) / 14 * 13 + 3.5) / 14
H4_R = 244.5 - (244.0 - 2.5 * H4_ATR)


def test_h4_on_crafted_bars_with_the_real_setup_and_exits_hand_computed():
    assert H4_R == pytest.approx(3.8609693878)
    rows, counts = research2.backtest_idx_dip(ins.by_symbol("^GSPC"),
                                              daily(H4_ROWS, start=dt.datetime(2023, 1, 1, tzinfo=UTC)))
    by = {r.exit_kind: r for r in rows}
    assert set(by) == {"E0c", "EL"} and len(rows) == 2
    cost = lambda nights: (0.030 + nights * 0.020) / 100 * 244.5 / H4_R          # noqa: E731  (long: 0.020)
    e0c = (248.1 - 244.5) / H4_R - cost(2)
    el = 0.25 * 0.5 + 0.25 * 1.0 + 0.5 * (248.0 - 244.5) / H4_R - cost(16)
    assert by["E0c"].result_r == pytest.approx(e0c) and by["E0c"].result_r == pytest.approx(0.8880800793)
    assert by["EL"].result_r == pytest.approx(el) and by["EL"].result_r == pytest.approx(0.6066128180)
    assert (by["E0c"].hold_days, by["EL"].hold_days) == (2.0, 16.0)
    for r in rows:
        assert (r.setup, r.symbol, r.name, r.klass, r.period) == (IDX_DIP, "^GSPC", "US500", "Indices", "OOS")
        assert r.signal_ts == dt.datetime(2023, 1, 1, tzinfo=UTC) + 301 * DAY
    assert by["E0c"].exit_ts == dt.datetime(2023, 1, 1, tzinfo=UTC) + 304 * DAY
    assert by["EL"].exit_ts == dt.datetime(2023, 1, 1, tzinfo=UTC) + 318 * DAY
    for e in ("E0c", "EL"):
        c = counts[(IDX_DIP, e)]
        assert (c.signals, c.closed, c.skipped, c.open, c.blocked) == (1, 1, 0, 0, 0)


def test_h4_is_in_sample_when_the_signal_falls_between_2006_and_2016():
    rows, _ = research2.backtest_idx_dip(ins.by_symbol("^GSPC"),
                                         daily(H4_ROWS, start=dt.datetime(2010, 1, 1, tzinfo=UTC)))
    assert {r.period for r in rows} == {"IS"}


def test_h4_pays_the_long_financing_rate_it_is_long_only():
    rows, _ = research2.backtest_idx_dip(ins.by_symbol("^GSPC"),
                                         daily(H4_ROWS, start=dt.datetime(2023, 1, 1, tzinfo=UTC)))
    # 0.020 a night (the long rate), not the 0.005 of a short: 16 nights of EL at 0.005 would cost less
    el = next(r for r in rows if r.exit_kind == "EL")
    gross = 0.25 * 0.5 + 0.25 * 1.0 + 0.5 * (248.0 - 244.5) / H4_R
    short_rate_result = gross - (0.030 + 16 * 0.005) / 100 * 244.5 / H4_R
    assert el.result_r != pytest.approx(short_rate_result)


# ---- H3: 320 flat bars, a breakout bar (close 103, ATR(14) = 167/140), then
#   bar 321 opens at 103.5 (the entry): stop 103 - 2.5 ATR = 100.0178571, R = 3.4821429
#   bar 321 reaches +1R (106.9821) and closes at 107, a second breakout the open trade blocks; bar 322 trades
#   back to 103.2, through the entry the stop moved to
#   E1 / E2: a quarter at +1R, the rest stopped at the entry: +0.25R gross, 1 night (bar 321 to 322), cost
#   (0.40 + 0.06) % of the entry; E0 leaves on bar 323 (2 nights)
H3_ROWS = ([(100.0, 100.5, 99.5, 100.0)] * 320 + [(100.0, 103.5, 99.8, 103.0)]
           + [(103.5, 107.2, 103.6, 107.0), (107.0, 107.4, 103.2, 103.4), (103.4, 103.5, 99.0, 99.5)])
H3_ATR = (1.0 * 13 + 3.7) / 14
H3_R = 103.5 - (103.0 - 2.5 * H3_ATR)


def test_h3_on_crafted_bars_with_the_real_setup_and_exits_hand_computed():
    assert H3_R == pytest.approx(3.4821428571)
    coin = ins.by_symbol("SOL-USD")
    start = dt.datetime(2021, 1, 1, tzinfo=UTC)
    rows, counts = research2.backtest_cr_bo(coin, daily(H3_ROWS, start=start))
    by = {r.exit_kind: r for r in rows}
    assert set(by) == {"E0", "E1", "E2"}
    cost = lambda nights: (0.40 + nights * 0.06) / 100 * 103.5 / H3_R             # noqa: E731  (alt round trip)
    assert cost(1) == pytest.approx(0.1367261538)
    assert by["E1"].result_r == pytest.approx(0.25 - cost(1)) and by["E1"].result_r == pytest.approx(0.1132738462)
    assert by["E2"].result_r == pytest.approx(0.25 - cost(1))          # the first three quarters are E1's
    # E0: the high of 107.2 trails the stop to 107.2 - 3 ATR(321); bar 323 trades down through it
    atr321 = (H3_ATR * 13 + 4.2) / 14
    trail = 107.2 - 3 * atr321
    assert by["E0"].result_r == pytest.approx((trail - 103.5) / H3_R - cost(2))
    assert by["E0"].exit_ts == start + 323 * DAY and by["E1"].exit_ts == start + 322 * DAY
    for r in rows:
        assert (r.setup, r.symbol, r.name, r.klass, r.period) == (CR_BO, "SOL-USD", "SOLUSD", "Crypto", "OOS")
        assert r.signal_ts == start + 320 * DAY
    for e in ("E0", "E1", "E2"):
        c = counts[(CR_BO, e)]
        assert (c.signals, c.closed, c.blocked) == (2, 1, 1), e        # bar 321's own breakout is blocked


def test_h3_the_same_trade_in_sample_when_the_signal_is_in_2019():
    start = dt.datetime(2019, 12, 31, tzinfo=UTC) - 320 * DAY
    rows, _ = research2.backtest_cr_bo(ins.by_symbol("SOL-USD"), daily(H3_ROWS, start=start))
    assert {r.period for r in rows} == {"IS"}


def test_h3_costs_are_the_alts_not_bitcoins():
    # the same bars on BTC pay 0.25 % a round trip: a different result in R
    rows_btc, _ = research2.backtest_cr_bo(ins.by_symbol("BTC-USD"),
                                           daily(H3_ROWS, start=dt.datetime(2021, 1, 1, tzinfo=UTC)))
    rows_sol, _ = research2.backtest_cr_bo(ins.by_symbol("SOL-USD"),
                                           daily(H3_ROWS, start=dt.datetime(2021, 1, 1, tzinfo=UTC)))
    e1 = lambda rows: next(r.result_r for r in rows if r.exit_kind == "E1")          # noqa: E731
    assert e1(rows_btc) == pytest.approx(0.25 - (0.25 + 1 * 0.06) / 100 * 103.5 / H3_R)
    assert e1(rows_sol) < e1(rows_btc)


# ---- H5 (amendments A1 and A2): 320 flat bars, a jump bar (close 103, ATR(20) = 227/200 = 1.135), EUR 3 % over USD 2 %
#   the entry state turns long at bar 320 (SMA200 100.015, upper band 102.5154); signal stop 103 - 6 ATR(20) = 96.19
#   bar 321 opens at 103.2 (the entry): R = 7.01 = 6.18 ATR(20) -- over the round-1 bound of 6 ATR, inside H5's 8
#   bar 322 closes at 101.0: back INSIDE the 2.5 % band (the entry state is flat) but above SMA200 (100.0355), so the
#   original's hold condition still holds and the trade is kept -- a band exit would have left at bar 323's open
#   bar 323 closes at 99.9, under SMA200 (100.035): the hold drops, the trade leaves at the open of bar 324 (99.7)
#   E0c (its 6 ATR(20) trail stays at 96.89, far under every low) and EL alike: (99.7 - 103.2) / R gross, 3 nights,
#   cost (0.015 + 3 * 0.004) % of the entry
H5_ROWS = ([(100.0, 100.5, 99.5, 100.0)] * 320 + [(100.0, 103.5, 99.8, 103.0)]
           + [(103.2, 103.6, 102.8, 103.1), (103.1, 103.3, 100.8, 101.0), (101.0, 101.2, 99.6, 99.9),
              (99.7, 100.1, 99.4, 99.8)])
H5_ATR = (1.0 * 19 + 3.7) / 20
H5_R = 103.2 - (103.0 - 6 * H5_ATR)


def eur_over_usd():
    far_back = dt.date(2000, 1, 1)
    return rates.Rates({"EUR": rates.RateSeries("EUR", "e", ((far_back, 3.0),)),
                        "USD": rates.RateSeries("USD", "u", ((far_back, 2.0),))})


def test_h5_on_crafted_bars_with_the_real_setup_and_exits_hand_computed():
    assert H5_ATR == pytest.approx(1.135) and H5_R == pytest.approx(7.01) and H5_R / H5_ATR > 6.0
    inst = ins.by_symbol("EURUSD=X")
    start = dt.datetime(2023, 1, 1, tzinfo=UTC)
    rows, counts = research2.backtest_carry_fx(inst, daily(H5_ROWS, start=start), eur_over_usd())
    by = {r.exit_kind: r for r in rows}
    assert set(by) == {"E0c", "EL"} and len(rows) == 2
    cost = (0.015 + 3 * 0.004) / 100 * 103.2 / H5_R                    # the major's round trip, 0.004 a night
    want = (99.7 - 103.2) / H5_R - cost
    assert cost == pytest.approx(0.0039748930)
    assert want == pytest.approx(-0.5032616262)
    for e in ("E0c", "EL"):
        assert by[e].result_r == pytest.approx(want), e
        # held through bar 322 (back inside the band, above SMA200); gone at bar 324's open, not bar 323's
        assert by[e].exit_ts == start + 324 * DAY and by[e].hold_days == 3.0
        assert (by[e].setup, by[e].symbol, by[e].klass, by[e].period) == (CARRY_FX, "EURUSD=X", "FX", "OOS")
        assert by[e].signal_ts == start + 320 * DAY
        c = counts[(CARRY_FX, e)]
        assert (c.signals, c.closed, c.skipped) == (1, 1, 0)


def test_h5_uses_atr_20_for_the_stop_not_atr_14():
    # with ATR(14) (1.1929) the stop would be 95.8429 and R 7.3571: a different result in R
    rows, _ = research2.backtest_carry_fx(ins.by_symbol("EURUSD=X"), daily(H5_ROWS), eur_over_usd())
    el = next(r for r in rows if r.exit_kind == "EL")
    atr14 = (1.0 * 13 + 3.7) / 14
    r14 = 103.2 - (103.0 - 6 * atr14)
    result14 = (99.7 - 103.2) / r14 - (0.015 + 3 * 0.004) / 100 * 103.2 / r14
    assert el.result_r == pytest.approx(-0.5032616262) and el.result_r != pytest.approx(result14)


def test_h5_an_entry_that_gapped_further_than_eight_atr_from_the_stop_is_skipped():
    inst = ins.by_symbol("EURUSD=X")
    stop = 103.0 - 6 * H5_ATR

    def counts_for(entry_open):
        rows = list(H5_ROWS)
        rows[321] = (entry_open, entry_open + 0.4, entry_open - 0.4, entry_open)
        _, counts = research2.backtest_carry_fx(inst, daily(rows), eur_over_usd())
        return counts[(CARRY_FX, "EL")]
    assert (104.9 - stop) / H5_ATR == pytest.approx(7.6740, abs=1e-3)
    assert counts_for(104.9).skipped == 0                      # 7.67 ATR(20): taken
    assert (105.5 - stop) / H5_ATR == pytest.approx(8.2026, abs=1e-3)
    assert counts_for(105.5).skipped == 1                      # 8.20 ATR(20): skipped


# the same climb with every close well above SMA200 (about 100.04): the price never drops the hold
H5_STEADY_ROWS = ([(100.0, 100.5, 99.5, 100.0)] * 320 + [(100.0, 103.5, 99.8, 103.0)]
                  + [(103.2, 103.6, 102.8, 103.1), (103.1, 103.5, 102.9, 103.0), (103.0, 103.2, 102.6, 102.9),
                     (102.9, 103.1, 102.7, 103.0)])


def test_h5_a_trade_is_held_until_the_hold_drops_and_the_rates_can_drop_it_with_the_price_above_the_average():
    inst = ins.by_symbol("EURUSD=X")
    # with the rates unchanged and every close above SMA200 the trade is still open when the data ends
    rows, counts = research2.backtest_carry_fx(inst, daily(H5_STEADY_ROWS), eur_over_usd())
    assert rows == [] and counts[(CARRY_FX, "EL")].open == 1 and counts[(CARRY_FX, "E0c")].open == 1
    # EUR's October value (1.0, under USD's 2.0) is published on 2023-11-15, which is bar 322: the rates stop
    # agreeing with the long at that close, so it leaves at the open of bar 323 although the price is above SMA200
    far_back = dt.date(2000, 1, 1)
    flipping = rates.Rates({"EUR": rates.RateSeries("EUR", "e", ((far_back, 3.0), (dt.date(2023, 10, 1), 1.0))),
                            "USD": rates.RateSeries("USD", "u", ((far_back, 2.0),))})
    start = dt.datetime(2023, 11, 15, tzinfo=UTC) - 322 * DAY
    rows, _ = research2.backtest_carry_fx(inst, daily(H5_STEADY_ROWS, start=start), flipping)
    assert {r.exit_kind for r in rows} == {"E0c", "EL"}
    for r in rows:
        assert r.exit_ts == start + 323 * DAY and r.hold_days == 2.0
        cost = (0.015 + 2 * 0.004) / 100 * 103.2 / H5_R
        assert r.result_r == pytest.approx((103.0 - 103.2) / H5_R - cost)


def test_h5_the_opposite_rates_give_no_long_and_a_short_is_held_while_the_close_stays_under_the_average():
    inst = ins.by_symbol("EURUSD=X")
    far_back = dt.date(2000, 1, 1)
    usd_over_eur = rates.Rates({"EUR": rates.RateSeries("EUR", "e", ((far_back, 2.0),)),
                                "USD": rates.RateSeries("USD", "u", ((far_back, 3.0),))})
    rows, counts = research2.backtest_carry_fx(inst, daily(H5_ROWS), usd_over_eur)
    assert rows == [] and counts[(CARRY_FX, "EL")].signals == 0       # a rise with the carry against it
    mirrored = [(200.0 - o, 200.0 - low, 200.0 - h, 200.0 - c) for (o, h, low, c) in H5_ROWS]
    rows, _ = research2.backtest_carry_fx(inst, daily(mirrored), usd_over_eur)
    assert {r.exit_kind for r in rows} == {"E0c", "EL"}              # a fall with the carry on the short side
    # the mirror image of the long in price terms: entry 200 - 103.2 = 96.8, exit 200 - 99.7 = 100.3, the same R.
    # Costs are a percentage of the entry price, so the short pays them on 96.8 where the long paid on 103.2.
    want = (96.8 - 100.3) / H5_R - (0.015 + 3 * 0.004) / 100 * 96.8 / H5_R
    assert want == pytest.approx(-0.5030151213)
    for r in rows:                                                    # held through bar 322, gone at bar 324's open
        assert r.exit_ts == D0 + 324 * DAY and r.hold_days == 3.0
        assert r.result_r == pytest.approx(want)


def test_h5_without_any_rates_has_no_signals():
    rows, counts = research2.backtest_carry_fx(ins.by_symbol("EURUSD=X"), daily(H5_ROWS), rates.Rates())
    assert rows == [] and counts[(CARRY_FX, "E0c")].signals == 0


def test_h5_pays_the_pairs_own_round_trip_on_a_cross():
    # the same trade on EURGBP (a cross: 0.030 % round trip) costs more than on EURUSD (0.015 %)
    far_back = dt.date(2000, 1, 1)
    r = rates.Rates({c: rates.RateSeries(c, c, ((far_back, v),)) for c, v in
                     (("EUR", 3.0), ("USD", 2.0), ("GBP", 2.0))})
    major, _ = research2.backtest_carry_fx(ins.by_symbol("EURUSD=X"), daily(H5_ROWS), r)
    cross, _ = research2.backtest_carry_fx(ins.by_symbol("EURGBP=X"), daily(H5_ROWS), r)
    el = lambda rows: next(x.result_r for x in rows if x.exit_kind == "EL")           # noqa: E731
    assert el(major) - el(cross) == pytest.approx(0.015 / 100 * 103.2 / H5_R)


# ================================================================== which exit is judged
def test_h3_is_judged_on_e1_or_e2_whichever_has_the_higher_out_of_sample_mean_r():
    rows = (make_rows(CR_BO, "E1", "OOS", 55, 45) + make_rows(CR_BO, "E2", "OOS", 60, 40)
            + make_rows(CR_BO, "E0", "OOS", 90, 10))               # E0 is never a candidate
    assert research2.judged_exit(rows, CR_BO) == "E2"
    rows = make_rows(CR_BO, "E1", "OOS", 60, 40) + make_rows(CR_BO, "E2", "OOS", 55, 45)
    assert research2.judged_exit(rows, CR_BO) == "E1"


def test_h3_a_tie_or_no_trades_is_judged_on_e1_and_in_sample_trades_do_not_choose():
    tie = make_rows(CR_BO, "E1", "OOS", 55, 45) + make_rows(CR_BO, "E2", "OOS", 55, 45)
    assert research2.judged_exit(tie, CR_BO) == "E1" and research2.judged_exit([], CR_BO) == "E1"
    rows = (make_rows(CR_BO, "E1", "OOS", 60, 40) + make_rows(CR_BO, "E2", "OOS", 55, 45)
            + make_rows(CR_BO, "E2", "IS", 90, 10))
    assert research2.judged_exit(rows, CR_BO) == "E1"


def test_h4_and_h5_are_judged_on_el_whatever_e0c_does():
    for name, klass in ((IDX_DIP, "Indices"), (CARRY_FX, "FX")):
        rows = (make_rows(name, "E0c", "OOS", 90, 10, klass=klass) + make_rows(name, "EL", "OOS", 40, 60, klass=klass))
        assert research2.judged_exit(rows, name) == "EL"
        assert research2.judged_exit([], name) == "EL"


# ================================================================== the gate, line by line
def gate(name=CR_BO, is_stats=None, oos=None, exit_kind="E1"):
    return research2.gate_hypothesis(name, exit_kind, is_stats or S(n=50, mean=0.01), oos or S())


def test_the_gate_lines_and_thresholds_of_h3_and_h4():
    for name in (CR_BO, IDX_DIP):
        g = gate(name)
        assert [c.rule for c in g.checks] == [
            "out-of-sample trades", "out-of-sample profit factor", "out-of-sample mean R",
            "out-of-sample t-statistic", "in-sample mean R"]
        assert [c.threshold for c in g.checks][:4] == [">= 100", ">= 1.10", "> 0", ">= 2.33"]
        assert all(c.passed for c in g.checks) and g.passed and g.verdict == "PASS"
        assert g.summary() == "PASS" and g.failures() == []


def test_the_gate_of_h5_needs_only_forty_out_of_sample_trades():
    g = gate(CARRY_FX, oos=S(n=40))
    assert g.passed and g.checks[0].threshold == ">= 40"
    assert not gate(CARRY_FX, oos=S(n=39)).passed
    # 100 is the bar of the other two
    assert gate(CARRY_FX, oos=S(n=100)).passed and not gate(CR_BO, oos=S(n=99)).passed
    assert gate(IDX_DIP, oos=S(n=100)).passed and not gate(IDX_DIP, oos=S(n=99)).passed


@pytest.mark.parametrize("name, oos, failing", [
    (CR_BO, S(n=99), "out-of-sample trades"),
    (IDX_DIP, S(n=99), "out-of-sample trades"),
    (CARRY_FX, S(n=39), "out-of-sample trades"),
    (CR_BO, S(pf=1.0999), "out-of-sample profit factor"),
    (IDX_DIP, S(pf=1.0999), "out-of-sample profit factor"),
    (CARRY_FX, S(n=40, pf=1.0999), "out-of-sample profit factor"),
    (CR_BO, S(mean=0.0), "out-of-sample mean R"),
    (CR_BO, S(mean=-0.01), "out-of-sample mean R"),
    (CR_BO, S(t=2.3299), "out-of-sample t-statistic"),
    (IDX_DIP, S(t=2.3299), "out-of-sample t-statistic"),
    (CARRY_FX, S(n=40, t=2.3299), "out-of-sample t-statistic"),
])
def test_the_gate_fails_one_line_at_a_time(name, oos, failing):
    g = gate(name, oos=oos)
    assert not g.passed and g.verdict == "FAIL"
    assert [c.rule for c in g.checks if not c.passed] == [failing]
    assert "FAIL (" in g.summary() and failing in g.summary()


def test_the_gate_passes_exactly_on_its_thresholds():
    on_the_line = S(n=100, pf=1.10, mean=1e-9, t=2.33)
    assert gate(CR_BO, oos=on_the_line).passed and gate(IDX_DIP, oos=on_the_line).passed
    assert gate(CARRY_FX, oos=S(n=40, pf=1.10, mean=1e-9, t=2.33)).passed


def test_an_infinite_profit_factor_passes_its_line():
    g = gate(oos=S(pf=math.inf))
    assert g.passed and next(c for c in g.checks if "profit factor" in c.rule).value == "inf"


def test_in_sample_mean_r_must_be_positive_when_there_are_30_or_more_in_sample_trades():
    for name in research2.HYPOTHESES:
        oos = S(n=150)
        assert gate(name, is_stats=S(n=30, mean=0.0001), oos=oos).passed
        g = gate(name, is_stats=S(n=30, mean=0.0), oos=oos)                 # strictly greater than zero
        assert not g.passed and [c.rule for c in g.checks if not c.passed] == ["in-sample mean R"]
        assert not gate(name, is_stats=S(n=30, mean=-0.01), oos=oos).passed
        assert not gate(name, is_stats=S(n=500, mean=-0.5), oos=oos).passed


def test_with_fewer_than_30_in_sample_trades_the_in_sample_line_is_reported_not_required():
    for n in (0, 1, 29):
        g = gate(is_stats=S(n=n, mean=-0.7), oos=S(n=150))
        assert g.passed, n
        line = g.checks[-1]
        assert line.rule == "in-sample mean R" and line.passed
        assert f"({n} trades" in line.value and "not required" in line.value
    # at 30 it is required and says nothing of the kind
    line = gate(is_stats=S(n=30, mean=0.2)).checks[-1]
    assert "not required" not in line.value and line.value.startswith("+0.2000")


def test_the_gate_has_no_class_level_there_is_no_per_instrument_picking():
    g = gate()
    assert not hasattr(g, "class_checks") and not hasattr(g, "enabled_classes")


# ================================================================== decide()
def passing_rows():
    rows = []
    # H3: E1 flat, E2 pays; out of sample 150 trades (88 wins), in sample 40 (a loss), E0 beside it
    rows += make_rows(CR_BO, "E1", "OOS", 75, 75) + make_rows(CR_BO, "E2", "OOS", 90, 60)
    rows += make_rows(CR_BO, "E2", "IS", 24, 16) + make_rows(CR_BO, "E0", "OOS", 100, 50)
    # H4: EL passes (65 wins of 100, 100 trades, in sample 50), E0c loses
    rows += make_rows(IDX_DIP, "EL", "OOS", 65, 35, klass="Indices", symbol="^GSPC")
    rows += make_rows(IDX_DIP, "EL", "IS", 30, 20, klass="Indices", symbol="^GSPC")
    rows += make_rows(IDX_DIP, "E0c", "OOS", 30, 70, klass="Indices", symbol="^GSPC")
    # H5: 39 out-of-sample trades, so one short of its bar
    rows += make_rows(CARRY_FX, "EL", "OOS", 30, 9, klass="FX", symbol="EURUSD=X")
    rows += make_rows(CARRY_FX, "E0c", "OOS", 30, 9, klass="FX", symbol="EURUSD=X")
    return rows


def test_decide_judges_each_hypothesis_on_its_own_exit_and_enables_those_that_pass():
    d = research2.decide(passing_rows())
    assert d.judged == {CR_BO: "E2", IDX_DIP: "EL", CARRY_FX: "EL"}
    assert d.gates[CR_BO].passed and d.gates[IDX_DIP].passed and not d.gates[CARRY_FX].passed
    assert d.enabled == [CR_BO, IDX_DIP]
    assert [c.rule for c in d.gates[CARRY_FX].checks if not c.passed] == ["out-of-sample trades"]
    assert d.stats[CR_BO]["OOS"].n == 150 and d.stats[CR_BO]["OOS"].mean_r == pytest.approx(0.2)
    assert d.stats[CR_BO]["IS"].n == 40
    assert d.stats[IDX_DIP]["OOS"].n == 100 and d.stats[CARRY_FX]["OOS"].n == 39


def test_decide_does_not_let_a_better_e0c_or_e0_stand_in_for_the_judged_exit():
    rows = (make_rows(IDX_DIP, "E0c", "OOS", 90, 10, klass="Indices")           # a glowing E0c
            + make_rows(IDX_DIP, "EL", "OOS", 45, 55, klass="Indices"))         # a losing EL
    d = research2.decide(rows)
    assert not d.gates[IDX_DIP].passed and d.enabled == []
    assert d.stats[IDX_DIP]["OOS"].n == 100 and d.stats[IDX_DIP]["OOS"].mean_r < 0


def test_decide_uses_out_of_sample_trades_for_the_gate_and_in_sample_only_for_its_last_line():
    rows = make_rows(CR_BO, "E1", "IS", 100, 0) + make_rows(CR_BO, "E1", "OOS", 40, 60)
    d = research2.decide(rows)
    assert not d.gates[CR_BO].passed
    assert [c.passed for c in d.gates[CR_BO].checks] == [True, False, False, False, True]


def test_decide_with_in_sample_loss_blocks_a_hypothesis_with_enough_trades():
    rows = (make_rows(CR_BO, "E1", "OOS", 90, 60) + make_rows(CR_BO, "E1", "IS", 10, 25))
    d = research2.decide(rows)
    assert not d.gates[CR_BO].passed
    assert [c.rule for c in d.gates[CR_BO].checks if not c.passed] == ["in-sample mean R"]


def test_decide_on_no_trades_at_all():
    d = research2.decide([])
    assert d.enabled == [] and set(d.judged) == set(research2.HYPOTHESES)
    assert d.judged == {CR_BO: "E1", IDX_DIP: "EL", CARRY_FX: "EL"}
    assert not any(g.passed for g in d.gates.values())


def test_there_is_no_class_level_so_a_passing_hypothesis_is_enabled_for_its_whole_universe():
    # trades of only one coin carry the whole gate: the verdict is on the pooled sample
    rows = make_rows(CR_BO, "E1", "OOS", 90, 60, symbol="SOL-USD")
    d = research2.decide(rows)
    assert d.gates[CR_BO].passed and d.enabled == [CR_BO]


# ================================================================== the round2 block and enabled.json
def test_the_round2_payload_is_the_machine_readable_outcome():
    d = research2.decide(passing_rows())
    payload = research2.round2_payload(d, NOW)
    assert list(payload) == ["enabled", "exit", "run_at"]
    assert payload["enabled"] == [CR_BO, IDX_DIP]
    assert payload["exit"] == {CR_BO: "E2", IDX_DIP: "EL", CARRY_FX: "EL"}     # every hypothesis, passed or not
    assert payload["run_at"] == "2026-10-05T12:00:00+00:00"
    assert json.loads(json.dumps(payload)) == payload


ROUND1_JSON = {"exit": {"PB-D": "E1", "BO-D": "E1", "PB-H1-GOLD": "E1"}, "enabled": [],
               "run_at": "2026-10-04T22:42:04+00:00"}


def write_round1(path):
    path.write_text(json.dumps(ROUND1_JSON, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def test_merging_adds_the_round2_block_and_leaves_the_round_one_keys_byte_for_byte(tmp_path):
    path = tmp_path / "enabled.json"
    write_round1(path)
    before = path.read_text()
    round2 = research2.round2_payload(research2.decide(passing_rows()), NOW)
    merged = research2.merge_enabled(path, round2)
    after = path.read_text()
    loaded = json.loads(after)
    assert list(loaded) == ["exit", "enabled", "run_at", "round2"]
    assert {k: loaded[k] for k in ROUND1_JSON} == ROUND1_JSON and loaded["round2"] == round2
    assert merged == loaded
    # the text of the round-1 keys is untouched: everything before the closing brace, up to the comma
    head = before.rstrip().removesuffix("}").rstrip()
    assert after.startswith(head + ",\n  \"round2\": ")
    assert after.endswith("\n") and not after.endswith("\n\n")


def test_merging_again_replaces_the_round2_block_in_place(tmp_path):
    path = tmp_path / "enabled.json"
    write_round1(path)
    first = research2.round2_payload(research2.decide(passing_rows()), NOW)
    research2.merge_enabled(path, first)
    second = research2.round2_payload(research2.decide([]), NOW + dt.timedelta(days=1))
    research2.merge_enabled(path, second)
    loaded = json.loads(path.read_text())
    assert list(loaded) == ["exit", "enabled", "run_at", "round2"] and loaded["round2"] == second
    assert path.read_text().count('"round2"') == 1
    assert {k: loaded[k] for k in ROUND1_JSON} == ROUND1_JSON


def test_merging_into_a_missing_file_writes_just_the_round2_block(tmp_path):
    path = tmp_path / "docs" / "cfd" / "enabled.json"
    round2 = research2.round2_payload(research2.decide([]), NOW)
    research2.merge_enabled(path, round2)
    assert json.loads(path.read_text()) == {"round2": round2}


@pytest.mark.parametrize("junk", ["{not json", "[]", '"text"', "null"])
def test_merging_refuses_a_file_it_cannot_merge_into_and_leaves_it_alone(tmp_path, junk):
    path = tmp_path / "enabled.json"
    path.write_text(junk, encoding="utf-8")
    with pytest.raises(ValueError):
        research2.merge_enabled(path, {"enabled": [], "exit": {}, "run_at": "x"})
    assert path.read_text() == junk and not list(tmp_path.glob("*.tmp"))


def test_write_outputs_writes_the_report_and_merges_enabled_json(tmp_path):
    out = tmp_path / "docs" / "cfd"
    out.mkdir(parents=True)
    write_round1(out / "enabled.json")
    (out / "BACKTEST_REPORT.md").write_text("round one report\n")
    round2 = research2.round2_payload(research2.decide(passing_rows()), NOW)
    paths = research2.write_outputs("# report\n", round2, out)
    assert (out / "BACKTEST_REPORT_R2.md").read_text() == "# report\n"
    assert (out / "BACKTEST_REPORT.md").read_text() == "round one report\n"          # round 1 untouched
    assert json.loads((out / "enabled.json").read_text())["round2"] == round2
    assert {p.name for p in paths} == {"BACKTEST_REPORT_R2.md", "enabled.json"}


def test_write_outputs_does_not_write_the_report_when_the_merge_would_fail(tmp_path):
    (tmp_path / "enabled.json").write_text("{broken")
    with pytest.raises(ValueError):
        research2.write_outputs("# report\n", {"enabled": [], "exit": {}, "run_at": "x"}, tmp_path)
    assert not (tmp_path / "BACKTEST_REPORT_R2.md").exists()


# ================================================================== the report
def fake_results(rows):
    res = research2.Results2()
    res.rows = list(rows)
    res.counts[(CR_BO, "E1")] = research.Counts(signals=10, closed=8, skipped=1, open=1, blocked=0)
    res.coverage.append(research.Coverage("SOL-USD", "SOLUSD", "1d", 2000, dt.date(2020, 4, 11),
                                          dt.date(2026, 10, 3), data.Dropped(missing=2, wick=1)))
    res.rate_coverage.append(research2.RateCoverage("EUR", "IR3TIB01EZM156N", 400, dt.date(1994, 1, 1),
                                                    dt.date(2026, 8, 1), "2026-10-05"))
    res.rate_coverage.append(research2.RateCoverage("JPY", "IR3TIB01JPM156N", 0, None, None, None,
                                                    "ConnectionError: down"))
    return res


def test_the_report_names_the_outcome_the_gate_and_every_hypothesis_exit_and_period():
    res = fake_results(passing_rows())
    d = research2.decide(res.rows)
    text = research2.render_report(res, d, NOW)
    assert text.startswith("# CFD backtest report, round 2")
    assert "2026-10-05" in text and "docs/cfd/PREREGISTRATION_R2.md" in text
    assert "Enabled for live signals: CR-BO, IDX-DIP." in text
    assert "nothing from round 2 goes live" not in text
    for name in research2.HYPOTHESES:
        assert f"## {name}" in text.splitlines()
        assert f"### {name} (judged exit {d.judged[name]})" in text.splitlines()
    assert "PASS" in text and "FAIL" in text
    assert "out-of-sample t-statistic" in text and ">= 2.33" in text and ">= 1.10" in text
    assert ">= 100" in text and ">= 40" in text and "in-sample mean R" in text


def test_the_report_says_so_when_nothing_passed():
    rows = make_rows(CR_BO, "E1", "OOS", 40, 60)
    text = research2.render_report(fake_results(rows), research2.decide(rows), NOW)
    assert "**No hypothesis passed the gate: nothing from round 2 goes live.**" in text
    assert "Enabled for live signals" not in text


def test_the_report_has_the_pooled_table_of_every_exit_and_period_with_the_judged_exit_marked():
    res = fake_results(passing_rows())
    d = research2.decide(res.rows)
    text = research2.render_report(res, d, NOW)
    h3, h4, h5 = section(text, CR_BO), section(text, IDX_DIP), section(text, CARRY_FX)
    for body, exits_ in ((h3, ("E0", "E1", "E2 *")), (h4, ("E0c", "EL *")), (h5, ("E0c", "EL *"))):
        for e in exits_:
            for period in ("IS", "OOS"):
                assert f"| {e} | {period} |" in body, (e, period)
    assert "E1 *" not in h3 and "E0 *" not in h3                       # only the judged one is starred
    # the pooled row of H4's EL out of sample: 100 trades, 65 wins (PF 65/35 = 1.86, mean +0.30)
    line = next(ln for ln in h4.splitlines() if ln.startswith("| EL * | OOS |"))
    assert [c.strip() for c in line.split("|")[3:6]] == ["100", "65.0%", "1.86"]


def test_the_report_has_a_per_instrument_table_for_information():
    res = fake_results(passing_rows())
    text = research2.render_report(res, research2.decide(res.rows), NOW)
    body = section(text, IDX_DIP)
    assert "for information" in body
    assert "| US500 | Indices | OOS |" in body
    # a coin that has no trades is not listed
    assert "| SOLUSD | Crypto |" in section(text, CR_BO)


def test_the_report_has_the_trade_accounting_the_data_and_the_rates():
    res = fake_results(passing_rows())
    text = research2.render_report(res, research2.decide(res.rows), NOW)
    assert "| CR-BO | E1 | 10 | 8 | 1 | 1 | 0 |" in text                  # signals, closed, skipped, open, blocked
    assert "SOLUSD" in text and "2000" in text and "2020-04-11" in text and "2026-10-03" in text
    assert "3 (missing 2, inverted 0, wick 1, duplicate 0, unparsable 0, forming 0)" in text
    rates_body = section(text, "Rates")
    assert "IR3TIB01EZM156N" in rates_body and "400" in rates_body
    assert "1994-01" in rates_body and "2026-08" in rates_body
    assert "no rates" in rates_body and "ConnectionError: down" in rates_body


def test_the_report_notes_state_how_the_pre_registration_was_read():
    text = research2.render_report(fake_results([]), research2.decide([]), NOW)
    notes = section(text, "Notes")
    for needle in ("15th", "0.40", "0.004", "ATR(14)", "ATR(20)", "hold condition", "entries only", "time stop",
                   "8 ATR", "60 %", "amendment"):
        assert needle in notes, needle


def test_the_report_states_h5s_amended_rules_in_its_own_section():
    h5 = section(research2.render_report(fake_results([]), research2.decide([]), NOW), CARRY_FX)
    assert "ATR(20)" in h5 and "SMA200" in h5 and "hold" in h5
    assert "held while" in h5 and "entries only" in h5
    assert "falls back inside the band" not in h5


def test_the_report_is_deterministic():
    res = fake_results(passing_rows())
    d = research2.decide(res.rows)
    assert research2.render_report(res, d, NOW) == research2.render_report(res, d, NOW)


# ================================================================== the run, behind fake fetches
def synthetic_fetch(calls=None):
    """Random-walk Yahoo-shaped rows for any symbol, daily from 2005 (crypto from 2017) to 2022."""
    def fetch(symbol, interval):
        if calls is not None:
            calls.append((symbol, interval))
        rng = random.Random(f"{symbol}-{interval}")
        price, out = 100.0, []
        crypto = symbol.endswith("-USD")
        day = dt.date(2017, 1, 3) if crypto else dt.date(2005, 1, 3)
        while day <= dt.date(2022, 12, 30):
            if crypto or day.weekday() < 5:
                o = price
                price = max(1.0, price * (1 + rng.gauss(0.0004, 0.014)))
                hi = max(o, price) * (1 + abs(rng.gauss(0, 0.003)))
                lo = min(o, price) * (1 - abs(rng.gauss(0, 0.003)))
                out.append((day.isoformat(), o, hi, lo, price))
            day += DAY
        return out
    return fetch


PHASE = {"USD": 0, "EUR": 20, "GBP": 40, "JPY": 60, "AUD": 10, "CAD": 30, "CHF": 50, "NZD": 70}


def fake_fred(calls=None, fail=()):
    """Monthly rates 1994-2022 swinging between 0.5 % and 3.5 % with a different phase per currency, so
    the rate comparison of a pair flips a few times over the years."""
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
SMALL = {CR_BO: ins.ALT_COINS[:2], IDX_DIP: ins.INDEX_UNIVERSE[:2], CARRY_FX: ins.FX_UNIVERSE[:3]}


@pytest.fixture(scope="module")
def outcome(tmp_path_factory):
    base = tmp_path_factory.mktemp("cfd2run")
    out = base / "out"
    out.mkdir()
    write_round1(out / "enabled.json")
    return research2.run(fetch=synthetic_fetch(), rates_fetch=fake_fred(), now=NOW_RUN,
                         cache_dir=base / "cache", out_dir=out, universes=SMALL), base


def test_run_end_to_end_writes_the_report_and_merges_enabled_json(outcome):
    out, base = outcome
    report = (base / "out" / "BACKTEST_REPORT_R2.md").read_text()
    payload = json.loads((base / "out" / "enabled.json").read_text())
    assert report.startswith("# CFD backtest report, round 2")
    assert list(payload) == ["exit", "enabled", "run_at", "round2"]
    assert {k: payload[k] for k in ROUND1_JSON} == ROUND1_JSON                  # round 1 untouched
    r2 = payload["round2"]
    assert set(r2) == {"enabled", "exit", "run_at"} and r2["run_at"] == NOW_RUN.isoformat()
    assert set(r2["exit"]) == set(research2.HYPOTHESES)
    assert r2["exit"][IDX_DIP] == r2["exit"][CARRY_FX] == "EL" and r2["exit"][CR_BO] in ("E1", "E2")
    assert r2["enabled"] == out.decision.enabled == out.payload["enabled"]
    assert not (base / "out" / "BACKTEST_REPORT.md").exists()


def test_run_produces_trades_for_every_hypothesis_and_exit_in_both_periods(outcome):
    out, _ = outcome
    for name, kinds in ((CR_BO, ("E0", "E1", "E2")), (IDX_DIP, ("E0c", "EL")), (CARRY_FX, ("E0c", "EL"))):
        for kind in kinds:
            periods = {r.period for r in out.results.rows if r.setup == name and r.exit_kind == kind}
            assert periods, (name, kind)
    # the periods a hypothesis can have: crypto coins start in 2017, so their in-sample is 2017-2019
    assert {"IS", "OOS"} <= {r.period for r in out.results.rows if r.setup == IDX_DIP and r.exit_kind == "EL"}
    assert {"IS", "OOS"} <= {r.period for r in out.results.rows if r.setup == CR_BO and r.exit_kind == "E1"}
    assert {"IS", "OOS"} <= {r.period for r in out.results.rows if r.setup == CARRY_FX and r.exit_kind == "EL"}


def test_run_applies_each_period_split_by_the_signal_date(outcome):
    out, _ = outcome
    for r in out.results.rows:
        assert r.period == research.period_of(r.klass, r.signal_ts.date()) and r.period is not None
    crypto = [r for r in out.results.rows if r.klass == "Crypto"]
    assert any(r.period == "IS" and r.signal_ts.year >= 2018 for r in crypto)
    assert not any(r.period == "OOS" and r.signal_ts.year < 2020 for r in crypto)


def test_run_trades_are_closed_costed_and_inside_their_universes(outcome):
    out, _ = outcome
    assert all(math.isfinite(r.result_r) and r.exit_ts >= r.signal_ts for r in out.results.rows)
    universe = {i.symbol for u in SMALL.values() for i in u}
    assert {r.symbol for r in out.results.rows} <= universe
    assert {r.symbol for r in out.results.rows if r.setup == CR_BO} <= {i.symbol for i in SMALL[CR_BO]}
    assert {r.symbol for r in out.results.rows if r.setup == IDX_DIP} <= {i.symbol for i in SMALL[IDX_DIP]}


def test_run_decision_matches_a_recomputation_from_the_rows(outcome):
    out, _ = outcome
    again = research2.decide(out.results.rows)
    assert again.judged == out.decision.judged and again.enabled == out.decision.enabled
    for name in research2.HYPOTHESES:
        assert again.gates[name].passed == out.decision.gates[name].passed
        assert again.gates[name].checks == out.decision.gates[name].checks


def test_run_the_report_lists_every_instrument_and_every_rate_series_it_used(outcome):
    out, base = outcome
    text = (base / "out" / "BACKTEST_REPORT_R2.md").read_text()
    for inst in [i for u in SMALL.values() for i in u]:
        assert inst.name in text
    needed = {c for i in SMALL[CARRY_FX] for c in (i.base, i.quote)}
    assert {c.currency for c in out.results.rate_coverage} == needed
    for c in needed:
        assert rates.SERIES[c] in text
    assert "docs/cfd/PREREGISTRATION_R2.md" in text


def test_run_is_deterministic_and_reads_the_caches_the_second_time(tmp_path):
    calls, fred_calls = [], []
    kwargs = dict(now=NOW_RUN, cache_dir=tmp_path / "cache", universes=SMALL)
    a = research2.run(fetch=synthetic_fetch(calls), rates_fetch=fake_fred(fred_calls),
                      out_dir=tmp_path / "a", **kwargs)
    n_bars, n_rates = len(calls), len(fred_calls)
    assert n_bars == 2 + 2 + 3 and n_rates == len({c for i in SMALL[CARRY_FX] for c in (i.base, i.quote)})
    b = research2.run(fetch=synthetic_fetch(calls), rates_fetch=fake_fred(fred_calls),
                      out_dir=tmp_path / "b", **kwargs)
    assert len(calls) == n_bars and len(fred_calls) == n_rates                     # all from the caches
    report_a = (tmp_path / "a" / "BACKTEST_REPORT_R2.md").read_text()
    assert report_a == (tmp_path / "b" / "BACKTEST_REPORT_R2.md").read_text()
    assert a.decision.enabled == b.decision.enabled
    research2.run(fetch=synthetic_fetch(calls), rates_fetch=fake_fred(fred_calls), out_dir=tmp_path / "c",
                  refresh=True, **kwargs)
    assert len(calls) == 2 * n_bars and len(fred_calls) == 2 * n_rates             # --refresh goes back to the sources


def test_run_survives_an_instrument_whose_data_fails_and_a_currency_whose_rates_fail(tmp_path):
    good = synthetic_fetch()

    def flaky(symbol, interval):
        if symbol in ("SOL-USD", "GBPUSD=X"):
            raise RuntimeError("Yahoo is down")
        return good(symbol, interval)
    log = []
    out = research2.run(fetch=flaky, rates_fetch=fake_fred(fail={rates.SERIES["JPY"]}), now=NOW_RUN,
                        cache_dir=tmp_path / "cache", out_dir=tmp_path / "out", universes=SMALL,
                        log=log.append)
    text = (tmp_path / "out" / "BACKTEST_REPORT_R2.md").read_text()
    assert "SOLUSD" in text and "no data" in text
    assert "SOL-USD" not in {r.symbol for r in out.results.rows}
    assert "GBPUSD=X" not in {r.symbol for r in out.results.rows}
    assert any("SOL-USD" in m for m in log) and any("JPY" in m for m in log)
    jpy = next(c for c in out.results.rate_coverage if c.currency == "JPY")
    assert jpy.observations == 0 and "RuntimeError" in jpy.error
    assert "RuntimeError: FRED is down" in text
    # no rates for JPY: USDJPY has no state and so no trades
    assert "USDJPY=X" not in {r.symbol for r in out.results.rows}


def test_run_without_any_rates_gives_h5_no_trades_and_a_failing_gate(tmp_path):
    out = research2.run(fetch=synthetic_fetch(), rates_fetch=fake_fred(fail=set(rates.SERIES.values())),
                        now=NOW_RUN, cache_dir=tmp_path / "cache", out_dir=tmp_path / "out", universes=SMALL,
                        log=lambda m: None)
    assert not [r for r in out.results.rows if r.setup == CARRY_FX]
    assert not out.decision.gates[CARRY_FX].passed and CARRY_FX not in out.decision.enabled


def test_run_without_the_h5_universe_does_not_fetch_any_rates(tmp_path):
    fred_calls = []
    research2.run(fetch=synthetic_fetch(), rates_fetch=fake_fred(fred_calls), now=NOW_RUN,
                  cache_dir=tmp_path / "cache", out_dir=tmp_path / "out",
                  universes={CR_BO: ins.ALT_COINS[:1], IDX_DIP: (), CARRY_FX: ()})
    assert fred_calls == []


def test_run_with_write_false_writes_nothing(tmp_path):
    out = research2.run(fetch=synthetic_fetch(), rates_fetch=fake_fred(), now=NOW_RUN,
                        cache_dir=tmp_path / "cache", out_dir=tmp_path / "out", universes=SMALL, write=False)
    assert out.report.startswith("# CFD backtest report, round 2")
    assert not (tmp_path / "out").exists()


def test_run_merges_into_an_existing_enabled_json_and_creates_one_when_missing(tmp_path):
    out_dir = tmp_path / "out"
    research2.run(fetch=synthetic_fetch(), rates_fetch=fake_fred(), now=NOW_RUN,
                  cache_dir=tmp_path / "cache", out_dir=out_dir, universes=SMALL)
    assert list(json.loads((out_dir / "enabled.json").read_text())) == ["round2"]


# ================================================================== amendment A3: the wick threshold of crypto
def wick_fetch(good, factor, at=500):
    """`good` with the row at index `at` of every series given a high `factor` times its close."""
    def fetch(symbol, interval):
        rows = list(good(symbol, interval))
        day, o, h, low, c = rows[at]
        rows[at] = (day, o, c * factor, low, c)
        return rows
    return fetch


def test_the_wick_threshold_is_60_percent_for_the_coins_and_15_percent_for_everything_else():
    for coin in ins.ALT_COINS:
        assert research2.max_wick_for(coin) == 0.60
    for inst in ins.INDEX_UNIVERSE + ins.FX_UNIVERSE:
        assert research2.max_wick_for(inst) == 0.15
    for symbol in ("GC=F", "SI=F", "CL=F", "BZ=F"):
        assert research2.max_wick_for(ins.by_symbol(symbol)) == 0.15


def run_with_wick(tmp_path, factor):
    return research2.run(fetch=wick_fetch(synthetic_fetch(), factor), rates_fetch=fake_fred(), now=NOW_RUN,
                         cache_dir=tmp_path / "cache", out_dir=tmp_path / "out", write=False,
                         universes={CR_BO: ins.ALT_COINS[:2], IDX_DIP: ins.INDEX_UNIVERSE[:1],
                                    CARRY_FX: ins.FX_UNIVERSE[:1]})


def test_run_cleans_a_coin_at_60_percent_and_an_index_or_a_pair_at_15(tmp_path):
    out = run_with_wick(tmp_path, 1.30)                  # a bar with a high 30 % over its close, in every series
    coverage = {c.symbol: c for c in out.results.coverage}
    assert {s: c.dropped.wick for s, c in coverage.items()} == {
        "SOL-USD": 0, "XRP-USD": 0, "^GSPC": 1, "EURUSD=X": 1}
    # the coins keep that bar, the others lose it
    assert {s: c.bars for s, c in coverage.items()} == {
        "SOL-USD": 2188, "XRP-USD": 2188, "^GSPC": 4694, "EURUSD=X": 4694}
    assert not (tmp_path / "out").exists()


def test_run_still_drops_a_coin_bar_whose_wick_is_over_60_percent(tmp_path):
    out = run_with_wick(tmp_path, 1.61)
    coverage = {c.symbol: c for c in out.results.coverage}
    assert {s: c.dropped.wick for s, c in coverage.items()} == {
        "SOL-USD": 1, "XRP-USD": 1, "^GSPC": 1, "EURUSD=X": 1}
    out = run_with_wick(tmp_path / "again", 1.59)
    assert {c.symbol: c.dropped.wick for c in out.results.coverage}["SOL-USD"] == 0


def test_run_reads_the_coins_through_the_loader_with_the_crypto_threshold(monkeypatch, tmp_path):
    asked = {}
    real = data.load_daily

    def spy(symbol, **kw):
        asked[symbol] = kw.get("max_wick")
        return real(symbol, **kw)
    monkeypatch.setattr(data, "load_daily", spy)
    run_with_wick(tmp_path, 1.0)
    assert asked == {"SOL-USD": 0.60, "XRP-USD": 0.60, "^GSPC": 0.15, "EURUSD=X": 0.15}


# ================================================================== the command line
def run_main(argv, tmp_path, *, fetch=None, rates_fetch=None, universes=SMALL):
    return research2.main(argv, fetch=fetch or synthetic_fetch(), rates_fetch=rates_fetch or fake_fred(),
                          now=NOW_RUN, out_dir=tmp_path / "out", cache_dir=tmp_path / "cache",
                          universes=universes)


def test_cli_without_run_does_nothing_and_asks_for_it(capsys, tmp_path):
    calls: list = []
    rc = research2.main([], fetch=synthetic_fetch(calls), rates_fetch=fake_fred(calls),
                        out_dir=tmp_path / "out", cache_dir=tmp_path / "cache")
    assert rc == 2 and calls == []
    assert "--run" in capsys.readouterr().out
    assert not (tmp_path / "out").exists()


def test_cli_run_prints_per_hypothesis_the_judged_exit_the_gate_and_the_key_numbers(capsys, tmp_path):
    rc = run_main(["--run"], tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    for name, label in (("CR-BO", "H3"), ("IDX-DIP", "H4"), ("CARRY-FX", "H5")):
        line = next(ln for ln in out.splitlines() if ln.startswith(f"{label} {name}"))
        assert "judged exit E" in line and ("gate PASS" in line or "gate FAIL" in line)
        assert "out-of-sample" in line and "in-sample" in line and "PF" in line and "mean R" in line
    # a failed gate lists its failed lines, with the value and what it needed; a passed gate lists none
    failed = [ln for ln in out.splitlines() if ln.startswith("  failed: ")]
    verdicts = [("gate FAIL" in ln) for ln in out.splitlines() if ln[:2] in ("H3", "H4", "H5")]
    assert len(failed) == sum(verdicts)
    assert all("needs" in ln for ln in failed)
    # every exit of every hypothesis has its out-of-sample numbers beside the judged one
    for kind in ("E0", "E1", "E2", "E0c", "EL"):
        assert any(ln.lstrip().startswith(kind) and "OOS" in ln for ln in out.splitlines()), kind
    assert "enabled:" in out and "written:" in out
    assert (tmp_path / "out" / "BACKTEST_REPORT_R2.md").exists()
    assert json.loads((tmp_path / "out" / "enabled.json").read_text())["round2"]


def test_cli_marks_the_judged_exit_in_the_per_exit_lines(capsys, tmp_path):
    run_main(["--run"], tmp_path)
    out = capsys.readouterr().out
    el = [ln for ln in out.splitlines() if ln.strip().startswith("EL")]
    assert el and all("*" in ln for ln in el)
    e0c = [ln for ln in out.splitlines() if ln.strip().startswith("E0c")]
    assert e0c and not any("*" in ln for ln in e0c)


def test_cli_warns_when_a_series_or_a_currency_has_no_data(capsys, tmp_path):
    good = synthetic_fetch()

    def flaky(symbol, interval):
        if symbol == "SOL-USD":
            raise RuntimeError("rate limited")
        return good(symbol, interval)
    rc = run_main(["--run"], tmp_path, fetch=flaky, rates_fetch=fake_fred(fail={rates.SERIES["EUR"]}))
    out = capsys.readouterr().out
    assert rc == 0
    assert "WARNING: no data for SOL-USD (1d)" in out
    assert "WARNING: no rates for EUR" in out
    # and a complete run says nothing of the kind
    rc = run_main(["--run"], tmp_path / "again")
    assert "WARNING" not in capsys.readouterr().out


def test_cli_refresh_flag_refetches(tmp_path):
    calls: list = []
    fred_calls: list = []
    common = dict(fetch=synthetic_fetch(calls), rates_fetch=fake_fred(fred_calls), now=NOW_RUN,
                  out_dir=tmp_path / "out", cache_dir=tmp_path / "cache",
                  universes={CR_BO: ins.ALT_COINS[:1], IDX_DIP: ins.INDEX_UNIVERSE[:1], CARRY_FX: ins.FX_UNIVERSE[:1]})
    research2.main(["--run"], **common)
    n, m = len(calls), len(fred_calls)
    research2.main(["--run"], **common)
    assert (len(calls), len(fred_calls)) == (n, m)
    research2.main(["--run", "--refresh"], **common)
    assert (len(calls), len(fred_calls)) == (2 * n, 2 * m)


def test_the_module_never_imports_the_network_libraries_at_import_time():
    import subprocess
    import sys
    code = ("import sys; import cfd.research2; "
            "print('requests' in sys.modules, 'yfinance' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=str(research2.OUT_DIR.parent.parent))
    assert out.stdout.split() == ["False", "False"], out.stderr
