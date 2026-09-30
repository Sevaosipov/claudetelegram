"""positions.py: what the user reports buying, and when to close it."""
from __future__ import annotations

import datetime as dt
import json

import pytest

import db
import model
import model_score
import paper
import positions
from conftest import add_bafin_txn, add_form_144, add_sec_sale, add_sweden_txn

TODAY = dt.date(2026, 9, 23)


@pytest.fixture(autouse=True)
def _no_network_seams(monkeypatch):
    """The two seams a position's exits reach the network through: with no history and no
    headlines the exit rules that need them simply don't fire. Tests that care hand their own
    closes_fn / news_fn, or override these."""
    monkeypatch.setattr(paper, "_closes", lambda symbol, days: [])
    monkeypatch.setattr(model, "default_news", lambda ticker, source: [])


def _strong_journal(conn, ticker, members, source="SEC"):
    """A row as the tiered design wrote it (tier "strong"): /bought still reads those rows;
    the model's own rows are covered in test_bot_model.py."""
    db.journal_signal(conn, {"source": source, "kind": "cluster", "ticker": ticker,
                             "tier": "strong", "members": json.dumps(members)})


def _open(conn, ticker="AAA", price=100.0, days_ago=5, stop=None):
    """Opens a position with no price history (so no stored stop); `stop` sets one directly."""
    pos = positions.open_position(conn, ticker, price, today=TODAY - dt.timedelta(days=days_ago),
                                  closes_fn=lambda t, s=None: [])
    if stop is not None:
        conn.execute("UPDATE positions SET stop_pct = ? WHERE id = ?", (stop, pos.id))
        conn.commit()
    return pos


def _no_price(ticker, source=None):
    return None


def test_open_takes_the_insiders_from_the_latest_strong_signal(conn):
    _strong_journal(conn, "AAA", ["Old Buyer"])
    _strong_journal(conn, "AAA", ["Boss Person", "Board Person"])
    pos = _open(conn)
    assert pos.insiders == ["Boss Person", "Board Person"] and pos.signal_id is not None


def test_open_without_a_signal_has_no_insiders(conn):
    assert _open(conn).insiders == []


def test_second_open_on_the_same_ticker_is_refused(conn):
    _open(conn)
    with pytest.raises(ValueError):
        _open(conn)


def test_insider_sale_after_opening_closes(conn):
    _strong_journal(conn, "AAA", ["Boss Person"])
    _open(conn)
    add_sec_sale(conn, "AAA", "PERSON BOSS", 500_000, date=(TODAY - dt.timedelta(days=1)).isoformat())
    [alert] = positions.check_exits(conn, today=TODAY, price_fn=_no_price)
    assert alert.trigger == "insider_sell" and "PERSON BOSS" in alert.detail


def test_sale_before_opening_does_not_count(conn):
    _strong_journal(conn, "AAA", ["Boss Person"])
    add_sec_sale(conn, "AAA", "Boss Person", 500_000, date=(TODAY - dt.timedelta(days=30)).isoformat())
    _open(conn)
    assert positions.check_exits(conn, today=TODAY, price_fn=_no_price) == []


def test_planned_10b5_1_sale_does_not_count(conn):
    _strong_journal(conn, "AAA", ["Boss Person"])
    _open(conn)
    add_sec_sale(conn, "AAA", "Boss Person", 500_000, date=(TODAY - dt.timedelta(days=1)).isoformat())
    conn.execute("UPDATE sec_sales SET is_10b5_1 = 1")
    assert positions.check_exits(conn, today=TODAY, price_fn=_no_price) == []


def test_form_144_notice_counts_as_selling(conn):
    _strong_journal(conn, "AAA", ["Boss Person"])
    _open(conn)
    add_form_144(conn, "AAA", "Boss Person", 900_000, date=(TODAY - dt.timedelta(days=1)).isoformat())
    [alert] = positions.check_exits(conn, today=TODAY, price_fn=_no_price)
    assert alert.trigger == "insider_sell" and "144" in alert.detail


def test_small_drawdown_does_not_close(conn):
    _open(conn, price=100.0)
    assert positions.check_exits(conn, today=TODAY, price_fn=lambda t, s=None: 90.0) == []


def test_each_position_alerts_once(conn):
    _open(conn, days_ago=model.MAX_HOLD_DAYS)
    alerts = positions.check_exits(conn, today=TODAY, price_fn=_no_price)
    positions.mark_alerted(conn, alerts, today=TODAY)
    assert positions.check_exits(conn, today=TODAY, price_fn=_no_price) == []


def test_close_removes_it_from_open_positions(conn):
    _open(conn)
    closed = positions.close_position(conn, "aaa", today=TODAY)
    assert closed.close_reason == "manual" and positions.open_positions(conn) == []
    assert positions.close_position(conn, "AAA", today=TODAY) is None


def test_open_position_stores_an_explicit_source(conn):
    """telegram_bot passes "CRYPTO" for crypto tickers even when there's no strong
    signal to read a source off of."""
    pos = positions.open_position(conn, "CRYPTO:BTC", 50_000.0, source="CRYPTO")
    assert pos.source == "CRYPTO"


def test_open_position_explicit_source_overrides_signal_journal(conn):
    _strong_journal(conn, "AAA", ["Boss Person"], source="SEC")
    pos = positions.open_position(conn, "AAA", 100.0, source="CRYPTO")
    assert pos.source == "CRYPTO"


# --------------------------------------------------------------- position_source
def test_position_source_is_crypto_for_a_crypto_ticker(conn):
    assert positions.position_source(conn, "CRYPTO:BTC") == "CRYPTO"


def test_position_source_reads_the_latest_strong_journal_row(conn):
    _strong_journal(conn, "EQNR", ["Boss Person"], source="NORWAY")
    assert positions.position_source(conn, "EQNR") == "NORWAY"


def test_position_source_is_none_with_no_signal_and_not_crypto(conn):
    assert positions.position_source(conn, "ZZZZ") is None


def test_open_position_derives_source_from_position_source_when_not_given(conn):
    _strong_journal(conn, "EQNR", ["Boss Person"], source="NORWAY")
    pos = positions.open_position(conn, "EQNR", 270.0)
    assert pos.source == "NORWAY"


# ------------------------------------------------------- pricing by listing
def test_last_close_uses_the_crypto_symbol(conn, monkeypatch):
    """positions.last_close must never price a crypto position through the bare
    "BTC"/"ETH" Yahoo tickers -- those are unrelated US-listed ETFs."""
    seen = []
    monkeypatch.setattr(positions, "_yahoo_close", lambda symbol: seen.append(symbol) or 65_000.0)
    assert positions.last_close("CRYPTO:BTC") == 65_000.0
    assert seen == ["BTC-USD"]


def test_last_close_uses_the_source_venue_suffix(conn, monkeypatch):
    seen = []
    monkeypatch.setattr(positions, "_yahoo_close", lambda symbol: seen.append(symbol) or 100.0)
    positions.last_close("NRC", "NORWAY")
    assert seen == ["NRC.OL"]


def test_last_close_treats_an_unknown_source_as_the_us_listing(conn, monkeypatch):
    seen = []
    monkeypatch.setattr(positions, "_yahoo_close", lambda symbol: seen.append(symbol) or 100.0)
    positions.last_close("EQNR")
    assert seen == ["EQNR"]


def test_last_close_is_none_for_an_isin(conn, monkeypatch):
    """BaFin and FI identify issuers by ISIN, not ticker -- an ISIN never resolves
    on Yahoo, so pricing it would silently price the wrong instrument (or nothing)."""
    monkeypatch.setattr(positions, "_yahoo_close", lambda symbol: 100.0)
    assert positions.last_close("DE0007164600", "BAFIN") is None


def test_check_exits_prices_through_the_positions_own_source(conn):
    """check_exits must pass the position's source along, not just its ticker --
    otherwise the caller has no way to price a non-US listing correctly."""
    _open(conn, ticker="NRC")
    conn.execute("UPDATE positions SET source = 'NORWAY'")
    seen = []

    def price_fn(ticker, source):
        seen.append((ticker, source))
        return None
    positions.check_exits(conn, today=TODAY, price_fn=price_fn)
    assert seen == [("NRC", "NORWAY")]


# --------------------------------------------------- European sale triggers
def test_oslo_sale_after_opening_closes(conn):
    _strong_journal(conn, "NRC", ["Boss Person"], source="NORWAY")
    _open(conn, ticker="NRC")
    conn.execute(
        "INSERT INTO norway_purchases (message_id, person, issuer_name, ticker, txn_type, "
        "txn_date, shares, price, currency, value, source_url) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (1, "Boss Person", "Test ASA", "NRC", "S", (TODAY - dt.timedelta(days=1)).isoformat(),
         1000, 100.0, "NOK", 100_000, "u"))
    conn.commit()
    [alert] = positions.check_exits(conn, today=TODAY, price_fn=_no_price)
    assert alert.trigger == "insider_sell" and "Oslo" in alert.detail


def test_sweden_sale_after_opening_closes(conn):
    _strong_journal(conn, "SE0000000001", ["Boss Person"], source="SWEDEN")
    _open(conn, ticker="SE0000000001")
    add_sweden_txn(conn, "SE0000000001", "Boss Person", 500_000,
                   date=(TODAY - dt.timedelta(days=1)).isoformat(), txn_type="S")
    [alert] = positions.check_exits(conn, today=TODAY, price_fn=_no_price)
    assert alert.trigger == "insider_sell" and "FI" in alert.detail


def test_bafin_sale_after_opening_closes(conn):
    _strong_journal(conn, "DE0007164600", ["Boss Person"], source="BAFIN")
    _open(conn, ticker="DE0007164600")
    add_bafin_txn(conn, "DE0007164600", "Boss Person", 500_000,
                  date=(TODAY - dt.timedelta(days=1)).strftime("%d.%m.%Y"), txn_type="S")
    [alert] = positions.check_exits(conn, today=TODAY, price_fn=_no_price)
    assert alert.trigger == "insider_sell" and "BaFin" in alert.detail


def test_bafin_sale_before_opening_does_not_count(conn):
    _strong_journal(conn, "DE0007164600", ["Boss Person"], source="BAFIN")
    add_bafin_txn(conn, "DE0007164600", "Boss Person", 500_000,
                  date=(TODAY - dt.timedelta(days=30)).strftime("%d.%m.%Y"), txn_type="S")
    _open(conn, ticker="DE0007164600")
    assert positions.check_exits(conn, today=TODAY, price_fn=_no_price) == []


# ------------------------------------------------------------- coin positions
_FALLING = {"ret_7d": -6.2, "above_ma20": False}
_RISING = {"ret_7d": 3.0, "above_ma20": True}


def _trend(value):
    return lambda conn, sym: value


def _caution_journal(conn, ticker="CRYPTO:BTC", days_ago=1, kind="etf_flow", value=9e8):
    db.journal_signal(conn, {"source": "CRYPTO_ETF", "kind": kind, "ticker": ticker,
                             "tier": "caution", "total_value_eur": value})
    conn.execute("UPDATE signal_journal SET emitted_at = ? WHERE id = (SELECT max(id) FROM signal_journal)",
                 ((TODAY - dt.timedelta(days=days_ago)).isoformat() + " 12:00:00",))
    conn.commit()


def test_a_caution_confirmed_by_the_price_closes_a_coin(conn):
    _open(conn, "CRYPTO:BTC", 84_500.0, days_ago=5)
    _caution_journal(conn, days_ago=1)
    [alert] = positions.check_exits(conn, today=TODAY, price_fn=lambda t, s=None: 79_900.0,
                                    trend_fn=_trend(_FALLING))
    assert alert.trigger == "caution" and alert.last_price == 79_900.0
    assert "отток из спот-ETF" in alert.detail and "-6.2% за 7 дн." in alert.detail
    assert "(€900 млн)" in alert.detail


def test_an_unconfirmed_caution_does_not_close(conn):
    _open(conn, "CRYPTO:BTC", 84_500.0)
    _caution_journal(conn, days_ago=1)
    assert positions.check_exits(conn, today=TODAY, price_fn=lambda t, s=None: 84_000.0,
                                 trend_fn=_trend(_RISING)) == []


def test_a_caution_from_before_the_position_does_not_count(conn):
    _open(conn, "CRYPTO:BTC", 84_500.0, days_ago=2)
    _caution_journal(conn, days_ago=4)
    assert positions.check_exits(conn, today=TODAY, price_fn=lambda t, s=None: 84_000.0,
                                 trend_fn=_trend(_FALLING)) == []


def test_a_caution_the_price_confirms_days_later_still_closes(conn):
    _open(conn, "CRYPTO:BTC", 84_500.0, days_ago=6)
    _caution_journal(conn, days_ago=3)
    [alert] = positions.check_exits(conn, today=TODAY, price_fn=lambda t, s=None: 80_000.0,
                                    trend_fn=_trend(_FALLING))
    assert alert.trigger == "caution"


def test_a_caution_older_than_a_week_does_not_count(conn):
    _open(conn, "CRYPTO:BTC", 84_500.0, days_ago=20)
    _caution_journal(conn, days_ago=8)
    assert positions.check_exits(conn, today=TODAY, price_fn=lambda t, s=None: 84_000.0,
                                 trend_fn=_trend(_FALLING)) == []


def test_a_caution_on_another_coin_does_not_count(conn):
    _open(conn, "CRYPTO:BTC", 84_500.0)
    _caution_journal(conn, ticker="CRYPTO:ETH")
    assert positions.check_exits(conn, today=TODAY, price_fn=lambda t, s=None: 84_000.0,
                                 trend_fn=_trend(_FALLING)) == []


def test_no_price_trend_means_the_caution_waits(conn):
    _open(conn, "CRYPTO:BTC", 84_500.0)
    _caution_journal(conn, days_ago=1)
    assert positions.check_exits(conn, today=TODAY, price_fn=lambda t, s=None: 84_000.0,
                                 trend_fn=_trend(None)) == []


def test_a_coin_with_no_stop_and_no_history_falls_back_to_25_percent(conn):
    _open(conn, "CRYPTO:BTC", 100.0)
    assert positions.check_exits(conn, today=TODAY, price_fn=lambda t, s=None: 80.0,
                                 trend_fn=_trend(None)) == []
    [alert] = positions.check_exits(conn, today=TODAY, price_fn=lambda t, s=None: 74.9,
                                    trend_fn=_trend(None))
    assert alert.trigger == "trailing_stop" and alert.detail == "−25% от максимума 100.00"


# ------------------------------------------------- the model's exits on your positions
def _days(start: dt.date, values):
    """Closes on consecutive calendar days from `start` -- (iso date, close), oldest first."""
    return [((start + dt.timedelta(days=i)).isoformat(), float(v)) for i, v in enumerate(values)]


def _held_bars(before, after, days_ago=5):
    """`before`: closes ending the day before the position opened; `after`: closes from the
    opening day on (the position was opened `days_ago` days before TODAY)."""
    opened = TODAY - dt.timedelta(days=days_ago)
    return _days(opened - dt.timedelta(days=len(before)), before) + _days(opened, after)


def _check(conn, price=None, bars=(), *, news=(), trend=None):
    """check_exits with every seam stubbed: the current price, the history, the headlines."""
    return positions.check_exits(
        conn, today=TODAY, price_fn=lambda t, s=None: price, trend_fn=_trend(trend),
        closes_fn=lambda t, s=None: list(bars), news_fn=lambda t, s=None: list(news))


def _days_ago_for_bdays(n):
    """How many calendar days ago a position was opened to have been held `n` business days."""
    days = 0
    while paper.business_days_between((TODAY - dt.timedelta(days=days)).isoformat(), TODAY) < n:
        days += 1
    return days


_CHOPPY = [100.0, 106.0] * 20       # 6% swings: three typical moves is about 17.5%


# ------------------------------------------------------------ the stop at open
def test_open_position_stores_the_stop_its_price_history_gives(conn):
    closes = _days(TODAY - dt.timedelta(days=60), [100.0] * 40)
    seen = []
    pos = positions.open_position(conn, "AAA", 100.0, today=TODAY,
                                  closes_fn=lambda t, s=None: seen.append((t, s)) or closes)
    assert pos.stop_pct == 0.10 and seen == [("AAA", None)]
    assert positions.open_positions(conn)[0].stop_pct == 0.10          # stored, not just returned


def test_the_stored_stop_scales_with_the_stocks_own_volatility(conn):
    closes = _days(TODAY - dt.timedelta(days=60), _CHOPPY)
    pos = positions.open_position(conn, "AAA", 100.0, today=TODAY, closes_fn=lambda t, s=None: closes)
    expected = model_score.stop_distance(_CHOPPY, "stock")
    assert expected > model_score.STOP_MIN["stock"]
    assert pos.stop_pct == pytest.approx(expected)


def test_a_coins_stop_is_sized_as_a_coin_and_priced_by_its_crypto_source(conn):
    closes = _days(TODAY - dt.timedelta(days=60), [100.0] * 40)
    seen = []
    pos = positions.open_position(conn, "CRYPTO:BTC", 50_000.0, today=TODAY, source="CRYPTO",
                                  closes_fn=lambda t, s=None: seen.append((t, s)) or closes)
    assert pos.stop_pct == model_score.STOP_MIN["crypto"] and seen == [("CRYPTO:BTC", "CRYPTO")]


def test_no_price_history_stores_no_stop(conn):
    pos = positions.open_position(conn, "AAA", 100.0, today=TODAY, closes_fn=lambda t, s=None: [])
    assert pos.stop_pct is None and positions.open_positions(conn)[0].stop_pct is None


def test_the_default_history_is_the_yahoo_series_of_the_listing_the_position_is_on(conn, monkeypatch):
    seen = []
    flat = _days(TODAY - dt.timedelta(days=60), [100.0] * 40)
    monkeypatch.setattr(paper, "_closes", lambda symbol, days: seen.append((symbol, days)) or flat)
    pos = positions.open_position(conn, "NRC", 100.0, today=TODAY, source="NORWAY")
    assert seen == [("NRC.OL", paper.PRICE_DAYS)] and pos.stop_pct == 0.10


def test_the_default_history_is_empty_without_a_symbol_or_when_the_fetch_fails(monkeypatch):
    calls = []
    monkeypatch.setattr(paper, "_closes", lambda symbol, days: calls.append(symbol) or [("2026-09-01", 1.0)])
    assert positions.daily_closes("DE0007164600", "BAFIN") == [] and calls == []      # an ISIN has none

    def boom(symbol, days):
        raise RuntimeError("offline")
    monkeypatch.setattr(paper, "_closes", boom)
    assert positions.daily_closes("AAA", None) == []


# ------------------------------------------------------------- the trailing stop
def test_the_trailing_stop_fires_from_the_peak_not_the_entry(conn):
    _open(conn, price=100.0, stop=0.10)
    bars = _held_bars([200.0] * 3, [110.0, 130.0, 125.0])
    [alert] = _check(conn, price=116.0, bars=bars)                 # 116 is above the entry: an entry-based stop is far
    assert alert.trigger == "trailing_stop" and alert.last_price == 116.0
    assert alert.detail == "−10% от максимума 130.00"


def test_above_the_trailing_stop_nothing_fires(conn):
    _open(conn, price=100.0, stop=0.10)
    assert _check(conn, price=118.0, bars=_held_bars([], [110.0, 130.0, 125.0])) == []


def test_the_peak_includes_the_entry_price(conn):
    _open(conn, price=100.0, stop=0.10)
    bars = _held_bars([], [95.0, 96.0])                            # every close since opening is under the entry
    [alert] = _check(conn, price=89.0, bars=bars)
    assert alert.trigger == "trailing_stop" and alert.detail == "−10% от максимума 100.00"
    assert _check(conn, price=91.0, bars=bars) == []


def test_a_close_from_before_the_opening_is_not_the_peak(conn):
    _open(conn, price=100.0, stop=0.10)
    assert _check(conn, price=95.0, bars=_held_bars([200.0] * 3, [100.0, 101.0])) == []


def test_the_close_on_the_opening_day_counts_towards_the_peak(conn):
    _open(conn, price=100.0, stop=0.10)
    [alert] = _check(conn, price=130.0, bars=_held_bars([], [150.0, 100.0]))
    assert alert.trigger == "trailing_stop" and alert.detail == "−10% от максимума 150.00"


def test_a_position_without_a_stop_computes_it_from_the_closes_before_opening(conn):
    _open(conn, price=100.0)                                       # no stored stop
    stop = model_score.stop_distance(_CHOPPY, "stock")
    assert 0.16 < stop < 0.19                                      # not the 10% floor, not the 15% fallback
    bars = _held_bars(_CHOPPY, [100.0, 100.0])
    assert _check(conn, price=83.0, bars=bars) == []               # 17% down: inside its own stop
    [alert] = _check(conn, price=82.0, bars=bars)
    assert alert.trigger == "trailing_stop" and alert.detail == f"−{stop * 100:.0f}% от максимума 100.00"


def test_a_stock_with_too_little_history_to_size_a_stop_falls_back_to_15_percent(conn):
    _open(conn, price=100.0)
    bars = _held_bars([100.0] * 5, [100.0])
    assert _check(conn, price=85.5, bars=bars) == []
    [alert] = _check(conn, price=84.9, bars=bars)
    assert alert.trigger == "trailing_stop" and alert.detail == "−15% от максимума 100.00"


def test_a_bar_dated_today_is_still_in_progress_and_is_ignored(conn):
    """A coin's (or a stock's) bar for the current day is not a close yet: a spike in it must
    not set the peak, or a dip in it trip the stop."""
    _open(conn, price=100.0, stop=0.10)
    bars = _held_bars([], [100.0, 130.0]) + [(TODAY.isoformat(), 200.0)]
    assert _check(conn, price=150.0, bars=bars) == []               # peak 130 -> stop 117; 200 would give 180
    [alert] = _check(conn, price=116.0, bars=bars)
    assert alert.detail == "−10% от максимума 130.00"


def test_a_bar_dated_today_does_not_flip_a_coins_trend(conn):
    _open(conn, "CRYPTO:BTC", 100.0, stop=0.25)
    bars = _days(TODAY - dt.timedelta(days=130), _CLIMB) + [(TODAY.isoformat(), 50.0)]
    assert _check(conn, price=_CLIMB[-1], bars=bars) == []


def test_without_a_price_the_price_rules_wait(conn):
    _open(conn, price=100.0, stop=0.10)
    assert _check(conn, price=None, bars=_held_bars([], [100.0, 50.0])) == []


# ---------------------------------------------------------------- dead money
def test_dead_money_at_61_business_days_and_2_percent(conn):
    _open(conn, price=100.0, days_ago=_days_ago_for_bdays(61), stop=0.10)
    bars = _held_bars([], [100.0, 102.0], days_ago=_days_ago_for_bdays(61))
    [alert] = _check(conn, price=102.0, bars=bars)
    assert alert.trigger == "dead_money" and alert.detail == "60 торговых дней без роста"


@pytest.mark.parametrize("bdays, fires", [(60, True), (59, False)])
def test_dead_money_starts_at_60_business_days(conn, bdays, fires):
    _open(conn, price=100.0, days_ago=_days_ago_for_bdays(bdays), stop=0.10)
    alerts = _check(conn, price=102.0, bars=_held_bars([], [100.0, 102.0], days_ago=_days_ago_for_bdays(bdays)))
    assert [a.trigger for a in alerts] == (["dead_money"] if fires else [])


def test_no_dead_money_at_plus_6_percent(conn):
    days = _days_ago_for_bdays(61)
    _open(conn, price=100.0, days_ago=days, stop=0.10)
    assert _check(conn, price=106.0, bars=_held_bars([], [100.0, 106.0], days_ago=days)) == []


def test_a_coin_is_never_dead_money(conn):
    days = _days_ago_for_bdays(61)
    _open(conn, "CRYPTO:BTC", 100.0, days_ago=days, stop=0.15)
    assert _check(conn, price=102.0, bars=_held_bars([], [100.0, 102.0], days_ago=days)) == []


# ----------------------------------------------------------------------- time
def test_a_year_in_the_position_closes_it(conn):
    _open(conn, price=100.0, days_ago=model.MAX_HOLD_DAYS, stop=0.10)
    bars = _held_bars([], [100.0, 120.0], days_ago=model.MAX_HOLD_DAYS)       # up 20%: not dead money
    [alert] = _check(conn, price=120.0, bars=bars)
    assert alert.trigger == "time" and alert.detail == f"{model.MAX_HOLD_DAYS} дн. в позиции"


def test_a_day_short_of_the_year_does_not(conn):
    days = model.MAX_HOLD_DAYS - 1
    _open(conn, price=100.0, days_ago=days, stop=0.10)
    assert _check(conn, price=120.0, bars=_held_bars([], [100.0, 120.0], days_ago=days)) == []


def test_the_year_closes_a_position_even_with_no_price(conn):
    _open(conn, days_ago=model.MAX_HOLD_DAYS)
    [alert] = _check(conn, price=None)
    assert alert.trigger == "time" and alert.last_price is None


# ----------------------------------------------------------------------- news
_RED = [{"title": "Acme accused of fraud by regulators", "published": "2026-09-22"}]


def test_a_red_flag_headline_closes_the_position(conn):
    _open(conn, price=100.0, stop=0.10)
    [alert] = _check(conn, price=100.0, bars=_held_bars([], [100.0]), news=_RED)
    assert alert.trigger == "news" and alert.detail == "новости: Acme accused of fraud by regulators"


def test_ordinary_headlines_do_not_close_it(conn):
    _open(conn, price=100.0, stop=0.10)
    news = [{"title": "Acme raises guidance"}, {"title": "Acme misses estimates"}]
    assert _check(conn, price=100.0, bars=_held_bars([], [100.0]), news=news) == []


def test_the_news_are_fetched_for_the_position_and_only_when_nothing_else_fired(conn):
    _open(conn, price=100.0, stop=0.10)
    asked = []

    def news_fn(ticker, source):
        asked.append((ticker, source))
        return []
    positions.check_exits(conn, today=TODAY, price_fn=lambda t, s=None: 100.0, trend_fn=_trend(None),
                          closes_fn=lambda t, s=None: _held_bars([], [100.0]), news_fn=news_fn)
    assert asked == [("AAA", None)]

    asked.clear()
    [alert] = positions.check_exits(conn, today=TODAY, price_fn=lambda t, s=None: 80.0,
                                    trend_fn=_trend(None), closes_fn=lambda t, s=None: _held_bars([], [100.0]),
                                    news_fn=news_fn)
    assert alert.trigger == "trailing_stop" and asked == []           # the stop decided: no fetch


def test_the_default_news_are_the_models(conn, monkeypatch):
    _open(conn, price=100.0, stop=0.10)
    monkeypatch.setattr(model, "default_news", lambda ticker, source: _RED)
    [alert] = positions.check_exits(conn, today=TODAY, price_fn=lambda t, s=None: 100.0,
                                    trend_fn=_trend(None), closes_fn=lambda t, s=None: _held_bars([], [100.0]))
    assert alert.trigger == "news"


def test_a_coin_red_flag_counts_for_a_coin_only(conn):
    hack = [{"title": "Bitcoin exchange hack drains funds", "published": "2026-09-22"}]
    _open(conn, "CRYPTO:BTC", 100.0, stop=0.15)
    [alert] = _check(conn, price=100.0, bars=_held_bars([], [100.0]), news=hack)
    assert alert.trigger == "news" and "hack" in alert.detail
    conn.execute("DELETE FROM positions")
    _open(conn, "AAA", 100.0, stop=0.10)
    assert _check(conn, price=100.0, bars=_held_bars([], [100.0]), news=hack) == []


# ------------------------------------------------------------------ coin trend
_SLIDE = [140.0 - 0.32 * i for i in range(130)]         # 140 down to 98.7 over 130 days
_CLIMB = [100.0 + 0.32 * i for i in range(130)]


def test_a_coin_below_its_100_day_average_and_down_over_20_days_closes(conn):
    _open(conn, "CRYPTO:BTC", 100.0, stop=0.25)
    bars = _days(TODAY - dt.timedelta(days=130), _SLIDE)
    [alert] = _check(conn, price=_SLIDE[-1], bars=bars)
    assert alert.trigger == "trend_down" and alert.detail == "ниже 100-дн. средней, 20 дн. в минусе"


def test_a_coin_in_an_uptrend_stays(conn):
    _open(conn, "CRYPTO:BTC", 100.0, stop=0.25)
    assert _check(conn, price=_CLIMB[-1], bars=_days(TODAY - dt.timedelta(days=130), _CLIMB)) == []


def test_a_coin_with_less_than_121_closes_has_no_trend_to_read(conn):
    _open(conn, "CRYPTO:BTC", 100.0, stop=0.25)
    assert _check(conn, price=_SLIDE[-1], bars=_days(TODAY - dt.timedelta(days=100), _SLIDE[:100])) == []


def test_a_stocks_downtrend_is_not_an_exit(conn):
    _open(conn, "AAA", 100.0, stop=0.10)
    assert _check(conn, price=_SLIDE[-1], bars=_days(TODAY - dt.timedelta(days=130), _SLIDE)) == []


# ------------------------------------------------------------------- precedence
def test_an_insider_sale_comes_before_the_trailing_stop(conn):
    _strong_journal(conn, "AAA", ["Boss Person"])
    _open(conn, price=100.0, stop=0.10)
    add_sec_sale(conn, "AAA", "PERSON BOSS", 500_000, date=(TODAY - dt.timedelta(days=1)).isoformat())
    [alert] = _check(conn, price=50.0, bars=_held_bars([], [100.0]), news=_RED)
    assert alert.trigger == "insider_sell"


def test_a_confirmed_caution_comes_before_the_stop_and_the_trend(conn):
    _open(conn, "CRYPTO:BTC", 100.0, stop=0.15)
    _caution_journal(conn, days_ago=1)
    [alert] = _check(conn, price=60.0, bars=_days(TODAY - dt.timedelta(days=130), _SLIDE),
                     trend=_FALLING, news=_RED)
    assert alert.trigger == "caution"


def test_the_stop_comes_before_dead_money_time_and_news(conn):
    days = model.MAX_HOLD_DAYS + 5
    _open(conn, price=100.0, days_ago=days, stop=0.10)
    [alert] = _check(conn, price=85.0, bars=_held_bars([], [100.0], days_ago=days), news=_RED)
    assert alert.trigger == "trailing_stop"


def test_dead_money_comes_before_time_and_news(conn):
    days = model.MAX_HOLD_DAYS + 5
    _open(conn, price=100.0, days_ago=days, stop=0.10)
    [alert] = _check(conn, price=102.0, bars=_held_bars([], [100.0, 102.0], days_ago=days), news=_RED)
    assert alert.trigger == "dead_money"


def test_time_comes_before_news(conn):
    days = model.MAX_HOLD_DAYS + 5
    _open(conn, price=100.0, days_ago=days, stop=0.10)
    [alert] = _check(conn, price=130.0, bars=_held_bars([], [100.0, 130.0], days_ago=days), news=_RED)
    assert alert.trigger == "time"


def test_an_older_positions_table_gains_the_stop_column(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE positions (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, "
                "source TEXT, opened_at TEXT NOT NULL, entry_price REAL NOT NULL, "
                "insiders TEXT NOT NULL DEFAULT '[]', signal_id INTEGER, closed_at TEXT, "
                "close_reason TEXT, close_alerted_at TEXT)")
    old.execute("INSERT INTO positions (ticker, opened_at, entry_price) VALUES ('OLD', '2026-01-05', 10.0)")
    old.commit()
    old.close()
    migrated = db.connect(str(path))
    [pos] = positions.open_positions(migrated)
    assert pos.ticker == "OLD" and pos.stop_pct is None
    migrated.close()
