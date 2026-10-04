"""model.py: the daily scoring pass. Offline -- every seam (prices, news, the coin trend, Trading 212, the
signals) is a stub, and the dates are fixed so weekday arithmetic is deterministic. (The virtual portfolio that
once traded on these scores is gone: spec 2026-10-04-remove-model-portfolio.md.)"""
from __future__ import annotations

import datetime as dt
import sqlite3
import types

import pytest

import db
import model
import model_score
from cluster.roles import Buyer
from conftest import add_sec_purchase, add_stake

TODAY = dt.date(2026, 10, 5)          # a Monday
YESTERDAY = TODAY - dt.timedelta(days=1)
T212 = types.SimpleNamespace(can_buy=lambda ticker, source: True)
FALLING = {"ret_7d": -6.0, "above_ma20": False}
RISING = {"ret_7d": 3.0, "above_ma20": True}


def _day(n: int) -> str:
    return (TODAY - dt.timedelta(days=n)).isoformat()


def _bars(closes, end: dt.date = TODAY):
    """Consecutive daily closes ending on `end`, oldest first."""
    n = len(closes)
    return [((end - dt.timedelta(days=n - 1 - i)).isoformat(), float(c)) for i, c in enumerate(closes)]


class Fetch:
    def __init__(self, series: dict):
        self.series = series

    def __call__(self, symbol, days=None):
        return self.series.get(symbol, [])


def _zigzag(n=100, lo=100.0, hi=120.0):
    """n closes alternating hi/lo and ending on lo. Daily moves of ~18% clamp the stop to
    its 25% cap; the series is too short for a 6-month line and ends below the close of
    a month ago, so it scores no momentum at all."""
    return [lo if (n - 1 - i) % 2 == 0 else hi for i in range(n)]


def _stock_bars(tail=(), n=100, lo=100.0, hi=120.0):
    """A zigzag through the day before TODAY, then `tail` closes for TODAY, TODAY+1 ..."""
    history = _bars(_zigzag(n, lo, hi), YESTERDAY)
    return history + [((TODAY + dt.timedelta(days=i)).isoformat(), float(c)) for i, c in enumerate(tail)]


def _rising(n=200):
    return _bars([100.0 * 1.002 ** i for i in range(n)], YESTERDAY)


def _falling(n=200):
    return _bars([300.0 - i for i in range(n)], YESTERDAY)


# ------------------------------------------------------------------- signals
def _sig(ticker="AAA", *, n=3, roles=("ceo",), pct=0.25, source="SEC", size=(1e9, 1e6),
         corroborated=()):
    """A ClusterSignal stand-in. The defaults score 60, the BUY bar: three buyers with a CEO
    and 0.25% of the company add up to 62, which the insiders part caps at 60. Any
    `corroborated` source adds 5."""
    buyers = [Buyer(name=f"P{i}", role=roles[i] if i < len(roles) else "director", total_eur=100_000.0)
              for i in range(n)]
    return types.SimpleNamespace(
        ticker=ticker, source=source, company=f"{ticker} Corp", buyers=buyers, buyer_count=n,
        holder_only=False, value_pct_of_mcap=pct, position_increase_pct=None, first_buy=False,
        market_cap_eur=size[0], avg_daily_value=size[1], corroborated_by=list(corroborated),
        member_names=[b.name for b in buyers])


def _stake(ticker="AAA", person="Fund LP", form="SCHEDULE 13D", percent=10.0, prev=None):
    return types.SimpleNamespace(
        ticker=ticker, source="SEC13DG", company=f"{ticker} Corp", person=person, form_type=form,
        percent=percent, prev_percent=prev, is_activist=form.startswith("SCHEDULE 13D"),
        market_cap_eur=1e9, avg_daily_value=1e6, corroborated_by=[])


def _flow(coin="BTC", *, bullish=True, company="спот-ETF США", days_ago=1):
    return types.SimpleNamespace(
        ticker=f"CRYPTO:{coin}", source="CRYPTO_ETF", crypto_kind="etf_flow", coin=coin,
        bullish=bullish, company=company, window_end=_day(days_ago), corroborated_by=[])


# ------------------------------------------------------------------- runners
def _seams(**over):
    return {"news_fn": lambda t, s: [], "trend_fn": lambda c, s: None, "t212": T212, **over}


def _score(conn, signals=(), series=None, **seams):
    return model.score_today(conn, TODAY, fetch=Fetch(series or {}), signals=list(signals), **_seams(**seams))


# ------------------------------------------------------- the archived paper tables
def test_the_stop_and_score_columns_reach_a_database_made_before_them():
    """The paper_* tables are an archive nothing reads or writes any more; an old database still gets
    the columns they were given later, so it matches the schema."""
    old = sqlite3.connect(":memory:")
    for table in ("paper_orders", "paper_positions"):
        old.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, book TEXT)")
    db._migrate(old)
    for table in ("paper_orders", "paper_positions"):
        assert {"stop_pct", "score"} <= {r[1] for r in old.execute(f"PRAGMA table_info({table})")}


# ------------------------------------------------------- the news default
def test_default_news_keeps_recent_headlines_and_those_with_no_readable_date(monkeypatch):
    today = dt.date.today()
    items = [{"title": "fresh", "published": (today - dt.timedelta(days=3)).isoformat()},
             {"title": "on the line", "published": (today - dt.timedelta(days=14)).isoformat()},
             {"title": "stale", "published": (today - dt.timedelta(days=15)).isoformat()},
             {"title": "no date", "published": ""},
             {"title": "garbled", "published": "yesterday-ish"}]
    monkeypatch.setattr(model.sources, "news", lambda asset: (items, "Yahoo"))
    kept = model.default_news("AAA", "SEC")
    assert [i["title"] for i in kept] == ["fresh", "on the line", "no date", "garbled"]


def test_default_news_asks_for_the_coin_or_the_stocks_own_listing(monkeypatch):
    asked = []
    monkeypatch.setattr(model.sources, "news", lambda asset: asked.append(asset) or ([], None))
    model.default_news("CRYPTO:BTC", "CRYPTO")
    model.default_news("EQNR", "NORWAY")
    assert (asked[0].kind, asked[0].yahoo) == ("crypto", "BTC-USD")
    assert (asked[1].kind, asked[1].yahoo) == ("stock", "EQNR.OL")


def test_default_news_is_empty_without_a_listing_and_on_any_failure(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(model.sources, "news", lambda asset: calls.append(asset) or ([], None))
    assert model.default_news("DE0007164600", "BAFIN") == [] and calls == []     # an ISIN: no quote
    monkeypatch.setattr(model.sources, "news", lambda asset: (_ for _ in ()).throw(OSError("down")))
    assert model.default_news("AAA", "SEC") == []
    assert "AAA" in capsys.readouterr().err
    monkeypatch.setattr(model.sources, "news", lambda asset: (None, None))
    assert model.default_news("AAA", "SEC") == []


# ------------------------------------------------------- candidate signals
def test_candidates_are_the_finders_signals_disclosed_in_the_last_14_days_enriched(conn, monkeypatch):
    fresh, edge, stale = (types.SimpleNamespace(name=n) for n in ("fresh", "edge", "stale"))
    disclosed = {"fresh": _day(1), "edge": _day(14), "stale": _day(15)}
    seen = {}

    def finders(conn_, **kw):
        seen.update(kw)
        return [fresh, edge, stale]

    def enrich(conn_, signals):
        seen["enriched"] = list(signals)
        return signals[::-1]

    monkeypatch.setattr(model.strategy, "buy_side_signals", finders)
    monkeypatch.setattr(model.cluster, "disclosed_on", lambda conn_, s: disclosed[s.name])
    monkeypatch.setattr(model.cluster, "enrich_signals", enrich)
    assert model.candidate_signals(conn, TODAY) == [edge, fresh]        # what enrich returned
    assert seen["enriched"] == [fresh, edge]
    assert seen["ignore_alert_state"] is True and seen["onchain"] is False
    assert set(seen) == {"enriched", "ignore_alert_state", "onchain", "cluster_kwargs", "stake_kwargs"}
    assert seen["cluster_kwargs"] == {"min_value": 50_000, "solo_threshold": 250_000}
    assert seen["stake_kwargs"] == {"min_percent": 5.0, "activist_only": False,
                                    "new_positions_only": False, "max_age_days": 14}


def test_candidates_can_be_narrowed_to_tickers_before_recency_and_enrichment(conn, monkeypatch):
    sigs = [types.SimpleNamespace(name=n, ticker=t)
            for n, t in (("a1", "AAA"), ("b1", "BBB"), ("a2", "AAA"), ("c1", "CRYPTO:BTC"))]
    checked, enriched = [], []

    def disclosed(conn_, s):
        checked.append(s.name)
        return _day(1)

    monkeypatch.setattr(model.strategy, "buy_side_signals", lambda conn_, **kw: sigs)
    monkeypatch.setattr(model.cluster, "disclosed_on", disclosed)
    monkeypatch.setattr(model.cluster, "enrich_signals",
                        lambda conn_, signals: enriched.extend(signals) or signals)
    found = model.candidate_signals(conn, TODAY, tickers={"$aaa", "CRYPTO:btc"})
    assert [s.name for s in found] == ["a1", "a2", "c1"]
    assert checked == ["a1", "a2", "c1"] and [s.name for s in enriched] == ["a1", "a2", "c1"]
    assert model.candidate_signals(conn, TODAY, tickers=set()) == []
    assert len(model.candidate_signals(conn, TODAY)) == 4            # no filter: all of them


def test_candidates_come_out_of_the_real_finders(conn, monkeypatch):
    today = dt.date.today()
    recent = (today - dt.timedelta(days=2)).isoformat()
    for owner in ("Buyer One", "Buyer Two"):
        add_sec_purchase(conn, "AAA", owner, 40_000, recent, filed_date=recent)   # €80k in all
    for owner in ("Buyer One", "Buyer Two"):
        add_sec_purchase(conn, "BBB", owner, 20_000, recent, filed_date=recent)   # €40k: below 50k
    monkeypatch.setattr(model.cluster, "enrich_signals", lambda conn_, signals: signals)
    found = model.candidate_signals(conn, today)
    assert [(s.source, s.ticker) for s in found] == [("SEC", "AAA")]


# --------------------------------------------------------------- score_day
PAPER_TABLES = ("paper_books", "paper_orders", "paper_positions", "paper_equity")


def _score_day(conn, signals=(), series=None, today=TODAY, **seams):
    return model.score_day(conn, today, fetch=Fetch(series or {}), signals=list(signals), **_seams(**seams))


def _paper_rows(conn) -> int:
    return sum(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in PAPER_TABLES)


# the coins nothing is known about: no prices, no flows, no news -- scored 0, watched
QUIET = [(f"CRYPTO:{c}", 0, "watch") for c in model.COINS[1:]]


def test_score_day_scores_stores_the_days_scores_and_reports_them(conn):
    report = _score_day(conn, [_sig("AAA", corroborated=("BAFIN",)), _sig("BBB", n=2, roles=(), pct=None)],
                  {"AAA": _stock_bars(), "BBB": _stock_bars(), "BTC-USD": _rising()})
    assert isinstance(report, model.ScoreReport) and report.complete is True
    assert [(s.ticker, s.total, s.decision) for s in report.scored] == [
        ("AAA", 65.0, "buy"), ("CRYPTO:BTC", 60, "buy"), ("BBB", 34.0, "skip")] + QUIET
    assert report.decisions == {"AAA": "buy", "CRYPTO:BTC": "buy", "BBB": "skip",
                                **{t: d for t, _total, d in QUIET}}
    assert [(s.ticker, s.total) for s in model.cached_scores(conn, TODAY)] == [
        (s.ticker, s.total) for s in report.scored]


def test_score_day_trades_nothing_and_writes_no_paper_row(conn):
    """The virtual books are gone: scoring places no order, opens no book, stamps no equity."""
    _score_day(conn, [_sig("AAA")], {"AAA": _stock_bars(), "BTC-USD": _rising()})
    assert _paper_rows(conn) == 0


def test_score_day_hands_every_seam_to_the_scoring(conn, monkeypatch):
    seen = {}

    def score_today(conn_, today, **kw):
        seen.update(kw, today=today)
        return []
    monkeypatch.setattr(model, "score_today", score_today)
    fetch, news, trend, signals = Fetch({}), lambda t, s: [], lambda c, s: None, [_sig("AAA")]
    model.score_day(conn, TODAY, fetch=fetch, news_fn=news, trend_fn=trend, signals=signals, t212=T212)
    assert seen == {"today": TODAY, "fetch": fetch, "news_fn": news, "trend_fn": trend,
                    "signals": signals, "t212": T212}


def test_score_day_scores_for_today_when_no_day_is_given(conn):
    report = model.score_day(conn, fetch=Fetch({}), signals=[], **_seams())
    assert report.complete is True
    assert model.cached_scores(conn) is not None and model.cached_scores(conn, dt.date.today()) is not None


def test_score_day_without_scores_is_incomplete_and_keeps_nothing(conn, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("finders down")
    monkeypatch.setattr(model, "score_today", boom)
    report = _score_day(conn, [_sig("AAA")], {"AAA": _stock_bars()})
    assert (report.complete, report.scored, report.decisions) == (False, [], {})
    assert model.cached_scores(conn, TODAY) is None
    assert "[model] scoring failed: RuntimeError: finders down" in capsys.readouterr().err


def test_score_day_undoes_what_a_failed_scoring_left_in_the_open_transaction(conn, monkeypatch):
    def half_done(*a, **k):
        conn.execute("INSERT INTO kv_cache (key, value, computed_at) VALUES ('half', 1, '2026-10-05T00:00:00')")
        raise RuntimeError("down")
    monkeypatch.setattr(model, "score_today", half_done)
    assert _score_day(conn).complete is False
    assert conn.execute("SELECT COUNT(*) FROM kv_cache WHERE key = 'half'").fetchone() == (0,)


def test_scores_that_could_not_be_kept_do_not_make_the_day_incomplete(conn, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("cache")
    monkeypatch.setattr(model, "keep_scores", boom)
    report = _score_day(conn, [_sig("AAA")], {"AAA": _stock_bars()})
    assert report.complete is True and [s.ticker for s in report.scored][:1] == ["AAA"]
    assert "[model] scores not kept: RuntimeError: cache" in capsys.readouterr().err


# -------------------------------------------------------------- score_today
def test_score_today_scores_and_places_nothing(conn):
    scored = _score(conn, [_sig("AAA", corroborated=("BAFIN",)), _sig("BBB", n=2, roles=(), pct=None)],
                    {"AAA": _stock_bars(), "BBB": _stock_bars(), "BTC-USD": _rising()})
    assert [(s.ticker, s.total, s.decision) for s in scored] == [
        ("AAA", 65.0, "buy"), ("CRYPTO:BTC", 60, "buy"), ("BBB", 34.0, "skip")] + QUIET
    assert conn.execute("SELECT count(*) FROM paper_orders").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM paper_books").fetchone()[0] == 0


def test_only_stock_signals_are_scored_as_stocks(conn):
    signals = [_sig("AAA"), _sig("CRYPTO:BTC", source="HOUSE"), _flow("BTC")]
    scored = _score(conn, signals, {"AAA": _stock_bars()})
    assert [s.ticker for s in scored if s.kind == "stock"] == ["AAA"]
    assert [s.ticker for s in scored if s.kind == "crypto"] == [f"CRYPTO:{c}" for c in model.COINS]


def test_the_best_score_per_ticker_is_kept(conn):
    stake = _stake("AAA", percent=10.0)                     # 30 + 2 * 5 = 40
    scored = _score(conn, [stake, _sig("AAA")], {"AAA": _stock_bars()})
    [aaa] = [s for s in scored if s.ticker == "AAA"]
    assert aaa.signal is not stake and aaa.total == 60.0 + 15   # the cluster, with the activist beside it


def test_news_is_fetched_only_for_a_score_worth_it(conn):
    asked = []

    def news(ticker, source):
        asked.append(ticker)
        return [{"title": "Acme wins contract"}]

    weak = _sig("WWW", n=2, roles=(), pct=None)              # 34 without news: below 35
    edge = _sig("EEE", n=1, roles=("ceo",), pct=0.01)        # 22 + 10 + 3 = 35: just enough
    fair = _sig("FFF", n=3, roles=(), pct=None)              # 42
    series = {t: _stock_bars() for t in ("WWW", "EEE", "FFF")}
    scored = _score(conn, [weak, edge, fair], series, news_fn=news)
    assert [t for t in asked if not t.startswith("CRYPTO")] == ["EEE", "FFF"]
    assert {s.ticker: s.total for s in scored if s.kind == "stock"} == {"WWW": 34.0, "EEE": 40.0, "FFF": 47.0}


def test_a_red_flag_headline_blocks_a_stock(conn):
    scored = _score(conn, [_sig("AAA")], {"AAA": _stock_bars()},
                    news_fn=lambda t, s: [{"title": "AAA going concern doubt"}])
    [aaa] = [s for s in scored if s.ticker == "AAA"]
    assert (aaa.decision, aaa.block) == ("block", "AAA going concern doubt")


@pytest.mark.parametrize("filed_days_ago,form,percent,points", [
    (10, "SCHEDULE 13D", 8.0, 15),
    (30, "SCHEDULE 13D", 8.0, 15),           # 30 days ago still counts
    (31, "SCHEDULE 13D", 8.0, 0),
    (10, "SCHEDULE 13D", 5.0, 15),           # from 5% ...
    (10, "SCHEDULE 13D", 4.9, 0),
    (10, "SCHEDULE 13D", 49.9, 15),          # ... to below control
    (10, "SCHEDULE 13D", 50.0, 0),           # a controlling stake is not an activist nearby
    (10, "SCHEDULE 13D/A", 8.0, 0),          # an amendment is not a new activist
    (10, "SC 13D/A", 8.0, 0),
    (10, "SCHEDULE 13G", 12.0, 6),           # a passive 10%+
    (10, "SCHEDULE 13G", 10.0, 6),           # exactly 10% counts
    (10, "SCHEDULE 13G", 9.0, 0),
    (10, "SCHEDULE 13G/A", 12.0, 0),         # amendments don't count for 13G either
    (10, "SCHEDULE 13G", 50.0, 0),
])
def test_recent_stake_filings_are_a_trigger_for_a_cluster_on_the_same_ticker(
        conn, filed_days_ago, form, percent, points):
    add_stake(conn, "AAA", "Fund LP", percent, form_type=form, event_date=_day(filed_days_ago))
    [aaa] = [s for s in _score(conn, [_sig("AAA")], {"AAA": _stock_bars()}) if s.ticker == "AAA"]
    assert aaa.triggers == points and aaa.total == 60.0 + points


def test_todays_candidates_are_triggers_too_and_politicians_count(conn):
    signals = [_sig("AAA"), _stake("AAA", "Fund LP", "SCHEDULE 13D", 8.0),
               _sig("AAA", source="HOUSE", n=0, roles=(), pct=None), _sig("BBB"),
               _stake("BBB", "Big Fund", "SCHEDULE 13G", 12.0)]
    scored = {s.ticker: s for s in _score(conn, signals, {"AAA": _stock_bars(), "BBB": _stock_bars()})}
    assert scored["AAA"].triggers == 20 and "покупают политики" in scored["AAA"].reasons   # 15 + 5
    assert scored["BBB"].triggers == 6 and scored["BBB"].total == 66.0


@pytest.mark.parametrize("percent,points", [(10.0, 6), (9.9, 0)])
def test_a_passive_stake_among_todays_candidates_counts_from_10_percent(conn, percent, points):
    signals = [_sig("AAA"), _stake("AAA", "Big Fund", "SCHEDULE 13G", percent)]
    [aaa] = [s for s in _score(conn, signals, {"AAA": _stock_bars()}) if s.ticker == "AAA"]
    assert aaa.triggers == points


def test_the_trading_212_label_never_changes_the_decision(conn):
    no_t212 = types.SimpleNamespace(can_buy=lambda ticker, source: ticker != "AAA")
    scored = _score(conn, [_sig("AAA"), _sig("BBB")], {"AAA": _stock_bars(), "BBB": _stock_bars()},
                    t212=no_t212)
    by = {s.ticker: s for s in scored}
    assert (by["AAA"].t212, by["AAA"].decision) == (False, "buy")
    assert (by["BBB"].t212, by["BBB"].decision) == (True, "buy")


def test_the_default_trading_212_list_is_looked_up_and_a_failure_means_no_label(conn, monkeypatch):
    import trading212
    lookups = []
    monkeypatch.setattr(trading212, "availability",
                        lambda conn_: lookups.append(1) or types.SimpleNamespace(can_buy=lambda t, s: False))
    [aaa] = [s for s in _score(conn, [_sig("AAA")], {"AAA": _stock_bars()}, t212=None) if s.ticker == "AAA"]
    assert aaa.t212 is False and lookups == [1]

    def broken(conn_):
        raise OSError("no key")
    monkeypatch.setattr(trading212, "availability", broken)
    [aaa] = [s for s in _score(conn, [_sig("AAA")], {"AAA": _stock_bars()}, t212=None) if s.ticker == "AAA"]
    assert aaa.t212 is None


def test_a_stock_with_no_quote_is_watched_not_bought(conn):
    isin = _sig("DE0007164600", source="BAFIN")
    [s] = [s for s in _score(conn, [isin]) if s.kind == "stock"]
    assert (s.decision, s.untradeable, s.total) == ("watch", "нет цены", 60.0)


def test_a_coins_flows_are_the_last_7_days_and_a_caution_needs_the_price(conn):
    def coin(signals, trend=None):
        scored = _score(conn, signals, {"BTC-USD": _rising()}, trend_fn=lambda c, s: trend)
        return next(s for s in scored if s.ticker == "CRYPTO:BTC")

    undated = _flow()
    del undated.window_end
    assert coin([undated]).flows == 15                               # no date: counts
    assert coin([_flow(days_ago=7)]).flows == 15
    assert coin([_flow(days_ago=8)]).flows == 0                      # a stale inflow
    assert coin([_flow(bullish=False)], trend=RISING).flows == 0     # an outflow the price ignores
    blocked = coin([_flow(bullish=False)], trend=FALLING)
    assert (blocked.flows, blocked.decision) == (-20, "block")
    assert blocked.caution == "спот-ETF США — цена подтверждает"


def test_the_coin_trend_is_asked_only_when_there_is_a_bearish_signal(conn):
    asked = []
    _score(conn, [_flow("BTC")], {"BTC-USD": _rising()}, trend_fn=lambda c, s: asked.append(s) or None)
    assert asked == []
    _score(conn, [_flow("ETH", bullish=False)], {}, trend_fn=lambda c, s: asked.append(s) or None)
    assert asked == ["ETH"]


def test_coin_headlines_score_and_a_red_flag_blocks(conn):
    news = {"CRYPTO:BTC": [{"title": "Bitcoin ETF upgrade lifts price"}],
            "CRYPTO:ETH": [{"title": "Protocol exploit drains ETH"}],
            "CRYPTO:SOL": [{"title": "Solana downgrade by a rating agency"}]}
    scored = _score(conn, [], {"BTC-USD": _rising(), "ETH-USD": _rising(), "SOL-USD": _rising()},
                    news_fn=lambda t, s: news.get(t, []))
    by = {s.ticker: s for s in scored}
    assert (by["CRYPTO:BTC"].news, by["CRYPTO:BTC"].total) == (0, 60)          # a good headline adds nothing to a coin
    assert (by["CRYPTO:SOL"].news, by["CRYPTO:SOL"].total, by["CRYPTO:SOL"].decision) == (-10, 50, "watch")
    assert (by["CRYPTO:ETH"].decision, by["CRYPTO:ETH"].block) == ("block", "Protocol exploit drains ETH")


def test_keyword_good_news_gives_no_alt_an_edge_over_another(conn):
    """The live check: AVAX and ENA led the BUYs only because «upgrade» in crypto headlines is a network upgrade."""
    news = {"CRYPTO:AVAX": [{"title": "Avalanche network upgrade goes live"}, {"title": "AVAX upgrade beats estimates"}],
            "CRYPTO:ENA": [{"title": "Ethena raises forecast after upgrade"}]}
    series = {f"{c}-USD": _rising() for c in model.COINS}
    scored = _score(conn, [], series, news_fn=lambda t, s: news.get(t, []))
    assert {s.total for s in scored if s.kind == "crypto"} == {60} and {s.news for s in scored if s.kind == "crypto"} == {0}


# ------------------------------------------------- stake candidates: only a real signal is a trigger
@pytest.mark.parametrize("stake,points", [
    (_stake("AAA", form="SCHEDULE 13D", percent=8.0), 15),
    (_stake("AAA", form="SCHEDULE 13D/A", percent=8.0), 0),               # no growth on file
    (_stake("AAA", form="SCHEDULE 13D/A", percent=8.0, prev=6.0), 15),    # grew 2 points
    (_stake("AAA", form="SCHEDULE 13D", percent=60.0), 0),                # controls the company
    (_stake("AAA", form="SCHEDULE 13G/A", percent=12.0), 0),
    (_stake("AAA", form="SCHEDULE 13G", percent=55.0), 0),
])
def test_a_stake_among_todays_candidates_counts_only_when_it_scores(conn, stake, points):
    [aaa] = [s for s in _score(conn, [_sig("AAA"), stake], {"AAA": _stock_bars()})
             if s.ticker == "AAA" and s.signal is not stake]
    assert aaa.triggers == points


# ------------------------------------------------- pruning: no fetch for what can't be watched
def test_a_signal_that_cannot_reach_the_watchlist_is_scored_without_any_fetch(conn):
    """insiders + triggers + the most momentum (15) and news (10) could add: below 45 -> no
    price history and no headlines are fetched; it is still scored, as a skip."""
    fetched, asked = [], []

    def fetch(symbol, days=None):
        fetched.append(symbol)
        return _stock_bars()

    def news(ticker, source):
        asked.append(ticker)
        return [{"title": "Acme wins contract"}]

    low = _stake("LOW", form="SCHEDULE 13G", percent=9.0)         # 19: at most 44
    amended = _stake("AMD", form="SCHEDULE 13D/A", percent=8.0)   # 0
    edge = _stake("EDG", form="SCHEDULE 13G", percent=10.0)       # 15 ... 20 + 25 = 45: fetched
    scored = model.score_today(conn, TODAY, fetch=fetch, news_fn=news, trend_fn=lambda c, s: None,
                               signals=[low, amended, edge], t212=T212)
    stocks = {s.ticker: s for s in scored if s.kind == "stock"}
    assert "LOW" not in fetched and "AMD" not in fetched and "EDG" in fetched
    assert [t for t in asked if not t.startswith("CRYPTO")] == []        # EDG scores 15 < 35: no news
    for ticker, total in (("LOW", 19.0), ("AMD", 0.0)):
        s = stocks[ticker]
        assert (s.total, s.decision, s.momentum, s.news) == (total, "skip", 0, 0)
        assert s.stop_pct is None and s.last_close is None
        assert s.reasons[-1] == "импульс и новости не считались: до наблюдения не дотянуть"
    assert "не считались" not in " ".join(stocks["EDG"].reasons)


def test_without_pruning_even_a_weak_signal_gets_its_prices_and_news(conn):
    """The analyst asks about one ticker: momentum and news are worth fetching then."""
    fetched, asked = [], []

    def fetch(symbol, days=None):
        fetched.append(symbol)
        return _stock_bars()

    low = _stake("LOW", form="SCHEDULE 13G", percent=9.0)
    scored = model.score_today(conn, TODAY, fetch=fetch, news_fn=lambda t, s: asked.append(t) or [],
                               trend_fn=lambda c, s: None, signals=[low], t212=T212, prune=False)
    [s] = [s for s in scored if s.ticker == "LOW"]
    assert "LOW" in fetched and "не считались" not in " ".join(s.reasons)


def test_the_pruning_line_is_the_watch_bar_less_the_momentum_and_news_caps(conn):
    fetched = []

    def fetch(symbol, days=None):
        fetched.append(symbol)
        return []

    politician = _sig("POL", source="HOUSE", n=2, roles=(), pct=None)     # 0 + 5
    model.score_today(conn, TODAY, fetch=fetch, news_fn=lambda t, s: [], trend_fn=lambda c, s: None,
                      signals=[politician, _sig("AAA")], t212=T212)
    assert "POL" not in fetched and "AAA" in fetched
    assert model_score.MOMENTUM_CAP + model_score.NEWS_MAX == 25 and model_score.STOCK_WATCH == 45


# ------------------------------------------------- today's scores are kept for the menu and the analyst
def test_the_day_keeps_its_scores_and_they_come_back_as_they_were(conn):
    import telegram_notify
    report = _score_day(conn, [_sig("AAA", corroborated=("BAFIN",)), _sig("BBB", n=2, roles=(), pct=None)],
                  {"AAA": _stock_bars(), "BBB": _stock_bars(), "BTC-USD": _rising()},
                  t212=types.SimpleNamespace(can_buy=lambda t, s: t != "BBB"))
    cached = model.cached_scores(conn, TODAY)
    assert [(s.kind, s.ticker, s.total, s.decision) for s in cached] == [
        (s.kind, s.ticker, s.total, s.decision) for s in report.scored]
    by = {s.ticker: s for s in cached}
    aaa, real = by["AAA"], next(s for s in report.scored if s.ticker == "AAA")
    for attr in ("company", "source", "insiders", "triggers", "momentum", "news", "reasons", "block",
                 "untradeable", "t212", "stop_pct"):
        assert getattr(aaa, attr) == getattr(real, attr), attr
    assert by["BBB"].t212 is False
    btc = by["CRYPTO:BTC"]
    assert (btc.kind, btc.coin, btc.trend, btc.flows, btc.news) == ("crypto", "BTC", 60, 0, 0)
    assert telegram_notify.format_scored(cached) == telegram_notify.format_scored(report.scored)


def test_only_todays_kept_scores_count(conn):
    _score_day(conn, [_sig("AAA")], {"AAA": _stock_bars()})
    assert model.cached_scores(conn, TODAY) is not None
    assert model.cached_scores(conn, TODAY + dt.timedelta(days=1)) is None
    assert model.cached_scores(conn, YESTERDAY) is None


def test_a_failed_scoring_keeps_nothing(conn, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("finders down")
    monkeypatch.setattr(model, "score_today", boom)
    _score_day(conn, [_sig("AAA")], {"AAA": _stock_bars()})
    assert model.cached_scores(conn, TODAY) is None


def test_no_scoring_today_means_no_kept_scores(conn):
    assert model.cached_scores(conn, TODAY) is None


# ------------------------------------------------- news is fetched once a ticker for the whole pass
def test_headlines_are_fetched_once_per_ticker_for_the_whole_pass(conn):
    calls = []
    _score(conn, [_sig("AAA"), _sig("AAA", n=4)], {"AAA": _stock_bars()},
           news_fn=lambda t, s: calls.append((t, s)) or [])
    assert calls.count(("AAA", "SEC")) == 1                       # two signals on one name: one fetch
    assert ("CRYPTO:BTC", "CRYPTO") in calls and ("CRYPTO:ETH", "CRYPTO") in calls


# ------------------------------------------------- the thirteen coins and the bitcoin regime filter
ALTS = model.COINS[2:]


def _coin(scored, coin):
    return next(s for s in scored if s.ticker == f"CRYPTO:{coin}")


def test_the_scored_coins_are_the_thirteen_and_the_majors_are_bitcoin_and_ether():
    assert model.COINS == ("BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "AVAX", "HYPE", "LTC", "ENA", "LINK",
                           "TRX", "SUI")
    assert model.MAJOR_COINS == ("BTC", "ETH") and ALTS == model.COINS[2:] and len(ALTS) == 11


def test_all_thirteen_coins_are_scored_and_kept(conn):
    series = {f"{c}-USD": _rising() for c in model.COINS}
    report = _score_day(conn, [], series)
    assert sorted(s.ticker for s in report.scored) == sorted(f"CRYPTO:{c}" for c in model.COINS)
    assert {s.decision for s in report.scored} == {"buy"} and {s.total for s in report.scored} == {60}
    kept = model.cached_scores(conn, TODAY)
    assert sorted(s.coin for s in kept) == sorted(model.COINS)


def test_each_coin_score_has_the_60_day_return_of_its_own_closes_and_the_day_keeps_it(conn):
    flat = _bars([100.0] * 200, YESTERDAY)
    scored = _score_day(conn, [], {"BTC-USD": _rising(), "SOL-USD": _falling(), "XRP-USD": flat,
                                   "LINK-USD": _rising(100)}).scored
    by = {s.coin: s for s in scored if s.kind == "crypto"}
    assert by["BTC"].ret60 == pytest.approx(1.002 ** 60 - 1)
    assert by["SOL"].ret60 == pytest.approx(101.0 / 161.0 - 1) and by["XRP"].ret60 == 0.0      # 300 falling by 1 a day
    assert by["LINK"].ret60 is None and by["DOGE"].ret60 is None                  # under 121 closes, and no prices at all
    kept = {s.coin: s.ret60 for s in model.cached_scores(conn, TODAY) if s.kind == "crypto"}
    assert kept == {c: by[c].ret60 for c in model.COINS}


def test_a_stock_has_no_return_in_the_kept_scores(conn):
    _score_day(conn, [_sig("AAA")], {"AAA": _stock_bars()})
    assert not hasattr(next(s for s in model.cached_scores(conn, TODAY) if s.ticker == "AAA"), "ret60")


def test_every_scored_coin_asks_for_its_own_prices_and_headlines(conn):
    fetched, asked = [], []

    def fetch(symbol, days=None):
        fetched.append(symbol)
        return []
    model.score_today(conn, TODAY, fetch=fetch, news_fn=lambda t, s: asked.append((t, s)) or [],
                      trend_fn=lambda c, s: None, signals=[], t212=T212)
    assert sorted(set(fetched)) == sorted(f"{c}-USD" for c in model.COINS)
    assert sorted(asked) == sorted((f"CRYPTO:{c}", "CRYPTO") for c in model.COINS)


@pytest.mark.parametrize("coin", ALTS)
def test_an_alt_uptrend_is_a_buy_when_bitcoin_is_above_its_100_day_average(conn, coin):
    scored = _score(conn, [], {"BTC-USD": _rising(), f"{coin}-USD": _rising()})
    s = _coin(scored, coin)
    assert (s.total, s.decision) == (60, "buy") and "биткоин ниже" not in " ".join(s.reasons)


@pytest.mark.parametrize("coin", ALTS)
def test_an_alt_uptrend_is_only_watched_when_bitcoin_is_below_it(conn, coin):
    scored = _score(conn, [], {"BTC-USD": _falling(), f"{coin}-USD": _rising()})
    s = _coin(scored, coin)
    assert (s.total, s.decision) == (60, "watch")
    assert s.reasons[-1] == "биткоин ниже 100-дн. средней — альты не покупаем"


def test_bitcoin_and_ether_are_not_gated(conn):
    scored = _score(conn, [], {"BTC-USD": _rising(), "ETH-USD": _rising()})
    assert (_coin(scored, "BTC").decision, _coin(scored, "ETH").decision) == ("buy", "buy")
    scored = _score(conn, [], {"BTC-USD": _falling(), "ETH-USD": _rising()})        # bitcoin itself is down
    assert _coin(scored, "BTC").decision == "watch"
    eth = _coin(scored, "ETH")
    assert (eth.decision, eth.total) == ("buy", 60) and "биткоин ниже" not in " ".join(eth.reasons)


def test_the_regime_is_bitcoins_close_against_its_100_day_mean_and_nothing_else(conn):
    """Bitcoin above its mean but with falling returns is "up" for the filter: only above_ma100 counts."""
    closes = [300.0] * 21 + [100.0] * 79 + [110.0] * 20 + [120.0]                   # above the mean, 120-day return down
    assert model_score.coin_trend(closes)["above_ma100"] is True
    scored = _score(conn, [], {"BTC-USD": _bars(closes, YESTERDAY), "SOL-USD": _rising()})
    assert _coin(scored, "SOL").decision == "buy"
    assert _coin(scored, "BTC").decision == "watch"                                  # its own trend is not up for 60


def test_without_a_bitcoin_history_the_alts_are_not_bought(conn):
    scored = _score(conn, [], {"SOL-USD": _rising()})                               # BTC-USD: nothing
    sol = _coin(scored, "SOL")
    assert (sol.total, sol.decision) == (60, "watch") and "альты не покупаем" in sol.reasons[-1]


def test_an_alt_with_too_little_history_stays_watch_with_its_own_reason(conn):
    scored = _score(conn, [], {"BTC-USD": _rising(), "SOL-USD": _rising(100)})
    sol = _coin(scored, "SOL")
    assert sol.decision == "watch" and sol.reasons == ["мало истории"]
    scored = _score(conn, [], {"BTC-USD": _falling(), "SOL-USD": _rising(100)})
    assert _coin(scored, "SOL").reasons == ["мало истории"]                          # the gate had nothing to stop


def test_an_alt_uses_the_bitcoin_closes_completed_before_today(conn):
    """A bar dated today is still in progress: a crash in it must not flip the regime."""
    rising_then_crash = _rising() + [(TODAY.isoformat(), 1.0)]
    scored = _score(conn, [], {"BTC-USD": rising_then_crash, "SOL-USD": _rising()})
    assert _coin(scored, "SOL").decision == "buy"


def test_an_alts_own_etf_inflow_counts_and_a_confirmed_outflow_blocks_it(conn):
    scored = _score(conn, [_flow("SOL")], {"BTC-USD": _rising(), "SOL-USD": _rising()})
    sol = _coin(scored, "SOL")
    assert (sol.flows, sol.total, sol.decision) == (15, 75, "buy")
    asked = []
    scored = _score(conn, [_flow("SOL", bullish=False)], {"BTC-USD": _rising(), "SOL-USD": _rising()},
                    trend_fn=lambda c, s: asked.append(s) or FALLING)
    sol = _coin(scored, "SOL")
    assert (sol.flows, sol.decision, sol.caution) == (-20, "block", "спот-ETF США — цена подтверждает")
    assert asked == ["SOL"]


def test_a_flow_on_one_coin_is_not_another_coins(conn):
    scored = _score(conn, [_flow("LINK")], {"BTC-USD": _rising()})
    assert {s.coin: s.flows for s in scored if s.kind == "crypto"} == {
        c: (15 if c == "LINK" else 0) for c in model.COINS}


def test_the_coin_red_flags_apply_to_an_alt(conn):
    scored = _score(conn, [], {"BTC-USD": _rising(), "HYPE-USD": _rising()},
                    news_fn=lambda t, s: [{"title": "Hyperliquid exploit drains vault"}] if t.endswith("HYPE") else [])
    hype = _coin(scored, "HYPE")
    assert (hype.decision, hype.block) == ("block", "Hyperliquid exploit drains vault")


def test_a_sol_etf_inflow_goes_through_the_whole_chain_to_a_high_risk_buy_line(conn):
    """Farside rows -> the ETF finder -> the day's scores -> the week's pick -> the message."""
    import cluster
    import crypto_etf
    import signals_weekly
    import weekly
    quiet = [1e6, -1e6] * 5                                                      # ten ordinary days, then $60m
    amounts = quiet + [60e6]
    db.save_etf_flows(conn, [crypto_etf.Flow("SOL", (TODAY - dt.timedelta(days=len(amounts) - i)).isoformat(),
                                             "BSOL", usd) for i, usd in enumerate(amounts)])
    signals = cluster.find_etf_flow_signals(conn, today=TODAY)                  # $60m on a short history: SOL's $50m bar
    assert [(s.ticker, s.bullish) for s in signals] == [("CRYPTO:SOL", True)]
    series = {"BTC-USD": _rising(), "SOL-USD": _rising(), "ETH-USD": _falling()}
    scored = _score(conn, signals, series)
    sol = _coin(scored, "SOL")
    assert (sol.flows, sol.total, sol.decision) == (15, 75, "buy")
    picks = signals_weekly.pick_buys(conn, TODAY, scored)
    assert [p.ticker for p in picks] == ["CRYPTO:SOL", "CRYPTO:BTC"]                           # 75 before 60
    texts = dict(weekly.week_signals(conn, TODAY, [signals_weekly.pick_record(p) for p in picks]))
    assert texts["buy:CRYPTO:SOL"] == ("🟢 <b>SOL!</b>: покупка — выше 100-дн. средней; 20 дн. +4%; "
                                       "балл 75, стоп −15%, высокий риск")
    assert texts["buy:CRYPTO:BTC"] == "🟢 <b>BTC!</b>: покупка — выше 100-дн. средней; 20 дн. +4%; балл 60, стоп −15%"


# ------------------------------------------------- the exit rules the user's positions read from here
def test_the_exit_constants_and_the_activist_helper_positions_reads_are_here():
    assert model.FALLBACK_STOP == {"stock": 0.15, "crypto": 0.25}
    assert (model.DEAD_MONEY_BDAYS, model.DEAD_MONEY_MIN_RETURN, model.MAX_HOLD_DAYS) == (60, 0.05, 365)
    assert model.STAKE_SOURCE == "SEC13DG" and callable(model._activist_cut) and callable(model.default_news)
