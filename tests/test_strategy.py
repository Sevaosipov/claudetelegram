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


def test_rule_b_two_insiders_with_the_ceo_is_strong(conn, sized):
    _buy(conn, "AAA", "Boss", officer=1, director=0, title="Chief Executive Officer")
    _buy(conn, "AAA", "Board")
    [t] = _select(conn).strong
    assert any("CEO" in m for m in t.met)


def test_two_directors_without_a_top_exec_is_a_candidate(conn, sized):
    _buy(conn, "AAA", "Board One")
    _buy(conn, "AAA", "Board Two")
    sel = _select(conn)
    assert _tiers(sel) == (set(), {"AAA"}) and sel.candidates[0].missed


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


def test_exit_signals_are_never_listed(conn, sized):
    exit_sig = cluster.ExitSignal(source="SEC", ticker="AAA", company="C", total_buyers=2,
                                  seller_count=2, lines=[], seller_names=["A", "B"])
    sel = strategy.select(conn, [exit_sig], _T212())
    assert not sel.strong and not sel.candidates
