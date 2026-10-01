"""model.py: the model portfolio on the paper books. Offline -- every seam (prices, news, the
coin trend, sectors, Trading 212, the signals) is a stub, and the dates are fixed so weekday
arithmetic is deterministic."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import types

import pytest

import db
import model
import model_score
import paper
from cluster.roles import Buyer
from conftest import add_sec_purchase, add_sec_sale, add_stake

TODAY = dt.date(2026, 10, 5)          # a Monday
YESTERDAY = TODAY - dt.timedelta(days=1)
S, C = model.STOCK_BOOK, model.CRYPTO_BOOK
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
    return {"news_fn": lambda t, s: [], "trend_fn": lambda c, s: None,
            "sector_fn": lambda t, s: None, "t212": T212, **over}


def _run(conn, signals=(), series=None, today=TODAY, **seams):
    return model.run(conn, today, fetch=Fetch(series or {}), signals=list(signals), **_seams(**seams))


def _score(conn, signals=(), series=None, **seams):
    seams = _seams(**seams)
    del seams["sector_fn"]
    return model.score_today(conn, TODAY, fetch=Fetch(series or {}), signals=list(signals), **seams)


def _books(conn):
    model.create_books(conn, TODAY - dt.timedelta(days=30))


def _position(conn, ticker="AAA", *, book=S, source="SEC", fill_days_ago=10, net=8_000.0,
              value=None, insiders=("Jane Doe",), stop_pct=0.10, score=64.0, closed_days_ago=None):
    """A position written straight into the book (its cost comes out of the book's cash)."""
    _books(conn)
    closed = _day(closed_days_ago) if closed_days_ago is not None else None
    symbol = paper.listing(ticker, source)[0]
    cur = conn.execute(
        "INSERT INTO paper_positions (book, ticker, source, symbol, currency, fill_date, cost_eur, "
        "net_eur, entry_close, entry_fx, insiders, reason, last_value, stop_pct, score, closed_date, "
        "close_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (book, ticker, source, symbol, "USD", _day(fill_days_ago), net, net, 100.0, 1.16,
         json.dumps(list(insiders)), "балл 64: тест", value if value is not None else net, stop_pct,
         score, closed, "стоп" if closed else None))
    if closed is None:
        conn.execute("UPDATE paper_books SET cash_eur = cash_eur - ? WHERE code = ?", (net, book))
    conn.commit()
    return paper._rows(conn, "SELECT * FROM paper_positions WHERE id = ?", (cur.lastrowid,))[0]


def _coin_position(conn, **kw):
    kw = {"net": 10_000.0, "fill_days_ago": 5, "stop_pct": None, "insiders": (), **kw}
    return _position(conn, "CRYPTO:BTC", book=C, source="CRYPTO", **kw)


def _by_status(conn, book, status):
    return [o for o in paper.orders(conn, book) if o["status"] == status]


# ------------------------------------------------------------------- books
def test_books_are_created_once_with_their_money(conn):
    model.create_books(conn, TODAY)
    model.create_books(conn, TODAY + dt.timedelta(days=1))
    rows = conn.execute("SELECT code, sleeve, start_date, start_eur, cash_eur, bench_symbol "
                        "FROM paper_books ORDER BY code").fetchall()
    assert rows == [("MODEL-C", "crypto", TODAY.isoformat(), 30_000.0, 30_000.0, "BTC-USD"),
                    ("MODEL-S", "stock", TODAY.isoformat(), 70_000.0, 70_000.0, "SPY")]
    assert model.BOOKS == ("MODEL-S", "MODEL-C")


def test_the_stop_and_score_columns_reach_a_database_made_before_them():
    old = sqlite3.connect(":memory:")
    for table in ("paper_orders", "paper_positions"):
        old.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, book TEXT)")
    db._migrate(old)
    for table in ("paper_orders", "paper_positions"):
        assert {"stop_pct", "score"} <= {r[1] for r in old.execute(f"PRAGMA table_info({table})")}


def test_the_model_value_is_both_books_and_zero_before_they_exist(conn):
    assert model.model_value(conn) == 0.0
    _books(conn)
    assert model.model_value(conn) == 100_000.0
    _position(conn, value=9_000.0, net=8_000.0)      # up 1 000 on 8 000 that left the cash
    assert model.model_value(conn) == pytest.approx(101_000.0)


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


# --------------------------------------------------------------- the buys
def test_a_buying_stock_places_one_pending_order_sized_by_its_stop(conn):
    closes = _zigzag(100, 100.0, 105.0)
    stop = model_score.stop_distance(closes, "stock")
    assert 0.10 < stop < 0.25                          # neither clamp: the size follows the stop
    report = _run(conn, [_sig("AAA")], {"AAA": _bars(closes, YESTERDAY)})
    [o] = paper.orders(conn, S)
    assert (o["ticker"], o["source"], o["side"], o["status"]) == ("AAA", "SEC", "buy", "pending")
    assert o["amount_eur"] == pytest.approx(1_000 / stop) and o["amount_eur"] < 10_000
    assert o["stop_pct"] == pytest.approx(stop) and o["score"] == 60.0
    assert o["reason"] == "балл 60: 3 инсайдера из руководства; CEO среди покупателей; 0,25% компании"
    assert json.loads(o["insiders"]) == ["P0", "P1", "P2"] and o["created"] == TODAY.isoformat()
    [t] = report.buys
    assert (t.side, t.ticker, t.company, t.score, t.result, t.t212) == ("buy", "AAA", "AAA Corp", 60.0, None, True)
    assert t.amount_eur == pytest.approx(o["amount_eur"]) and t.stop_pct == pytest.approx(stop)
    assert t.reasons == ["3 инсайдера из руководства", "CEO среди покупателей", "0,25% компании"]
    assert report.sells == [] and paper.orders(conn, C) == []


def test_the_order_fills_at_the_next_close_and_the_position_carries_its_stop_and_score(conn):
    _run(conn, [_sig("AAA")], {"AAA": _stock_bars()})
    later = dt.date(2026, 10, 7)
    _run(conn, [], {"AAA": _stock_bars(tail=(110.0, 130.0))}, today=later)     # closes of 10/05, 10/06
    [p] = paper.open_positions(conn, S)
    assert (p["fill_date"], p["entry_close"], p["cost_eur"]) == ("2026-10-06", 130.0, 4_000.0)
    assert (p["stop_pct"], p["score"]) == (0.25, 60.0)
    assert [o["status"] for o in paper.orders(conn, S)] == ["filled"]
    assert paper.cash(conn, S) == pytest.approx(66_000.0)


def test_two_buys_are_placed_highest_score_first(conn):
    report = _run(conn, [_sig("AAA"), _sig("BBB", corroborated=("BAFIN",))],
                  {"AAA": _stock_bars(), "BBB": _stock_bars()})
    assert [(o["ticker"], o["score"]) for o in paper.orders(conn, S)] == [("BBB", 65.0), ("AAA", 60.0)]
    assert [t.ticker for t in report.buys] == ["BBB", "AAA"]


def test_a_thirteenth_buy_finds_no_place(conn):
    tickers = [f"T{i:02d}" for i in range(13)]
    report = _run(conn, [_sig(t) for t in tickers], {t: _stock_bars() for t in tickers})
    placed = _by_status(conn, S, "pending")
    assert [o["ticker"] for o in placed] == tickers[:12]
    assert {o["amount_eur"] for o in placed} == {4_000.0}         # 1% of 100k / a 25% stop
    [skipped] = _by_status(conn, S, "skipped")
    assert (skipped["ticker"], skipped["note"]) == ("T12", "мест нет")
    assert skipped["score"] == 60.0 and skipped["stop_pct"] == 0.25
    assert len(report.buys) == 12


def test_the_same_skip_on_consecutive_days_is_recorded_once(conn):
    tickers = [f"T{i:02d}" for i in range(13)]
    series = {t: _stock_bars() for t in tickers}
    _run(conn, [_sig(t) for t in tickers], series)
    _run(conn, [_sig(t) for t in tickers], series, today=TODAY + dt.timedelta(days=1))
    [skipped] = _by_status(conn, S, "skipped")
    assert (skipped["ticker"], skipped["note"], skipped["created"]) == ("T12", "мест нет", TODAY.isoformat())
    assert len(_by_status(conn, S, "pending")) == 12


def test_a_fourth_buy_in_one_sector_is_skipped_and_an_unknown_sector_is_free(conn):
    sectors = {"AAA": "Tech", "BBB": "Tech", "CCC": "Tech", "DDD": "Tech", "EEE": "Energy"}
    tickers = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]                 # FFF: no sector known
    report = _run(conn, [_sig(t) for t in tickers], {t: _stock_bars() for t in tickers},
                  sector_fn=lambda t, s: sectors.get(t))
    assert [o["ticker"] for o in _by_status(conn, S, "pending")] == ["AAA", "BBB", "CCC", "EEE", "FFF"]
    [skipped] = _by_status(conn, S, "skipped")
    assert (skipped["ticker"], skipped["note"]) == ("DDD", "сектор заполнен")
    assert [t.ticker for t in report.buys] == ["AAA", "BBB", "CCC", "EEE", "FFF"]


def test_held_positions_count_toward_the_sector_cap(conn):
    for t in ("HH1", "HH2", "HH3"):
        _position(conn, t)
    sectors = {"HH1": "Tech", "HH2": "Tech", "HH3": "Tech", "AAA": "Tech"}
    _run(conn, [_sig("AAA")], {"AAA": _stock_bars()}, sector_fn=lambda t, s: sectors.get(t))
    [o] = paper.orders(conn, S)
    assert (o["ticker"], o["status"], o["note"]) == ("AAA", "skipped", "сектор заполнен")


def _fake_yfinance(monkeypatch, info=None, error=None):
    """Stands in for yfinance.Ticker(symbol).info; returns the list of symbols asked."""
    import yfinance
    asked = []

    class FakeTicker:
        def __init__(self, symbol):
            asked.append(symbol)

        @property
        def info(self):
            if error:
                raise error
            return info
    monkeypatch.setattr(yfinance, "Ticker", FakeTicker)
    return asked


def test_the_default_sector_comes_from_yfinance_info(conn, monkeypatch):
    asked = _fake_yfinance(monkeypatch, {"sector": "Tech"})
    tickers = ["AAA", "BBB", "CCC", "DDD"]
    _run(conn, [_sig(t) for t in tickers], {t: _stock_bars() for t in tickers}, sector_fn=None)
    assert [o["note"] for o in paper.orders(conn, S)] == [None, None, None, "сектор заполнен"]
    assert asked.count("AAA") == 1                            # looked up once for the whole run


def test_the_default_sector_is_asked_on_the_listings_own_symbol(monkeypatch):
    asked = _fake_yfinance(monkeypatch, {"sector": "Energy"})
    assert model._default_sector("EQNR", "NORWAY") == "Energy"
    assert model._default_sector("BRK.B", "SEC") == "Energy"
    assert asked == ["EQNR.OL", "BRK-B"]                      # not EQNR-OL


def test_the_default_sector_is_none_when_unknown_or_failing(monkeypatch, capsys):
    asked = _fake_yfinance(monkeypatch, {"longName": "Acme"})
    assert model._default_sector("AAA", "SEC") is None        # no sector in the info
    asked.clear()
    assert model._default_sector("DE0007164600", "BAFIN") is None and asked == []   # no listing
    _fake_yfinance(monkeypatch, None)
    assert model._default_sector("AAA", "SEC") is None        # info is None
    _fake_yfinance(monkeypatch, error=OSError("down"))
    assert model._default_sector("AAA", "SEC") is None
    assert "no sector for AAA" in capsys.readouterr().err


@pytest.mark.parametrize("closed_days_ago,placed", [(10, False), (30, False), (31, True)])
def test_a_ticker_sold_within_30_days_is_not_bought_back(conn, closed_days_ago, placed):
    _position(conn, "AAA", closed_days_ago=closed_days_ago)
    report = _run(conn, [_sig("AAA")], {"AAA": _stock_bars()})
    [o] = paper.orders(conn, S)
    if placed:
        assert (o["status"], len(report.buys)) == ("pending", 1)
    else:
        assert (o["status"], o["note"], report.buys) == ("skipped", "недавно продан", [])


def test_an_order_below_the_minimum_is_skipped_as_too_small(conn):
    model.create_books(conn, TODAY)
    conn.execute("UPDATE paper_books SET cash_eur = 1000")        # a model worth 2 000
    report = _run(conn, [_sig("AAA")], {"AAA": _stock_bars()})
    [o] = paper.orders(conn, S)
    assert (o["status"], o["note"], o["amount_eur"]) == ("skipped", "мало", None)
    assert report.buys == []                                       # 1% of 2 000 / 25% = 80


@pytest.mark.parametrize("cash,status,amount", [(200.0, "skipped", None), (700.0, "pending", 700.0)])
def test_a_sleeve_short_of_cash_buys_with_what_is_left_if_that_is_half(conn, cash, status, amount):
    model.create_books(conn, TODAY)
    conn.execute("UPDATE paper_books SET cash_eur = ? WHERE code = ?", (cash, S))
    _run(conn, [_sig("AAA")], {"AAA": _stock_bars()})
    [o] = paper.orders(conn, S)
    assert (o["status"], o["amount_eur"]) == (status, amount)     # the size is ~1 200
    assert o["note"] == ("нет денег" if status == "skipped" else None)
    assert (o["stop_pct"], o["score"]) == (0.25, 60.0)            # kept on the skip, too


def test_a_held_or_pending_ticker_gets_no_second_order_and_no_skip_row(conn):
    _position(conn, "AAA", stop_pct=0.25)
    signals, series = [_sig("AAA"), _sig("BBB")], {"AAA": _stock_bars(), "BBB": _stock_bars()}
    _run(conn, signals, series)
    assert [(o["ticker"], o["status"]) for o in paper.orders(conn, S)] == [("BBB", "pending")]
    report = _run(conn, signals, series)                            # the same day again
    assert len(paper.orders(conn, S)) == 1 and report.buys == []


def test_a_watch_or_blocked_stock_is_not_bought(conn):
    weak = _sig("WWW", n=2, roles=(), pct=None)                     # 34: skip
    report = _run(conn, [weak, _sig("BBB")], {"WWW": _stock_bars(), "BBB": _stock_bars()},
                  news_fn=lambda t, s: [{"title": "BBB faces SEC investigation"}])
    assert paper.orders(conn, S) == [] and report.buys == []
    assert report.decisions["BBB"] == model_score.BLOCK and report.decisions["WWW"] == model_score.SKIP


# ------------------------------------------------ the weekly buys: run(buy=False)
def test_a_run_without_buys_places_no_order_and_keeps_scoring_and_the_snapshot(conn):
    series = {"AAA": _stock_bars(), "BTC-USD": _rising()}
    report = _run(conn, [_sig("AAA"), _flow("BTC")], series, buy=False)
    assert paper.orders(conn, S) == [] and paper.orders(conn, C) == []     # no buy, no skip row either
    assert report.buys == []
    assert report.decisions["AAA"] == model_score.BUY                      # ... but it was scored,
    assert report.decisions["CRYPTO:BTC"] == model_score.BUY
    assert [s.ticker for s in model.cached_scores(conn, TODAY)][:1] == ["CRYPTO:BTC"]      # ... and kept
    assert conn.execute("SELECT COUNT(*) FROM paper_equity").fetchone()[0] == 2      # ... and stamped


def test_a_run_without_buys_still_sells_on_an_exit_rule(conn):
    _position(conn, "AAA", stop_pct=0.10)                     # the zigzag's peak 120 puts the line at 108
    report = _run(conn, [_sig("BBB")], {"AAA": _stock_bars(), "BBB": _stock_bars()}, buy=False)
    assert [t.reasons for t in report.sells] == [["стоп: −10% от максимума"]]
    assert [(o["ticker"], o["side"], o["status"]) for o in paper.orders(conn, S)] == [("AAA", "sell", "pending")]
    assert report.buys == []


def test_a_run_without_buys_does_not_skip_a_blocked_candidate_either(conn):
    _position(conn, "OLD", closed_days_ago=5)                 # sold 5 days ago: a buy would be «недавно продан»
    _run(conn, [_sig("OLD")], {"OLD": _stock_bars()}, buy=False)
    assert paper.orders(conn, S) == []


def test_buys_are_on_unless_asked_off(conn):
    series = {"AAA": _stock_bars(), "BTC-USD": _rising()}
    report = _run(conn, [_sig("AAA"), _flow("BTC")], series)               # the default
    assert sorted(t.ticker for t in report.buys) == ["AAA", "CRYPTO:BTC"]
    explicit = _run(conn, [_sig("BBB")], {"BBB": _stock_bars()}, buy=True)
    assert [t.ticker for t in explicit.buys] == ["BBB"]


# ------------------------------------------------------------- stock exits
PATH = [100, 105, 110, 120, 130, 128, 125, 122, 120]     # closes from the fill day (10 days ago) on


def _path(last):
    return PATH + [last]


def _exit(conn, pos, closes=None, headlines=None, today=TODAY):
    bars = _bars(closes, YESTERDAY) if closes else []
    return model.stock_exit_reason(conn, pos, bars, today, headlines)


@pytest.mark.parametrize("last,expected", [(116, "стоп: −10% от максимума"), (118, None)])
def test_the_trailing_stop_follows_the_highest_close_since_the_fill(conn, last, expected):
    pos = _position(conn, stop_pct=0.10)                        # peak 130: the line is 117
    assert _exit(conn, pos, _path(last)) == expected


def test_the_fill_days_own_close_can_be_the_peak(conn):
    pos = _position(conn, stop_pct=0.10)
    fell = [100, 95, 96, 95, 94, 93, 92, 91, 90, 89]         # the fill day is the highest close
    assert _exit(conn, pos, fell) == "стоп: −10% от максимума"


def test_a_high_before_the_fill_does_not_set_the_peak(conn):
    pos = _position(conn, stop_pct=0.10)
    bars = _bars([200.0, 200.0] + _path(118), YESTERDAY)        # two closes before the fill day
    assert model.stock_exit_reason(conn, pos, bars, TODAY, None) is None


def test_a_position_without_a_stored_stop_takes_the_one_its_history_gives(conn):
    pos = _position(conn, stop_pct=None)
    calm = _bars([100.0] * 24 + _path(116), YESTERDAY)          # flat before the fill: the 10% floor
    assert model.stock_exit_reason(conn, pos, calm, TODAY, None) == "стоп: −10% от максимума"


@pytest.mark.parametrize("last,expected", [(110, "стоп: −15% от максимума"), (112, None)])
def test_with_no_history_before_the_fill_the_stop_is_15_percent(conn, last, expected):
    pos = _position(conn, stop_pct=None)                        # peak 130: the line is 110.5
    assert _exit(conn, pos, _path(last)) == expected


def test_an_insider_selling_after_the_fill_sells(conn):
    add_sec_sale(conn, "AAA", "Jane Doe", 500_000, _day(5))
    pos = _position(conn)
    assert _exit(conn, pos, [100.0] * 10) == f"продаёт инсайдер: Jane Doe — Form 4, {_day(5)}"


def test_an_insider_sale_before_the_fill_does_not_count(conn):
    add_sec_sale(conn, "AAA", "Jane Doe", 500_000, _day(20))
    assert _exit(conn, _position(conn), [100.0] * 10) is None


@pytest.mark.parametrize("before,after,source,expected", [
    (9.0, 6.0, "SEC13DG", "активист сократил долю"),
    (9.0, 11.0, "SEC13DG", None),                # raised
    (9.0, 9.0, "SEC13DG", None),                 # unchanged
    (9.0, None, "SEC13DG", None),                # nothing filed since
    (9.0, 6.0, "SEC", None),                     # a cluster buy, not a stake
])
def test_an_activist_cutting_the_stake_sells(conn, before, after, source, expected):
    add_stake(conn, "AAA", "Fund LP", before, event_date=_day(30))
    if after is not None:
        add_stake(conn, "AAA", "Fund LP", after, event_date=_day(3))
    add_stake(conn, "AAA", "Someone Else", 1.0, event_date=_day(2))     # another holder: ignored
    pos = _position(conn, source=source, insiders=("Fund LP",))
    assert _exit(conn, pos, [100.0] * 10) == expected


@pytest.mark.parametrize("days_ago,bdays,value,expected", [
    (83, 59, 8_160.0, None),                    # one business day short
    (84, 60, 8_160.0, "стоит на месте"),        # +2%
    (85, 61, 8_160.0, "стоит на месте"),
    (85, 61, 8_399.0, "стоит на месте"),        # +4.99%
    (85, 61, 8_480.0, None),                    # +6%: it is going somewhere
])
def test_a_position_that_goes_nowhere_for_60_business_days_sells(conn, days_ago, bdays, value, expected):
    pos = _position(conn, fill_days_ago=days_ago, value=value, net=8_000.0)
    assert paper.business_days_between(pos["fill_date"], TODAY) == bdays
    assert _exit(conn, pos, [100.0] * days_ago) == expected


@pytest.mark.parametrize("days_ago,value,expected", [
    (364, 12_000.0, None),
    (365, 12_000.0, "год в позиции"),
    (365, 8_160.0, "стоит на месте"),           # dead money is checked first
])
def test_a_position_held_a_year_sells(conn, days_ago, value, expected):
    pos = _position(conn, fill_days_ago=days_ago, value=value, net=8_000.0)
    assert _exit(conn, pos, [100.0] * days_ago) == expected


def test_a_red_flag_headline_sells_and_other_news_does_not(conn):
    pos = _position(conn)
    red = [{"title": "Acme under SEC investigation", "published": "2026-10-01"}]
    bad = [{"title": "Analyst downgrade for Acme", "published": "2026-10-01"}]
    assert _exit(conn, pos, [100.0] * 10, red) == "новости: Acme under SEC investigation"
    assert _exit(conn, pos, [100.0] * 10, bad) is None


def test_headlines_are_fetched_only_when_no_earlier_rule_fires(conn):
    asked = []

    def headlines():
        asked.append(1)
        return [{"title": "Acme accused of fraud"}]

    pos = _position(conn, stop_pct=0.10)
    assert _exit(conn, pos, _path(116), headlines) == "стоп: −10% от максимума"
    assert asked == []                                        # the stop decided: no fetch
    assert _exit(conn, pos, _path(118), headlines) == "новости: Acme accused of fraud"
    assert asked == [1]


def test_a_coins_headlines_are_fetched_last_too(conn):
    asked = []

    def headlines():
        asked.append(1)
        return [{"title": "Exchange hack drains hot wallet"}]

    pos = _coin_position(conn)
    assert _coin_exit(conn, pos, _falling(), headlines) == "тренд вниз"
    assert asked == []
    assert _coin_exit(conn, pos, _rising(), headlines) == "новости: Exchange hack drains hot wallet"
    assert asked == [1]


def test_the_first_matching_exit_wins(conn):
    add_sec_sale(conn, "AAA", "Jane Doe", 500_000, _day(5))
    pos = _position(conn, stop_pct=0.10)
    red = [{"title": "Acme fraud"}]
    assert _exit(conn, pos, _path(116), red) == "стоп: −10% от максимума"
    assert _exit(conn, pos, _path(118), red).startswith("продаёт инсайдер")


# --------------------------------------------------------------- coin exits
def _coin_exit(conn, pos, bars, headlines=None, trend=None):
    return model.coin_exit_reason(conn, pos, bars, TODAY, headlines, lambda c, s: trend)


def test_a_coins_stop_defaults_to_25_percent(conn):
    pos = _coin_position(conn)
    path = [100, 120, 140, 160, 180, 200, 190, 170, 160, 149]        # ends yesterday; the fill is its 6th
    assert _coin_exit(conn, pos, _bars(path, YESTERDAY)) == "стоп: −25% от максимума"
    assert _coin_exit(conn, pos, _bars(path[:-1] + [151], YESTERDAY)) is None
    assert _coin_exit(conn, _coin_position(conn, stop_pct=0.15), _bars(path[:-1] + [169], YESTERDAY)
                      ) == "стоп: −15% от максимума"


def test_a_coin_below_its_100_day_average_and_falling_is_a_downtrend_exit(conn):
    pos = _coin_position(conn)
    assert _coin_exit(conn, pos, _falling()) == "тренд вниз"
    assert _coin_exit(conn, pos, _rising()) is None


def _journal(conn, coin, tier, days_ago):
    db.journal_signal(conn, {"source": "CRYPTO_ETF", "kind": "etf_flow", "ticker": f"CRYPTO:{coin}",
                             "tier": tier, "total_value_eur": 9e8})
    conn.execute("UPDATE signal_journal SET emitted_at = ? WHERE id = (SELECT max(id) FROM signal_journal)",
                 (_day(days_ago) + " 12:00:00",))
    conn.commit()


def test_a_price_confirmed_caution_is_an_exit_and_an_unconfirmed_one_is_not(conn):
    pos = _coin_position(conn)
    _journal(conn, "BTC", "caution", 2)
    reason = _coin_exit(conn, pos, _rising(), trend=FALLING)
    assert reason.startswith("осторожно: отток из спот-ETF (€900 млн); цена подтверждает")
    assert _coin_exit(conn, pos, _rising(), trend=RISING) is None


def test_a_coin_red_flag_includes_the_coin_words_a_stock_ignores(conn):
    headlines = [{"title": "Exchange hack drains hot wallet"}]
    assert _coin_exit(conn, _coin_position(conn), _rising(), headlines) == \
        "новости: Exchange hack drains hot wallet"
    assert _exit(conn, _position(conn), [100.0] * 10, headlines) is None


# ------------------------------------------------------------- the run: sells
def test_a_red_flag_headline_places_a_sale_and_reports_it_once(conn):
    _position(conn, "AAA", net=8_000.0, stop_pct=0.10, score=64.0)
    closes = [100.0] * 9 + [110.0]                                   # up 10% since the fill
    fraud = lambda t, s: [{"title": "AAA accused of fraud", "published": "2026-10-02"}]  # noqa: E731
    report = _run(conn, [], {"AAA": _bars(closes, YESTERDAY)}, news_fn=fraud)
    [order] = [o for o in paper.orders(conn, S) if o["side"] == "sell"]
    assert (order["status"], order["reason"]) == ("pending", "новости: AAA accused of fraud")
    [t] = report.sells
    assert (t.side, t.ticker, t.reasons, t.stop_pct, t.score) == ("sell", "AAA", ["новости: AAA accused of fraud"], 0.10, 64.0)
    assert t.amount_eur == pytest.approx(8_800.0) and t.result == pytest.approx(0.10)
    again = _run(conn, [], {"AAA": _bars(closes, YESTERDAY)}, news_fn=fraud)   # still pending
    assert again.sells == [] and len([o for o in paper.orders(conn, S) if o["side"] == "sell"]) == 1


def test_a_sale_fills_at_the_next_close_on_a_later_run(conn):
    _position(conn, "AAA", net=8_000.0)
    fraud = lambda t, s: [{"title": "AAA accused of fraud"}]  # noqa: E731
    _run(conn, [], {"AAA": _bars([100.0] * 9 + [110.0], YESTERDAY)}, news_fn=fraud)
    series = {"AAA": _bars([100.0] * 9 + [110.0], YESTERDAY) + [("2026-10-05", 111.0), ("2026-10-06", 121.0)]}
    _run(conn, [], series, today=dt.date(2026, 10, 7), news_fn=fraud)
    [closed] = paper.closed_positions(conn, S)
    assert closed["closed_date"] == "2026-10-06"
    assert closed["proceeds_eur"] == pytest.approx(8_000 * 1.21 * (1 - 0.0025))


def test_headlines_are_fetched_once_per_ticker_for_the_whole_run(conn):
    _position(conn, "AAA", stop_pct=0.25)
    calls = []
    _run(conn, [_sig("AAA")], {"AAA": _stock_bars()},
         news_fn=lambda t, s: calls.append((t, s)) or [])
    assert calls.count(("AAA", "SEC")) == 1
    assert ("CRYPTO:BTC", "CRYPTO") in calls and ("CRYPTO:ETH", "CRYPTO") in calls


def test_a_position_sold_on_the_stop_never_asks_for_its_headlines(conn):
    _position(conn, "AAA", stop_pct=0.10)                     # the zigzag's peak 120 puts the line at 108
    calls = []
    report = _run(conn, [], {"AAA": _stock_bars()}, news_fn=lambda t, s: calls.append(t) or [])
    assert [t.reasons for t in report.sells] == [["стоп: −10% от максимума"]]
    assert "AAA" not in calls


# ------------------------------------------------------------- the run: coins
def test_a_rising_coin_with_a_bullish_flow_is_bought_for_the_crypto_sleeve(conn):
    report = _run(conn, [_flow("BTC")], {"BTC-USD": _rising()})
    [o] = paper.orders(conn, C)
    assert (o["ticker"], o["source"], o["side"], o["status"]) == ("CRYPTO:BTC", "CRYPTO", "buy", "pending")
    assert o["stop_pct"] == 0.15 and o["score"] == 75.0             # trend 60 + flow 15
    assert o["amount_eur"] == pytest.approx(1_000 / 0.15)           # 6 667: under 35% of 30 000
    assert o["amount_eur"] <= 0.35 * 30_000
    assert o["reason"].startswith("балл 75: выше 100-дн. средней; 20 дн. +4%")
    [t] = report.buys
    assert (t.ticker, t.company, t.score) == ("CRYPTO:BTC", "BTC", 75.0)
    assert paper.orders(conn, S) == []                              # ETH has no series: only watched
    assert report.decisions["CRYPTO:ETH"] == model_score.WATCH


def test_a_coin_is_capped_at_35_percent_of_the_crypto_sleeve(conn):
    model.create_books(conn, TODAY)
    conn.execute("UPDATE paper_books SET cash_eur = 6_000 WHERE code = ?", (C,))       # sleeve 6k, model 76k
    _run(conn, [_flow("BTC")], {"BTC-USD": _rising()})
    [o] = paper.orders(conn, C)
    assert o["amount_eur"] == pytest.approx(0.35 * 6_000)           # risk sizing says 5 067


def test_a_falling_coin_that_is_held_is_sold_as_a_downtrend(conn):
    _coin_position(conn, net=10_000.0)
    report = _run(conn, [], {"BTC-USD": _falling()})
    [o] = [o for o in paper.orders(conn, C) if o["side"] == "sell"]
    assert (o["status"], o["reason"]) == ("pending", "тренд вниз")
    [t] = report.sells
    assert (t.ticker, t.company, t.reasons) == ("CRYPTO:BTC", "BTC", ["тренд вниз"])
    assert t.result == pytest.approx(101 / 105 - 1)


@pytest.mark.parametrize("closed_days_ago,placed", [(3, False), (7, False), (8, True)])
def test_a_coin_sold_within_7_days_is_not_bought_back_though_its_trend_is_up(conn, closed_days_ago, placed):
    _coin_position(conn, fill_days_ago=20, closed_days_ago=closed_days_ago)
    report = _run(conn, [_flow("BTC")], {"BTC-USD": _rising()})
    [o] = paper.orders(conn, C)
    if placed:
        assert (o["status"], len(report.buys)) == ("pending", 1)
    else:
        assert (o["status"], o["note"], report.buys) == ("skipped", "недавно продан", [])
        assert o["score"] == 75.0                              # the score it would have bought on


def test_a_held_coin_is_not_bought_again(conn):
    _coin_position(conn)
    _run(conn, [_flow("BTC")], {"BTC-USD": _rising()})
    assert paper.orders(conn, C) == []


# ---------------------------------------------------------- failing sleeves
def test_a_failing_sleeve_is_logged_and_the_other_one_carries_on(conn, monkeypatch, capsys):
    real = paper.fill_orders

    def flaky(conn_, code, prices, today):
        if code == C:
            raise RuntimeError("boom")
        return real(conn_, code, prices, today)

    monkeypatch.setattr(paper, "fill_orders", flaky)
    series = {"AAA": _stock_bars(), "BTC-USD": _rising(), "SPY": _bars([100.0] * 30, YESTERDAY)}
    report = _run(conn, [_sig("AAA"), _flow("BTC")], series)
    assert [o["ticker"] for o in paper.orders(conn, S)] == ["AAA"]
    assert paper.orders(conn, C) == []                       # the failed sleeve neither buys ...
    assert conn.execute("SELECT book FROM paper_equity").fetchall() == [(S,)]     # ... nor is stamped
    assert "[model] MODEL-C failed: RuntimeError: boom" in capsys.readouterr().err
    assert report.bench is None and len(report.buys) == 1


# -------------------------------------------------------------- score_today
def test_score_today_scores_and_places_nothing(conn):
    scored = _score(conn, [_sig("AAA", corroborated=("BAFIN",)), _sig("BBB", n=2, roles=(), pct=None)],
                    {"AAA": _stock_bars(), "BBB": _stock_bars(), "BTC-USD": _rising()})
    assert [(s.ticker, s.total, s.decision) for s in scored] == [
        ("AAA", 65.0, "buy"), ("CRYPTO:BTC", 60, "buy"), ("BBB", 34.0, "skip"), ("CRYPTO:ETH", 0, "watch")]
    assert conn.execute("SELECT count(*) FROM paper_orders").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM paper_books").fetchone()[0] == 0


def test_only_stock_signals_are_scored_as_stocks(conn):
    signals = [_sig("AAA"), _sig("CRYPTO:BTC", source="HOUSE"), _flow("BTC")]
    scored = _score(conn, signals, {"AAA": _stock_bars()})
    assert [s.ticker for s in scored if s.kind == "stock"] == ["AAA"]
    assert [s.ticker for s in scored if s.kind == "crypto"] == ["CRYPTO:BTC", "CRYPTO:ETH"]


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
            "CRYPTO:ETH": [{"title": "Protocol exploit drains ETH"}]}
    scored = _score(conn, [], {"BTC-USD": _rising(), "ETH-USD": _rising()},
                    news_fn=lambda t, s: news.get(t, []))
    by = {s.ticker: s for s in scored}
    assert (by["CRYPTO:BTC"].news, by["CRYPTO:BTC"].total) == (5, 65)
    assert (by["CRYPTO:ETH"].decision, by["CRYPTO:ETH"].block) == ("block", "Protocol exploit drains ETH")


# ------------------------------------------------------------- the day report
def test_the_report_carries_decisions_and_the_two_books_value(conn):
    report = _run(conn, [_sig("AAA"), _sig("BBB", n=3, roles=(), pct=None),
                         _sig("CCC", n=2, roles=(), pct=None)],
                  {t: _stock_bars() for t in ("AAA", "BBB", "CCC")})
    assert report.decisions == {"AAA": "buy", "BBB": "skip", "CCC": "skip",
                                "CRYPTO:BTC": "watch", "CRYPTO:ETH": "watch"}
    assert [s.ticker for s in report.scored][:1] == ["AAA"]
    assert report.value == pytest.approx(100_000.0)
    assert report.value == pytest.approx(paper.book_value(conn, S) + paper.book_value(conn, C))
    assert conn.execute("SELECT book, value, cash FROM paper_equity ORDER BY book").fetchall() == [
        (C, 30_000.0, 30_000.0), (S, 70_000.0, 70_000.0)]


def test_the_benchmark_is_the_two_books_benchmarks_added_up(conn):
    flat = {"SPY": _bars([100.0] * 12, YESTERDAY), "BTC-USD": _bars([100.0] * 12, YESTERDAY)}
    assert _run(conn, [], flat).bench == pytest.approx(100_000.0)             # day one: the start money
    moved = {"SPY": _bars([100.0] * 6 + [110.0] * 6, dt.date(2026, 10, 11)),      # 10/05: 100, then 110
             "BTC-USD": _bars([100.0] * 6 + [120.0] * 6, dt.date(2026, 10, 11))}
    later = _run(conn, [], moved, today=dt.date(2026, 10, 12))
    assert later.bench == pytest.approx(70_000 * 1.10 + 30_000 * 1.20)


def test_the_benchmark_is_missing_when_either_book_has_none(conn):
    assert _run(conn, [], {"SPY": _bars([100.0] * 12, YESTERDAY)}).bench is None
    assert _run(conn, [], {}).bench is None


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
def test_the_run_keeps_todays_scores_and_they_come_back_as_they_were(conn):
    import telegram_notify
    report = _run(conn, [_sig("AAA", corroborated=("BAFIN",)), _sig("BBB", n=2, roles=(), pct=None)],
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
    _run(conn, [_sig("AAA")], {"AAA": _stock_bars()})
    assert model.cached_scores(conn, TODAY) is not None
    assert model.cached_scores(conn, TODAY + dt.timedelta(days=1)) is None
    assert model.cached_scores(conn, YESTERDAY) is None


def test_a_failed_scoring_keeps_nothing(conn, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("finders down")
    monkeypatch.setattr(model, "score_today", boom)
    _run(conn, [_sig("AAA")], {"AAA": _stock_bars()})
    assert model.cached_scores(conn, TODAY) is None


# ------------------------------------------------- the report says whether the pass was complete
def test_a_clean_pass_is_complete(conn):
    assert _run(conn, [_sig("AAA")], {"AAA": _stock_bars()}).complete is True
    assert _run(conn, [], {}, buy=False).complete is True          # whether it buys does not matter


def test_a_pass_with_a_failed_sleeve_is_not_complete(conn, monkeypatch):
    real = paper.fill_orders

    def flaky(conn_, code, prices, today):
        if code == C:
            raise RuntimeError("boom")
        return real(conn_, code, prices, today)

    monkeypatch.setattr(paper, "fill_orders", flaky)
    assert _run(conn, [_sig("AAA")], {"AAA": _stock_bars()}).complete is False


def test_a_failing_buy_in_one_sleeve_is_not_complete_though_the_other_buys(conn, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no sector")
    monkeypatch.setattr(model, "_buy_coins", boom)
    report = _run(conn, [_sig("AAA")], {"AAA": _stock_bars()})
    assert report.complete is False and [t.ticker for t in report.buys] == ["AAA"]


def test_a_failing_snapshot_is_not_complete(conn, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk")
    monkeypatch.setattr(paper, "_snapshot", boom)
    assert _run(conn, [], {}).complete is False


def test_a_failed_scoring_is_not_complete(conn, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("finders down")
    monkeypatch.setattr(model, "score_today", boom)
    assert _run(conn, [_sig("AAA")], {"AAA": _stock_bars()}).complete is False


def test_scores_that_could_not_be_kept_do_not_make_a_pass_incomplete(conn, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("cache")
    monkeypatch.setattr(model, "keep_scores", boom)
    assert _run(conn, [_sig("AAA")], {"AAA": _stock_bars()}).complete is True


def test_no_run_today_means_no_kept_scores(conn):
    assert model.cached_scores(conn, TODAY) is None
