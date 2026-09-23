"""outlook.py: situations, frequency tables and the walk-forward check. Offline --
synthetic price paths."""
from __future__ import annotations

import random

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
    assert obs[0][0] == int(dates[outlook.MIN_HISTORY - 1 + 21][:4])


def test_an_observation_belongs_to_the_year_its_label_is_known():
    """A December observation whose month ends in January is a January fact: labelled
    by the start date, a training fold (years < Y) would peek into year Y."""
    t = outlook.MIN_HISTORY - 1
    dates = [d.date().isoformat()
             for d in pd.date_range(pd.Timestamp("2015-12-20") - pd.Timedelta(days=t), periods=400)]
    assert dates[t] == "2015-12-20" and dates[t + 30] == "2016-01-19"
    obs = outlook.observations(list(zip(dates, _path(400))), 30)
    assert obs[0][0] == 2016


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


SITUATIONS = [f"{t}|{m}|{v}" for t in ("up", "down", "mixed")
              for m in ("strong_up", "strong_down", "flat") for v in ("high", "normal")]


def _market(seed, planted=None, effect=0.0, n_assets=40, years=range(2011, 2024)):
    """Noise shaped like a real table: each month a date-wide market move lifts or sinks
    every asset together, and half the assets share that month's situation (regimes
    cluster by date). No situation has an effect of its own except `planted`."""
    rnd = random.Random(seed)
    obs = []
    for y in years:
        for _month in range(12):
            shock = rnd.gauss(0, 0.12)
            regime = rnd.choice(SITUATIONS)
            for _asset in range(n_assets):
                k = regime if rnd.random() < 0.5 else rnd.choice(SITUATIONS)
                p = min(max(0.58 + shock + (effect if k == planted else 0.0), 0.02), 0.98)
                obs.append((y, k, rnd.random() < p))
    return obs


def test_noise_with_a_market_factor_rarely_has_an_edge():
    """A date-wide market factor fools the Brier-only rule (19-25% of pure-noise
    situations in the controller's simulation); the four-part rule keeps that to a few
    percent. Ten fixed seeds, every full and pooled row of each table."""
    rows = [r for seed in range(10)
            for r in outlook.build_table(_market(seed, n_assets=150)).values()]
    brier_only = sum(r["oos_n"] >= outlook.MIN_OOS and r["oos_brier_s"] < r["oos_brier_base"]
                     for r in rows)
    assert brier_only / len(rows) > 0.15
    assert sum(outlook.has_edge(r) for r in rows) / len(rows) <= 0.06


@pytest.mark.parametrize("seed", range(5))
def test_a_planted_effect_in_a_noisy_market_has_an_edge(seed):
    rows = outlook.build_table(_market(seed, planted="up|flat|normal", effect=0.15))
    assert outlook.has_edge(rows["up|flat|normal"])


def test_winning_overall_but_in_only_one_of_three_years_is_no_edge():
    """A situation far above the base rate in training, then one big year and two
    losing ones: its overall Brier beats the base rate, its record by year doesn't."""
    def year(y, s_obs, s_ups):
        background = [(y, "mixed|flat|normal", i % 2 == 0) for i in range(100)]
        return background + [(y, "up|strong_up|high", i < s_ups) for i in range(s_obs)]
    obs = [o for y in range(2010, 2014) for o in year(y, 20, 18)]    # training only
    obs += year(2014, 60, 60) + year(2015, 10, 5) + year(2016, 10, 5) + year(2017, 0, 0)
    row = outlook.build_table(obs)["up|strong_up|high"]
    assert (row["oos_n"], row["oos_folds"], row["oos_fold_wins"]) == (80, 3, 1)
    assert row["oos_brier_s"] < row["oos_brier_base"]              # overall it "wins"...
    assert not outlook.has_edge(row)                               # ...but only one year


EDGE = {"oos_n": 120, "oos_folds": 5, "oos_fold_wins": 3, "oos_brier_s": 0.23,
        "oos_brier_base": 0.24}


@pytest.mark.parametrize("change,edge", [
    ({}, True),
    ({"oos_n": 49}, False),                                        # too few observations
    ({"oos_folds": 2, "oos_fold_wins": 2}, False),                 # too few test years
    ({"oos_fold_wins": 2}, False),                                 # wins 2 of 5 years
    ({"oos_brier_s": 0.2392}, False),                              # skill 0.33% < 0.5%
    ({"oos_brier_s": 0.2387}, True),                               # skill 0.54%
    ({"oos_folds": None, "oos_fold_wins": None}, False),           # a row from before folds
])
def test_the_four_conditions_of_an_edge(change, edge):
    assert outlook.has_edge({**EDGE, **change}) is edge
    older = {k: v for k, v in EDGE.items() if k not in ("oos_folds", "oos_fold_wins")}
    assert not outlook.has_edge(older)


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
    outlook.save_table(conn, "stock", rows, 97)
    loaded, built_at = outlook.load_table(conn, "stock")
    assert loaded["up|flat|normal"]["n"] == 60 and built_at
    assert loaded["up|flat|normal"]["assets"] == 97
    assert (loaded["up|flat|normal"]["oos_folds"], loaded["up|flat|normal"]["oos_fold_wins"]) == \
        (rows["up|flat|normal"]["oos_folds"], rows["up|flat|normal"]["oos_fold_wins"]) == (7, 0)
    outlook.save_bars(conn, "AAA", [("2026-01-02", 1.0), ("2026-01-05", 2.0)])
    assert outlook.load_bars(conn, "AAA") == [("2026-01-02", 1.0), ("2026-01-05", 2.0)]


def test_an_older_outlook_table_gains_the_new_columns(tmp_path):
    import sqlite3

    import db
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE outlook_table (table_name TEXT NOT NULL, situation TEXT NOT NULL, "
                "n INTEGER NOT NULL, up INTEGER NOT NULL, base_rate REAL, oos_n INTEGER NOT NULL, "
                "oos_brier_s REAL, oos_brier_base REAL, built_at TEXT NOT NULL, "
                "PRIMARY KEY (table_name, situation))")
    old.commit()
    old.close()
    conn = db.connect(path)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(outlook_table)")}
    conn.close()
    assert {"assets", "oos_folds", "oos_fold_wins"} <= columns


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


def test_refresh_if_stale_rebuilds_only_an_old_table(conn, market, monkeypatch):
    rows = outlook.build_table(_obs("up|flat|normal", 5, lambda i: True))
    outlook.save_table(conn, "stock", rows, 2)
    outlook.save_table(conn, "crypto", rows, 3)
    old = (dt.datetime.now() - dt.timedelta(days=outlook.TABLE_MAX_AGE_DAYS + 1)).isoformat()
    conn.execute("UPDATE outlook_table SET built_at = ? WHERE table_name = 'stock'", (old,))
    conn.commit()
    _crypto_rows, crypto_built = outlook.load_table(conn, "crypto")
    rebuilt = []
    real_refresh = outlook.refresh
    monkeypatch.setattr(outlook, "refresh", lambda conn, kinds: rebuilt.append(list(kinds))
                        or real_refresh(conn, kinds))
    outlook.refresh_if_stale(conn)
    assert rebuilt == [["stock"]]
    stock, stock_built = outlook.load_table(conn, "stock")
    assert stock_built > old and next(iter(stock.values()))["assets"] == 2   # the market's AAA, BBB
    assert outlook.load_table(conn, "crypto")[1] == crypto_built


def test_a_split_triggers_a_full_refetch(conn, market, monkeypatch):
    outlook.save_bars(conn, "AAA", [(d, c * 2) for d, c in _bars(_path(4000))])   # stale scale
    outlook.refresh(conn, ["stock"])
    aaa_days = [days for sym, days in market if sym == "AAA"]
    assert aaa_days[-1] == outlook.FULL_HISTORY_DAYS
    assert outlook.load_bars(conn, "AAA")[-1][1] == pytest.approx(_path(4000)[-1])


def test_a_failed_write_after_a_split_keeps_the_old_bars(conn, market, monkeypatch):
    """If the reinsert after the split's DELETE fails, the DELETE must roll back too --
    otherwise it sits uncommitted until some unrelated later commit makes it permanent."""
    stale = [(d, c * 2) for d, c in _bars(_path(4000))]
    outlook.save_bars(conn, "AAA", stale)
    monkeypatch.setattr(outlook, "save_bars",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        outlook._top_up(conn, assets.stock_asset("AAA"))
    assert outlook.load_bars(conn, "AAA") == stale

    # An unrelated commit elsewhere on the same connection must not resurrect a
    # half-finished DELETE that was left pending in the open transaction.
    conn.execute("INSERT OR REPLACE INTO price_bars (symbol, date, close) VALUES (?,?,?)",
                 ("ZZZ", "2020-01-01", 1.0))
    conn.commit()
    assert outlook.load_bars(conn, "AAA") == stale


def test_refresh_logs_a_failed_split_write_and_keeps_going(conn, market, monkeypatch, capsys):
    stale = [(d, c * 2) for d, c in _bars(_path(4000))]
    outlook.save_bars(conn, "AAA", stale)
    real_save_bars = outlook.save_bars

    def flaky(conn, symbol, bars):
        if symbol == "AAA":
            raise RuntimeError("boom")
        real_save_bars(conn, symbol, bars)
    monkeypatch.setattr(outlook, "save_bars", flaky)
    outlook.refresh(conn, ["stock"])
    assert "AAA" in capsys.readouterr().err
    assert outlook.load_bars(conn, "AAA") == stale
    assert outlook.load_bars(conn, "BBB")                       # the other symbol still ran


# ---------------------------------------------------------------- crypto universe
def test_crypto_universe_drops_wrapped_staked_and_pegged_tokens(conn, monkeypatch):
    monkeypatch.setattr(outlook.sources, "cached_coins", lambda conn: [
        ("BTC", "bitcoin", "Bitcoin", 1), ("WBTC", "wrapped-bitcoin", "Wrapped Bitcoin", 2),
        ("STETH", "staked-ether", "Lido Staked Ether", 3), ("SOL", "solana", "Solana", 4),
        ("FIGR_HELOC", "figure-heloc", "Figure Heloc", 5), ("USDG", "global-dollar", "Global Dollar", 6),
        ("USDT", "tether", "Tether", 7)])
    assert outlook._universe(conn, "crypto") == ["BTC-USD", "SOL-USD"]


def test_refresh_skips_a_flat_priced_coin(conn, market, monkeypatch):
    series = {"AAA-USD": _bars(_path(1500, wiggle=0.02)),
              "PEG-USD": _bars(_path(1500, daily=0.0, wiggle=0.0005))}
    monkeypatch.setattr(outlook, "_universe", lambda conn, kind: sorted(series) if kind == "crypto" else [])
    monkeypatch.setattr(outlook.sources, "price_history",
                        lambda asset, days=800: (series[asset.yahoo], "Yahoo"))
    outlook.refresh(conn, ["crypto"])
    table, _at = outlook.load_table(conn, "crypto")
    only_aaa = outlook.build_table(outlook.observations(series["AAA-USD"], outlook.HORIZON["crypto"]))
    assert {k: r["n"] for k, r in table.items()} == {k: r["n"] for k, r in only_aaa.items()}
    assert next(iter(table.values()))["assets"] == 1


def test_lookup_reports_the_tables_asset_count(conn, market):
    outlook.refresh(conn, ["stock"])
    assert outlook.lookup(conn, assets.stock_asset("AAA"))["assets"] == 2


def test_one_kinds_universe_failing_does_not_stop_the_other(conn, market, monkeypatch, capsys):
    def universe(conn, kind):
        if kind == "stock":
            raise ConnectionError("wikipedia down")
        return ["AAA-USD"]
    crypto_bars = _bars(_path(1500, wiggle=0.02))
    monkeypatch.setattr(outlook, "_universe", universe)
    monkeypatch.setattr(outlook.sources, "price_history", lambda asset, days=800: (crypto_bars, "Yahoo"))
    outlook.refresh(conn, ["stock", "crypto"])
    assert outlook.load_table(conn, "stock") == ({}, None)
    assert outlook.load_table(conn, "crypto")[0]
    assert "wikipedia down" in capsys.readouterr().err


def test_todays_open_bar_is_not_stored(conn, monkeypatch):
    """Today's bar is still moving; stored, it would differ from the final close and
    trip the rescale check at the next top-up."""
    today = dt.datetime.now(dt.timezone.utc).date()
    bars = [((today - dt.timedelta(days=i)).isoformat(), 100.0 + i) for i in range(3, -2, -1)]
    monkeypatch.setattr(outlook.sources, "price_history", lambda asset, days=800: (bars, "Yahoo"))
    outlook._top_up(conn, assets.stock_asset("AAA"))
    stored = outlook.load_bars(conn, "AAA")
    assert [d for d, _c in stored] == [d for d, _c in bars if d < today.isoformat()]
    outlook._top_up(conn, assets.stock_asset("AAA"))          # the top-up path, too
    assert outlook.load_bars(conn, "AAA") == stored


def test_lookup_reuses_the_dossiers_bars(conn, market, monkeypatch):
    outlook.refresh(conn, ["stock"])
    fetched = []
    monkeypatch.setattr(outlook.sources, "price_history",
                        lambda asset, days=800: fetched.append(days) or (None, None))
    res = outlook.lookup(conn, assets.stock_asset("AAA"), bars=_bars(_path(400)), source="Nasdaq")
    assert fetched == [] and res["status"] == "ok" and res["source"] == "Nasdaq"
    outlook.lookup(conn, assets.stock_asset("AAA"), bars=_bars(_path(100)), source="Nasdaq")
    assert fetched == [outlook.LOOKUP_DAYS]                    # too short: fetched after all


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


def test_message_names_the_crypto_tables_coin_count():
    crypto = {**OK, "table": "crypto", "assets": 31}
    assert "по 31 крупнейшим монетам" in outlook.format_outlook(crypto, "SOL")
    assert "по крупнейшим монетам" in outlook.format_outlook({**crypto, "assets": None}, "SOL")
    assert "монетам" not in outlook.format_outlook({**OK, "assets": 97}, "NVDA")


@pytest.mark.parametrize("status,words", [("no_table", "ещё не готов"),
                                          ("no_prices", "нет свежих цен"),
                                          ("short_history", "мало истории")])
def test_message_for_each_status(status, words):
    assert words in outlook.format_outlook({"status": status}, "X")
