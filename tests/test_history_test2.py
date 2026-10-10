"""history_test2.py: the pure parts of part 2 -- the five exits and the choice between them, the liquidity
floor, the sensitivity to the missing companies, the reading of a 13D's index row and head."""
from __future__ import annotations

import datetime as dt

import pytest

import history_test as ht
import history_test2 as h2


def closes(start: str, values: list[float]) -> list[tuple[str, float]]:
    day, out = dt.date.fromisoformat(start), []
    for v in values:
        while day.weekday() > 4:
            day += dt.timedelta(days=1)
        out.append((day.isoformat(), v))
        day += dt.timedelta(days=1)
    return out


BASE = [10.0 + 0.05 * i for i in range(231)]                    # the entry is bar 231 at 21.6


def sig(series):
    return ht.Signal("AAA", series[230][0], 60, 231)


# ------------------------------------------------------------------ the exits
def test_the_five_exits_on_one_path():
    path = [21.6] + [21.6 * (1 + 0.004 * i) for i in range(1, 60)] + [20.0] * 250       # up 24 %, then down 25 % for good
    series = closes("2019-01-01", BASE + path)
    s = sig(series)
    peak = max(path)
    e0, e1, e2, e3, e4 = (h2.close_by(r, s, series) for r in h2.EXITS)
    assert e0.how == "stop" and e0.ret == pytest.approx(20.0 / 21.6 - 1 - ht.COST)      # 25 % off the peak: out
    assert e1.how == "stop" and 20.0 <= peak * 0.80                                    # the wide stop (20 %) too
    assert (e2.how, e2.exit_day) == ("time", series[231 + 126][0])
    assert (e3.how, e3.exit_day) == ("time", series[231 + 252][0])
    assert e4.how == "time"                                                             # 20.0 is above 0.75 x 21.6
    assert h2.close_by("E4", s, closes("2019-01-01", BASE + [21.6, 16.0, 16.0])).how == "stop"
    assert h2.close_by("E2", s, closes("2019-01-01", BASE + [21.6, 22.0])).how == "end"
    assert h2.close_by("E2", s, closes("2019-01-01", BASE + [21.6])) is None
    with pytest.raises(ValueError):
        h2.close_by("E9", s, series)


def _table(first: dict, second: dict) -> dict:
    blank = lambda: {r: {"ret": [0.0], "excess": [0.0]} for r in h2.EXITS}             # noqa: E731
    table = {"first": blank(), "second": blank()}
    for half, values in (("first", first), ("second", second)):
        for rule, excess in values.items():
            table[half][rule] = {"ret": excess, "excess": excess}
    return table


def test_the_first_half_chooses_by_the_median_and_the_second_must_confirm_both():
    lucky = [-0.05, -0.05, 3.0]                                 # a huge mean, a poor median
    steady = [0.02, 0.03, 0.04]
    assert h2.choose_exit(_table({"E2": lucky, "E3": steady}, {"E3": [0.03, 0.04], "E0": [0.0, 0.01]})) == ("E3", True)
    assert h2.choose_exit(_table({"E3": steady}, {"E3": [-0.02, -0.01, 0.9], "E0": [0.0, 0.01]})) == ("E3", False)   # median below
    assert h2.choose_exit(_table({"E3": steady}, {"E3": [0.02, 0.02], "E0": [0.0, 0.5]})) == ("E3", False)    # mean below
    assert h2.choose_exit(_table({"E0": steady}, {})) == ("E0", False)


def test_the_exit_table_puts_each_signal_in_its_half():
    series = closes("2014-01-01", BASE + [21.6 + 0.01 * i for i in range(300)])
    spy = closes("2014-01-01", [100.0] * len(series))
    table = h2.exit_table([(sig(series), series)], spy)
    assert all(len(table["first"][r]["excess"]) == 1 for r in h2.EXITS)
    assert all(table["second"][r]["ret"] == [] for r in h2.EXITS)


# ------------------------------------------------------------------ liquidity
def test_the_floor_reads_the_twenty_bars_before_the_entry():
    series = closes("2019-01-01", BASE + [21.6, 22.0])
    s = sig(series)
    entry_day = series[231][0]
    dv = [(d, 150_000.0) for d, _c in series[:231]] + [(entry_day, 1.0)]
    assert h2.adv_before(dv, entry_day) == 150_000.0 and h2.liquid(s, series, dv)
    assert not h2.liquid(s, series, [(d, 50_000.0) for d, _c in series])
    assert h2.adv_before(dv[:10], entry_day) is None and not h2.liquid(s, series, [])


# ------------------------------------------------------------------ the missing companies
def test_the_sensitivity_assumes_the_same_rate_of_signals():
    s = h2.sensitivity(seen_signals=780, seen_mean=0.098, seen_days=4758, missing_days=6191)
    assert s["missing"] == round(6191 * 780 / 4758)
    assert s["at"][0.0] == pytest.approx(780 * 0.098 / (780 + s["missing"]))
    assert s["at"][-0.25] < 0 < s["at"][0.0] and s["zero"] == pytest.approx(-780 * 0.098 / s["missing"])
    assert h2.sensitivity(10, 0.1, 100, 0)["zero"] is None


# ------------------------------------------------------------------ the stakes
IDX = """Form Type   Company Name                                                  CIK         Date Filed  File Name
---------------------------------------------------------------------------------------------------------------
SC 13D      ACME CORP                                                     1234        2015-03-02  edgar/data/1234/0001-15-000001.txt
SC 13D      BIG FUND LP                                                   999         2015-03-02  edgar/data/999/0001-15-000001.txt
SC 13D/A    ACME CORP                                                     1234        2015-03-09  edgar/data/1234/0001-15-000002.txt
SC 13G      ACME CORP                                                     1234        2015-03-10  edgar/data/1234/0001-15-000003.txt
SCHEDULE 13D  NEW CO, INC.                                                77          2025-02-03  edgar/data/77/0001-25-000009.txt
"""
HEAD = """<SEC-HEADER>
SUBJECT COMPANY:
	COMPANY DATA:
		COMPANY CONFORMED NAME:			ACME CORP
		CENTRAL INDEX KEY:			0000001234
FILED BY:
	COMPANY DATA:
		COMPANY CONFORMED NAME:			BIG FUND LP
		CENTRAL INDEX KEY:			0000000999
</SEC-HEADER>
<p>11 AGGREGATE AMOUNT BENEFICIALLY OWNED: 1,000,000</p>
<p>13 PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW (11)</p><p>&nbsp;14.2%</p>
<p>13 PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW (11): 9.9 %</p>
"""


def test_the_index_gives_the_initial_13ds_and_no_amendment_or_13g():
    assert h2.index_rows(IDX) == [("edgar/data/1234/0001-15-000001.txt", "1234", "2015-03-02"),
                                  ("edgar/data/999/0001-15-000001.txt", "999", "2015-03-02"),
                                  ("edgar/data/77/0001-25-000009.txt", "77", "2025-02-03")]


def test_the_head_gives_the_subject_and_the_largest_percent():
    assert h2.subject_and_percent(HEAD) == ("1234", 14.2)
    assert h2.subject_and_percent("no header here") == (None, None)
    assert h2.subject_and_percent(HEAD.replace("14.2%", "100%").replace("9.9 %", "0%"))[1] is None


def test_stake_points_are_the_bots_and_a_candidate_needs_twelve_and_a_half_percent():
    assert h2.stake_points(5.0) == 30 and h2.stake_points(14.2) == pytest.approx(48.4)
    assert h2.stake_points(30.0) == 50 and h2.stake_points(55.0) == 0
    filings = [["a", "1", "ACME", "2015-03-02", 14.2], ["b", "2", "SMALL", "2015-03-02", 8.0],
               ["c", "3", None, "2015-03-02", 20.0], ["d", "4", "NOPCT", "2015-03-02", None],
               ["e", "5", "CTRL", "2015-03-02", 60.0], ["f", "1", "ACME", "2015-03-02", 14.2]]
    assert h2.stake_candidates(filings) == [("ACME", "2015-03-02", pytest.approx(48.4))]


def test_the_report_can_leave_signals_out_without_changing_which_are_made():
    series = closes("2014-01-01", BASE + [21.6 + 0.01 * i for i in range(400)])
    table = {"AAA": series, "SPY": closes("2014-01-01", [100.0] * len(series))}
    cands = [("AAA", series[230][0], 52.0)]
    assert "Buy signals: 1." in ht.build_report(cands, prices_of=lambda s: table.get(s, []))
    assert "Buy signals: 0." in ht.build_report(cands, prices_of=lambda s: table.get(s, []), keep=lambda s: False)
