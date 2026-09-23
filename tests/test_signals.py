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
import passes
import research
from conftest import add_form_144, add_house_txn, add_sec_purchase, add_sec_sale, add_senate_txn, add_stake

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


def test_stake_in_an_issuer_with_no_ticker_is_not_a_signal(conn):
    """Nothing to follow: the alert used to print the issuer's CIK as its ticker."""
    add_stake(conn, None, "Alternative Liquidity Index LP", 83.0)
    add_stake(conn, "", "Some Other Holder", 40.0)
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


def test_new_positions_only_drops_amendments_however_large(conn):
    """An existing holder's stake jumping is still an amendment, not a new
    activist showing up -- new_positions_only is a stricter bar than
    min_increase_pp, not a bigger version of the same check."""
    add_stake(conn, "AAA", "Activist Fund", 6.0, event_date="2026-08-01")
    add_stake(conn, "AAA", "Activist Fund", 40.0, form_type="SCHEDULE 13D/A",
              event_date="2026-09-01")
    assert cluster.find_stake_signals(conn, new_positions_only=True) == []


def test_new_positions_only_keeps_a_genuine_first_filing(conn):
    add_stake(conn, "AAA", "Brand New Activist", 12.5)
    signals = cluster.find_stake_signals(conn, new_positions_only=True)
    assert len(signals) == 1 and signals[0].prev_percent is None


def test_new_positions_only_drops_an_amendment_with_no_prior_local_history(conn):
    """The database only recently started tracking 13D/G filings, so for most
    holders the very first row WE have is already an amendment to a
    long-standing real position -- prev_pct being None (nothing seen before
    in our own history) must not be mistaken for "this is a new position".
    The SEC's own form_type ("SCHEDULE 13D" vs "...13D/A") is the actual
    signal, independent of how much scan history we happen to have."""
    add_stake(conn, "AAA", "Long-Standing Holder", 12.5, form_type="SCHEDULE 13D/A")
    assert cluster.find_stake_signals(conn, new_positions_only=True) == []


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


def test_find_stake_signals_merges_co_filers_of_the_same_filing(conn):
    """SEC rules require every control person in a fund's ownership chain (GP,
    LP, individual managers) to be listed as its own 'reporting person' on ONE
    filing -- without merging, one filing with N co-filers becomes N
    near-identical signals, which is exactly what flooded the digest."""
    add_stake(conn, "AAA", "Fund GP LLC", 12.5, accession="acc-1")
    add_stake(conn, "AAA", "Fund LP", 12.5, accession="acc-1")
    add_stake(conn, "AAA", "Individual Manager", 12.5, accession="acc-1")
    signals = cluster.find_stake_signals(conn)
    assert len(signals) == 1
    assert signals[0].person == "Fund GP LLC"
    assert signals[0].co_filer_names == ["Fund LP", "Individual Manager"]


def test_find_stake_signals_keeps_different_filings_separate(conn):
    """Two different filing groups on the same ticker are genuinely different
    holders, not co-filers of the same position -- must not be merged."""
    add_stake(conn, "AAA", "Fund One GP", 12.5, accession="acc-1")
    add_stake(conn, "AAA", "Fund Two GP", 9.0, accession="acc-2")
    signals = cluster.find_stake_signals(conn)
    assert len(signals) == 2
    assert all(s.co_filer_names == [] for s in signals)


def test_find_stake_signals_drops_filings_older_than_max_age_days(conn):
    """Unlike the cluster finders (which scope to window_days back from today
    by construction), sec_stakes has no natural recency window -- without
    max_age_days a stake signal can resurface a filing that's years old."""
    old = (TODAY - dt.timedelta(days=100)).isoformat()
    add_stake(conn, "AAA", "Old Fund", 12.5, event_date=old)
    assert cluster.find_stake_signals(conn, max_age_days=30) == []


def test_find_stake_signals_keeps_filings_within_max_age_days(conn):
    add_stake(conn, "AAA", "Recent Fund", 12.5, event_date=RECENT)
    signals = cluster.find_stake_signals(conn, max_age_days=30)
    assert len(signals) == 1


def test_find_stake_signals_max_age_days_defaults_to_no_filtering(conn):
    old = (TODAY - dt.timedelta(days=400)).isoformat()
    add_stake(conn, "AAA", "Old Fund", 12.5, event_date=old)
    assert len(cluster.find_stake_signals(conn)) == 1


def test_find_stake_signals_merged_group_does_not_refire_after_commit(conn):
    """Committing a merged signal must record alert-state for every co-filer,
    not just the one shown -- otherwise an un-recorded co-filer looks 'new'
    again on the very next run and the whole group re-fires anyway."""
    add_stake(conn, "AAA", "Fund GP LLC", 12.5, accession="acc-1")
    add_stake(conn, "AAA", "Fund LP", 12.5, accession="acc-1")
    signals = cluster.find_stake_signals(conn)
    cluster.commit_stake_alert(conn, signals[0])
    assert cluster.find_stake_signals(conn) == []


# ------------------------------------------------------- scanned-days ledger
def test_unscanned_days_are_returned_and_today_is_always_included(conn):
    days = passes._days_to_scan(conn, "SEC", lookback_days=4, max_new_days=5)
    assert TODAY in days
    assert len(days) == 4


def test_scanned_days_are_skipped(conn):
    yesterday = TODAY - dt.timedelta(days=1)
    db.mark_day_scanned(conn, "SEC", yesterday.isoformat(), 100)
    days = passes._days_to_scan(conn, "SEC", lookback_days=3, max_new_days=5)
    assert yesterday not in days
    assert TODAY in days


def test_backfill_is_capped_per_run(conn):
    """A ten-day outage must not turn into one enormous run; the rest drains over
    the following runs, because each run records what it scanned."""
    days = passes._days_to_scan(conn, "SEC", lookback_days=10, max_new_days=2)
    assert len(days) == 3, "two backlog days plus today"
    assert TODAY in days


def test_today_is_never_marked_scanned(conn):
    """Today's index is still being published, so marking it done would freeze the
    day's remaining filings out permanently."""
    passes._scan_day(conn, "SEC", TODAY, TODAY, lambda: 5)
    assert TODAY.isoformat() not in db.scanned_days(conn, "SEC")


def test_a_failed_day_is_left_unscanned_for_retry(conn):
    """SEC answers 403 for a day with no index but 503 when throttling; the second
    must come back on the next run rather than being recorded as empty."""
    import requests
    yesterday = TODAY - dt.timedelta(days=1)

    def boom():
        raise requests.RequestException("503")

    assert passes._scan_day(conn, "SEC", yesterday, TODAY, boom) is None
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


# ------------------------------------------------- House/Senate cluster tightness
#
# HOUSE_WINDOW_DAYS (45) is how far back to scan for source rows at all, kept wide
# to tolerate the STOCK Act's up-to-45-day filing lag. It used to also be, in
# effect, how close together a cluster's members had to have bought -- which meant
# two members buying the same ticker 40 days apart, with no relation to each
# other, still read as a "cluster" as long as both landed somewhere in the scan.
# HOUSE_CLUSTER_SPAN_DAYS (14) is the fix: members must buy within that span OF
# EACH OTHER, not just somewhere within the wider lookback.

def _house_date(days_ago: int) -> str:
    d = TODAY - dt.timedelta(days=days_ago)
    return f"{d.month:02d}/{d.day:02d}/{d.year}"


def test_tight_purchase_window_finds_the_earliest_qualifying_window():
    d0 = dt.date(2026, 1, 1)
    dated = [
        (d0, "A"),
        (d0 + dt.timedelta(days=40), "B"),   # far from A -- not a match with A
        (d0 + dt.timedelta(days=41), "C"),   # within 14 days of B -- B+C qualify
    ]
    start, end, names = cluster._tight_purchase_window(dated, span_days=14, min_buyers=2)
    assert (start, end) == (d0 + dt.timedelta(days=40), d0 + dt.timedelta(days=41))
    assert names == {"B", "C"}


def test_tight_purchase_window_none_when_nothing_qualifies():
    d0 = dt.date(2026, 1, 1)
    dated = [(d0, "A"), (d0 + dt.timedelta(days=30), "B")]
    assert cluster._tight_purchase_window(dated, span_days=14, min_buyers=2) is None


def test_house_cluster_within_span_still_fires(conn):
    add_house_txn(conn, "AAA", "Rep A", "$100,001 - $250,000", date=_house_date(5))
    add_house_txn(conn, "AAA", "Rep B", "$100,001 - $250,000", date=_house_date(2))
    signals = cluster.find_house_clusters(conn)
    assert len(signals) == 1
    assert signals[0].buyer_count == 2
    assert signals[0].reason == "cluster"


def test_house_members_40_days_apart_no_longer_cluster(conn):
    """Both land inside the 45-day HOUSE_WINDOW_DAYS scan, but 40 days apart from
    each other -- this must NOT read as coordinated buying any more."""
    add_house_txn(conn, "AAA", "Rep A", "$100,001 - $250,000", date=_house_date(44))
    add_house_txn(conn, "AAA", "Rep B", "$100,001 - $250,000", date=_house_date(2))
    assert cluster.find_house_clusters(conn) == []


def test_house_cluster_excludes_a_stale_third_member_outside_the_span(conn):
    """Two members cluster tightly; a third member's much older purchase of the
    same ticker (still inside the 45-day scan) must not be folded into the
    signal's buyer count or total -- it isn't part of what just happened."""
    add_house_txn(conn, "AAA", "Rep Old", "$1,000,001 - $5,000,000", date=_house_date(44))
    add_house_txn(conn, "AAA", "Rep A", "$100,001 - $250,000", date=_house_date(5))
    add_house_txn(conn, "AAA", "Rep B", "$100,001 - $250,000", date=_house_date(2))
    signals = cluster.find_house_clusters(conn)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.buyer_count == 2
    assert "Rep Old" not in sig.member_names
    assert set(sig.member_names) == {"Rep A", "Rep B"}


def test_house_solo_whale_still_fires_without_a_tight_cluster(conn):
    """A single large buyer, no second member anywhere near in time -- the
    solo-whale path is about one person's size, not about clustering-tightness,
    and must be unaffected by HOUSE_CLUSTER_SPAN_DAYS."""
    add_house_txn(conn, "AAA", "Whale", "$1,000,001 - $5,000,000", date=_house_date(2))
    signals = cluster.find_house_clusters(conn)
    assert len(signals) == 1
    assert signals[0].reason == "solo"
    assert signals[0].buyer_count == 1


def test_house_solo_whale_total_excludes_an_unrelated_distant_small_buyer(conn):
    """A whale purchase plus a small, unrelated purchase by someone else 40 days
    earlier (too far apart to form a tight cluster): the solo signal must report
    only the whale's own total, not the two summed together."""
    add_house_txn(conn, "AAA", "Small Buyer", "$15,001 - $50,000", date=_house_date(44))
    add_house_txn(conn, "AAA", "Whale", "$1,000,001 - $5,000,000", date=_house_date(2))
    signals = cluster.find_house_clusters(conn)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.reason == "solo"
    assert sig.member_names == ["Whale"]


def test_senate_cluster_also_respects_the_span(conn):
    """find_senate_clusters shares find_house_clusters -- confirm the span
    constraint carries through the kwargs.setdefault plumbing, not just the
    House's own default parameter."""
    from conftest import add_senate_txn
    old = (TODAY - dt.timedelta(days=44)).isoformat()
    add_senate_txn(conn, "AAA", "Senator A", "$100,001 - $250,000", old)
    add_senate_txn(conn, "AAA", "Senator B", "$100,001 - $250,000", RECENT)
    assert cluster.find_senate_clusters(conn) == []


# --------------------------------------------------------- corroboration
def _journal_row(conn, ticker, source, days_ago=0, kind="cluster"):
    """Insert a signal_journal row with a controlled age, for testing the
    corroboration window boundary -- db.journal_signal() always stamps
    emitted_at as "now", so a backdated row needs raw SQL."""
    when = (TODAY - dt.timedelta(days=days_ago)).isoformat()
    conn.execute(
        "INSERT INTO signal_journal (source, ticker, kind, emitted_at) VALUES (?, ?, ?, ?)",
        (source, ticker, kind, when),
    )
    conn.commit()


def test_corroboration_handles_an_empty_signal_list(conn):
    cluster.find_corroboration(conn, [])  # must not raise


def test_corroboration_matches_another_source_in_the_same_batch(conn):
    add_sec_purchase(conn, "AAA", "Buyer One", 300_000, RECENT)
    add_sec_purchase(conn, "AAA", "Buyer Two", 300_000, RECENT)
    add_senate_txn(conn, "AAA", "Sen. One", "$60,001 - $100,000", RECENT)
    add_senate_txn(conn, "AAA", "Sen. Two", "$60,001 - $100,000", RECENT)
    signals = cluster.find_sec_clusters(conn) + cluster.find_senate_clusters(conn)
    cluster.find_corroboration(conn, signals)
    by_source = {s.source: s.corroborated_by for s in signals}
    assert by_source["SEC"] == ["SENATE"]
    assert by_source["SENATE"] == ["SEC"]


def test_corroboration_matches_journal_history_within_the_window(conn):
    add_sec_purchase(conn, "AAA", "Buyer One", 300_000, RECENT)
    add_sec_purchase(conn, "AAA", "Buyer Two", 300_000, RECENT)
    _journal_row(conn, "AAA", "BAFIN", days_ago=10)
    signals = cluster.find_sec_clusters(conn)
    cluster.find_corroboration(conn, signals)
    assert signals[0].corroborated_by == ["BAFIN"]


def test_corroboration_ignores_journal_history_outside_the_window(conn):
    add_sec_purchase(conn, "AAA", "Buyer One", 300_000, RECENT)
    add_sec_purchase(conn, "AAA", "Buyer Two", 300_000, RECENT)
    _journal_row(conn, "AAA", "BAFIN", days_ago=cluster.CORROBORATION_WINDOW_DAYS + 5)
    signals = cluster.find_sec_clusters(conn)
    cluster.find_corroboration(conn, signals)
    assert signals[0].corroborated_by == []


def test_corroboration_is_empty_with_no_other_activity(conn):
    add_sec_purchase(conn, "AAA", "Buyer One", 300_000, RECENT)
    add_sec_purchase(conn, "AAA", "Buyer Two", 300_000, RECENT)
    signals = cluster.find_sec_clusters(conn)
    cluster.find_corroboration(conn, signals)
    assert signals[0].corroborated_by == []


def test_corroboration_deduplicates_repeated_sources(conn):
    """A second SEC-sourced journal row must not make SEC corroborate itself --
    same disclosure regime, not another one."""
    add_sec_purchase(conn, "AAA", "Buyer One", 300_000, RECENT)
    add_sec_purchase(conn, "AAA", "Buyer Two", 300_000, RECENT)
    _journal_row(conn, "AAA", "SEC", days_ago=5)
    signals = cluster.find_sec_clusters(conn)
    cluster.find_corroboration(conn, signals)
    assert signals[0].corroborated_by == []


def test_corroboration_treats_sec_and_sec13dg_as_the_same_regime(conn):
    """A Form 4 insider cluster and a 13D/G stake filing can both come from the
    same >10%-holder's single position change -- not independent corroboration,
    even though they're two different source labels. See the design spec's
    Non-goals for why this pairing specifically, unlike e.g. SEC vs SENATE."""
    add_sec_purchase(conn, "AAA", "Buyer One", 300_000, RECENT)
    add_sec_purchase(conn, "AAA", "Buyer Two", 300_000, RECENT)
    _journal_row(conn, "AAA", "SEC13DG", days_ago=5)
    signals = cluster.find_sec_clusters(conn)
    cluster.find_corroboration(conn, signals)
    assert signals[0].corroborated_by == []


def test_corroboration_regime_mapping_is_symmetric(conn):
    """The SEC/SEC13DG regime collapse has to work from the stake-signal side
    too, not just the cluster side."""
    stake = cluster.StakeSignal(source="SEC13DG", ticker="AAA", company="Test",
                                 person="Big Fund", form_type="SCHEDULE 13D",
                                 percent=9.0, prev_percent=None, amount_owned=1,
                                 event_date="", url="")
    _journal_row(conn, "AAA", "SEC", days_ago=5)
    cluster.find_corroboration(conn, [stake])
    assert stake.corroborated_by == []


def test_corroboration_still_fires_across_genuinely_different_regimes(conn):
    """The regime collapse must not swallow real cross-source corroboration --
    SEC and SENATE are unrelated regulatory regimes, not a collapsed pair."""
    add_sec_purchase(conn, "AAA", "Buyer One", 300_000, RECENT)
    add_sec_purchase(conn, "AAA", "Buyer Two", 300_000, RECENT)
    _journal_row(conn, "AAA", "SEC13DG", days_ago=5)
    _journal_row(conn, "AAA", "SENATE", days_ago=5)
    signals = cluster.find_sec_clusters(conn)
    cluster.find_corroboration(conn, signals)
    assert signals[0].corroborated_by == ["SENATE"]


def test_corroboration_treats_house_and_senate_as_the_same_regime(conn):
    """House and Senate PTRs are both STOCK Act filings -- different chambers
    of the same regulatory framework, not independent regimes. Weaker case
    than SEC/SEC13DG (genuinely different filers, unlike a single >10%-holder
    generating both an SEC filing pair), but the same collapse applies."""
    add_house_txn(conn, "AAA", "Rep A", "$100,001 - $250,000", date=_house_date(5))
    add_house_txn(conn, "AAA", "Rep B", "$100,001 - $250,000", date=_house_date(2))
    _journal_row(conn, "AAA", "SENATE", days_ago=5)
    signals = cluster.find_house_clusters(conn)
    cluster.find_corroboration(conn, signals)
    assert signals[0].corroborated_by == []


def test_corroboration_house_senate_regime_is_symmetric(conn):
    """The House/Senate regime collapse has to work from the Senate side too,
    not just the House side."""
    add_senate_txn(conn, "AAA", "Sen. One", "$60,001 - $100,000", date=RECENT)
    add_senate_txn(conn, "AAA", "Sen. Two", "$60,001 - $100,000", date=RECENT)
    _journal_row(conn, "AAA", "HOUSE", days_ago=5)
    signals = cluster.find_senate_clusters(conn)
    cluster.find_corroboration(conn, signals)
    assert signals[0].corroborated_by == []


def test_corroboration_includes_exit_signals(conn):
    """Exits count on either side -- co-occurrence, not agreement. See
    cluster.find_corroboration's docstring."""
    add_sec_purchase(conn, "AAA", "Buyer One", 300_000, RECENT)
    add_sec_purchase(conn, "AAA", "Buyer Two", 300_000, RECENT)
    _journal_row(conn, "AAA", "HOUSE", days_ago=3, kind="exit")
    signals = cluster.find_sec_clusters(conn)
    cluster.find_corroboration(conn, signals)
    assert signals[0].corroborated_by == ["HOUSE"]


def test_corroboration_survives_the_full_chain_to_the_dossier(conn):
    """find_corroboration -> score_signal -> _signal_features -> journal_signal ->
    corroboration_summary, wired together for real. Every other test in this
    file either checks find_corroboration alone or hand-assigns
    .corroborated_by directly -- this is the one place the whole chain runs
    end to end, with .corroborated_by actually DERIVED from a real
    find_corroboration call (via enrich_signals) before journaling, not set by
    hand."""
    add_sec_purchase(conn, "AAA", "Buyer One", 300_000, RECENT)
    add_sec_purchase(conn, "AAA", "Buyer Two", 300_000, RECENT)
    add_senate_txn(conn, "AAA", "Sen. One", "$60,001 - $100,000", RECENT)
    add_senate_txn(conn, "AAA", "Sen. Two", "$60,001 - $100,000", RECENT)
    signals = cluster.find_sec_clusters(conn) + cluster.find_senate_clusters(conn)
    assert {s.source for s in signals} == {"SEC", "SENATE"}   # sanity: both regimes fired

    enriched = cluster.enrich_signals(conn, signals)
    for sig in enriched:
        db.journal_signal(conn, bot._signal_features(sig))

    summary = research.corroboration_summary(conn, "AAA")
    assert summary["all_sources"] == ["SEC", "SENATE"]
    assert summary["recent_sources"] == ["SEC", "SENATE"]


# ------------------------------------------------------- tiered daily digest
def test_send_digest_commits_signals_and_close_alerts_only_on_success(conn, monkeypatch):
    import positions
    import strategy
    RECENT = (TODAY - dt.timedelta(days=1)).isoformat()
    sig = cluster.ClusterSignal(source="SEC", ticker="AAA", company="C", buyer_count=3,
                                total_value=1e6, members=[], window_start=RECENT,
                                window_end=RECENT, member_names=["A", "B", "C"])
    sig.tier = strategy.STRONG
    sel = strategy.Selection([strategy.Tiered(sig, strategy.STRONG, ["x"], [])], [], True)
    pos = positions.open_position(conn, "ZZZ", 10.0, today=TODAY - dt.timedelta(days=100))
    closes = positions.check_exits(conn, price_fn=lambda t, s=None: None)

    monkeypatch.setattr("telegram_notify.send_text", lambda msg: False)
    assert bot._send_digest(conn, sel, closes) is False
    assert db.get_alert_state(conn, "SEC", "AAA") is None
    assert positions.check_exits(conn, price_fn=lambda t, s=None: None)       # not marked yet

    sent = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    assert bot._send_digest(conn, sel, closes) is True
    assert "Сильные" in sent[0] and "ZZZ" in sent[0]
    assert db.get_alert_state(conn, "SEC", "AAA") is not None
    assert conn.execute("SELECT tier FROM signal_journal").fetchone()[0] == "strong"
    assert positions.check_exits(conn, price_fn=lambda t, s=None: None) == []


def test_send_digest_sends_nothing_when_there_is_nothing(conn, monkeypatch):
    import strategy
    sent = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    assert bot._send_digest(conn, strategy.Selection([], [], True), []) is False
    assert sent == []
