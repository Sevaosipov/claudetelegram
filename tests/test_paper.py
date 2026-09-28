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
    assert len(rows) == 11
    assert ("R1-E1", "stock", TODAY.isoformat(), 80_000.0, 80_000.0, "SPY") in rows
    assert ("R1-E1-AN", "stock", TODAY.isoformat(), 80_000.0, 80_000.0, "SPY") in rows
    assert ("C-B", "crypto", TODAY.isoformat(), 20_000.0, 20_000.0, "BTC-USD") in rows


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


def test_stock_step_places_a_sale_when_an_exit_holds(conn):
    _book(conn)
    _position(conn, "R1-E3", fill_days_ago=91)
    paper.stock_step(conn, paper.BOOK_BY_CODE["R1-E3"], _sel(), paper.Prices(Fetch({})), TODAY)
    [o] = paper.orders(conn, "R1-E3")
    assert (o["side"], o["reason"]) == ("sell", "91 дн. в позиции")
