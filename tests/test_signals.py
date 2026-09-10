"""Signal and dedup logic, against a seeded in-memory database.

The cases below are mostly regressions: each one corresponds to a way the bot was
either alerting on something meaningless or -- worse, because it is invisible --
permanently suppressing something real.
"""
from __future__ import annotations

import datetime as dt

import pytest

import bot
import cluster
import db
from conftest import add_form_144, add_sec_purchase, add_sec_sale, add_stake

TODAY = dt.date.today()
RECENT = (TODAY - dt.timedelta(days=2)).isoformat()
LATER = (TODAY - dt.timedelta(days=1)).isoformat()


# ------------------------------------------------------------ cluster basics
def test_single_small_buyer_is_not_a_signal(conn):
    add_sec_purchase(conn, "AAA", "Buyer One", 120_000, RECENT)
    assert cluster.find_sec_clusters(conn) == []


def test_two_buyers_make_a_cluster(conn):
    add_sec_purchase(conn, "AAA", "Buyer One", 120_000, RECENT)
    add_sec_purchase(conn, "AAA", "Buyer Two", 120_000, RECENT)
    signals = cluster.find_sec_clusters(conn)
    assert len(signals) == 1
    assert signals[0].reason == "cluster"
    assert signals[0].buyer_count == 2
    assert set(signals[0].member_names) == {"Buyer One", "Buyer Two"}


def test_one_large_buyer_is_a_solo_signal(conn):
    add_sec_purchase(conn, "AAA", "Whale", 5_000_000, RECENT)
    signals = cluster.find_sec_clusters(conn)
    assert len(signals) == 1 and signals[0].reason == "solo"


def test_repeat_purchases_by_one_person_are_summed(conn):
    add_sec_purchase(conn, "AAA", "Whale", 400_000, RECENT)
    add_sec_purchase(conn, "AAA", "Whale", 400_000, LATER)
    signals = cluster.find_sec_clusters(conn)
    assert len(signals) == 1, "two buys by one person should aggregate past the solo bar"
    assert signals[0].buyer_count == 1


def test_junk_ticker_never_becomes_a_cluster_key(conn):
    """A non-traded fund files issuerTradingSymbol as the literal "NONE"; that value
    once grouped unrelated issuers together and produced a live alert."""
    add_sec_purchase(conn, "NONE", "Buyer One", 2_000_000, RECENT, issuer="Some Fund LP")
    add_sec_purchase(conn, "NONE", "Buyer Two", 2_000_000, RECENT, issuer="Other Fund LP")
    assert cluster.find_sec_clusters(conn) == []


# --------------------------------------------------------------- derivatives
def test_derivatives_excluded_from_totals_by_default(conn):
    """A code-P row on the derivative table is priced at a strike, not at what the
    stock costs, so summing it with common stock distorts the headline value."""
    add_sec_purchase(conn, "AAA", "Buyer One", 120_000, RECENT)
    add_sec_purchase(conn, "AAA", "Buyer Two", 9_000_000, RECENT, derivative=1)
    assert cluster.find_sec_clusters(conn) == [], "derivative row should not create a cluster"
    included = cluster.find_sec_clusters(conn, include_derivatives=True)
    assert len(included) == 1 and included[0].buyer_count == 2


# ------------------------------------------------------------- who is buying
def test_holder_only_cluster_is_flagged(conn):
    """An institution adding to a >10% stake and the people running the company
    buying are different events that used to render identically."""
    add_sec_purchase(conn, "AAA", "Big Fund LLC", 5_000_000, RECENT,
                     director=0, officer=0, ten_pct=1)
    sig = cluster.find_sec_clusters(conn)[0]
    assert sig.holder_only is True
    assert "10%+ Owner" in sig.members[0]


def test_insiders_only_drops_pure_holders(conn):
    add_sec_purchase(conn, "AAA", "Big Fund LLC", 5_000_000, RECENT,
                     director=0, officer=0, ten_pct=1)
    add_sec_purchase(conn, "BBB", "Real CEO", 5_000_000, RECENT,
                     director=0, officer=1, title="CEO")
    tickers = {s.ticker for s in cluster.find_sec_clusters(conn, insiders_only=True)}
    assert tickers == {"BBB"}


# ------------------------------------------------------------------- dedup
def test_alerted_cluster_does_not_repeat(conn):
    add_sec_purchase(conn, "AAA", "Buyer One", 120_000, RECENT)
    add_sec_purchase(conn, "AAA", "Buyer Two", 120_000, RECENT)
    sig = cluster.find_sec_clusters(conn)[0]
    cluster.commit_alert(conn, sig)
    assert cluster.find_sec_clusters(conn) == []


def test_new_participant_refires_even_when_headcount_falls(conn):
    """The bug this replaced a count-only rule to fix. The cluster window is
    rolling, so buyers age out of it: once a ticker had been alerted at four
    buyers, a fresh cluster of three *different* insiders months later was
    suppressed forever, because 3 <= 4."""
    db.save_cluster_alert_state(conn, "SEC", "AAA", 4,
                                ["Old One", "Old Two", "Old Three", "Old Four"], 1_000_000)
    add_sec_purchase(conn, "AAA", "New One", 200_000, RECENT)
    add_sec_purchase(conn, "AAA", "New Two", 200_000, RECENT)
    signals = cluster.find_sec_clusters(conn)
    assert len(signals) == 1, "a cluster of entirely new people must re-fire"


def test_solo_whale_refires_when_the_position_grows(conn):
    """A solo buyer's count is 1 and stays 1 however much more they buy, so under a
    count-only rule they could never re-fire at all."""
    add_sec_purchase(conn, "AAA", "Whale", 1_000_000, RECENT)
    cluster.commit_alert(conn, cluster.find_sec_clusters(conn)[0])
    assert cluster.find_sec_clusters(conn) == []
    add_sec_purchase(conn, "AAA", "Whale", 1_000_000, LATER)  # position doubles
    assert len(cluster.find_sec_clusters(conn)) == 1


def test_member_names_with_commas_survive_a_round_trip(conn):
    """Names routinely contain commas -- "CASCADE INVESTMENT, L.L.C." -- which a
    comma-joined store split into two, so the set never matched and the signal
    re-fired on every run forever."""
    name = "CASCADE INVESTMENT, L.L.C."
    db.save_cluster_alert_state(conn, "SEC", "AAA", 1, [name], 500_000)
    assert db.get_alert_state(conn, "SEC", "AAA")["members"] == {name}
    assert not cluster.should_alert(conn, "SEC", "AAA", [name], 500_000)


# -------------------------------------------------------------- exit signals
def test_exit_signal_requires_half_the_buyers_to_sell(conn):
    for i in range(4):
        add_sec_purchase(conn, "AAA", f"Buyer {i}", 200_000, "2026-01-10")
    add_sec_sale(conn, "AAA", "Buyer 0", 200_000, "2026-05-10")
    assert cluster.find_sec_exit_signals(conn) == [], "one seller out of four is not an exit"
    add_sec_sale(conn, "AAA", "Buyer 1", 200_000, "2026-05-10")
    assert len(cluster.find_sec_exit_signals(conn)) == 1


def test_form_144_counts_as_an_early_exit(conn):
    """A Form 144 is filed before the sale, and so before the Form 4 recording it --
    which is the entire reason for collecting it."""
    for i in range(2):
        add_sec_purchase(conn, "AAA", f"Buyer {i}", 200_000, "2026-01-10")
    add_form_144(conn, "AAA", "Buyer 0", 500_000, "2026-05-10")
    add_form_144(conn, "AAA", "Buyer 1", 500_000, "2026-05-11")
    signals = cluster.find_sec_exit_signals(conn)
    assert len(signals) == 1
    assert "Form 144" in " ".join(signals[0].lines)
    assert cluster.find_sec_exit_signals(conn, include_form_144=False) == []


def test_small_form_144s_are_ignored(conn):
    for i in range(2):
        add_sec_purchase(conn, "AAA", f"Buyer {i}", 200_000, "2026-01-10")
        add_form_144(conn, "AAA", f"Buyer {i}", 1_000, "2026-05-10")
    assert cluster.find_sec_exit_signals(conn) == []


def test_form_144_matches_a_name_written_the_other_way_round(conn):
    """Form 4 files names surname-first and upper-cased; Form 144 takes whatever the
    filer typed. Matching them literally would find essentially nothing."""
    add_sec_purchase(conn, "AAA", "CHEN SEAN", 200_000, "2026-01-10")
    add_sec_purchase(conn, "AAA", "SMITH JOHN A", 200_000, "2026-01-10")
    add_form_144(conn, "AAA", "Sean Chen", 500_000, "2026-05-10")
    add_form_144(conn, "AAA", "John A. Smith", 500_000, "2026-05-10")
    assert len(cluster.find_sec_exit_signals(conn)) == 1


@pytest.mark.parametrize("a,b", [
    ("CROOK NATHANIEL GLENN", "Crook Nathaniel Glenn"),
    ("SEAN CHEN", "Chen Sean"),
    ("Smith Jr., John A.", "JOHN A SMITH JR"),
])
def test_name_key_matches_equivalent_spellings(a, b):
    assert cluster.name_key(a) == cluster.name_key(b)


def test_name_key_keeps_different_people_apart():
    assert cluster.name_key("John Smith") != cluster.name_key("Jane Smith")


def test_name_key_does_not_merge_people_who_differ_only_by_initial():
    """Middle initials are kept precisely so two relatives don't collapse into one
    key: a false match here fabricates an exit signal claiming someone sold."""
    assert cluster.name_key("Smith John A") != cluster.name_key("Smith John B")


# ------------------------------------------------------------- stake signals
def test_new_activist_stake_is_a_signal(conn):
    add_stake(conn, "AAA", "Activist Fund LP", 12.5)
    signals = cluster.find_stake_signals(conn)
    assert len(signals) == 1
    assert signals[0].percent == 12.5 and signals[0].is_activist


def test_stake_below_the_filing_threshold_is_ignored(conn):
    add_stake(conn, "AAA", "Fund", 3.0)
    assert cluster.find_stake_signals(conn) == []


def test_trivial_stake_increase_is_not_news(conn):
    """Index funds file 13G/A amendments constantly over fractions of a point."""
    add_stake(conn, "AAA", "Index Fund", 9.10, form_type="SCHEDULE 13G",
              event_date="2026-08-01")
    add_stake(conn, "AAA", "Index Fund", 9.25, form_type="SCHEDULE 13G/A",
              event_date="2026-09-01")
    assert cluster.find_stake_signals(conn) == []


def test_material_stake_increase_is_news(conn):
    add_stake(conn, "AAA", "Activist Fund", 6.0, event_date="2026-08-01")
    add_stake(conn, "AAA", "Activist Fund", 14.0, form_type="SCHEDULE 13D/A",
              event_date="2026-09-01")
    signals = cluster.find_stake_signals(conn)
    assert len(signals) == 1 and signals[0].prev_percent == 6.0


def test_stake_alert_state_is_per_holder(conn):
    """Two unrelated funds building stakes in the same company are two separate
    pieces of news; one must not mask the other."""
    add_stake(conn, "AAA", "Fund One", 8.0)
    add_stake(conn, "AAA", "Fund Two", 9.0)
    signals = cluster.find_stake_signals(conn)
    assert len(signals) == 2
    for s in signals:
        cluster.commit_stake_alert(conn, s)
    assert cluster.find_stake_signals(conn) == []


def test_activist_only_skips_passive_filers(conn):
    add_stake(conn, "AAA", "Index Fund", 9.0, form_type="SCHEDULE 13G")
    add_stake(conn, "BBB", "Activist", 9.0, form_type="SCHEDULE 13D")
    tickers = {s.ticker for s in cluster.find_stake_signals(conn, activist_only=True)}
    assert tickers == {"BBB"}


# ------------------------------------------------------- scanned-days ledger
def test_unscanned_days_are_returned_and_today_is_always_included(conn):
    days = bot._days_to_scan(conn, "SEC", lookback_days=4, max_new_days=5)
    assert TODAY in days
    assert len(days) == 4


def test_scanned_days_are_skipped(conn):
    yesterday = TODAY - dt.timedelta(days=1)
    db.mark_day_scanned(conn, "SEC", yesterday.isoformat(), 100)
    days = bot._days_to_scan(conn, "SEC", lookback_days=3, max_new_days=5)
    assert yesterday not in days
    assert TODAY in days


def test_backfill_is_capped_per_run(conn):
    """A ten-day outage must not turn into one enormous run; the rest drains over
    the following runs, because each run records what it scanned."""
    days = bot._days_to_scan(conn, "SEC", lookback_days=10, max_new_days=2)
    assert len(days) == 3, "two backlog days plus today"
    assert TODAY in days


def test_today_is_never_marked_scanned(conn):
    """Today's index is still being published, so marking it done would freeze the
    day's remaining filings out permanently."""
    bot._scan_day(conn, "SEC", TODAY, TODAY, lambda: 5)
    assert TODAY.isoformat() not in db.scanned_days(conn, "SEC")


def test_a_failed_day_is_left_unscanned_for_retry(conn):
    """SEC answers 403 for a day with no index but 503 when throttling; the second
    must come back on the next run rather than being recorded as empty."""
    import requests
    yesterday = TODAY - dt.timedelta(days=1)

    def boom():
        raise requests.RequestException("503")

    assert bot._scan_day(conn, "SEC", yesterday, TODAY, boom) is None
    assert yesterday.isoformat() not in db.scanned_days(conn, "SEC")


# --------------------------------------------------------- source liveness
def test_silent_source_is_reported_after_enough_empty_runs(conn):
    """A broken parser and a quiet source both produce zero rows; this is the only
    thing that tells them apart."""
    for _ in range(3):
        assert bot.check_source_liveness(conn, {"BAFIN": 0}, threshold=4) == []
    stale = bot.check_source_liveness(conn, {"BAFIN": 0}, threshold=4)
    assert stale and "BAFIN" in stale[0]


def test_a_productive_run_resets_the_streak(conn):
    for _ in range(3):
        bot.check_source_liveness(conn, {"BAFIN": 0}, threshold=4)
    bot.check_source_liveness(conn, {"BAFIN": 7}, threshold=4)
    assert bot.check_source_liveness(conn, {"BAFIN": 0}, threshold=4) == []


# ------------------------------------------------------------------- Senate
def test_senate_cluster_uses_the_same_logic_as_the_house(conn):
    """Both chambers file under the same STOCK Act rules in the same amount
    brackets, so they share the finder -- only the table, the source label and the
    stored date format differ."""
    from conftest import add_senate_txn
    add_senate_txn(conn, "AAA", "Senator One", "$100,001 - $250,000", RECENT)
    add_senate_txn(conn, "AAA", "Senator Two", "$100,001 - $250,000", RECENT)
    signals = cluster.find_senate_clusters(conn)
    assert len(signals) == 1
    assert signals[0].source == "SENATE"
    assert signals[0].buyer_count == 2


def test_senate_iso_dates_are_windowed_correctly(conn):
    """The Senate table stores ISO dates while the House stores M/D/YYYY; parsing
    one with the other's format silently drops every row."""
    from conftest import add_senate_txn
    old = (TODAY - dt.timedelta(days=400)).isoformat()
    add_senate_txn(conn, "AAA", "Senator One", "$100,001 - $250,000", old)
    add_senate_txn(conn, "AAA", "Senator Two", "$100,001 - $250,000", old)
    assert cluster.find_senate_clusters(conn) == [], "trades outside the window must not cluster"
    add_senate_txn(conn, "BBB", "Senator One", "$100,001 - $250,000", RECENT)
    add_senate_txn(conn, "BBB", "Senator Two", "$100,001 - $250,000", RECENT)
    assert [s.ticker for s in cluster.find_senate_clusters(conn)] == ["BBB"]


def test_senate_and_house_alert_state_do_not_collide(conn):
    """Same ticker, two chambers: alerting on one must not silence the other."""
    from conftest import add_senate_txn, add_sec_purchase  # noqa: F401
    add_senate_txn(conn, "AAA", "Senator One", "$100,001 - $250,000", RECENT)
    add_senate_txn(conn, "AAA", "Senator Two", "$100,001 - $250,000", RECENT)
    sig = cluster.find_senate_clusters(conn)[0]
    cluster.commit_alert(conn, sig)
    assert cluster.find_senate_clusters(conn) == []
    assert db.get_alert_state(conn, "SENATE", "AAA") is not None
    assert db.get_alert_state(conn, "HOUSE", "AAA") is None


def test_senate_exit_signal(conn):
    from conftest import add_senate_txn
    for i in range(2):
        add_senate_txn(conn, "AAA", f"Senator {i}", "$100,001 - $250,000", "2026-01-10")
    for i in range(2):
        add_senate_txn(conn, "AAA", f"Senator {i}", "$100,001 - $250,000", "2026-05-10",
                       txn_type="S")
    signals = cluster.find_senate_exit_signals(conn)
    assert len(signals) == 1 and signals[0].source == "SENATE"


def test_commit_signals_marks_every_signal_type(conn):
    """The over-limit branch commits through this too: leaving a flood uncommitted
    means warning about the same signals on every subsequent run, forever."""
    from conftest import add_senate_txn  # noqa: F401
    add_sec_purchase(conn, "AAA", "Buyer One", 200_000, RECENT)
    add_sec_purchase(conn, "AAA", "Buyer Two", 200_000, RECENT)
    add_stake(conn, "BBB", "Activist Fund", 11.0)
    for i in range(2):
        add_sec_purchase(conn, "CCC", f"Seller {i}", 200_000, "2026-01-10")
        add_sec_sale(conn, "CCC", f"Seller {i}", 200_000, "2026-05-10")

    signals = (cluster.find_sec_clusters(conn) + cluster.find_stake_signals(conn)
               + cluster.find_sec_exit_signals(conn))
    assert len(signals) == 3, "expected one of each signal type"
    bot._commit_signals(conn, signals)

    assert cluster.find_sec_clusters(conn) == []
    assert cluster.find_stake_signals(conn) == []
    assert cluster.find_sec_exit_signals(conn) == []
