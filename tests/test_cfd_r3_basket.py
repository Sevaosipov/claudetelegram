"""cfd/basket.py: H7 (CARRY-BASKET) of PREREGISTRATION_R3.md as pure functions -- the ranking, the legs,
the month's return, the turnover and financing costs, the calendar, and the monthly statistics. Every
expected number is worked out by hand in the comments."""
from __future__ import annotations

import datetime as dt
import math

import pytest

from cfd import basket as bk
from cfd.data import Bar

UTC = dt.timezone.utc
D = dt.date


def approx(x):
    return pytest.approx(x, rel=1e-9, abs=1e-12)


def test_the_constants_are_the_preregistered_ones():
    assert bk.CURRENCIES == ("USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD")
    assert (bk.LEGS_PER_SIDE, bk.MIN_CURRENCIES) == (3, 6)
    assert bk.TURNOVER_COST_PCT == approx(0.030)          # 0.015 % x 2
    assert bk.FINANCING_MARKUP_PCT == 0.004
    assert bk.LEG_WEIGHT == approx(1 / 3)


# ================================================================== the ranking
ALL = {"USD": 5.0, "EUR": 4.0, "GBP": 3.0, "JPY": 0.1, "AUD": 4.5, "CAD": 2.0, "CHF": 0.5, "NZD": 3.5}


def test_top_three_long_bottom_three_short_by_rate():
    # sorted: USD 5.0, AUD 4.5, EUR 4.0, NZD 3.5, GBP 3.0, CAD 2.0, CHF 0.5, JPY 0.1
    b = bk.select(ALL)
    assert b.longs == ("USD", "AUD", "EUR")
    assert b.shorts == ("JPY", "CHF", "CAD")                # the lowest first
    assert b.legs == {"USD": 1, "AUD": 1, "EUR": 1, "JPY": -1, "CHF": -1, "CAD": -1}


def test_a_negative_rate_ranks_below_zero():
    rates = {**ALL, "JPY": -0.1, "CHF": -0.5}
    assert bk.select(rates).shorts == ("CHF", "JPY", "CAD")


def test_a_currency_without_a_rate_is_left_out_and_seven_still_make_a_basket():
    rates = {**ALL, "USD": None}                            # AUD 4.5, EUR 4.0, NZD 3.5 | GBP 3.0 | CAD, CHF, JPY
    b = bk.select(rates)
    assert b.longs == ("AUD", "EUR", "NZD") and b.shorts == ("JPY", "CHF", "CAD")


def test_six_currencies_are_the_minimum_and_split_three_and_three():
    rates = {**ALL, "USD": None, "GBP": None}               # AUD EUR NZD | CAD CHF JPY
    b = bk.select(rates)
    assert b.longs == ("AUD", "EUR", "NZD") and b.shorts == ("JPY", "CHF", "CAD")
    assert bk.select({**rates, "CAD": None}) is None         # five: no basket


def test_a_missing_key_counts_as_no_rate():
    assert bk.select({"USD": 1.0, "EUR": 2.0, "GBP": 3.0, "JPY": 4.0, "AUD": 5.0}) is None


def test_ties_go_by_the_order_of_the_currency_list():
    rates = {c: 1.0 for c in bk.CURRENCIES}                  # all equal: USD EUR GBP long, NZD CHF CAD short
    b = bk.select(rates)
    assert b.longs == ("USD", "EUR", "GBP") and b.shorts == ("NZD", "CHF", "CAD")


# ================================================================== a leg and a month
def test_a_leg_is_the_spot_change_plus_the_monthly_carry():
    # 1.10 -> 1.122 is +2 %; (4.0 - 5.0) / 12 / 100 = -0.000833333
    assert bk.leg_return(1.10, 1.122, 4.0, 5.0) == approx(0.02 - 1 / 1200)
    assert bk.leg_return(1.0, 1.0, 5.0, 5.0) == 0.0          # USD against itself
    assert bk.leg_return(0.5, 0.45, -0.5, 2.0) == approx(-0.10 - 2.5 / 1200)


def test_the_basket_month_is_the_mean_of_the_longs_minus_the_mean_of_the_shorts():
    b = bk.Basket(("A", "B", "C"), ("D", "E", "F"))
    legs = {"A": 0.02, "B": 0.01, "C": 0.0, "D": -0.01, "E": 0.0, "F": 0.02}
    # longs mean 0.01; shorts mean 0.01 / 3; the short side's rise is a loss
    assert bk.basket_return(b, legs) == approx(0.01 - 0.01 / 3)


# ================================================================== turnover
LAST = bk.Basket(("USD", "AUD", "EUR"), ("JPY", "CHF", "CAD"))


def test_the_first_basket_has_six_entries():
    assert bk.turnover_events(None, LAST) == 6


def test_an_unchanged_basket_has_no_turnover():
    assert bk.turnover_events(LAST, bk.Basket(("EUR", "USD", "AUD"), ("CAD", "JPY", "CHF"))) == 0


def test_a_leg_that_leaves_and_one_that_enters_each_count_once():
    new = bk.Basket(("USD", "EUR", "NZD"), ("JPY", "CHF", "GBP"))       # AUD, CAD leave; NZD, GBP enter
    assert bk.turnover_events(LAST, new) == 4


def test_a_leg_that_changes_side_leaves_one_and_enters_the_other():
    new = bk.Basket(("USD", "AUD", "JPY"), ("EUR", "CHF", "CAD"))       # JPY short->long, EUR long->short
    assert bk.turnover_events(LAST, new) == 4


# ================================================================== costs
def test_costs_of_a_month_hand_computed():
    # 4 events: 4 x 0.030 % / 3 = 0.04 %;  31 nights: 31 x 0.004 % x 6 legs / 3 = 31 x 0.008 % = 0.248 %
    assert bk.month_cost(4, 31) == approx(0.0004 + 0.00248)
    assert bk.month_cost(0, 30) == approx(0.0024)
    assert bk.month_cost(6, 0) == approx(0.0006)


# ================================================================== the calendar and the prices
def test_the_rebalance_is_the_first_bar_of_each_calendar_month():
    days = [D(2020, 1, 3), D(2020, 1, 2), D(2020, 1, 2), D(2020, 2, 3), D(2020, 2, 4), D(2020, 3, 2),
            D(2021, 1, 4), D(2020, 12, 31)]
    assert bk.rebalance_dates(days) == [D(2020, 1, 2), D(2020, 2, 3), D(2020, 3, 2), D(2020, 12, 31),
                                        D(2021, 1, 4)]


def bar(day, close):
    return Bar(dt.datetime(day.year, day.month, day.day, tzinfo=UTC), close, close, close, close)


def test_usd_values_keep_xxxusd_and_invert_usdxxx():
    bars = [bar(D(2020, 1, 2), 0.5), bar(D(2020, 1, 3), 4.0)]
    assert bk.usd_values(bars, False) == [(D(2020, 1, 2), 0.5), (D(2020, 1, 3), 4.0)]
    assert bk.usd_values(bars, True) == [(D(2020, 1, 2), 2.0), (D(2020, 1, 3), 0.25)]


def test_the_inverse_pair_gives_the_same_spot_change_as_the_direct_one():
    # USDJPY 100 -> 110: JPY in USD 0.01 -> 0.0090909 = -9.0909 %, which is 100 / 110 - 1
    v = bk.usd_values([bar(D(2020, 1, 2), 100.0), bar(D(2020, 2, 3), 110.0)], True)
    assert bk.leg_return(v[0][1], v[1][1], 0.0, 0.0) == approx(100 / 110 - 1)


def test_a_value_is_that_of_the_latest_bar_on_or_before_the_day():
    s = [(D(2020, 1, 2), 1.0), (D(2020, 1, 6), 2.0)]
    assert bk.value_on(s, D(2020, 1, 1)) is None
    assert bk.value_on(s, D(2020, 1, 2)) == 1.0
    assert bk.value_on(s, D(2020, 1, 5)) == 1.0
    assert bk.value_on(s, D(2020, 1, 6)) == 2.0
    assert bk.value_on(s, D(2021, 1, 1)) == 2.0


# ================================================================== the months, end to end
# Rebalance days 2020-01-02, 02-03, 03-02, 04-01 (a bogus bar on 01-03 and 02-04 must not be used).
START = [D(2020, 1, 2), D(2020, 2, 3), D(2020, 3, 2), D(2020, 4, 1)]
RATES = {"USD": 2.0, "EUR": 0.5, "GBP": 1.0, "JPY": -0.1, "AUD": 1.5, "CAD": 1.2, "CHF": -0.5, "NZD": 1.7}


def rate_on(c, day):
    if c == "EUR" and day >= D(2020, 3, 1):
        return 3.0
    return RATES[c]


# price in USD on the four rebalance days
PRICES = {
    "NZD": [0.60, 0.606, 0.606, 0.606],          # +1 %, flat, flat
    "AUD": [0.70, 0.693, 0.693, 0.693],          # -1 %
    "CHF": [1.00, 1.02, 1.02, 1.02],             # +2 %
    "JPY": [0.0090, 0.0090, 0.0090, 0.0090],
    "EUR": [1.10, 1.111, 1.111, 1.13322],        # +1 %, flat, +2 %
    "CAD": [0.75, 0.75, 0.75, 0.75],
    "GBP": [1.30, 1.30, 1.30, 1.287],            # -1 % in the third month
}


def series(prices):
    s = [(d, p) for d, p in zip(START, prices)]
    return sorted(s + [(D(2020, 1, 3), 999.0), (D(2020, 2, 4), 999.0)])


VALUES = {c: series(p) for c, p in PRICES.items()}


def test_three_months_hand_computed_end_to_end():
    months = bk.run_months(VALUES, rate_on)
    assert [(m.start, m.end) for m in months] == [(START[0], START[1]), (START[1], START[2]),
                                                  (START[2], START[3])]       # the last day starts no month
    # --- January (32 nights): USD 2.0, NZD 1.7, AUD 1.5 long; CHF -0.5, JPY -0.1, EUR 0.5 short
    m1 = months[0]
    assert m1.basket == bk.Basket(("USD", "NZD", "AUD"), ("CHF", "JPY", "EUR"))
    # carry (rate - 2.0) / 1200: NZD -0.00025, AUD -0.000416667, CHF -0.002083333, JPY -0.00175, EUR -0.00125
    # longs: USD 0, NZD 0.01 - 0.00025 = 0.00975, AUD -0.01 - 0.000416667 = -0.010416667 -> mean -0.000222222
    # shorts: CHF 0.02 - 0.002083333 = 0.017916667, JPY -0.00175, EUR 0.01 - 0.00125 = 0.00875 -> mean 0.008305556
    assert m1.gross == approx(-0.000222222222 - 0.008305555556)
    assert m1.nights == 32 and m1.events == 6
    assert m1.cost == approx(6 * 0.0003 / 3 + 32 * 0.00008)               # 0.0006 + 0.00256 = 0.00316
    assert m1.net == approx(-0.008527777778 - 0.00316)
    # --- February (28 nights), same basket, prices flat: only the carry
    m2 = months[1]
    assert m2.basket == m1.basket and m2.events == 0 and m2.nights == 28
    # longs: 0, -0.00025, -0.000416667 -> mean -0.000222222; shorts: -0.002083333, -0.00175, -0.00125 -> -0.001694444
    assert m2.gross == approx(-0.000222222222 + 0.001694444444)
    assert m2.cost == approx(28 * 0.00008)
    assert m2.net == approx(0.001472222222 - 0.00224)
    # --- March (30 nights): EUR's rate rose to 3.0: EUR 3.0, USD 2.0, NZD 1.7 long; CHF, JPY, GBP 1.0 short
    m3 = months[2]
    assert m3.basket == bk.Basket(("EUR", "USD", "NZD"), ("CHF", "JPY", "GBP"))
    assert m3.events == 4 and m3.nights == 30      # AUD leaves, EUR changes side (2), GBP enters
    # longs: EUR 0.02 + 1/1200 = 0.020833333, USD 0, NZD -0.00025 -> mean 0.006861111
    # shorts: CHF -0.002083333, JPY -0.00175, GBP -0.01 + (1.0 - 2.0) / 1200 = -0.010833333 -> mean -0.004888889
    assert m3.gross == approx(0.006861111111 + 0.004888888889)
    assert m3.cost == approx(4 * 0.0001 + 30 * 0.00008)                   # 0.0004 + 0.0024
    assert m3.net == approx(0.01175 - 0.0028)


def test_a_currency_without_a_price_is_left_out_of_the_ranking():
    values = {c: s for c, s in VALUES.items() if c != "NZD"}              # NZD cannot trade: CAD takes its place
    m = bk.run_months(values, rate_on)[0]
    assert "NZD" not in m.basket.legs
    assert m.basket.longs == ("USD", "AUD", "CAD") and m.basket.shorts == ("CHF", "JPY", "EUR")


def test_a_series_that_ends_before_the_month_does_is_left_out_for_that_month():
    values = dict(VALUES)
    values["AUD"] = [(d, p) for d, p in VALUES["AUD"] if d <= D(2020, 2, 3)]       # ends on Feb 3
    months = bk.run_months(values, rate_on)
    assert "AUD" in months[0].basket.legs                                        # January ends Feb 3: covered
    assert "AUD" not in months[1].basket.legs and "AUD" not in months[2].basket.legs


def test_a_day_that_one_pair_lacks_uses_its_last_price_before_it():
    values = dict(VALUES)
    values["NZD"] = [(D(2020, 1, 2), 0.60), (D(2020, 1, 31), 0.606), (D(2020, 3, 2), 0.606),
                     (D(2020, 4, 1), 0.606)]                                      # no bar on Feb 3
    m = bk.run_months(values, rate_on)[0]
    assert m.basket.longs == ("USD", "NZD", "AUD")
    assert m.gross == approx(-0.000222222222 - 0.008305555556)                   # the same +1 % from the Jan 31 bar


def test_fewer_than_six_currencies_means_no_basket_that_month():
    def few(c, day):
        return None if c in ("EUR", "GBP", "JPY") and day < D(2020, 2, 15) else rate_on(c, day)
    months = bk.run_months(VALUES, few)
    # January (01-02) and February (02-03) have five rates (USD, AUD, CAD, CHF, NZD): no month. March has eight.
    assert [m.start for m in months] == [START[2]]
    assert months[0].events == 6                                                  # nothing was held before


def test_without_a_usd_rate_no_carry_can_be_measured_and_there_is_no_month():
    def no_usd(c, day):
        return None if c == "USD" and day < D(2020, 3, 1) else rate_on(c, day)
    assert [m.start for m in bk.run_months(VALUES, no_usd)] == [START[2]]


def test_after_a_month_without_a_basket_the_turnover_is_against_the_last_one_held():
    def gap(c, day):                                    # February has five rates only
        return None if c in ("EUR", "GBP", "JPY") and D(2020, 2, 1) <= day < D(2020, 3, 1) else rate_on(c, day)
    months = bk.run_months(VALUES, gap)
    assert [m.start for m in months] == [START[0], START[2]]
    # March's basket against January's (EUR changed side and AUD left, GBP entered): 4 events, not 6
    assert months[1].events == 4


def test_no_data_no_months():
    assert bk.run_months({}, rate_on) == []
    assert bk.run_months({c: [(D(2020, 1, 2), 1.0)] for c in PRICES}, rate_on) == []     # one day: no complete month


# ================================================================== the statistics
R = [0.02, -0.01, 0.03, -0.04, 0.01, 0.02]


def test_month_statistics_hand_computed():
    s = bk.month_stats(R)
    assert s.n == 6 and s.win_rate == approx(4 / 6)
    assert s.profit_factor == approx(0.08 / 0.05)                 # (0.02 + 0.03 + 0.01 + 0.02) / |-0.01 - 0.04|
    assert s.mean == approx(0.005)
    assert s.total == approx(0.03)
    # deviations from 0.005: 0.015 -0.015 0.025 -0.045 0.005 0.015 -> squares sum 0.00335; / 5 = 0.00067
    sd = math.sqrt(0.00067)
    assert s.t_stat == approx(0.005 / (sd / math.sqrt(6)))
    assert s.t_stat == pytest.approx(0.4732, abs=1e-4)


def test_the_worst_peak_to_trough_is_in_percent_of_the_compounded_curve():
    # equity 1.02, 1.0098, 1.040094 (peak), then -4 % -> 0.99849024: (1.040094 - 0.99849024) / 1.040094 = 4.0 %
    assert bk.month_stats(R).max_drawdown_pct == approx(4.0)
    # from the start: -10 % is a 10 % drop; a later recovery does not erase it
    assert bk.month_stats([-0.10, 0.20]).max_drawdown_pct == approx(10.0)
    # two losses in a row compound: 1 - 0.9 * 0.8 = 28 %
    assert bk.month_stats([-0.10, -0.20]).max_drawdown_pct == approx(28.0)
    assert bk.month_stats([0.01, 0.02]).max_drawdown_pct == 0.0


def test_a_series_without_losses_has_an_infinite_profit_factor_and_one_without_wins_a_zero_one():
    assert bk.month_stats([0.01, 0.02]).profit_factor == math.inf
    assert bk.month_stats([-0.01, -0.02]).profit_factor == 0.0
    assert bk.month_stats([0.0, 0.0]).profit_factor == 0.0


def test_t_is_zero_without_a_spread_or_with_under_two_months_and_empty_is_all_zero():
    assert bk.month_stats([0.01, 0.01, 0.01]).t_stat == 0.0
    assert bk.month_stats([0.05]).t_stat == 0.0
    assert bk.month_stats([]) == bk.MonthStats(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def test_stats_of_months_uses_the_net_returns_and_by_year_groups_by_the_start_year():
    b = bk.Basket(("A", "B", "C"), ("D", "E", "F"))

    def month(y, mo, net):
        return bk.Month(D(y, mo, 1), D(y, mo, 28), b, 27, 0, net + 0.001, 0.001, net)
    months = [month(2019, 12, 0.02), month(2020, 1, -0.01), month(2020, 2, 0.03)]
    assert bk.stats_of(months) == bk.month_stats([0.02, -0.01, 0.03])
    years = bk.by_year(months)
    assert [y for y, _ in years] == [2019, 2020]
    assert years[1][1] == bk.month_stats([-0.01, 0.03])
