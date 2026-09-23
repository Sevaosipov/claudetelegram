"""strategy.py: which signals are Сильный, which Кандидат, which don't make it.
Offline: enrich_signals is replaced by `sized`, price trends by a stub."""
from __future__ import annotations

import datetime as dt

import pytest

import cluster
import crypto
import strategy
from conftest import add_house_txn, add_sec_purchase, add_stake

TODAY = dt.date.today()
RECENT = (TODAY - dt.timedelta(days=1)).isoformat()
BIG, LIQUID = 5e9, 50e6


class _T212:
    def __init__(self, missing=()):
        self.missing = set(missing)

    def can_buy(self, ticker, source):
        return ticker not in self.missing


@pytest.fixture
def sized(monkeypatch):
    """Stand-in for enrich_signals: every stock gets the size set here, every signal
    a score equal to its position (so ordering is deterministic)."""
    state = {"cap": BIG, "adv": LIQUID}

    def fake(conn, signals):
        for i, s in enumerate(signals):
            if not getattr(s, "crypto_kind", None):
                s.market_cap_eur, s.avg_daily_value = state["cap"], state["adv"]
            s.score = 100.0 - i
        return signals
    monkeypatch.setattr(cluster, "enrich_signals", fake)
    return state


def _buy(conn, ticker, owner, usd=116_000, **kw):
    add_sec_purchase(conn, ticker, owner, usd, RECENT, filed_date=RECENT, **kw)


def _select(conn, t212=None):
    return strategy.select(conn, cluster.find_sec_clusters(conn) + cluster.find_house_clusters(conn)
                           + cluster.find_stake_signals(conn), t212 or _T212())


def _tiers(sel):
    return ({t.signal.ticker for t in sel.strong}, {t.signal.ticker for t in sel.candidates})


# ---------------------------------------------------------------- stock rules
def test_rule_a_three_insiders_is_strong(conn, sized):
    for o in ("A", "B", "C"):
        _buy(conn, "AAA", o)
    strong, _ = _tiers(_select(conn))
    assert strong == {"AAA"}


def test_rule_b_two_insiders_with_a_250k_ceo_is_strong(conn, sized):
    # $350,000 -> ~€301,700 at the offline fallback FX rate, above TOP_EXEC_MIN_EUR.
    _buy(conn, "AAA", "Boss", usd=350_000, officer=1, director=0, title="Chief Executive Officer")
    _buy(conn, "AAA", "Board")
    [t] = _select(conn).strong
    assert any("CEO" in m for m in t.met)


def test_rule_b_ceo_below_top_exec_min_is_only_a_candidate(conn, sized):
    """Two insiders including a CEO used to be enough for rule (b) regardless of
    how much the CEO actually bought -- the user asked for a real conviction
    purchase (>= TOP_EXEC_MIN_EUR), not just the CEO's presence in the cluster.
    The ✗ line must say so specifically -- not the generic "no CEO/CFO/Chair in
    the cluster" line, which would be false here: the CEO IS in the cluster,
    just under the euro bar."""
    _buy(conn, "AAA", "Boss", officer=1, director=0, title="Chief Executive Officer")  # ~€100k
    _buy(conn, "AAA", "Board")
    sel = _select(conn)
    assert _tiers(sel) == (set(), {"AAA"})
    assert sel.candidates[0].missed == ["CEO купил только на €100.0 тыс (< €250.0 тыс)"]


def test_two_directors_without_a_top_exec_is_a_candidate(conn, sized):
    _buy(conn, "AAA", "Board One")
    _buy(conn, "AAA", "Board Two")
    sel = _select(conn)
    assert _tiers(sel) == (set(), {"AAA"})
    assert sel.candidates[0].missed == [
        "нет 3+ инсайдеров, CEO/CFO/Chair с покупкой от €250 тыс или крупной покупки CEO/CFO"]


def test_rule_c_ceo_conviction_buy_is_strong(conn, sized):
    _buy(conn, "AAA", "Boss", usd=700_000, officer=1, director=0,
         title="Chief Executive Officer", shares=100, shares_owned_after=500)
    assert _tiers(_select(conn))[0] == {"AAA"}


def test_small_ceo_buy_is_only_a_candidate(conn, sized):
    _buy(conn, "AAA", "Boss", usd=700_000, officer=1, director=0,
         title="Chief Executive Officer", shares=100, shares_owned_after=100_000)   # +0.1%
    assert _tiers(_select(conn)) == (set(), {"AAA"})


def test_holders_only_is_a_candidate(conn, sized):
    for o in ("Fund A", "Fund B", "Fund C"):
        _buy(conn, "AAA", o, usd=700_000, director=0, ten_pct=1)
    assert _tiers(_select(conn)) == (set(), {"AAA"})


def test_congress_is_candidate_only(conn, sized):
    date = (TODAY - dt.timedelta(days=20)).strftime("%m/%d/%Y")
    for m in ("One", "Two", "Three"):
        add_house_txn(conn, "AAA", m, "$250,001 - $500,000", date=date)
    assert _tiers(_select(conn)) == (set(), {"AAA"})


def test_activist_stake_is_candidate_only(conn, sized):
    add_stake(conn, "AAA", "Activist", 12.0)
    assert _tiers(_select(conn)) == (set(), {"AAA"})


# ---------------------------------------------------------------------- floors
def test_below_the_size_floor_is_dropped(conn, sized):
    sized["cap"] = 100e6
    for o in ("A", "B", "C"):
        _buy(conn, "AAA", o)
    assert _tiers(_select(conn)) == (set(), set())


def test_illiquid_is_dropped(conn, sized):
    sized["adv"] = 200_000
    for o in ("A", "B", "C"):
        _buy(conn, "AAA", o)
    assert _tiers(_select(conn)) == (set(), set())


def test_unknown_size_caps_at_candidate(conn, sized):
    sized["cap"] = None
    for o in ("A", "B", "C"):
        _buy(conn, "AAA", o)
    [t] = _select(conn).candidates
    assert "размер неизвестен" in t.missed


def test_old_disclosure_is_dropped(conn, sized):
    old = (TODAY - dt.timedelta(days=6)).isoformat()
    for o in ("A", "B", "C"):
        add_sec_purchase(conn, "AAA", o, 116_000, old, filed_date=old)
    assert _tiers(_select(conn)) == (set(), set())


def test_not_on_trading_212_is_dropped(conn, sized):
    for o in ("A", "B", "C"):
        _buy(conn, "AAA", o)
    assert _tiers(_select(conn, _T212(missing={"AAA"}))) == (set(), set())


def test_without_trading_212_nothing_is_filtered_and_it_says_so(conn, sized):
    for o in ("A", "B", "C"):
        _buy(conn, "AAA", o)
    sel = strategy.select(conn, cluster.find_sec_clusters(conn), None)
    assert not sel.t212_checked and _tiers(sel)[0] == {"AAA"}


def test_candidates_are_capped(conn, sized):
    for i in range(15):
        _buy(conn, f"T{i:02d}", "Board One")
        _buy(conn, f"T{i:02d}", "Board Two")
    assert len(_select(conn).candidates) == strategy.MAX_CANDIDATES


def _scored_enrich(monkeypatch, score):
    """Like `sized`, but every signal gets the same fixed score instead of one
    derived from list position -- for testing the CANDIDATE_MIN_SCORE cutoff
    itself rather than the ordering `sized` is built for."""
    def fake(conn, signals):
        for s in signals:
            if not getattr(s, "crypto_kind", None):
                s.market_cap_eur, s.avg_daily_value = BIG, LIQUID
            s.score = score
        return signals
    monkeypatch.setattr(cluster, "enrich_signals", fake)


def test_stock_candidate_below_min_score_is_dropped(conn, monkeypatch):
    _scored_enrich(monkeypatch, strategy.CANDIDATE_MIN_SCORE - 1)
    _buy(conn, "AAA", "Board One")
    _buy(conn, "AAA", "Board Two")
    assert _tiers(_select(conn)) == (set(), set())


def test_stock_candidate_at_min_score_is_kept(conn, monkeypatch):
    _scored_enrich(monkeypatch, strategy.CANDIDATE_MIN_SCORE)
    _buy(conn, "AAA", "Board One")
    _buy(conn, "AAA", "Board Two")
    assert _tiers(_select(conn)) == (set(), {"AAA"})


def test_min_score_does_not_apply_to_strong_signals(conn, monkeypatch):
    _scored_enrich(monkeypatch, 0.0)
    for o in ("A", "B", "C"):
        _buy(conn, "AAA", o)
    assert _tiers(_select(conn)) == ({"AAA"}, set())


def test_crypto_candidate_is_exempt_from_min_score(conn, monkeypatch):
    _scored_enrich(monkeypatch, 0.0)
    _trend(monkeypatch, None)   # "цена не проверена" -> candidate
    [t] = strategy.select(conn, [_etf()], _T212()).candidates
    assert t.signal.ticker == "CRYPTO:BTC"


def test_selected_signals_carry_their_tier(conn, sized):
    for o in ("A", "B", "C"):
        _buy(conn, "AAA", o)
    [t] = _select(conn).strong
    assert t.signal.tier == strategy.STRONG


# ---------------------------------------------------------------------- crypto
def _etf(value_eur=5e8, bullish=True):
    return cluster.CryptoSignal("CRYPTO_ETF", "etf_flow", "CRYPTO:BTC", "спот-ETF: IBIT", bullish,
                                None, value_eur, RECENT, RECENT, [], None, ["k"])


def _trend(monkeypatch, trend):
    monkeypatch.setattr(crypto, "price_trend", lambda conn, sym: trend)


def test_crypto_inflow_with_confirming_price_is_strong(conn, sized, monkeypatch):
    _trend(monkeypatch, {"ret_7d": 4.0, "above_ma20": True})
    assert len(strategy.select(conn, [_etf()], _T212()).strong) == 1


def test_crypto_inflow_without_confirmation_is_a_candidate(conn, sized, monkeypatch):
    _trend(monkeypatch, {"ret_7d": -2.0, "above_ma20": False})
    [t] = strategy.select(conn, [_etf()], _T212()).candidates
    assert any("не подтверждает" in m for m in t.missed)


def test_crypto_without_a_price_check_is_a_candidate(conn, sized, monkeypatch):
    _trend(monkeypatch, None)
    [t] = strategy.select(conn, [_etf()], _T212()).candidates
    assert "цена не проверена" in t.missed


def test_crypto_outflow_is_never_listed(conn, sized, monkeypatch):
    _trend(monkeypatch, {"ret_7d": 4.0, "above_ma20": True})
    sel = strategy.select(conn, [_etf(bullish=False)], _T212())
    assert not sel.strong and not sel.candidates


def test_small_treasury_buy_is_not_listed(conn, sized, monkeypatch):
    _trend(monkeypatch, {"ret_7d": 4.0, "above_ma20": True})
    small = cluster.CryptoSignal("CRYPTO_TREASURY", "treasury", "CRYPTO:BTC", "Acme", True,
                                 100, 8e6, RECENT, RECENT, [], None, ["k"])
    sel = strategy.select(conn, [small], _T212())
    assert not sel.strong and not sel.candidates


def test_exit_signals_are_kept_separately_not_tiered(conn, sized):
    exit_sig = cluster.ExitSignal(source="SEC", ticker="AAA", company="C", total_buyers=2,
                                  seller_count=2, lines=[], seller_names=["A", "B"])
    sel = strategy.select(conn, [exit_sig], _T212())
    assert not sel.strong and not sel.candidates
    assert sel.exits == [exit_sig]


def test_exit_signals_bypass_recency_t212_and_size_filters(conn, sized):
    """Unlike buy-side signals, exits carry no window_start/end recency, aren't
    checked against Trading 212, and get no size floor -- they're already
    deduplicated by the exit finders' own alert state (should_alert_exit)."""
    old_exit = cluster.ExitSignal(source="SEC", ticker="ZZZZ", company="C", total_buyers=2,
                                  seller_count=2, lines=[], seller_names=["A", "B"])
    sel = strategy.select(conn, [old_exit], _T212(missing={"ZZZZ"}))
    assert sel.exits == [old_exit]


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
# The one shared finder list bot.run_cluster_pass, menu._find_signals and
# calibrate_strategy._signals all call, so they can't drift out of sync again.
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
    by_name = dict(calls)
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
