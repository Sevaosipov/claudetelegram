"""outlook.py: situations, frequency tables and the walk-forward check. Offline --
synthetic price paths."""
from __future__ import annotations

import pandas as pd
import pytest

import outlook


def _path(n, daily=0.001, wiggle=0.01, tail=()):
    """A steady drift with a regular zig-zag (volatility never zero), then `tail`
    extra daily returns."""
    closes, p = [], 100.0
    for i in range(n):
        p *= 1 + daily + (wiggle if i % 2 else -wiggle)
        closes.append(p)
    for r in tail:
        p *= 1 + r
        closes.append(p)
    return closes


# ------------------------------------------------------------------ situations
def test_steady_uptrend_is_up_flat_normal():
    assert outlook.situation(_path(400), 21) == "up|flat|normal"


def test_steady_downtrend_is_down():
    assert outlook.situation(_path(400, daily=-0.001), 21).startswith("down|")


def test_a_big_month_is_a_strong_move():
    assert outlook.situation(_path(400, tail=[0.015] * 21), 21).split("|")[1] == "strong_up"
    assert outlook.situation(_path(400, tail=[-0.015] * 21), 21).split("|")[1] == "strong_down"


def test_a_volatility_spike_is_high_volatility():
    spike = [0.05 if i % 2 else -0.05 for i in range(21)]
    assert outlook.situation(_path(400, tail=spike), 21).endswith("|high")


def test_too_little_history_has_no_situation():
    assert outlook.situation(_path(outlook.MIN_HISTORY - 1), 21) is None
    assert outlook.situation(_path(outlook.MIN_HISTORY), 21) is not None


def test_situation_from_tradingview_is_pooled_over_volatility():
    snap = {"close": 110.0, "SMA50": 105.0, "SMA200": 100.0, "Perf.1M": 20.0, "Volatility.M": 2.0}
    assert outlook.situation_from_tv(snap, 21) == "up|strong_up|*"
    assert outlook.situation_from_tv({**snap, "Perf.1M": 1.0}, 21) == "up|flat|*"
    assert outlook.situation_from_tv({**snap, "SMA200": None}, 21) is None


def test_pooled_key():
    assert outlook.pooled("down|flat|high") == "down|flat|*"


# ---------------------------------------------------------------- observations
def test_observations_are_monthly_and_labelled():
    dates = [d.date().isoformat() for d in pd.bdate_range("2015-01-01", periods=600)]
    closes = _path(600, daily=0.002, wiggle=0.0005)          # rises every day
    obs = outlook.observations(list(zip(dates, closes)), 21)
    expected = len(range(outlook.MIN_HISTORY - 1, 600 - 21, 21))
    assert len(obs) == expected and all(up for _y, _k, up in obs)
    assert obs[0][0] == int(dates[outlook.MIN_HISTORY - 1][:4])


# ---------------------------------------------------------- table, walk-forward
def _obs(key, per_year, up_fn, years=range(2010, 2022)):
    return [(y, key, up_fn(i)) for y in years for i in range(per_year)]


def test_a_planted_effect_has_an_edge_and_noise_does_not():
    noise = _obs("mixed|flat|normal", 100, lambda i: i % 2 == 0)
    rows = outlook.build_table(noise)
    assert not outlook.has_edge(rows["mixed|flat|normal"])     # its rate is the base rate

    planted = _obs("up|strong_up|normal", 30, lambda i: True)
    rows = outlook.build_table(noise + planted)
    row = rows["up|strong_up|normal"]
    assert outlook.has_edge(row) and row["n"] == 30 * 12 and row["up"] == 30 * 12


def test_a_thin_situation_has_no_edge():
    rows = outlook.build_table(_obs("mixed|flat|normal", 100, lambda i: i % 2 == 0)
                               + _obs("down|strong_down|high", 3, lambda i: True))
    assert rows["down|strong_down|high"]["oos_n"] < outlook.MIN_OOS
    assert not outlook.has_edge(rows["down|strong_down|high"])


def test_table_includes_pooled_rows():
    rows = outlook.build_table(_obs("up|flat|normal", 5, lambda i: True)
                               + _obs("up|flat|high", 5, lambda i: False))
    assert rows["up|flat|*"]["n"] == rows["up|flat|normal"]["n"] + rows["up|flat|high"]["n"]


def test_tables_and_bars_round_trip(conn):
    rows = outlook.build_table(_obs("up|flat|normal", 5, lambda i: True))
    outlook.save_table(conn, "stock", rows)
    loaded, built_at = outlook.load_table(conn, "stock")
    assert loaded["up|flat|normal"]["n"] == 60 and built_at
    outlook.save_bars(conn, "AAA", [("2026-01-02", 1.0), ("2026-01-05", 2.0)])
    assert outlook.load_bars(conn, "AAA") == [("2026-01-02", 1.0), ("2026-01-05", 2.0)]


def test_walk_forward_folds_boundaries_are_correct():
    """Test that walk-forward folds start at the table's 5th year and end at year[-1].
    With years 2010-2021 (12 years), OOS_START_OFFSET_YEARS=4 means first fold year is
    2014 (years[0]=2010, 2010+4=2014). Folds run through 2020 (years[-1]=2021, but < 2021).
    Test years are 2014, 2015, 2016, 2017, 2018, 2019, 2020 = 7 years.
    """
    obs = _obs("up|flat|normal", 30, lambda i: True, years=range(2010, 2022))
    rows = outlook.build_table(obs)
    row = rows["up|flat|normal"]
    # 7 test years × 30 observations per year = 210 ≥ MIN_OOS (50)
    assert row["oos_n"] == 7 * 30


def test_walk_forward_is_not_wall_clock_dependent():
    """Test that walk-forward result doesn't depend on today's date.
    Same observations shifted by +10 years should give identical oos_n."""
    obs_past = _obs("up|flat|normal", 30, lambda i: True, years=range(2010, 2022))
    rows_past = outlook.build_table(obs_past)

    obs_future = _obs("up|flat|normal", 30, lambda i: True, years=range(2020, 2032))
    rows_future = outlook.build_table(obs_future)

    # Both should have the same oos_n (7 years of test data)
    assert rows_past["up|flat|normal"]["oos_n"] == rows_future["up|flat|normal"]["oos_n"]
    assert rows_past["up|flat|normal"]["oos_n"] == 7 * 30


# ------------------------------------------------------- refresh, lookup, message
import datetime as dt  # noqa: E402

import assets  # noqa: E402


def _bars(closes, start="2010-01-01"):
    return [(d.date().isoformat(), c)
            for d, c in zip(pd.bdate_range(start, periods=len(closes)), closes)]


@pytest.fixture
def market(monkeypatch):
    series = {"AAA": _bars(_path(4000)), "BBB": _bars(_path(4000, daily=-0.0005))}
    calls = []

    def history(asset, days=800):
        calls.append((asset.yahoo, days))
        bars = series.get(asset.yahoo)
        return (bars, "Yahoo") if bars else (None, None)
    monkeypatch.setattr(outlook, "_universe", lambda conn, kind: ["AAA", "BBB"] if kind == "stock" else [])
    monkeypatch.setattr(outlook.sources, "price_history", history)
    monkeypatch.setattr(outlook.tradingview, "fetch_snapshot", lambda q, session=None: None)
    return calls


def test_refresh_stores_bars_and_builds_the_table(conn, market):
    outlook.refresh(conn, ["stock"])
    table, built_at = outlook.load_table(conn, "stock")
    assert table and built_at and len(outlook.load_bars(conn, "AAA")) == 4000


def test_refresh_if_stale_does_nothing_when_fresh(conn, market):
    outlook.refresh(conn, ["stock", "crypto"])
    before = len(market)
    outlook.refresh_if_stale(conn)
    assert len(market) == before


def test_a_split_triggers_a_full_refetch(conn, market, monkeypatch):
    outlook.save_bars(conn, "AAA", [(d, c * 2) for d, c in _bars(_path(4000))])   # stale scale
    outlook.refresh(conn, ["stock"])
    aaa_days = [days for sym, days in market if sym == "AAA"]
    assert aaa_days[-1] == outlook.FULL_HISTORY_DAYS
    assert outlook.load_bars(conn, "AAA")[-1][1] == pytest.approx(_path(4000)[-1])


def test_lookup_without_a_table(conn, market):
    assert outlook.lookup(conn, assets.stock_asset("AAA"))["status"] == "no_table"


def test_lookup_places_the_asset_and_reads_the_table(conn, market):
    outlook.refresh(conn, ["stock"])
    res = outlook.lookup(conn, assets.stock_asset("AAA"))
    assert res["status"] == "ok" and res["table"] == "stock" and not res["pooled"]
    assert res["situation"].count("|") == 2 and res["n"] > 0 and 0 < res["base_rate"] < 1


def test_lookup_falls_back_to_tradingview(conn, market, monkeypatch):
    outlook.refresh(conn, ["stock"])
    monkeypatch.setattr(outlook.tradingview, "fetch_snapshot", lambda q, session=None: {
        "close": 110.0, "SMA50": 105.0, "SMA200": 100.0, "Perf.1M": 1.0, "Volatility.M": 2.0})
    res = outlook.lookup(conn, assets.stock_asset("SAP.DE"))
    assert res["status"] == "ok" and res["pooled"] and res["source"] == "TradingView"
    assert res["situation"] == "up|flat|*" and res["european"]


def test_lookup_without_any_prices(conn, market):
    outlook.refresh(conn, ["stock"])
    assert outlook.lookup(conn, assets.stock_asset("ZZZ"))["status"] == "no_prices"
    assert outlook.lookup(conn, assets.resolve("DE0007164600"))["status"] == "no_prices"


def test_lookup_with_too_little_history(conn, market, monkeypatch):
    outlook.refresh(conn, ["stock"])
    monkeypatch.setattr(outlook.sources, "price_history",
                        lambda asset, days=800: (_bars(_path(100)), "Yahoo"))
    assert outlook.lookup(conn, assets.stock_asset("NEW"))["status"] == "short_history"


OK = {"status": "ok", "table": "stock", "situation": "up|strong_up|normal", "pooled": False,
      "source": "Yahoo", "n": 4210, "p": 0.61, "base_rate": 0.56, "edge": True,
      "european": False}


def test_message_with_an_edge():
    text = outlook.format_outlook(OK, "NVDA")
    assert text.splitlines()[0] == ("📈 Прогноз на месяц: рост в 61% похожих ситуаций "
                                    "(n = 4 210) · обычно 56% · проверено на истории")
    assert "ситуация: тренд вверх · месяц сильный рост · волатильность обычная" in text
    assert text.splitlines()[-1].strip().startswith("частота в прошлом, не гарантия")


def test_message_without_an_edge_and_with_own_history():
    text = outlook.format_outlook({**OK, "edge": False, "own_n": 36, "own_p": 0.64}, "NVDA")
    assert text.splitlines()[0] == "📈 Прогноз на месяц: нет преимущества над базовой частотой (обычно 56%)"
    assert "у самой NVDA в такой ситуации: 64% (n = 36)" in text


def test_message_notes():
    text = outlook.format_outlook({**OK, "european": True, "source": "TradingView",
                                   "pooled": True, "situation": "up|flat|*"}, "SAP.DE")
    assert "(без учёта волатильности)" in text and "таблица по акциям США" in text
    assert "цены: TradingView" in text


@pytest.mark.parametrize("status,words", [("no_table", "ещё не готов"),
                                          ("no_prices", "нет свежих цен"),
                                          ("short_history", "мало истории")])
def test_message_for_each_status(status, words):
    assert words in outlook.format_outlook({"status": status}, "X")
