"""prices._closes keeps the series it gets and serves it when Yahoo refuses, with the price of the hour."""
from __future__ import annotations

import datetime as dt
import json

import prices

TODAY = dt.date.today()


def bars(n=5, last_days_ago=1):
    start = TODAY - dt.timedelta(days=n - 1 + last_days_ago)
    return [((start + dt.timedelta(days=i)).isoformat(), 100.0 + i) for i in range(n)]


def test_a_series_yahoo_gave_is_kept_and_served_when_yahoo_refuses(monkeypatch, capsys):
    monkeypatch.setattr(prices.sources, "price_history", lambda asset, days: (bars(), "Yahoo"))
    assert prices._closes("NVDA", 800) == bars()
    monkeypatch.setattr(prices.sources, "price_history", lambda asset, days: (None, None))
    monkeypatch.setattr(prices.sources, "current_price", lambda asset: (111.0, "TradingView"))
    got = prices._closes("NVDA", 800)
    assert got[:-1] == bars() and got[-1] == (TODAY.isoformat(), 111.0)       # today's price on top
    assert "Yahoo не ответил" in capsys.readouterr().err


def test_a_stock_series_from_another_source_is_still_refused_but_the_cache_answers(monkeypatch):
    monkeypatch.setattr(prices.sources, "price_history", lambda asset, days: (bars(), "Yahoo"))
    prices._closes("NVDA", 800)
    monkeypatch.setattr(prices.sources, "price_history", lambda asset, days: ([("2026-01-01", 1.0)], "Nasdaq"))
    monkeypatch.setattr(prices.sources, "current_price", lambda asset: (None, None))
    assert prices._closes("NVDA", 800) == bars()                              # the unadjusted series is not used


def test_nothing_kept_an_old_copy_or_a_broken_file_is_no_series(monkeypatch):
    monkeypatch.setattr(prices.sources, "price_history", lambda asset, days: (None, None))
    monkeypatch.setattr(prices.sources, "current_price", lambda asset: (111.0, "TradingView"))
    assert prices._closes("NVDA", 800) == []
    prices.PRICE_CACHE_DIR.mkdir(parents=True)
    old = (TODAY - dt.timedelta(days=prices.PRICE_CACHE_DAYS + 1)).isoformat()
    prices._cache_file("NVDA").write_text(json.dumps({"saved": old, "bars": bars()}))
    assert prices._closes("NVDA", 800) == []
    prices._cache_file("NVDA").write_text("{not json")
    assert prices._closes("NVDA", 800) == []


def test_todays_bar_is_not_added_twice_and_a_failing_price_leaves_the_series(monkeypatch):
    with_today = bars(last_days_ago=0)
    monkeypatch.setattr(prices.sources, "price_history", lambda asset, days: (with_today, "Yahoo"))
    prices._closes("CRYPTO-X-USD".replace("CRYPTO-X", "BTC"), 800)
    monkeypatch.setattr(prices.sources, "price_history", lambda asset, days: (None, None))

    def boom(asset):
        raise RuntimeError("down")
    monkeypatch.setattr(prices.sources, "current_price", boom)
    assert prices._closes("BTC-USD", 800) == with_today
