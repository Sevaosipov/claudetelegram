"""paper.py: the virtual books. Offline -- prices come from a stub fetch, dates are
fixed so weekday arithmetic is deterministic."""
from __future__ import annotations

import datetime as dt
import json
import types

import pytest

import db
import paper
import strategy
from conftest import add_sec_sale

TODAY = dt.date(2026, 10, 5)          # a Monday


def _days(n: int) -> str:
    return (TODAY - dt.timedelta(days=n)).isoformat()


def _bars(closes, end: dt.date = TODAY):
    """Consecutive daily closes ending on `end`, oldest first."""
    n = len(closes)
    return [((end - dt.timedelta(days=n - 1 - i)).isoformat(), float(c)) for i, c in enumerate(closes)]


class Fetch:
    def __init__(self, series: dict):
        self.series, self.calls = series, []

    def __call__(self, symbol, days=None):
        self.calls.append(symbol)
        return self.series.get(symbol, [])


def _sig(ticker="AAA", source="SEC", tier="strong", score=80.0, members=("Jane Doe",)):
    return types.SimpleNamespace(ticker=ticker, source=source, company=f"{ticker} Corp", tier=tier,
                                 score=score, member_names=list(members))


def _sel(strong=(), candidates=()):
    return strategy.Selection(strong=[strategy.Tiered(s, s.tier) for s in strong],
                              candidates=[strategy.Tiered(s, s.tier) for s in candidates],
                              t212_checked=True)


# ------------------------------------------------------------------ foundation
def test_books_are_created_once_with_their_money(conn):
    paper.create_books(conn, TODAY)
    paper.create_books(conn, TODAY + dt.timedelta(days=1))
    rows = conn.execute("SELECT code, sleeve, start_date, start_eur, cash_eur, bench_symbol "
                        "FROM paper_books").fetchall()
    assert len(rows) == 13
    assert ("R1-E1", "stock", TODAY.isoformat(), 80_000.0, 80_000.0, "SPY") in rows
    assert ("R1-E1-AN", "stock", TODAY.isoformat(), 80_000.0, 80_000.0, "SPY") in rows
    assert ("C-B", "crypto", TODAY.isoformat(), 20_000.0, 20_000.0, "BTC-USD") in rows
    assert ("H1", "small", TODAY.isoformat(), 20_000.0, 20_000.0, "IWM") in rows


def test_book_labels():
    assert paper.BOOK_BY_CODE["R2-E3"].label == "R2·E3"
    assert paper.BOOK_BY_CODE["R1-E1-AN"].label == "R1·E1+аналитики"
    assert paper.BOOK_BY_CODE["C-A"].label == "C-A"


@pytest.mark.parametrize("ticker,source,expected", [
    ("AAPL", "SEC", ("AAPL", "USD")),
    ("BRK.B", "HOUSE", ("BRK-B", "USD")),
    ("EQNR", "NORWAY", ("EQNR.OL", "NOK")),
    ("VOLV-B", "SWEDEN", ("VOLV-B.ST", "SEK")),
    ("CRYPTO:BTC", "CRYPTO", ("BTC-USD", "USD")),
    ("DE0007164600", "BAFIN", None),
])
def test_listing(ticker, source, expected):
    assert paper.listing(ticker, source) == expected


def test_fees():
    assert paper.fee("AAPL", "USD") == 0.0025
    assert paper.fee("SAP", "EUR") == 0.0010
    assert paper.fee("CRYPTO:BTC", "USD") == 0.0050


def test_prices_are_fetched_once_and_a_failure_is_empty():
    calls = []

    def fetch(symbol, days):
        calls.append(symbol)
        if symbol == "BAD":
            raise RuntimeError("down")
        return [("2026-10-01", 1.0)]
    p = paper.Prices(fetch)
    assert p.bars("AAA") == p.bars("AAA") == [("2026-10-01", 1.0)]
    assert p.bars("BAD") == [] and calls == ["AAA", "BAD"]


def test_prices_with_a_date_keep_only_completed_bars():
    series = {"AAA": _bars([100, 110, 120])}                          # ... TODAY-1, TODAY
    assert paper.Prices(Fetch(series), today=TODAY).bars("AAA") == [(_days(2), 100.0), (_days(1), 110.0)]
    assert paper.Prices(Fetch(series)).bars("AAA") == series["AAA"]   # without a date: every bar


@pytest.mark.parametrize("symbol,source,kept", [
    ("AAPL", "Nasdaq", False),       # not adjusted for dividends and splits
    ("AAPL", "Yahoo", True),
    ("BTC-USD", "Binance", True),    # a coin pays no dividend: any source will do
])
def test_stock_closes_come_from_yahoo_only(monkeypatch, symbol, source, kept):
    bars = [("2026-10-01", 1.0), ("2026-10-02", 2.0)]
    monkeypatch.setattr(paper.sources, "price_history", lambda asset, days: (bars, source))
    assert paper._closes(symbol, 420) == (bars if kept else [])


def test_series_helpers():
    bars = [("2026-10-01", 10.0), ("2026-10-02", 11.0), ("2026-10-05", 12.0)]
    assert paper.close_on_or_before(bars, "2026-10-03") == 11.0
    assert paper.close_on_or_before(bars, "2026-09-30") is None
    assert paper.first_close_after(bars, "2026-10-02") == ("2026-10-05", 12.0)
    assert paper.first_close_after(bars, "2026-10-05") is None


def test_business_days_between():
    assert paper.business_days_between("2026-10-02", dt.date(2026, 10, 9)) == 5
    assert paper.business_days_between("2026-10-05", dt.date(2026, 10, 5)) == 0


# ------------------------------------------------------------- orders & fills
def _book(conn, code="R1-E1"):
    paper.create_books(conn, TODAY - dt.timedelta(days=30))
    return code


def test_a_buy_fills_at_the_first_close_after_the_decision(conn):
    code = _book(conn)
    assert paper.place_buy(conn, code, "AAA", "SEC", "Сильный", TODAY - dt.timedelta(days=3),
                           8_000.0, max_positions=10) == "pending"
    prices = paper.Prices(Fetch({"AAA": _bars([100, 110, 120, 130])}))
    paper.fill_orders(conn, code, prices, TODAY)
    [p] = paper.open_positions(conn, code)
    assert p["fill_date"] == _days(2) and p["entry_close"] == 110.0      # not the decision day's 100
    assert p["cost_eur"] == 8_000.0 and p["net_eur"] == pytest.approx(8_000 * (1 - 0.0025))
    assert paper.cash(conn, code) == pytest.approx(72_000.0)
    assert [o["status"] for o in paper.orders(conn, code)] == ["filled"]


def test_value_follows_the_adjusted_close_and_the_currency(conn):
    code = _book(conn)
    paper.place_buy(conn, code, "EQNR", "NORWAY", "Сильный", TODAY - dt.timedelta(days=3),
                    8_000.0, max_positions=10)
    prices = paper.Prices(Fetch({"EQNR.OL": _bars([100, 100, 110, 125])}))
    paper.fill_orders(conn, code, prices, TODAY)
    paper.mark_to_market(conn, code, prices)
    [p] = paper.open_positions(conn, code)
    assert p["currency"] == "NOK" and p["entry_fx"] == pytest.approx(10.74)
    assert p["last_value"] == pytest.approx(p["net_eur"] * 1.25)
    assert paper.book_value(conn, code) == pytest.approx(72_000 + p["net_eur"] * 1.25)


def test_a_position_held_past_the_usual_history_is_still_valued_and_sold(conn):
    code = _book(conn, "C-B")
    pos = _coin_position(conn, code, "BTC", fill_days_ago=500)      # entry close 100
    asked = []

    def fetch(symbol, days):
        asked.append(days)
        return _bars([100.0] * 598 + [140.0, 150.0]) if days >= 530 else _bars([150.0] * days)
    paper.mark_to_market(conn, code, paper.Prices(fetch), TODAY)
    assert paper.open_positions(conn, code)[0]["last_value"] == pytest.approx(15_000.0)
    paper.place_sell(conn, code, pos, "ниже 200-дн. средней", TODAY - dt.timedelta(days=2))
    paper.fill_orders(conn, code, paper.Prices(fetch), TODAY)       # at TODAY-1's 140
    [closed] = paper.closed_positions(conn, code)
    assert closed["proceeds_eur"] == pytest.approx(14_000.0 * (1 - 0.005))
    assert asked == [530, 530]


def test_a_weaker_dollar_lowers_the_eur_value_at_an_unchanged_price(conn, monkeypatch):
    code = _book(conn)
    rate = {"USD": 1.16}
    monkeypatch.setattr(paper.fx, "per_eur", lambda currency, conn=None: rate[currency])
    paper.place_buy(conn, code, "AAA", "SEC", "Сильный", TODAY - dt.timedelta(days=3), 8_000.0,
                    max_positions=10)
    prices = paper.Prices(Fetch({"AAA": _bars([100, 100, 100, 100])}))
    paper.fill_orders(conn, code, prices, TODAY)
    rate["USD"] = 1.276                                              # 10% more dollars per euro
    paper.mark_to_market(conn, code, prices, TODAY)
    [p] = paper.open_positions(conn, code)
    assert p["entry_fx"] == pytest.approx(1.16)
    assert p["last_value"] == pytest.approx(p["net_eur"] * (100 / 100) * (1.16 / 1.276))
    assert p["last_value"] == pytest.approx(p["net_eur"] / 1.1)      # 10% less in EUR


def test_a_sale_fills_at_the_next_close_and_pays_the_fee(conn):
    code = _book(conn)
    paper.place_buy(conn, code, "AAA", "SEC", "Сильный", TODAY - dt.timedelta(days=4),
                    8_000.0, max_positions=10)
    prices = paper.Prices(Fetch({"AAA": _bars([100, 100, 100, 120, 150])}))
    paper.fill_orders(conn, code, prices, TODAY)                # buys at 100 (TODAY-3)
    [p] = paper.open_positions(conn, code)
    paper.place_sell(conn, code, p, "стоп -15%", TODAY - dt.timedelta(days=2))
    paper.place_sell(conn, code, p, "стоп -15%", TODAY - dt.timedelta(days=2))   # no second order
    paper.fill_orders(conn, code, prices, TODAY)                # sells at 120 (TODAY-1)
    assert paper.open_positions(conn, code) == []
    [closed] = paper.closed_positions(conn, code)
    assert closed["closed_date"] == _days(1) and closed["close_reason"] == "стоп -15%"
    assert closed["proceeds_eur"] == pytest.approx(p["net_eur"] * 1.2 * (1 - 0.0025))
    assert paper.cash(conn, code) == pytest.approx(72_000 + closed["proceeds_eur"])
    assert len([o for o in paper.orders(conn, code) if o["side"] == "sell"]) == 1


def test_an_order_with_no_price_for_five_business_days_is_cancelled(conn):
    code = _book(conn)
    paper.place_buy(conn, code, "AAA", "SEC", "Сильный", dt.date(2026, 10, 2), 8_000.0,
                    max_positions=10)
    prices = paper.Prices(Fetch({}))
    paper.fill_orders(conn, code, prices, dt.date(2026, 10, 8))    # 4 business days: waiting
    assert [o["status"] for o in paper.orders(conn, code)] == ["pending"]
    paper.fill_orders(conn, code, prices, dt.date(2026, 10, 12))   # 6: cancelled
    [o] = paper.orders(conn, code)
    assert o["status"] == "cancelled" and o["note"] == "не исполнено: нет цены"


def test_a_sale_with_no_price_for_five_business_days_closes_at_the_last_value(conn):
    code = _book(conn)
    pos = _position(conn, code, value=9_000.0)
    paper.place_sell(conn, code, pos, "90 дн. в позиции", dt.date(2026, 10, 2))
    prices = paper.Prices(Fetch({}))                                 # delisted: no quotes at all
    paper.fill_orders(conn, code, prices, dt.date(2026, 10, 8))     # 4 business days: waiting
    assert paper.closed_positions(conn, code) == []
    paper.fill_orders(conn, code, prices, dt.date(2026, 10, 12))    # 6: closed at the last value
    [closed] = paper.closed_positions(conn, code)
    proceeds = 9_000.0 * (1 - 0.0025)
    assert closed["closed_date"] == "2026-10-12" and closed["proceeds_eur"] == pytest.approx(proceeds)
    assert closed["close_reason"] == "90 дн. в позиции (по последней цене: нет котировок)"
    assert paper.cash(conn, code) == pytest.approx(80_000 + proceeds)
    [o] = paper.orders(conn, code)
    assert (o["status"], o["note"]) == ("filled", "по последней цене")


def test_skips_are_recorded_with_why(conn):
    code = _book(conn)
    assert paper.place_buy(conn, code, "DE0007164600", "BAFIN", "Сильный", TODAY, 8_000.0,
                           max_positions=10) == "skipped"
    for i in range(10):
        assert paper.place_buy(conn, code, f"T{i}", "SEC", "Сильный", TODAY, 8_000.0,
                               max_positions=10) == "pending"
    assert paper.place_buy(conn, code, "T10", "SEC", "Сильный", TODAY, 8_000.0,
                           max_positions=10) == "skipped"
    notes = [o["note"] for o in paper.orders(conn, code) if o["status"] == "skipped"]
    assert notes == ["нет котировки", "мест нет"]


def test_a_partial_slice_needs_half_a_slice_of_cash(conn):
    code = _book(conn)
    conn.execute("UPDATE paper_books SET cash_eur = 5000 WHERE code = ?", (code,))
    assert paper.place_buy(conn, code, "AAA", "SEC", "Сильный", TODAY, 8_000.0,
                           max_positions=10) == "pending"
    assert [o["amount_eur"] for o in paper.orders(conn, code)] == [5_000.0]
    assert paper.place_buy(conn, code, "BBB", "SEC", "Сильный", TODAY, 8_000.0,
                           max_positions=10) == "skipped"
    assert paper.orders(conn, code)[-1]["note"] == "нет денег"


def test_a_held_or_pending_ticker_is_not_ordered_again(conn):
    code = _book(conn)
    assert paper.place_buy(conn, code, "AAA", "SEC", "Сильный", TODAY, 8_000.0,
                           max_positions=10) == "pending"
    assert paper.place_buy(conn, code, "AAA", "SEC", "Сильный", TODAY, 8_000.0,
                           max_positions=10) == "duplicate"
    assert len(paper.orders(conn, code)) == 1


# --------------------------------------------------------------- stock books
def _position(conn, code, ticker="AAA", fill_days_ago=10, net=8_000.0, value=None,
              insiders=("Jane Doe",), target=None, source="SEC"):
    conn.execute(
        "INSERT INTO paper_positions (book, ticker, source, symbol, currency, fill_date, cost_eur, "
        "net_eur, entry_close, entry_fx, insiders, target, reason, last_value) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (code, ticker, source, ticker, "USD", _days(fill_days_ago), net, net, 100.0, 1.16,
         json.dumps(list(insiders)), target, "Сильный", value if value is not None else net))
    conn.commit()
    return paper.open_positions(conn, code)[-1]


def _exit(conn, code, pos, bars=None):
    return paper.stock_exit_reason(conn, paper.BOOK_BY_CODE[code], pos, bars or [], TODAY)


def test_r1_takes_strong_stock_signals_and_r2_adds_high_scoring_candidates():
    sel = _sel([_sig("AAA"), _sig("CRYPTO:BTC", source="HOUSE")],
               [_sig("BBB", tier="candidate", score=75), _sig("CCC", tier="candidate", score=60)])
    assert [s.ticker for s in paper.stock_signals(sel, paper.BOOK_BY_CODE["R1-E1"])] == ["AAA"]
    assert [s.ticker for s in paper.stock_signals(sel, paper.BOOK_BY_CODE["R2-E1"])] == ["AAA", "BBB"]


def test_stock_step_orders_a_tenth_of_the_book_with_the_signals_insiders(conn):
    code = _book(conn)
    paper.stock_step(conn, paper.BOOK_BY_CODE[code], _sel([_sig("AAA", members=("Jane Doe", "John Roe"))]),
                     paper.Prices(Fetch({})), TODAY)
    [o] = paper.orders(conn, code)
    assert (o["ticker"], o["side"], o["amount_eur"]) == ("AAA", "buy", 8_000.0)
    assert json.loads(o["insiders"]) == ["Jane Doe", "John Roe"]
    assert o["reason"] == "Сильный: SEC, AAA Corp"


@pytest.mark.parametrize("code,days,expected", [
    ("R1-E1", 89, None), ("R1-E1", 90, "90 дн. в позиции"),
    ("R1-E2", 181, None), ("R1-E2", 182, "182 дн. в позиции"),
    ("R1-E3", 91, "91 дн. в позиции"), ("R1-E4", 90, "90 дн. в позиции"),
])
def test_holding_limits(conn, code, days, expected):
    _book(conn)
    assert _exit(conn, code, _position(conn, code, fill_days_ago=days)) == expected


@pytest.mark.parametrize("code,value,expected", [
    ("R1-E1", 6_799.0, "стоп -15%"), ("R1-E1", 10_001.0, None),
    ("R1-E2", 4_000.0, None),
    ("R1-E3", 6_799.0, "стоп -15%"),
    ("R1-E4", 6_799.0, "стоп -15%"), ("R1-E4", 10_001.0, "цель +25%"),
])
def test_stops_and_profit_targets(conn, code, value, expected):
    _book(conn)
    assert _exit(conn, code, _position(conn, code, value=value)) == expected


def test_an_insider_selling_after_the_buy_closes_e1_and_e4_only(conn):
    _book(conn)
    add_sec_sale(conn, "AAA", "Jane Doe", 500_000, _days(2))
    for code, expected in (("R1-E1", True), ("R1-E4", True), ("R1-E2", False), ("R1-E3", False)):
        reason = _exit(conn, code, _position(conn, code))
        assert (reason or "").startswith("продаёт инсайдер") is expected


def test_an_insider_sale_before_the_buy_does_not_count(conn):
    _book(conn)
    add_sec_sale(conn, "AAA", "Jane Doe", 500_000, _days(20))
    assert _exit(conn, "R1-E1", _position(conn, "R1-E1", fill_days_ago=10)) is None


def test_the_shadow_sells_at_the_analyst_target(conn):
    _book(conn)
    pos = _position(conn, "R1-E1-AN", target=150.0)
    assert _exit(conn, "R1-E1-AN", pos, _bars([140, 151])) == "цель аналитиков 150.00 достигнута"
    assert _exit(conn, "R1-E1-AN", pos, _bars([140, 149])) is None


def test_without_a_target_the_shadow_behaves_like_r1_e1(conn):
    _book(conn)
    pos = _position(conn, "R1-E1-AN", fill_days_ago=90)
    assert _exit(conn, "R1-E1-AN", pos, _bars([140, 151])) == "90 дн. в позиции"


def test_the_shadow_records_the_target_at_the_buy(conn, monkeypatch):
    _book(conn)
    monkeypatch.setattr(paper, "_analyst_target", lambda ticker, source: 150.0)
    paper.stock_step(conn, paper.BOOK_BY_CODE["R1-E1-AN"], _sel([_sig("AAA")]),
                     paper.Prices(Fetch({})), TODAY)
    assert paper.orders(conn, "R1-E1-AN")[0]["target"] == 150.0


def test_the_shadow_looks_up_targets_only_for_tickers_it_can_buy(conn, monkeypatch):
    _book(conn)
    asked = []
    monkeypatch.setattr(paper, "_analyst_target", lambda ticker, source: asked.append(ticker) or 150.0)
    _position(conn, "R1-E1-AN", "AAA")                                          # held
    paper.place_buy(conn, "R1-E1-AN", "BBB", "SEC", "Сильный", TODAY, 8_000.0)  # pending
    paper.stock_step(conn, paper.BOOK_BY_CODE["R1-E1-AN"], _sel([_sig("AAA"), _sig("BBB"), _sig("CCC")]),
                     paper.Prices(Fetch({})), TODAY)
    assert asked == ["CCC"]


def test_stock_step_places_a_sale_when_an_exit_holds(conn):
    _book(conn)
    _position(conn, "R1-E3", fill_days_ago=91)
    paper.stock_step(conn, paper.BOOK_BY_CODE["R1-E3"], _sel(), paper.Prices(Fetch({})), TODAY)
    [o] = paper.orders(conn, "R1-E3")
    assert (o["side"], o["reason"]) == ("sell", "91 дн. в позиции")


# -------------------------------------------------------------- crypto books
FALLING = {"ret_7d": -6.0, "above_ma20": False}
RISING = {"ret_7d": 3.0, "above_ma20": True}


def _coin_sig(coin="BTC"):
    return types.SimpleNamespace(ticker=f"CRYPTO:{coin}", source="CRYPTO_ETF", crypto_kind="etf_flow",
                                 coin=coin, tier="strong", company="спот-ETF США", score=100.0)


def _coin_position(conn, code, coin="BTC", fill_days_ago=5, net=10_000.0, value=None):
    conn.execute(
        "INSERT INTO paper_positions (book, ticker, source, symbol, currency, fill_date, cost_eur, "
        "net_eur, entry_close, entry_fx, insiders, reason, last_value) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (code, f"CRYPTO:{coin}", "CRYPTO", f"{coin}-USD", "USD", _days(fill_days_ago), net, net,
         100.0, 1.16, "[]", "Сильный", value if value is not None else net))
    conn.commit()
    return paper.open_positions(conn, code)[-1]


def _journal(conn, coin, tier, days_ago):
    db.journal_signal(conn, {"source": "CRYPTO_ETF", "kind": "etf_flow", "ticker": f"CRYPTO:{coin}",
                             "tier": tier, "total_value_eur": 9e8})
    conn.execute("UPDATE signal_journal SET emitted_at = ? WHERE id = (SELECT max(id) FROM signal_journal)",
                 (_days(days_ago) + " 12:00:00",))
    conn.commit()


def _trend_bars(last, end=TODAY):
    return _bars([100.0] * (paper.TREND_DAYS - 1) + [last], end)


def _crypto(conn, code, selection=None, series=None, trend=None, today=TODAY):
    paper.crypto_step(conn, paper.BOOK_BY_CODE[code], selection or _sel(),
                      paper.Prices(Fetch(series or {})), today, lambda c, s: trend)
    return paper.orders(conn, code)


def test_c_a_buys_the_coins_share_on_a_strong_signal(conn):
    _book(conn)
    [o] = _crypto(conn, "C-A", _sel([_coin_sig("BTC")]))
    assert (o["ticker"], o["side"], o["amount_eur"]) == ("CRYPTO:BTC", "buy", 10_000.0)


def test_c_a_sells_on_a_price_confirmed_caution(conn):
    _book(conn)
    _coin_position(conn, "C-A", "BTC", fill_days_ago=5)
    _journal(conn, "BTC", "caution", days_ago=1)
    [o] = _crypto(conn, "C-A", trend=FALLING)
    assert o["side"] == "sell" and o["reason"].startswith("осторожно: отток из спот-ETF")


def test_c_a_ignores_a_caution_the_price_does_not_confirm(conn):
    _book(conn)
    _coin_position(conn, "C-A", "BTC")
    _journal(conn, "BTC", "caution", days_ago=1)
    assert _crypto(conn, "C-A", trend=RISING) == []


@pytest.mark.parametrize("days,value,expected", [
    (90, None, "90 дн. в позиции"), (10, 7_499.0, "стоп -25%"), (10, 7_600.0, None)])
def test_c_a_time_limit_and_stop(conn, days, value, expected):
    _book(conn)
    _coin_position(conn, "C-A", "BTC", fill_days_ago=days, value=value)
    orders = _crypto(conn, "C-A")
    assert [o["reason"] for o in orders] == ([expected] if expected else [])


def test_c_b_holds_a_coin_above_its_200_day_average(conn):
    _book(conn)
    orders = _crypto(conn, "C-B", series={"BTC-USD": _trend_bars(110), "ETH-USD": _trend_bars(90)})
    assert [(o["ticker"], o["reason"]) for o in orders] == [("CRYPTO:BTC", "выше 200-дн. средней")]


def test_c_b_buys_below_the_average_on_a_strong_signal(conn):
    _book(conn)
    orders = _crypto(conn, "C-B", _sel([_coin_sig("ETH")]),
                     series={"BTC-USD": _trend_bars(90), "ETH-USD": _trend_bars(90)})
    assert [(o["ticker"], o["reason"]) for o in orders] == [("CRYPTO:ETH", "Сильный крипто-сигнал")]


def test_c_b_sells_below_the_average(conn):
    _book(conn)
    _coin_position(conn, "C-B", "BTC")
    [o] = _crypto(conn, "C-B", series={"BTC-USD": _trend_bars(90), "ETH-USD": _trend_bars(90)})
    assert (o["side"], o["reason"]) == ("sell", "ниже 200-дн. средней")


def test_c_b_keeps_a_held_coin_when_its_trend_is_unknown(conn):
    _book(conn)
    _coin_position(conn, "C-B", "BTC")
    assert _crypto(conn, "C-B", series={}) == []                      # no series at all


def test_c_b_does_not_buy_on_a_short_series(conn):
    _book(conn)
    short = {"BTC-USD": _bars([100.0] * 49 + [150.0]), "ETH-USD": _bars([100.0] * 49 + [150.0])}
    assert _crypto(conn, "C-B", series=short) == []


def test_c_b_keeps_a_coin_for_30_days_after_a_strong_signal(conn):
    _book(conn)
    _coin_position(conn, "C-B", "BTC")
    _journal(conn, "BTC", "strong", days_ago=10)
    assert _crypto(conn, "C-B", series={"BTC-USD": _trend_bars(90), "ETH-USD": _trend_bars(90)}) == []


def test_c_b_sells_on_a_caution_and_waits_7_days_before_buying_again(conn):
    _book(conn)
    pos = _coin_position(conn, "C-B", "BTC")
    _journal(conn, "BTC", "caution", days_ago=1)
    [o] = _crypto(conn, "C-B", trend=FALLING,
                  series={"BTC-USD": _trend_bars(110), "ETH-USD": _trend_bars(90)})
    assert o["side"] == "sell" and o["reason"].startswith("осторожно")
    conn.execute("UPDATE paper_positions SET closed_date = ?, close_reason = ? WHERE id = ?",
                 (_days(3), o["reason"], pos["id"]))
    conn.execute("UPDATE paper_orders SET status = 'filled' WHERE id = ?", (o["id"],))
    conn.commit()
    assert len(_crypto(conn, "C-B", series={"BTC-USD": _trend_bars(110), "ETH-USD": _trend_bars(90)})) == 1
    later = TODAY + dt.timedelta(days=5)
    orders = _crypto(conn, "C-B", today=later,
                     series={"BTC-USD": _trend_bars(110, later), "ETH-USD": _trend_bars(90, later)})
    assert orders[-1]["side"] == "buy" and orders[-1]["reason"] == "выше 200-дн. средней"


# --------------------------------------------------------------- daily run
def test_max_drawdown():
    assert paper.max_drawdown([100, 120, 90, 130, 117]) == pytest.approx(-0.25)
    assert paper.max_drawdown([100, 101]) == 0.0 and paper.max_drawdown([]) == 0.0


def _run_day(conn, day, series, selection=None):
    return paper.run(conn, selection or _sel(), today=day, fetch=Fetch(series),
                     trend_fn=lambda c, s: None)


def _live(bars, day, partial=999.0):
    """`bars` (completed) plus `day`'s still-open bar, which a run on `day` must never use."""
    return bars + [(day.isoformat(), float(partial))]


def test_run_opens_books_places_orders_and_fills_them_at_the_next_completed_close(conn, monkeypatch):
    monkeypatch.setattr(paper, "_analyst_target", lambda ticker, source: None)   # the shadow's network seam
    day1, day3 = TODAY - dt.timedelta(days=2), TODAY
    day0, day2 = day1 - dt.timedelta(days=1), day3 - dt.timedelta(days=1)
    assert _run_day(conn, day1, {"SPY": _live(_bars([500, 505], day0), day1),
                                 "AAA": _live(_bars([100, 100], day0), day1),
                                 "BTC-USD": _live(_trend_bars(110, day0), day1),
                                 "ETH-USD": _live(_trend_bars(90, day0), day1)},    # 999 open: no buy
                    _sel([_sig("AAA")])) == 13
    assert [o["status"] for o in paper.orders(conn, "R1-E1")] == ["pending"]
    _run_day(conn, day3, {"SPY": _live(_bars([500, 505, 505, 510], day2), day3),
                          "AAA": _live(_bars([100, 100, 100, 120], day2), day3),
                          "BTC-USD": _live(_trend_bars(110, day2), day3),
                          "ETH-USD": _live(_trend_bars(90, day2), day3)})
    [p] = paper.open_positions(conn, "R1-E1")
    assert p["fill_date"] == day2.isoformat() and p["entry_close"] == 120.0
    rows = conn.execute("SELECT date, value, cash, bench FROM paper_equity WHERE book = 'R1-E1' "
                        "ORDER BY date").fetchall()
    assert [r[0] for r in rows] == [day1.isoformat(), day3.isoformat()]
    assert rows[0][1] == pytest.approx(80_000.0) and rows[0][3] == pytest.approx(80_000.0)
    assert rows[1][1] == pytest.approx(72_000.0 + p["net_eur"])
    assert rows[1][3] == pytest.approx(80_000.0 * 510 / 505)
    [btc] = paper.open_positions(conn, "C-B")
    assert btc["ticker"] == "CRYPTO:BTC"


def test_a_crypto_buy_waits_for_the_next_close_to_complete(conn):
    d = TODAY - dt.timedelta(days=2)
    d1, d2 = d + dt.timedelta(days=1), d + dt.timedelta(days=2)
    _run_day(conn, d, {"BTC-USD": _live(_bars([100, 100], d - dt.timedelta(days=1)), d, 105)},
             _sel([_coin_sig("BTC")]))
    assert [(o["side"], o["status"]) for o in paper.orders(conn, "C-A")] == [("buy", "pending")]
    _run_day(conn, d1, {"BTC-USD": _live(_bars([100, 100, 104], d), d1, 115)})    # D+1 still open
    assert [o["status"] for o in paper.orders(conn, "C-A")] == ["pending"]
    assert paper.open_positions(conn, "C-A") == []
    _run_day(conn, d2, {"BTC-USD": _live(_bars([100, 100, 104, 120], d1), d2, 130)})
    [p] = paper.open_positions(conn, "C-A")
    assert (p["fill_date"], p["entry_close"]) == (d1.isoformat(), 120.0)     # D+1's final close


def test_a_book_without_a_benchmark_price_stores_no_benchmark(conn):
    _run_day(conn, TODAY, {})
    assert conn.execute("SELECT value, bench FROM paper_equity WHERE book = 'R1-E1'").fetchone() == \
        (80_000.0, None)


def test_one_failing_book_does_not_stop_the_others(conn, monkeypatch):
    real = paper.crypto_step

    def boom(conn, book, *a, **k):
        if book.code == "C-A":
            raise RuntimeError("bad")
        return real(conn, book, *a, **k)
    monkeypatch.setattr(paper, "crypto_step", boom)
    assert _run_day(conn, TODAY, {}) == 12


def test_bot_warns_when_the_paper_pass_fails(conn, monkeypatch):
    import bot
    sent = []
    monkeypatch.setattr(bot.telegram_notify, "send_text", lambda text: sent.append(text) or True)

    def fail(conn, selection):
        raise RuntimeError("x")
    monkeypatch.setattr(paper, "run", fail)
    bot._run_paper(conn, _sel(), types.SimpleNamespace(no_telegram=False))
    assert len(sent) == 1 and "бумажный портфель" in sent[0]
    monkeypatch.setattr(paper, "run", lambda conn, selection: 11)
    bot._run_paper(conn, _sel(), types.SimpleNamespace(no_telegram=False))
    assert len(sent) == 1


def test_bot_sends_the_monthly_report_after_a_good_pass(conn, monkeypatch):
    import bot
    import paper_report
    calls = []
    monkeypatch.setattr(paper, "run", lambda conn, selection: 11)
    monkeypatch.setattr(paper_report, "maybe_send_monthly_report",
                        lambda conn, today: calls.append(today) or True)
    bot._run_paper(conn, _sel(), types.SimpleNamespace(no_telegram=False))
    bot._run_paper(conn, _sel(), types.SimpleNamespace(no_telegram=True))
    assert len(calls) == 1


def test_bot_skips_the_paper_pass_on_a_filtered_run(conn, monkeypatch, capsys):
    import bot
    calls = []
    monkeypatch.setattr(paper, "run", lambda conn, selection: calls.append(1) or 11)
    for flag, value in (("sec_only", True), ("crypto_only", True), ("min_score", 50.0),
                        ("min_liquidity", 1e6)):
        bot._run_paper(conn, _sel(), types.SimpleNamespace(no_telegram=True, **{flag: value}))
        assert "[paper] skipped: filtered run" in capsys.readouterr().out
    assert calls == []
    bot._run_paper(conn, _sel(), types.SimpleNamespace(no_telegram=True, min_score=0))
    assert calls == [1]


def test_a_failing_monthly_report_does_not_escape_the_paper_pass(conn, monkeypatch):
    import bot
    import paper_report
    monkeypatch.setattr(paper, "run", lambda conn, selection: 11)

    def boom(conn, today):
        raise RuntimeError("report")
    monkeypatch.setattr(paper_report, "maybe_send_monthly_report", boom)
    bot._run_paper(conn, _sel(), types.SimpleNamespace(no_telegram=False))    # no exception


# ------------------------------------------------------- small-company books
def _hr_sel(*sigs):
    return strategy.Selection(strong=[], candidates=[], t212_checked=True,
                              high_risk=[strategy.Tiered(s, "high_risk") for s in sigs])


def test_small_books_buy_only_high_risk_signals(conn):
    sel = strategy.Selection(strong=[strategy.Tiered(_sig("AAA"), "strong")], candidates=[],
                             t212_checked=True,
                             high_risk=[strategy.Tiered(_sig("SML", tier="high_risk"), "high_risk")])
    assert [s.ticker for s in paper.stock_signals(sel, paper.BOOK_BY_CODE["H1"])] == ["SML"]
    assert [s.ticker for s in paper.stock_signals(sel, paper.BOOK_BY_CODE["R1-E1"])] == ["AAA"]


def test_high_risk_buy_is_labelled_high_risk_not_candidate(conn):
    """paper._buy_reason used to map every non-strong tier to "Кандидат" -- a
    high-risk buy (H1/H2) was recorded as "Кандидат: SEC, ...", indistinguishable
    from a stock book's R2 candidate buys."""
    code = _book(conn, "H1")
    sel = _hr_sel(_sig("AAA", tier="high_risk"))
    paper.stock_step(conn, paper.BOOK_BY_CODE[code], sel, paper.Prices(Fetch({})), TODAY)
    [o] = paper.orders(conn, code)
    assert o["reason"] == "Высокий риск: SEC, AAA Corp"


def test_small_books_buy_a_fifth_and_hold_five(conn):
    _book(conn)
    sigs = [_sig(f"S{i}", tier="high_risk") for i in range(6)]
    paper.stock_step(conn, paper.BOOK_BY_CODE["H1"], _hr_sel(*sigs), paper.Prices(Fetch({})), TODAY)
    orders = paper.orders(conn, "H1")
    assert [o["amount_eur"] for o in orders if o["status"] == "pending"] == [4_000.0] * 5
    assert [o["note"] for o in orders if o["status"] == "skipped"] == ["мест нет"]


@pytest.mark.parametrize("code,days,value,expected", [
    ("H1", 181, 1_000.0, None), ("H1", 182, 4_000.0, "182 дн. в позиции"),
    ("H2", 91, 4_000.0, "91 дн. в позиции"), ("H2", 10, 2_799.0, "стоп -30%"),
    ("H2", 10, 6_001.0, "цель +50%"), ("H2", 10, 3_000.0, None),
])
def test_small_book_exits(conn, code, days, value, expected):
    _book(conn)
    pos = _position(conn, code, fill_days_ago=days, net=4_000.0, value=value)
    assert _exit(conn, code, pos) == expected


def test_run_trades_the_small_books_against_iwm(conn):
    day1 = TODAY - dt.timedelta(days=1)
    before = day1 - dt.timedelta(days=1)       # run() only uses bars dated before its day
    _run_day(conn, day1, {"IWM": _bars([200, 210], before), "SML": _bars([10, 10], before)},
             _hr_sel(_sig("SML", tier="high_risk")))
    assert [o["ticker"] for o in paper.orders(conn, "H1")] == ["SML"]
    assert conn.execute("SELECT bench FROM paper_equity WHERE book = 'H1'").fetchone()[0] == \
        pytest.approx(20_000.0)
