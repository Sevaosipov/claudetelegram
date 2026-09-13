"""Signal features, ranking and the backtest's statistics.

These cover the layer added to answer "which of these signals deserves attention",
plus the machinery that checks whether that ranking means anything.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

import backtest
import cluster
import db
from conftest import add_sec_purchase, add_stake

TODAY = dt.date.today()
RECENT = (TODAY - dt.timedelta(days=2)).isoformat()


# ----------------------------------------------------------- 10b5-1 exclusion
def test_10b5_1_purchases_are_excluded_by_default(conn):
    """Arranged months in advance on a fixed schedule, so it says nothing about
    what the insider thinks now."""
    add_sec_purchase(conn, "AAA", "Planner One", 400_000, RECENT, is_10b5_1=1)
    add_sec_purchase(conn, "AAA", "Planner Two", 400_000, RECENT, is_10b5_1=1)
    assert cluster.find_sec_clusters(conn) == []
    assert len(cluster.find_sec_clusters(conn, include_10b5_1=True)) == 1


def test_discretionary_buys_still_cluster_alongside_10b5_1_ones(conn):
    add_sec_purchase(conn, "AAA", "Planner", 400_000, RECENT, is_10b5_1=1)
    add_sec_purchase(conn, "AAA", "Decider One", 400_000, RECENT)
    add_sec_purchase(conn, "AAA", "Decider Two", 400_000, RECENT)
    sig = cluster.find_sec_clusters(conn)[0]
    assert sig.buyer_count == 2 and "Planner" not in sig.member_names


# ------------------------------------------------------------ position change
def test_position_increase_is_relative_to_prior_holding(conn):
    """Adding 100 shares to 900 held is a 11% increase, not the same event as
    adding 100 when you held nothing."""
    add_sec_purchase(conn, "AAA", "Topper", 600_000, RECENT, shares=100, shares_owned_after=1000)
    sig = cluster.find_sec_clusters(conn)[0]
    assert sig.position_increase_pct == pytest.approx(100 / 900 * 100, rel=0.01)


def test_brand_new_position_reports_100_percent(conn):
    """Held nothing before. Reported as a new position rather than as an infinite
    increase, which would poison any ranking that used it."""
    add_sec_purchase(conn, "AAA", "Starter", 600_000, RECENT, shares=500, shares_owned_after=500)
    assert cluster.find_sec_clusters(conn)[0].position_increase_pct == 100.0


# --------------------------------------------------------------- disclosure lag
def test_lag_is_measured_from_trade_to_filing(conn):
    add_sec_purchase(conn, "AAA", "Buyer", 600_000, "2026-09-01", filed_date="2026-09-03")
    assert cluster.find_sec_clusters(conn)[0].lag_days == 2


def test_missing_filing_date_yields_no_lag(conn):
    add_sec_purchase(conn, "AAA", "Buyer", 600_000, RECENT, filed_date="")
    assert cluster.find_sec_clusters(conn)[0].lag_days is None


# ------------------------------------------------------------------ first buy
def test_first_buy_is_not_claimed_on_a_shallow_database(conn):
    """On a fresh database every purchase is a first purchase. Firing the feature
    on everything would make it mean nothing."""
    add_sec_purchase(conn, "AAA", "Buyer", 600_000, RECENT)
    assert cluster.find_sec_clusters(conn)[0].first_buy is False


def test_first_buy_fires_once_there_is_history(conn):
    old = (TODAY - dt.timedelta(days=400)).isoformat()
    add_sec_purchase(conn, "ZZZ", "Someone Else", 100_000, old)   # gives the DB depth
    add_sec_purchase(conn, "AAA", "Newcomer", 600_000, RECENT)
    assert cluster.find_sec_clusters(conn)[0].first_buy is True


def test_not_a_first_buy_when_the_person_bought_before(conn):
    old = (TODAY - dt.timedelta(days=400)).isoformat()
    add_sec_purchase(conn, "AAA", "Regular", 100_000, old)
    add_sec_purchase(conn, "AAA", "Regular", 600_000, RECENT)
    assert cluster.find_sec_clusters(conn)[0].first_buy is False


# --------------------------------------------------------------------- scoring
def _signal(**kw):
    base = dict(source="SEC", ticker="AAA", company="Test", buyer_count=1,
                total_value=1_000_000, members=[], window_start="", window_end="")
    base.update(kw)
    return cluster.ClusterSignal(**base)


def test_more_buyers_scores_higher(conn):
    assert cluster.score_signal(_signal(buyer_count=3)) > cluster.score_signal(_signal(buyer_count=1))


def test_holders_only_scores_lower_than_an_officer_present(conn):
    assert cluster.score_signal(_signal(holder_only=True)) < cluster.score_signal(_signal(holder_only=False))


def test_illiquid_names_are_penalised(conn):
    liquid = _signal(avg_daily_value=50_000_000)
    thin = _signal(avg_daily_value=10_000)
    assert cluster.score_signal(thin) < cluster.score_signal(liquid)


def test_stale_disclosure_scores_below_a_fresh_one(conn):
    assert cluster.score_signal(_signal(lag_days=45)) < cluster.score_signal(_signal(lag_days=1))


def test_one_bad_input_cannot_dominate_the_ranking(conn):
    """Regression. An unbounded market-cap term let a nano-cap whose purchase came
    out at 226% of its market cap score 18,161 against a normal range of 20-60 --
    a single bad quote sorted the whole list."""
    absurd = _signal(value_pct_of_mcap=226.0, market_cap_eur=3_000_000)
    sane = _signal(buyer_count=4, value_pct_of_mcap=0.5, market_cap_eur=1e9, lag_days=1)
    assert cluster.score_signal(absurd) < cluster.score_signal(sane)
    assert cluster.score_signal(absurd) < 100


def test_every_component_is_bounded(conn):
    """Whatever the inputs, the score stays in a range a human can reason about."""
    extreme = _signal(buyer_count=500, value_pct_of_mcap=99.0, position_increase_pct=1000.0,
                      first_buy=True, lag_days=0, market_cap_eur=1e9,
                      avg_daily_value=1e9)
    assert cluster.score_signal(extreme) < 250


def test_activist_stake_outranks_a_passive_one_of_equal_size(conn):
    a = cluster.StakeSignal(source="SEC13DG", ticker="A", company="C", person="P",
                             form_type="SCHEDULE 13D", percent=9.0, prev_percent=None,
                             amount_owned=1, event_date="", url="")
    g = cluster.StakeSignal(source="SEC13DG", ticker="A", company="C", person="P",
                             form_type="SCHEDULE 13G", percent=9.0, prev_percent=None,
                             amount_owned=1, event_date="", url="")
    assert cluster.score_signal(a) > cluster.score_signal(g)


# ------------------------------------------------------------------- journal
def test_journalled_signal_round_trips(conn):
    import bot
    add_sec_purchase(conn, "AAA", "Buyer One", 300_000, RECENT)
    add_sec_purchase(conn, "AAA", "Buyer Two", 300_000, RECENT)
    sig = cluster.find_sec_clusters(conn)[0]
    db.journal_signal(conn, bot._signal_features(sig))
    row = conn.execute("SELECT source, kind, ticker, buyer_count FROM signal_journal").fetchone()
    assert row == ("SEC", "cluster", "AAA", 2)


def test_corroborated_by_round_trips_through_the_journal(conn):
    import bot
    add_sec_purchase(conn, "AAA", "Buyer One", 300_000, RECENT)
    add_sec_purchase(conn, "AAA", "Buyer Two", 300_000, RECENT)
    sig = cluster.find_sec_clusters(conn)[0]
    sig.corroborated_by = ["SENATE", "BAFIN"]
    db.journal_signal(conn, bot._signal_features(sig))
    row = conn.execute("SELECT corroborated_by FROM signal_journal").fetchone()
    assert json.loads(row[0]) == ["SENATE", "BAFIN"]


# ------------------------------------------------------------------- backtest
@pytest.mark.parametrize("wins,n,expected", [(5, 10, 1.0), (10, 10, pytest.approx(0.00195, rel=0.1))])
def test_sign_test(wins, n, expected):
    assert backtest.sign_test_p(wins, n) == expected


def test_small_groups_are_flagged_as_unreadable():
    rows = [{"return": 5.0, "excess": 4.0}] * 5
    assert backtest.summarise(rows, 21)["meaningful"] is False


def test_large_groups_are_not_flagged():
    rows = [{"return": 5.0, "excess": 4.0}] * backtest.MIN_MEANINGFUL_N
    stats = backtest.summarise(rows, 21)
    assert stats["meaningful"] is True and stats["hit_rate"] == 100.0


def test_summarise_ignores_rows_without_a_benchmark():
    rows = [{"return": 5.0, "excess": None}, {"return": 1.0, "excess": 2.0}]
    assert backtest.summarise(rows, 21)["n"] == 1


# ------------------------------------------------------- exit signal scoring
#
# score_signal special-cases StakeSignal (via hasattr(sig, "percent")) but, until
# now, nothing special-cased ExitSignal -- it fell through to the general path,
# which reads sig.buyer_count and other ClusterSignal-only fields. ExitSignal has
# neither, so enrich_signals() crashed the instant a real exit signal appeared
# (there just hadn't been one in the recorded data yet).

def _exit(**kw):
    base = dict(source="SEC", ticker="AAA", company="Test", total_buyers=3,
                seller_count=1, lines=[])
    base.update(kw)
    return cluster.ExitSignal(**base)


def test_score_signal_does_not_crash_on_an_exit_signal():
    cluster.score_signal(_exit())  # must not raise


def test_exit_signal_more_sellers_scores_higher():
    assert cluster.score_signal(_exit(seller_count=2)) > cluster.score_signal(_exit(seller_count=1))


def test_exit_signal_full_unwind_scores_above_a_partial_one():
    partial = cluster.score_signal(_exit(total_buyers=3, seller_count=2))
    full = cluster.score_signal(_exit(total_buyers=3, seller_count=3))
    assert full > partial


def test_enrich_signals_handles_a_mixed_batch_including_an_exit_signal(conn):
    """The real crash: enrich_signals() runs over whatever run_cluster_pass
    assembled, cluster/stake/exit signals together, in one pass."""
    signals = cluster.enrich_signals(conn, [_signal(), _exit()])
    assert all(hasattr(s, "score") for s in signals)


# ----------------------------------------------------------- corroboration
def test_corroboration_bonus_is_added_to_the_base_score():
    plain = cluster.score_signal(_signal())
    corroborated = cluster.score_signal(_signal(), corroborated_by=["SENATE"])
    assert corroborated > plain


def test_corroboration_bonus_scales_with_distinct_sources_then_caps():
    one = cluster.score_signal(_signal(), corroborated_by=["SENATE"])
    two = cluster.score_signal(_signal(), corroborated_by=["SENATE", "BAFIN"])
    many = cluster.score_signal(_signal(), corroborated_by=["SENATE", "BAFIN", "NORWAY", "SWEDEN"])
    assert one < two
    assert two == many  # capped


def test_corroboration_defaults_to_no_bonus():
    assert cluster.score_signal(_signal()) == cluster.score_signal(_signal(), corroborated_by=None)
    assert cluster.score_signal(_signal()) == cluster.score_signal(_signal(), corroborated_by=[])


def test_corroboration_bonus_applies_to_stake_and_exit_signals_too():
    stake = cluster.StakeSignal(source="SEC13DG", ticker="A", company="C", person="P",
                                form_type="SCHEDULE 13D", percent=9.0, prev_percent=None,
                                amount_owned=1, event_date="", url="")
    assert cluster.score_signal(stake, corroborated_by=["SENATE"]) > cluster.score_signal(stake)
    assert (cluster.score_signal(_exit(), corroborated_by=["SENATE"])
            > cluster.score_signal(_exit()))


def test_corroboration_bonus_stays_within_the_existing_bound(conn):
    """test_every_component_is_bounded's <250 ceiling, plus the capped
    corroboration bonus (30) on top, must still be a sane, bounded number."""
    extreme = _signal(buyer_count=500, value_pct_of_mcap=99.0, position_increase_pct=1000.0,
                      first_buy=True, lag_days=0, market_cap_eur=1e9, avg_daily_value=1e9)
    score = cluster.score_signal(extreme, corroborated_by=["SEC", "SENATE", "HOUSE", "BAFIN", "NORWAY"])
    assert score < 280


def test_enrich_signals_sets_corroboration_and_bonus_score(conn):
    a = _signal(ticker="AAA", source="SEC")
    b = cluster.ClusterSignal(source="SENATE", ticker="AAA", company="Test", buyer_count=1,
                              total_value=100_000, members=[], window_start="", window_end="")
    signals = cluster.enrich_signals(conn, [a, b])
    by_source = {s.source: s for s in signals}
    assert by_source["SEC"].corroborated_by == ["SENATE"]
    assert by_source["SENATE"].corroborated_by == ["SEC"]
    assert by_source["SEC"].score > cluster.score_signal(_signal(ticker="AAA", source="SEC"))
