"""paper.py: the virtual-book engine -- orders, fills, fees, valuation, the daily snapshot.
Offline -- prices come from a stub fetch, dates are fixed so weekday arithmetic is
deterministic. What the model portfolio trades on it is tested in test_model.py; the price helpers
it reads are tested in test_prices.py."""
from __future__ import annotations

import datetime as dt
import json

import pytest

import paper

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


# ------------------------------------------------------------------ foundation
def test_fees():
    assert paper.fee("AAPL", "USD") == 0.0025
    assert paper.fee("SAP", "EUR") == 0.0010
    assert paper.fee("CRYPTO:BTC", "USD") == 0.0050


# ------------------------------------------------------------- orders & fills
def _book(conn, code="TEST-S", *, start=80_000.0, sleeve="stock", bench="SPY", opened=None):
    """A book row opened 30 days ago (or on `opened`), with `start` euros in cash."""
    opened = opened or TODAY - dt.timedelta(days=30)
    conn.execute(
        "INSERT INTO paper_books (code, sleeve, start_date, start_eur, cash_eur, bench_symbol) "
        "VALUES (?,?,?,?,?,?)", (code, sleeve, opened.isoformat(), start, start, bench))
    conn.commit()
    return code


def test_a_buy_fills_at_the_first_close_after_the_decision(conn):
    code = _book(conn)
    assert paper.place_buy(conn, code, "AAA", "SEC", "тест", TODAY - dt.timedelta(days=3),
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
    paper.place_buy(conn, code, "EQNR", "NORWAY", "тест", TODAY - dt.timedelta(days=3),
                    8_000.0, max_positions=10)
    prices = paper.Prices(Fetch({"EQNR.OL": _bars([100, 100, 110, 125])}))
    paper.fill_orders(conn, code, prices, TODAY)
    paper.mark_to_market(conn, code, prices)
    [p] = paper.open_positions(conn, code)
    assert p["currency"] == "NOK" and p["entry_fx"] == pytest.approx(10.74)
    assert p["last_value"] == pytest.approx(p["net_eur"] * 1.25)
    assert paper.book_value(conn, code) == pytest.approx(72_000 + p["net_eur"] * 1.25)


def test_a_position_held_past_the_usual_history_is_still_valued_and_sold(conn):
    code = _book(conn, "TEST-C", start=20_000.0, sleeve="crypto", bench="BTC-USD")
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
    paper.place_buy(conn, code, "AAA", "SEC", "тест", TODAY - dt.timedelta(days=3), 8_000.0,
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
    paper.place_buy(conn, code, "AAA", "SEC", "тест", TODAY - dt.timedelta(days=4),
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
    paper.place_buy(conn, code, "AAA", "SEC", "тест", dt.date(2026, 10, 2), 8_000.0,
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
    assert paper.place_buy(conn, code, "DE0007164600", "BAFIN", "тест", TODAY, 8_000.0,
                           max_positions=10) == "skipped"
    for i in range(10):
        assert paper.place_buy(conn, code, f"T{i}", "SEC", "тест", TODAY, 8_000.0,
                               max_positions=10) == "pending"
    assert paper.place_buy(conn, code, "T10", "SEC", "тест", TODAY, 8_000.0,
                           max_positions=10) == "skipped"
    notes = [o["note"] for o in paper.orders(conn, code) if o["status"] == "skipped"]
    assert notes == ["нет котировки", "мест нет"]


def test_a_repeated_skip_is_recorded_once_for_two_weeks(conn):
    code = _book(conn)

    def skip(ticker, source, on):
        return paper.place_buy(conn, code, ticker, source, "тест", on, 8_000.0, max_positions=10)

    def skips():
        return [(o["ticker"], o["note"], o["created"]) for o in paper.orders(conn, code)]

    assert skip("DE0007164600", "BAFIN", TODAY) == "skipped"
    assert skip("DE0007164600", "BAFIN", TODAY + dt.timedelta(days=1)) == "skipped"   # still answered
    assert skip("DE0007164600", "BAFIN", TODAY + dt.timedelta(days=14)) == "skipped"
    assert skips() == [("DE0007164600", "нет котировки", TODAY.isoformat())]
    assert skip("DE0007164600", "BAFIN", TODAY + dt.timedelta(days=15)) == "skipped"  # a fresh row
    assert len(skips()) == 2
    for i in range(10):                                        # another ticker, another reason: recorded
        paper.place_buy(conn, code, f"T{i}", "SEC", "тест", TODAY, 100.0, max_positions=10)
    assert skip("T10", "SEC", TODAY) == "skipped"
    assert ("T10", "мест нет", TODAY.isoformat()) in skips()


def test_a_partial_slice_needs_half_a_slice_of_cash(conn):
    code = _book(conn)
    conn.execute("UPDATE paper_books SET cash_eur = 5000 WHERE code = ?", (code,))
    assert paper.place_buy(conn, code, "AAA", "SEC", "тест", TODAY, 8_000.0,
                           max_positions=10) == "pending"
    assert [o["amount_eur"] for o in paper.orders(conn, code)] == [5_000.0]
    assert paper.place_buy(conn, code, "BBB", "SEC", "тест", TODAY, 8_000.0,
                           max_positions=10) == "skipped"
    assert paper.orders(conn, code)[-1]["note"] == "нет денег"


def test_a_held_or_pending_ticker_is_not_ordered_again(conn):
    code = _book(conn)
    assert paper.place_buy(conn, code, "AAA", "SEC", "тест", TODAY, 8_000.0,
                           max_positions=10) == "pending"
    assert paper.place_buy(conn, code, "AAA", "SEC", "тест", TODAY, 8_000.0,
                           max_positions=10) == "duplicate"
    assert len(paper.orders(conn, code)) == 1


# ---------------------------------------------------------------- test positions
def _position(conn, code, ticker="AAA", fill_days_ago=10, net=8_000.0, value=None,
              insiders=("Jane Doe",), target=None, source="SEC"):
    conn.execute(
        "INSERT INTO paper_positions (book, ticker, source, symbol, currency, fill_date, cost_eur, "
        "net_eur, entry_close, entry_fx, insiders, target, reason, last_value) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (code, ticker, source, ticker, "USD", _days(fill_days_ago), net, net, 100.0, 1.16,
         json.dumps(list(insiders)), target, "тест", value if value is not None else net))
    conn.commit()
    return paper.open_positions(conn, code)[-1]


def _coin_position(conn, code, coin="BTC", fill_days_ago=5, net=10_000.0, value=None):
    conn.execute(
        "INSERT INTO paper_positions (book, ticker, source, symbol, currency, fill_date, cost_eur, "
        "net_eur, entry_close, entry_fx, insiders, reason, last_value) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (code, f"CRYPTO:{coin}", "CRYPTO", f"{coin}-USD", "USD", _days(fill_days_ago), net, net,
         100.0, 1.16, "[]", "тест", value if value is not None else net))
    conn.commit()
    return paper.open_positions(conn, code)[-1]


# ------------------------------------------------ drawdown, snapshot, day by day
def test_max_drawdown():
    assert paper.max_drawdown([100, 120, 90, 130, 117]) == pytest.approx(-0.25)
    assert paper.max_drawdown([100, 101]) == 0.0 and paper.max_drawdown([]) == 0.0


def _engine_day(conn, code, day, series):
    """One day of the engine the way model.run drives it: fill what is pending, revalue,
    store the day -- on completed bars only."""
    prices = paper.Prices(Fetch(series), day)
    paper.fill_orders(conn, code, prices, day)
    paper.mark_to_market(conn, code, prices, day)
    paper._snapshot(conn, code, prices, day)


def _live(bars, day, partial=999.0):
    """`bars` (completed) plus `day`'s still-open bar, which a run on `day` must never use."""
    return bars + [(day.isoformat(), float(partial))]


def test_orders_fill_at_the_next_completed_close_and_each_day_is_stored(conn):
    day1, day3 = TODAY - dt.timedelta(days=2), TODAY
    day0, day2 = day1 - dt.timedelta(days=1), day3 - dt.timedelta(days=1)
    code = _book(conn, opened=day1)
    _engine_day(conn, code, day1, {"SPY": _live(_bars([500, 505], day0), day1)})
    paper.place_buy(conn, code, "AAA", "SEC", "test", day1, 8_000.0, max_positions=10)
    assert [o["status"] for o in paper.orders(conn, code)] == ["pending"]
    _engine_day(conn, code, day3, {"SPY": _live(_bars([500, 505, 505, 510], day2), day3),
                                   "AAA": _live(_bars([100, 100, 100, 120], day2), day3)})
    [p] = paper.open_positions(conn, code)
    assert p["fill_date"] == day2.isoformat() and p["entry_close"] == 120.0    # day 3's 999 is still open
    rows = conn.execute("SELECT date, value, cash, bench FROM paper_equity WHERE book = ? "
                        "ORDER BY date", (code,)).fetchall()
    assert [r[0] for r in rows] == [day1.isoformat(), day3.isoformat()]
    assert rows[0][1] == pytest.approx(80_000.0) and rows[0][3] == pytest.approx(80_000.0)
    assert rows[1][1] == pytest.approx(72_000.0 + p["net_eur"])
    assert rows[1][3] == pytest.approx(80_000.0 * 510 / 505)


def test_a_crypto_buy_waits_for_the_next_close_to_complete(conn):
    code = _book(conn, "TEST-C", start=20_000.0, sleeve="crypto", bench="BTC-USD")
    d = TODAY - dt.timedelta(days=2)
    d1, d2 = d + dt.timedelta(days=1), d + dt.timedelta(days=2)
    paper.place_buy(conn, code, "CRYPTO:BTC", "CRYPTO", "test", d, 10_000.0, min_fraction=0.0)
    _engine_day(conn, code, d, {"BTC-USD": _live(_bars([100, 100], d - dt.timedelta(days=1)), d, 105)})
    assert [(o["side"], o["status"]) for o in paper.orders(conn, code)] == [("buy", "pending")]
    _engine_day(conn, code, d1, {"BTC-USD": _live(_bars([100, 100, 104], d), d1, 115)})    # D+1 still open
    assert [o["status"] for o in paper.orders(conn, code)] == ["pending"]
    assert paper.open_positions(conn, code) == []
    _engine_day(conn, code, d2, {"BTC-USD": _live(_bars([100, 100, 104, 120], d1), d2, 130)})
    [p] = paper.open_positions(conn, code)
    assert (p["fill_date"], p["entry_close"]) == (d1.isoformat(), 120.0)     # D+1's final close


def test_a_book_without_a_benchmark_price_stores_no_benchmark(conn):
    code = _book(conn)
    _engine_day(conn, code, TODAY, {})
    assert conn.execute("SELECT value, bench FROM paper_equity WHERE book = ?", (code,)).fetchone() == \
        (80_000.0, None)
