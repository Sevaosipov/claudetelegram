"""strategy.py: the finder list (buy_side_signals, exit_signals) and the predicate that says
a finder's signal is a caution. What to do with a signal is model_score.py's and
model.py's business, tested there. Offline: the finders are replaced by recorders."""
from __future__ import annotations

import inspect

import cluster
import strategy


# ------------------------------------------------------------ kinds of signal
def _coin(bullish):
    return cluster.CryptoSignal("CRYPTO_ETF", "etf_flow", "CRYPTO:BTC", "IBIT", bullish, None, 5e8,
                                "2026-09-29", "2026-09-29", [], None, ["k"])


def _exit():
    return cluster.ExitSignal(source="SEC", ticker="AAA", company="C", total_buyers=2,
                              seller_count=2, lines=[], seller_names=["A", "B"])


def test_a_bullish_coin_signal_and_a_cluster_are_never_caution():
    cluster_sig = cluster.ClusterSignal(source="SEC", ticker="AAA", company="C", buyer_count=2,
                                        total_value=1e6, members=[], window_start="", window_end="")
    for sig in (_coin(True), cluster_sig):
        assert not strategy.is_caution(sig)


def test_a_bearish_coin_signal_is_a_caution():
    assert strategy.is_caution(_coin(False))


def test_an_exit_signal_is_not_a_caution():
    assert not strategy.is_caution(_exit())


def test_the_journal_tier_for_a_caution_is_the_word_caution():
    assert strategy.CAUTION == "caution"     # positions._crypto_caution reads this from the journal


# --------------------------------------------------------------- exit_signals
#
# strategy.exit_signals: the shared exit-finder list, mirroring buy_side_signals.

_EXIT_FINDER_NAMES = ("find_sec_exit_signals", "find_house_exit_signals",
                      "find_bafin_exit_signals", "find_norway_exit_signals",
                      "find_sweden_exit_signals", "find_senate_exit_signals")


def _recording_exit_finders(monkeypatch):
    calls = []

    def make(name):
        def f(conn, **kw):
            calls.append((name, kw))
            return []
        return f
    for name in _EXIT_FINDER_NAMES:
        monkeypatch.setattr(cluster, name, make(name))
    return calls


def test_exit_signals_default_runs_every_finder(conn, monkeypatch):
    calls = _recording_exit_finders(monkeypatch)
    strategy.exit_signals(conn)
    assert {name for name, _ in calls} == set(_EXIT_FINDER_NAMES)


def test_exit_signals_respects_the_sources_dict(conn, monkeypatch):
    calls = _recording_exit_finders(monkeypatch)
    strategy.exit_signals(conn, sources={"sec": True, "house": False, "senate": False,
                                         "bafin": False, "norway": False, "sweden": False})
    assert {name for name, _ in calls} == {"find_sec_exit_signals"}


def test_exit_signals_forwards_ignore_alert_state_to_every_finder(conn, monkeypatch):
    calls = _recording_exit_finders(monkeypatch)
    strategy.exit_signals(conn, ignore_alert_state=True)
    assert all(kw["ignore_alert_state"] is True for _, kw in calls)


# --------------------------------------------------------- buy_side_signals
#
# The one shared finder list bot.collect_new_signals and model.candidate_signals
# both call, so they can't drift out of sync again.
# Wiring is checked by recording which finder each source maps to and what it was
# called with, rather than seeding real rows for nine different tables.

_FINDER_NAMES = ("find_sec_clusters", "find_house_clusters", "find_senate_clusters",
                 "find_bafin_clusters", "find_norway_clusters", "find_sweden_clusters",
                 "find_stake_signals", "find_treasury_signals", "find_etf_flow_signals",
                 "find_onchain_signals")


def _recording_finders(monkeypatch):
    calls = []

    def make(name):
        def f(conn, **kw):
            calls.append((name, kw))
            return []
        return f
    for name in _FINDER_NAMES:
        monkeypatch.setattr(cluster, name, make(name))
    return calls


def test_buy_side_signals_default_runs_every_source_but_not_onchain(conn, monkeypatch):
    calls = _recording_finders(monkeypatch)
    strategy.buy_side_signals(conn)
    called = {name for name, _ in calls}
    assert called == set(_FINDER_NAMES) - {"find_onchain_signals"}


def test_buy_side_signals_onchain_is_opt_in(conn, monkeypatch):
    calls = _recording_finders(monkeypatch)
    strategy.buy_side_signals(conn, onchain=True)
    assert "find_onchain_signals" in {name for name, _ in calls}


def test_buy_side_signals_respects_the_sources_dict(conn, monkeypatch):
    calls = _recording_finders(monkeypatch)
    strategy.buy_side_signals(conn, sources={"sec": True, "house": False, "senate": False,
                                             "bafin": False, "norway": False, "sweden": False,
                                             "crypto": False})
    assert {name for name, _ in calls} == {"find_sec_clusters", "find_stake_signals"}


def test_buy_side_signals_stakes_key_gates_independently_of_sec(conn, monkeypatch):
    """bot's --forms flag can select SEC forms without 13D/13G -- "sec": True must
    not force stakes on when the sources dict says otherwise."""
    calls = _recording_finders(monkeypatch)
    strategy.buy_side_signals(conn, sources={"sec": True, "stakes": False, "house": False,
                                             "senate": False, "bafin": False, "norway": False,
                                             "sweden": False, "crypto": False})
    assert {name for name, _ in calls} == {"find_sec_clusters"}


def test_buy_side_signals_missing_key_in_an_explicit_sources_dict_means_off(conn, monkeypatch):
    """sources=None means "run everything" (via the all-True dict comprehension),
    but once a caller passes its own dict, a key it left out must mean OFF, not
    ON -- the opposite of what `.get(key, True)` used to do."""
    calls = _recording_finders(monkeypatch)
    strategy.buy_side_signals(conn, sources={"sec": True})
    assert {name for name, _ in calls} == {"find_sec_clusters", "find_stake_signals"}


def test_buy_side_signals_default_stake_tuning_matches_run_daily(conn, monkeypatch):
    calls = _recording_finders(monkeypatch)
    strategy.buy_side_signals(conn)
    [kw] = [kw for name, kw in calls if name == "find_stake_signals"]
    assert kw["min_percent"] == 10.0 and kw["activist_only"] is True
    assert kw["new_positions_only"] is True and kw["max_age_days"] == 30


def test_buy_side_signals_forwards_cluster_and_source_specific_kwargs(conn, monkeypatch):
    calls = _recording_finders(monkeypatch)
    strategy.buy_side_signals(
        conn, cluster_kwargs={"min_value": 1.0, "solo_threshold": 2.0},
        sec_kwargs={"insiders_only": True}, sweden_kwargs={"include_share_programs": True})
    by_name = {}
    for name, kw in calls:
        by_name.setdefault(name, kw)
    assert by_name["find_sec_clusters"]["min_value"] == 1.0
    assert by_name["find_sec_clusters"]["insiders_only"] is True
    assert by_name["find_house_clusters"]["min_value"] == 1.0
    assert "insiders_only" not in by_name["find_house_clusters"]
    assert by_name["find_sweden_clusters"]["min_value"] == 1.0
    assert by_name["find_sweden_clusters"]["include_share_programs"] is True


def test_buy_side_signals_forwards_ignore_alert_state_to_every_finder(conn, monkeypatch):
    calls = _recording_finders(monkeypatch)
    strategy.buy_side_signals(conn, ignore_alert_state=True, onchain=True)
    assert all(kw["ignore_alert_state"] is True for _, kw in calls)


def test_buy_side_signals_runs_each_finder_once_with_no_second_pass(conn, monkeypatch):
    calls = _recording_finders(monkeypatch)
    strategy.buy_side_signals(conn, onchain=True, ignore_alert_state=True)
    names = [name for name, _ in calls]
    assert sorted(names) == sorted(_FINDER_NAMES)
    assert set(inspect.signature(strategy.buy_side_signals).parameters) == {
        "conn", "ignore_alert_state", "sources", "onchain", "sec_kwargs", "sweden_kwargs",
        "stake_kwargs", "cluster_kwargs"}


def test_buy_side_signals_hands_the_finders_their_own_thresholds_untouched(conn, monkeypatch):
    calls = _recording_finders(monkeypatch)
    strategy.buy_side_signals(conn, sources={"sec": True, "norway": True},
                              cluster_kwargs={"min_value": 50_000, "solo_threshold": 250_000})
    for name, kw in calls:
        if name in ("find_sec_clusters", "find_norway_clusters"):
            assert kw["min_value"] == 50_000 and kw["solo_threshold"] == 250_000
