"""Round 4 (docs/cfd/PREREGISTRATION_R4.md): the positioning index and its trades (H8), the London break of
the Asian range (H9), the monthly statistics and the user's gate. No network: the loaders are not run."""
from __future__ import annotations

import datetime as dt

import pytest

from cfd import research4 as r4
from cfd.data import Bar

UTC = dt.timezone.utc


def day_bar(d, o, h, low, c):
    return Bar(dt.datetime(d.year, d.month, d.day, tzinfo=UTC), o, h, low, c)


def hour_bar(day, hour, o, h, low, c):          # January: London time is UTC
    return Bar(dt.datetime(day.year, day.month, day.day, hour, tzinfo=UTC), o, h, low, c)


def trade(r, exit_date, entry_date=None):
    return r4.Trade("EURUSD", "long", entry_date or exit_date, exit_date, r, "stop")


# ------------------------------------------------------------------ statistics and the gate
def test_a_month_is_the_sum_of_the_trades_closed_in_it():
    trades = [trade(1.0, dt.date(2020, 1, 5)), trade(-0.4, dt.date(2020, 1, 20)), trade(-1.0, dt.date(2020, 2, 3)),
              trade(-0.5, dt.date(2020, 3, 3)), trade(2.0, dt.date(2020, 4, 3))]
    assert r4.month_results(trades) == {(2020, 1): pytest.approx(0.6), (2020, 2): -1.0, (2020, 3): -0.5,
                                        (2020, 4): 2.0}
    st = r4.stats(trades)
    assert (st.n, st.months, st.months_up, st.worst_month, st.losing_run) == (5, 4, 2, -1.0, 2)
    assert st.net == pytest.approx(1.1) and st.pf == pytest.approx(3.0 / 1.9) and st.win_rate == 0.4


def test_no_trades_are_all_zeros():
    assert r4.stats([]).n == 0 and r4.stats([]).months_up_share == 0.0


def test_the_split_is_by_the_entry_date():
    trades = [trade(1, dt.date(2005, 6, 1)), trade(1, dt.date(2016, 12, 30)), trade(1, dt.date(2017, 1, 2))]
    ins_, oos = r4.split(trades)
    assert [t.entry_date.year for t in ins_] == [2016] and [t.entry_date.year for t in oos] == [2017]


def _stats(n, net, months, up):
    return r4.Stats(n, 0.5, net / n if n else 0, 1.0, 1.1, net, months, up, -1.0, 2)


@pytest.mark.parametrize("is_net, oos, passed", [
    (5.0, _stats(120, 3.0, 40, 21), True),
    (5.0, _stats(99, 3.0, 40, 21), False),          # too few trades
    (5.0, _stats(120, -0.1, 40, 21), False),        # a loss out of sample
    (-0.1, _stats(120, 3.0, 40, 21), False),        # a loss in sample
    (5.0, _stats(120, 3.0, 40, 20), False),         # half of the months is not more than half
])
def test_the_gate(is_net, oos, passed):
    assert r4.gate("X", _stats(200, is_net, 60, 30), oos).passed is passed


# ------------------------------------------------------------------ H8: the index
def test_the_index_is_the_place_in_the_range_of_the_window():
    assert r4.cot_index([1, 2, 3, 2, 5, 0], window=3) == [None, None, 100.0, 0.0, 100.0, 0.0]
    assert r4.cot_index([1, 3, 2], window=3) == [None, None, 50.0]
    assert r4.cot_index([2, 2, 2], window=3) == [None, None, None]


def test_a_report_is_usable_on_the_second_bar_after_its_friday():
    tuesday = dt.date(2020, 1, 7)                                       # its Friday is the 10th
    dates = [dt.date(2020, 1, d) for d in (8, 9, 10, 13, 14, 15)]
    assert r4.usable_bar(tuesday, dates) == 4                           # Monday 13th is the first, Tuesday 14th
    assert r4.usable_bar(tuesday, dates[:4]) is None


def _cot_case(prices, nets, window=3):
    """Weekday bars from Monday 2020-01-06, flat but for `prices` ({index: (o, h, l, c)}); weekly reports
    from Tuesday 2019-12-17 with `nets`."""
    days, d = [], dt.date(2019, 12, 2)
    while len(days) < 60:
        if d.weekday() < 5:
            days.append(d)
        d += dt.timedelta(days=1)
    bars = [day_bar(x, *prices.get(i, (1.0, 1.01, 0.99, 1.0))) for i, x in enumerate(days)]
    reports = [(dt.date(2019, 12, 3) + dt.timedelta(weeks=k), n) for k, n in enumerate(nets)]
    return bars, reports, days


def test_an_extreme_long_of_the_speculators_is_a_short_closed_when_the_index_comes_back(monkeypatch):
    monkeypatch.setattr(r4, "COT_WINDOW", 3)
    monkeypatch.setattr(r4.ind, "atr", lambda bars, n: [0.02] * len(bars))
    # reports: Dec 3, 10, 17 (index 100 at the third: usable on the 2nd bar after Fri Dec 20 = Tue Dec 24),
    # then 24 (still high), 31 (back to the low of the window: index 0 <= 50 -> exit)
    bars, reports, days = _cot_case({}, [0.1, 0.2, 0.3, 0.3, 0.1])
    trades = r4.backtest_cot("EURUSD", True, bars, reports)
    assert len(trades) == 1
    t = trades[0]
    signal = days.index(dt.date(2019, 12, 24))
    assert (t.side, t.how, t.entry_date) == ("short", "report", days[signal + 1])
    assert t.exit_date == days[days.index(dt.date(2020, 1, 7)) + 1]     # usable Tue Jan 7, out at the next open
    nights = (t.exit_date - t.entry_date).days
    assert t.r == pytest.approx(0 - (0.015 + nights * 0.010) / 100 * 1.0 / 0.06)


def test_a_pair_that_moves_against_the_currency_takes_the_other_side_and_the_stop_costs_one_r(monkeypatch):
    monkeypatch.setattr(r4, "COT_WINDOW", 3)
    monkeypatch.setattr(r4.ind, "atr", lambda bars, n: [0.02] * len(bars))
    bars, reports, days = _cot_case({}, [0.1, 0.2, 0.3, 0.3, 0.3, 0.3, 0.3])
    signal = days.index(dt.date(2019, 12, 24))
    bars[signal + 3] = day_bar(days[signal + 3], 1.0, 1.01, 0.93, 0.95)        # through the long's stop at 0.94
    trades = r4.backtest_cot("USDJPY", False, bars, reports)
    t = trades[0]
    assert (t.side, t.how, t.exit_date) == ("long", "stop", days[signal + 3])
    assert t.r == pytest.approx(-1 - (0.015 + 2 * 0.010) / 100 * 1.0 / 0.06)


def test_no_trade_without_an_extreme_only_one_at_a_time_and_none_left_open_is_counted(monkeypatch):
    monkeypatch.setattr(r4, "COT_WINDOW", 3)
    monkeypatch.setattr(r4.ind, "atr", lambda bars, n: [0.02] * len(bars))
    bars, reports, _ = _cot_case({}, [0.1, 0.3, 0.2, 0.25, 0.22])              # the index: 50, 50, 0... never 95
    assert all(v is None or 5 < v < 95 for v in r4.cot_index([0.1, 0.3, 0.2, 0.25], 3))
    assert r4.backtest_cot("EURUSD", True, bars[:20], reports[:4]) == []
    # an extreme every week for four weeks, then back: one trade, not four
    bars, reports, _ = _cot_case({}, [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.1])
    assert len(r4.backtest_cot("EURUSD", True, bars, reports)) == 1
    # still open when the bars end: not a result
    bars, reports, _ = _cot_case({}, [0.1, 0.2, 0.3, 0.4, 0.5, 0.6])
    assert r4.backtest_cot("EURUSD", True, bars, reports) == []


# ------------------------------------------------------------------ H9: one London day
DAY = dt.date(2020, 1, 8)            # a Wednesday


def _night(high=1.1010, low=1.1000):
    return [(h, hour_bar(DAY, h, 1.1005, high, low, 1.1005)) for h in range(8)]


def test_a_break_up_is_a_long_held_to_the_evening():
    rows = _night() + [(8, hour_bar(DAY, 8, 1.1006, 1.1015, 1.1004, 1.1012)),
                       (9, hour_bar(DAY, 9, 1.1012, 1.1030, 1.1008, 1.1028)),
                       (20, hour_bar(DAY, 20, 1.1030, 1.1040, 1.1020, 1.1035))]
    t = r4.london_trade("EURUSD", DAY, rows, 0.0050, 0.015)
    assert (t.side, t.how) == ("long", "evening")
    assert t.r == pytest.approx((1.1030 - 1.1010) / 0.0010 - 0.015 / 100 * 1.1010 / 0.0010)


def test_a_break_down_stopped_at_the_other_side_loses_one_r():
    rows = _night() + [(9, hour_bar(DAY, 9, 1.1003, 1.1006, 1.0995, 1.0997)),
                       (10, hour_bar(DAY, 10, 1.0997, 1.1011, 1.0996, 1.1009))]
    t = r4.london_trade("EURUSD", DAY, rows, 0.0050, 0.015)
    assert (t.side, t.how) == ("short", "stop") and t.r == pytest.approx(-1 - 0.015 / 100 * 1.1000 / 0.0010)


def test_a_bar_that_breaks_both_sides_is_a_loss():
    rows = _night() + [(8, hour_bar(DAY, 8, 1.1005, 1.1012, 1.0998, 1.1005))]
    t = r4.london_trade("EURUSD", DAY, rows, 0.0050, 0.015)
    assert t.how == "both" and t.r < -1


def test_a_bar_that_opens_beyond_the_level_enters_at_its_open_and_a_gap_through_the_stop_fills_at_the_open():
    rows = _night() + [(8, hour_bar(DAY, 8, 1.1014, 1.1016, 1.1012, 1.1015)),
                       (9, hour_bar(DAY, 9, 1.0996, 1.0999, 1.0990, 1.0992))]
    t = r4.london_trade("EURUSD", DAY, rows, 0.0050, 0.015)
    distance = 1.1014 - 1.1000
    assert t.how == "stop" and t.r == pytest.approx((1.0996 - 1.1014) / distance - 0.015 / 100 * 1.1014 / distance)


@pytest.mark.parametrize("why, rows, atr, day", [
    ("a weekend", _night() + [(8, hour_bar(DAY, 8, 1.1006, 1.1015, 1.1004, 1.1012))], 0.0050, dt.date(2020, 1, 11)),
    ("no ATR yet", _night() + [(8, hour_bar(DAY, 8, 1.1006, 1.1015, 1.1004, 1.1012))], None, DAY),
    ("a wide range", _night() + [(8, hour_bar(DAY, 8, 1.1006, 1.1015, 1.1004, 1.1012))], 0.0016, DAY),
    ("too few night bars", _night()[:5] + [(8, hour_bar(DAY, 8, 1.1006, 1.1015, 1.1004, 1.1012))], 0.0050, DAY),
    ("no break in the window", _night() + [(8, hour_bar(DAY, 8, 1.1005, 1.1009, 1.1001, 1.1005)),
                                           (12, hour_bar(DAY, 12, 1.1005, 1.1020, 1.1001, 1.1015))], 0.0050, DAY),
])
def test_days_with_no_trade(why, rows, atr, day):
    assert r4.london_trade("EURUSD", day, rows, atr, 0.015) is None, why


def test_hours_are_london_hours_in_summer_too():
    bar = Bar(dt.datetime(2020, 7, 1, 7, tzinfo=UTC), 1, 1, 1, 1)             # 08:00 in London
    assert list(r4.london_days([bar]).values())[0][0][0] == 8


def test_the_days_atr_comes_from_the_days_before(monkeypatch):
    seen = []
    monkeypatch.setattr(r4.ind, "atr", lambda bars, n: [0.001 * (i + 1) for i in range(len(bars))])
    monkeypatch.setattr(r4, "london_trade", lambda pair, day, rows, atr, cost: seen.append((day, atr)))
    bars = [hour_bar(dt.date(2020, 1, d), 3, 1, 1, 1, 1) for d in (6, 7, 8)]
    r4.backtest_london("EURUSD", bars, 0.015)
    assert [a for _d, a in seen] == [None, 0.001, 0.002]


# ------------------------------------------------------------------ the report
def test_the_report_says_whether_each_passes_and_names_a_weak_pass():
    trades = [trade(0.3, dt.date(2018, 1 + k % 12, 5)) for k in range(120)] + [trade(0.2, dt.date(2010, 5, 5))]
    g = r4.gate("COT-EXT", *map(r4.stats, r4.split(trades)))
    text = r4.render_report(trades, [], [g, r4.gate("LDN-BO", r4.stats([]), r4.stats([]))],
                            dt.datetime(2026, 10, 9, tzinfo=UTC))
    assert "**Итог: проходит.**" in text and "**Итог: не проходит.**" in text
    assert r4.round4_block([g], dt.datetime(2026, 10, 9, tzinfo=UTC))["enabled"] == ["COT-EXT"]
