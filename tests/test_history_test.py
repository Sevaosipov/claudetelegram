"""history_test.py: the pure parts of the 20-year test of the stock rules -- the purchases read from the
SEC's tables, the cluster and its points, the signals, the trades, the statistics and the fixed reading."""
from __future__ import annotations

import datetime as dt

import pytest

import history_test as ht
import model_score


def P(symbol="AAA", filed="2020-03-02", owner="1", role="officer", value=100_000.0, inc=None):
    return ht.Purchase(symbol, filed, owner, role, value, inc)


def closes(start: str, values: list[float]) -> list[tuple[str, float]]:
    day, out = dt.date.fromisoformat(start), []
    for v in values:
        while day.weekday() > 4:
            day += dt.timedelta(days=1)
        out.append((day.isoformat(), v))
        day += dt.timedelta(days=1)
    return out


# ------------------------------------------------------------------ the tables
def test_a_purchase_is_a_form_4_code_p_acquisition_added_up_per_filing():
    subs = [{"ACCESSION_NUMBER": "a", "FILING_DATE": "31-JAN-2018", "DOCUMENT_TYPE": "4", "ISSUERTRADINGSYMBOL": "xrm"},
            {"ACCESSION_NUMBER": "b", "FILING_DATE": "31-JAN-2018", "DOCUMENT_TYPE": "3", "ISSUERTRADINGSYMBOL": "XRM"},
            {"ACCESSION_NUMBER": "c", "FILING_DATE": "01-FEB-2018", "DOCUMENT_TYPE": "4", "ISSUERTRADINGSYMBOL": "N/A"},
            {"ACCESSION_NUMBER": "d", "FILING_DATE": "02-FEB-2018", "DOCUMENT_TYPE": "4", "ISSUERTRADINGSYMBOL": "ZZZ"}]
    owners = [{"ACCESSION_NUMBER": "a", "RPTOWNERCIK": "7", "RPTOWNER_RELATIONSHIP": "Director,Officer",
               "RPTOWNER_TITLE": "Chief Executive Officer"},
              {"ACCESSION_NUMBER": "d", "RPTOWNERCIK": "8", "RPTOWNER_RELATIONSHIP": "TenPercentOwner", "RPTOWNER_TITLE": ""}]
    t = lambda acc, code, shares, price, after, ad="A": {"ACCESSION_NUMBER": acc, "TRANS_CODE": code,     # noqa: E731
                                                        "TRANS_ACQUIRED_DISP_CD": ad, "TRANS_SHARES": shares,
                                                        "TRANS_PRICEPERSHARE": price, "SHRS_OWND_FOLWNG_TRANS": after}
    trans = [t("a", "P", "1000", "30", "5000"), t("a", "P", "1000", "40", "6000"), t("a", "A", "9999", "1", "0"),
             t("a", "S", "500", "40", "0", "D"), t("b", "P", "9000", "30", "0"), t("d", "P", "100", "10", "100")]
    got = ht.purchases_of(subs, owners, trans)
    assert got == [ht.Purchase("XRM", "2018-01-31", "7", "ceo", 70_000.0, pytest.approx(50.0))]      # 2000 on 4000 held


# ------------------------------------------------------------------ the cluster
def test_the_points_are_the_bots_own_for_a_cluster_and_nothing_for_less_than_one():
    three = [P(owner="1", role="ceo"), P(owner="2"), P(owner="3", role="director")]
    assert ht.cluster_points(three) == 42 + 10
    assert ht.cluster_points([P(owner="1", value=60_000), P(owner="2", value=30_000)]) == 0      # under $100 000
    assert ht.cluster_points([P(owner="1", value=400_000)]) == 0                                  # one buyer, not solo
    assert ht.cluster_points([P(owner="1", role="ceo", value=600_000, inc=35.0)]) == 22 + 10 + 6  # a solo buyer
    assert ht.cluster_points([P(owner="1", role="holder"), P(owner="2", role="holder")]) == 15    # 10 % holders only
    assert ht.cluster_points([P(owner="1"), P(owner="1", filed="2020-03-03")]) == 0               # one person twice


def test_candidates_are_the_days_a_14_day_window_can_still_become_a_buy():
    rows = [P(owner="1", role="ceo", filed="2020-03-02"), P(owner="2", filed="2020-03-05"),
            P(owner="3", filed="2020-03-10"), P(owner="4", filed="2020-04-20"),
            P(symbol="BBB", owner="9", filed="2020-03-02")]
    assert ht.candidates(rows) == [("AAA", "2020-03-10", 52.0)]        # only the third filing completes it


# ------------------------------------------------------------------ signals and trades
def _rising(n=260, start=10.0, step=0.05):
    return [start + i * step for i in range(n)]


def test_a_candidate_with_momentum_is_a_signal_entered_at_the_first_close_after_the_filing():
    series = closes("2019-01-01", _rising())
    day = series[230][0]
    [s] = ht.signals_of("AAA", [(day, 52.0)], series)
    assert s.entry_index == 231 and s.points >= model_score.STOCK_BUY
    assert ht.signals_of("AAA", [(day, 52.0)], closes("2019-01-01", list(reversed(_rising())))) == []   # falling: 52 only
    assert ht.signals_of("AAA", [(series[5][0], 52.0)], series) == []                                  # too little history
    assert ht.signals_of("AAA", [(series[-1][0], 52.0)], series) == []                                 # no close after it


def test_a_filing_on_a_day_without_a_bar_enters_at_the_next_bar():
    series = closes("2019-01-01", _rising())
    i = next(k for k in range(225, 240) if dt.date.fromisoformat(series[k][0]).weekday() == 4)     # a Friday bar
    saturday = (dt.date.fromisoformat(series[i][0]) + dt.timedelta(days=1)).isoformat()
    [s] = ht.signals_of("AAA", [(saturday, 52.0)], series)
    assert s.entry_index == i + 1                                    # Monday's bar: the first close after it


def test_one_signal_while_the_trade_is_open_and_none_within_thirty_days():
    series = closes("2019-01-01", _rising(400))
    days = [(series[i][0], 52.0) for i in (230, 235, 300)]
    got = ht.signals_of("AAA", days, series)
    assert [s.entry_index for s in got] == [231]                 # the trade (a steady rise) is still open at 300


def test_the_trailing_stop_dead_money_and_the_year():
    up = closes("2019-01-01", _rising(231) + [21.6, 22.0, 19.0, 19.0])           # peak 22, stop 10 %: out at 19
    s = ht.Signal("AAA", up[230][0], 60, 231)
    t = ht.trade_of(s, up)
    assert (t.how, t.exit_day) == ("stop", up[233][0]) and t.ret == pytest.approx(19.0 / 21.6 - 1 - ht.COST)
    flat = closes("2019-01-01", _rising(231) + [21.6] * 70)
    assert ht.trade_of(ht.Signal("AAA", flat[230][0], 60, 231), flat).how == "dead"
    long = closes("2019-01-01", _rising(231) + [21.6 * (1 + 0.002 * i) for i in range(300)])
    year = ht.trade_of(ht.Signal("AAA", long[230][0], 60, 231), long)
    assert year.how == "time" and year.days >= 365
    short = closes("2019-01-01", _rising(231) + [21.6, 21.7])
    assert ht.trade_of(ht.Signal("AAA", short[230][0], 60, 231), short).how == "end"
    assert ht.trade_of(ht.Signal("AAA", short[230][0], 60, 232), short) is None      # nothing after the entry


def test_forward_returns_and_the_markets_move_over_the_same_days():
    series = closes("2019-01-01", [10.0] * 5 + [11.0] * 5)
    s = ht.Signal("AAA", series[0][0], 60, 0)
    assert ht.forward(s, series, 5) == pytest.approx(0.10) and ht.forward(s, series, 10) is None
    assert ht.move_between(series, series[0][0], series[5][0]) == pytest.approx(0.10)
    assert ht.move_between([], "2019-01-01", "2019-02-01") is None


# ------------------------------------------------------------------ statistics and the reading
def test_summary_and_profit_factor():
    s = ht.summary([0.10, -0.05, 0.20, 0.05])
    assert (s["n"], s["win"]) == (4, 0.75) and s["mean"] == pytest.approx(0.075) and s["t"] > 1
    assert ht.summary([]) == {"n": 0} and ht.profit_factor([0.2, -0.1]) == pytest.approx(2.0)


@pytest.mark.parametrize("first, second, t, tf, ts, word", [
    (0.02, 0.01, 2.5, 0.01, 0.02, "hold up:"),
    (0.02, -0.01, 2.5, 0.01, 0.02, "do NOT hold up"),
    (0.02, 0.01, 2.5, -0.01, 0.02, "do NOT hold up"),
    (0.02, 0.01, 1.2, 0.01, 0.02, "not shown either way: both"),
])
def test_the_reading_is_the_one_fixed_beforehand(first, second, t, tf, ts, word):
    got = ht.verdict({"mean": first}, {"mean": second}, {"mean": 0.01, "t": t}, {"mean": tf}, {"mean": ts})
    assert word in got
    assert "no signals" in ht.verdict({}, {"mean": 1}, {}, {"mean": 1}, {"mean": 1})


def test_the_report_runs_from_candidates_and_a_price_reader():
    series = closes("2014-01-01", _rising(700))
    spy = closes("2014-01-01", [100.0 + i * 0.01 for i in range(700)])
    table = {"AAA": series, "SPY": spy}
    cands = [("AAA", series[230][0], 52.0), ("GONE", "2015-06-01", 52.0)]
    text = ht.build_report(cands, prices_of=lambda s: table.get(s, []), now=dt.datetime(2026, 10, 10))
    assert "Candidate cluster days: 2 on 2 tickers; with prices: 1 (50 %). Buy signals: 1." in text
    assert "**Reading, as fixed beforehand: not shown either way (a half has no signals).**" in text
    assert "## A. The event study" in text and "## B. The bot's exits" in text and "| 2014 | 1 | 1 | 1 |" in text
