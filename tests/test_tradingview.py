"""TradingView snapshot -> view logic.

The two network calls (symbol search, scanner) are not tested. `analyze()` and
`format_view()` are pure functions over the scanner's field dict and carry all the
judgement -- the gauge label, the indicator-state phrasing, what to show when a
field is missing -- so that is what is checked here, offline.
"""
from __future__ import annotations

import pytest

import tradingview as tv


def _snap(**overrides):
    base = {
        "Recommend.All": 0.3, "Recommend.MA": 0.5, "Recommend.Other": 0.1,
        "RSI": 55.0, "MACD.macd": 1.0, "MACD.signal": 0.5, "ADX": 20.0,
        "close": 100.0, "SMA50": 95.0, "SMA200": 90.0,
        "Perf.1M": 4.0, "Perf.3M": 8.0, "Perf.YTD": 12.0, "Perf.Y": 25.0,
        "Volatility.D": 2.5, "price_earnings_ttm": 18.0,
        "earnings_per_share_basic_ttm": 5.5, "beta_1_year": 1.1, "_symbol": "NASDAQ:X",
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize("value,label", [
    (0.72, "Strong Buy"), (0.5, "Strong Buy"), (0.3, "Buy"), (0.1, "Buy"),
    (0.0, "Neutral"), (-0.09, "Neutral"), (-0.3, "Sell"), (-0.5, "Strong Sell"),
    (-0.9, "Strong Sell"), (None, None),
])
def test_gauge_label_matches_tradingviews_own_bands(value, label):
    assert tv._gauge_label(value) == label


def test_analyze_is_none_on_an_empty_snapshot():
    assert tv.analyze(None) is None
    assert tv.analyze({}) is None
    assert tv.analyze({"_symbol": "X", "beta_1_year": 1.0}) is None  # no gauge, RSI or price


def test_analyze_reports_rsi_extremes_only():
    assert tv.analyze(_snap(RSI=75.0))["rsi_state"] == "перекуплен (RSI > 70)"
    assert tv.analyze(_snap(RSI=22.0))["rsi_state"] == "перепродан (RSI < 30)"
    assert tv.analyze(_snap(RSI=50.0))["rsi_state"] is None


def test_analyze_reads_macd_cross():
    assert tv.analyze(_snap(**{"MACD.macd": 2.0, "MACD.signal": 1.0}))["macd_state"] == "MACD выше сигнальной"
    assert tv.analyze(_snap(**{"MACD.macd": 0.5, "MACD.signal": 1.0}))["macd_state"] == "MACD ниже сигнальной"


def test_analyze_places_price_against_the_moving_averages():
    assert "выше и 50-, и 200" in tv.analyze(_snap(close=110, SMA50=100, SMA200=95))["ma_state"]
    assert "ниже и 50-, и 200" in tv.analyze(_snap(close=80, SMA50=100, SMA200=95))["ma_state"]
    assert "между" in tv.analyze(_snap(close=98, SMA50=100, SMA200=95))["ma_state"]


def test_analyze_survives_missing_fundamentals():
    v = tv.analyze(_snap(price_earnings_ttm=None, earnings_per_share_basic_ttm=None, beta_1_year=None))
    assert v is not None
    assert v["pe_ttm"] is None and v["gauge_label"] == "Buy"


def test_format_view_labels_the_gauge_as_tradingviews_not_the_bots():
    text = tv.format_view(tv.analyze(_snap(**{"Recommend.All": 0.6})))
    assert "они называют это «Strong Buy»" in text
    assert "не имеет доказанной предсказательной" in text
    assert "а не мнение бота" in text


def test_format_view_never_renders_a_bot_verdict():
    text = tv.format_view(tv.analyze(_snap(**{"Recommend.All": 0.9})))
    for verdict in ("РЕКОМЕНДУЕМ", "СТОИТ КУПИТЬ", "покупайте", "наш прогноз", "мы считаем"):
        assert verdict not in text


def test_format_view_empty_on_no_view():
    assert tv.format_view(None) == ""


def test_resolve_prefers_a_primary_exchange(monkeypatch):
    """Symbol search can return the same company on several venues; the primary
    listing has the full indicator set, the regional ones often return nothing."""
    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return [
                {"symbol": "SSS", "exchange": "DUS"},
                {"symbol": "SSS", "exchange": "XETR"},
                {"symbol": "SSS", "exchange": "MUN"},
            ]

    class FakeSession:
        def get(self, *a, **k): return FakeResp()

    assert tv.resolve_symbol("DE0007190001", FakeSession()) == "XETR:SSS"


def test_resolve_falls_back_to_search_order_when_no_primary(monkeypatch):
    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return [{"symbol": "X", "exchange": "MUN"}, {"symbol": "X", "exchange": "BER"}]

    class FakeSession:
        def get(self, *a, **k): return FakeResp()

    assert tv.resolve_symbol("X", FakeSession()) == "MUN:X"
