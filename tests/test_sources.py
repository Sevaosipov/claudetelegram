"""sources.py: each chain tries independent providers in order. Offline -- providers
and the HTTP seam are stubbed."""
from __future__ import annotations

import datetime as dt

import pytest

import assets
import sources

BARS = [("2026-09-21", 10.0), ("2026-09-22", 11.0)]
NVDA = assets.stock_asset("NVDA")
EQNR = assets.stock_asset("EQNR.OL")
BTC = assets.crypto_asset("BTC")
ISIN = assets.resolve("DE0007164600")


class _Resp:
    def __init__(self, payload=None, content=b""):
        self.payload, self.content = payload, content

    def json(self):
        return self.payload


def _stub_history(monkeypatch, spot=None, **results):
    """Each history provider returns its given value, raises it if it's an exception,
    or returns None when not given. `spot` is the exchange price Yahoo's crypto bars
    are checked against (None: no spot available)."""
    for name in ("_yahoo_history", "_nasdaq_history", "_binance_history",
                 "_bybit_history", "_kraken_history"):
        value = results.get(name)

        def fake(*args, _v=value):
            if isinstance(_v, Exception):
                raise _v
            return _v
        monkeypatch.setattr(sources, name, fake)
    monkeypatch.setattr(sources, "_crypto_spot", lambda symbol: spot)


def test_first_available_takes_the_first_source_that_answers(capsys):
    def boom():
        raise ValueError("down")
    result = sources.first_available([("A", boom), ("B", lambda: None), ("C", lambda: [1]),
                                      ("D", lambda: [2])])
    assert result == ([1], "C")
    assert "A недоступен" in capsys.readouterr().out


def test_first_available_when_everything_fails():
    assert sources.first_available([("A", lambda: None), ("B", lambda: [])]) == (None, None)


def test_stock_history_falls_back_to_nasdaq(monkeypatch):
    _stub_history(monkeypatch, _yahoo_history=ConnectionError("x"), _nasdaq_history=BARS)
    assert sources.price_history(NVDA, 30) == (BARS, "Nasdaq")


def test_european_stock_has_no_nasdaq_fallback(monkeypatch):
    _stub_history(monkeypatch, _yahoo_history=ConnectionError("x"), _nasdaq_history=BARS)
    assert sources.price_history(EQNR, 30) == (None, None)


def test_crypto_history_chain_order(monkeypatch):
    _stub_history(monkeypatch, _yahoo_history=[], _binance_history=ConnectionError("x"),
                  _bybit_history=BARS, _kraken_history=[("2020-01-01", 1.0)])
    assert sources.price_history(BTC, 30) == (BARS, "Bybit")


def test_isin_has_no_price_history(monkeypatch):
    _stub_history(monkeypatch, _yahoo_history=BARS)
    assert sources.price_history(ISIN, 30) == (None, None)


def test_current_price_falls_back_to_tradingview(monkeypatch):
    _stub_history(monkeypatch, _yahoo_history=ConnectionError("x"))
    monkeypatch.setattr(sources, "_tradingview_close", lambda asset: 42.0)
    assert sources.current_price(EQNR) == (42.0, "TradingView")


def test_yahoo_crypto_bars_far_from_the_spot_fall_through(monkeypatch):
    """Yahoo lists a clashing coin under a numbered ticker, so the plain SYM-USD can be
    another token entirely (M-USD at 0.00029 while MemeCore trades at 1.22)."""
    binance = [("2026-09-22", 100.0)]
    _stub_history(monkeypatch, spot=100.0, _yahoo_history=BARS, _binance_history=binance)
    assert sources.price_history(BTC, 30) == (binance, "Binance")


def test_yahoo_crypto_bars_within_ten_percent_are_accepted(monkeypatch):
    _stub_history(monkeypatch, spot=11.9, _yahoo_history=BARS, _binance_history=[("x", 1.0)])
    assert sources.price_history(BTC, 30) == (BARS, "Yahoo")


def test_yahoo_crypto_bars_are_accepted_without_a_spot(monkeypatch):
    _stub_history(monkeypatch, spot=None, _yahoo_history=BARS, _binance_history=[("x", 1.0)])
    assert sources.price_history(BTC, 30) == (BARS, "Yahoo")


def _stub_crypto_prices(monkeypatch, **prices):
    """Each current-price provider returns its given value (default None)."""
    monkeypatch.setattr(sources, "_binance_price", lambda sym: prices.get("binance"))
    monkeypatch.setattr(sources, "_bybit_price", lambda sym: prices.get("bybit"))
    monkeypatch.setattr(sources.crypto, "price_usd", lambda conn, sym: prices.get("coingecko"))
    monkeypatch.setattr(sources, "_tradingview_close", lambda asset: prices.get("tradingview"))
    monkeypatch.setattr(sources, "_yahoo_history",
                        lambda sym, days: [("2026-09-22", prices["yahoo"])] if "yahoo" in prices else None)


def test_crypto_current_price_asks_the_exchanges_first(monkeypatch):
    _stub_crypto_prices(monkeypatch, binance=86000.0, bybit=1.0, coingecko=2.0,
                        tradingview=3.0, yahoo=4.0)
    assert sources.current_price(BTC) == (86000.0, "Binance")
    _stub_crypto_prices(monkeypatch, bybit=1.0, coingecko=2.0, tradingview=3.0, yahoo=4.0)
    assert sources.current_price(BTC) == (1.0, "Bybit")
    _stub_crypto_prices(monkeypatch, coingecko=2.0, tradingview=3.0, yahoo=4.0)
    assert sources.current_price(BTC) == (2.0, "CoinGecko")
    _stub_crypto_prices(monkeypatch, tradingview=3.0, yahoo=4.0)
    assert sources.current_price(BTC) == (3.0, "TradingView")
    _stub_crypto_prices(monkeypatch, yahoo=4.0)
    assert sources.current_price(BTC) == (4.0, "Yahoo")


def test_crypto_spot_takes_the_first_exchange_that_answers_and_never_raises(monkeypatch, capsys):
    def down(sym):
        raise ConnectionError("x")
    monkeypatch.setattr(sources, "_binance_price", down)
    monkeypatch.setattr(sources, "_bybit_price", lambda sym: 1.22)
    monkeypatch.setattr(sources.crypto, "price_usd", lambda conn, sym: 9.0)
    assert sources._crypto_spot("M") == 1.22
    monkeypatch.setattr(sources, "_bybit_price", down)
    monkeypatch.setattr(sources.crypto, "price_usd", lambda conn, sym: None)
    assert sources._crypto_spot("M") is None
    assert capsys.readouterr() == ("", "")


def test_bybit_price_parses_the_ticker(monkeypatch):
    seen = {}

    def fake(url, **params):
        seen.update(params)
        return _Resp({"result": {"list": [{"symbol": "MUSDT", "lastPrice": "1.2214"}]}})
    monkeypatch.setattr(sources, "_get", fake)
    assert sources._bybit_price("M") == 1.2214
    assert seen == {"category": "spot", "symbol": "MUSDT"}
    monkeypatch.setattr(sources, "_get", lambda url, **p: _Resp({"result": {"list": []}}))
    with pytest.raises((IndexError, KeyError, TypeError, ValueError)):
        sources._bybit_price("NOPE")


def test_nasdaq_history_parses_rows(monkeypatch):
    monkeypatch.setattr(sources, "_get", lambda url, **p: _Resp({"data": {"tradesTable": {"rows": [
        {"date": "09/22/2026", "close": "$1,339.75"}, {"date": "09/21/2026", "close": "$338.98"}]}}}))
    assert sources._nasdaq_history("AAPL", 10) == [("2026-09-21", 338.98), ("2026-09-22", 1339.75)]


def test_binance_history_parses_klines(monkeypatch):
    monkeypatch.setattr(sources, "_get", lambda url, **p: _Resp(
        [[1790035200000, "1", "2", "0.5", "118.57", "9"]]))
    assert sources._binance_history("SOL", 10) == [("2026-09-22", 118.57)]


def test_bybit_history_is_returned_oldest_first(monkeypatch):
    monkeypatch.setattr(sources, "_get", lambda url, **p: _Resp({"result": {"list": [
        ["1790121600000", "1", "2", "0.5", "86.0"], ["1790035200000", "1", "2", "0.5", "85.0"]]}}))
    assert sources._bybit_history("BTC", 10) == [("2026-09-22", 85.0), ("2026-09-23", 86.0)]


def test_kraken_history_uses_kraken_names_and_the_window(monkeypatch):
    seen = {}
    yesterday = dt.datetime.combine(dt.date.today() - dt.timedelta(days=1), dt.time(),
                                    dt.timezone.utc)
    old = yesterday - dt.timedelta(days=100)

    def fake(url, **params):
        seen.update(params)
        return _Resp({"error": [], "result": {"XXBTZUSD": [
            [int(old.timestamp()), "o", "h", "l", "1.0"],
            [int(yesterday.timestamp()), "o", "h", "l", "86000.1"]], "last": 1}})
    monkeypatch.setattr(sources, "_get", fake)
    assert sources._kraken_history("BTC", 10) == [(yesterday.date().isoformat(), 86000.1)]
    assert seen["pair"] == "XBTUSD"


def test_coin_list_falls_back_to_coinpaprika(monkeypatch):
    def down():
        raise ConnectionError("x")
    monkeypatch.setattr(sources, "_coingecko_coins", down)
    monkeypatch.setattr(sources, "_coinpaprika_coins", lambda: [("BTC", "btc-bitcoin", "Bitcoin", 1)])
    assert sources.coin_list() == ([("BTC", "btc-bitcoin", "Bitcoin", 1)], "CoinPaprika")


def test_cached_coins_are_cached_and_deduplicated(conn, monkeypatch):
    calls = []

    def coins():
        calls.append(1)
        return [("BTC", "bitcoin", "Bitcoin", 1), ("PEPE", "pepe", "Pepe", 30),
                ("PEPE", "other-pepe", "Other", 200)]
    monkeypatch.setattr(sources, "_coingecko_coins", coins)
    rows = sources.cached_coins(conn)
    sources.cached_coins(conn)
    assert calls == [1]
    assert [r[0] for r in rows] == ["BTC", "PEPE"] and rows[1][1] == "pepe"
    assert sources.coin_name(conn, "PEPE") == "Pepe"


def test_cached_coins_fall_back_to_the_builtin_list(conn, monkeypatch):
    monkeypatch.setattr(sources, "_coingecko_coins", lambda: None)
    monkeypatch.setattr(sources, "_coinpaprika_coins", lambda: None)
    assert {"BTC", "ETH"} <= sources.cached_coin_symbols(conn)


def test_cached_coins_negative_caching_avoids_retries(conn, monkeypatch):
    calls = []

    def failing_coins():
        calls.append(1)
        return None

    monkeypatch.setattr(sources, "_coingecko_coins", failing_coins)
    monkeypatch.setattr(sources, "_coinpaprika_coins", failing_coins)
    # First call should attempt to fetch (2 calls: CoinGecko + CoinPaprika)
    result1 = sources.cached_coins(conn)
    assert len(calls) == 2
    assert {"BTC", "ETH"} <= {r[0] for r in result1}
    # Second call within the hour should NOT attempt to fetch
    result2 = sources.cached_coins(conn)
    assert len(calls) == 2  # No additional calls
    assert result1 == result2


# ------------------------------------------------------ indicators, analysts, news
import tradingview  # noqa: E402

RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>Bitcoin climbs as ETF inflows return</title><link>https://example.test/a</link>
<pubDate>Tue, 22 Sep 2026 10:00:00 GMT</pubDate><source url="https://x">Wire A</source></item>
<item><title>Solana validators upgrade</title><link>https://example.test/b</link>
<pubDate>Mon, 21 Sep 2026 09:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_indicators_fall_back_to_a_local_calculation(monkeypatch):
    monkeypatch.setattr(tradingview, "fetch_snapshot", lambda q, session=None: None)
    closes = [100.0 + i for i in range(260)]
    view, src = sources.indicators(NVDA, closes)
    assert src == "расчёт по ценам"
    assert view["ma_state"] == "цена выше и 50-, и 200-дневной средней"
    assert view["rsi"] == 100.0 and view["gauge"] is None


def test_indicators_prefer_tradingview(monkeypatch):
    monkeypatch.setattr(tradingview, "fetch_snapshot",
                        lambda q, session=None: {"Recommend.All": 0.5, "RSI": 55.0, "close": 10.0})
    view, src = sources.indicators(NVDA, [1.0] * 10)
    assert src == "TradingView" and view["gauge"] == 0.5


def test_rsi_is_about_50_for_alternating_moves():
    assert 45 < sources._rsi([100.0, 101.0] * 30) < 55


def test_nasdaq_analyst_converts_to_the_yahoo_shape(monkeypatch):
    monkeypatch.setattr(sources, "_get", lambda url, **p: _Resp({"data": {"consensusOverview": {
        "lowPriceTarget": 245.0, "highPriceTarget": 400.0, "priceTarget": 334.9,
        "buy": 15, "sell": 4, "hold": 9}}}))
    raw = sources.nasdaq_analyst("AAPL")
    assert raw["price_targets"] == {"mean": 334.9, "low": 245.0, "high": 400.0}
    assert raw["recommendations"][0] == {"strongBuy": 0, "buy": 15, "hold": 9, "sell": 4,
                                         "strongSell": 0}


def test_nasdaq_analyst_without_coverage_is_none(monkeypatch):
    monkeypatch.setattr(sources, "_get", lambda url, **p: _Resp(
        {"data": {"consensusOverview": {"priceTarget": None}}}))
    assert sources.nasdaq_analyst("ZZZZ") is None


def test_google_news_parses_rss(monkeypatch):
    monkeypatch.setattr(sources, "_get", lambda url, **p: _Resp(content=RSS))
    items = sources._google_news("Bitcoin crypto")
    assert items[0] == {"title": "Bitcoin climbs as ETF inflows return", "publisher": "Wire A",
                        "published": "2026-09-22", "url": "https://example.test/a"}
    assert items[1]["publisher"] == "Google News"


def test_crypto_feeds_keep_only_matching_headlines_once(monkeypatch):
    monkeypatch.setattr(sources, "_get", lambda url, **p: _Resp(content=RSS))
    items = sources._crypto_feed_news("BTC", "Bitcoin")
    assert [i["title"] for i in items] == ["Bitcoin climbs as ETF inflows return"]
    assert items[0]["publisher"] == "CoinDesk"


def test_news_chain_falls_back_for_crypto(monkeypatch):
    def down(q):
        raise ConnectionError("x")
    monkeypatch.setattr(sources, "_yahoo_news", lambda s: [])
    monkeypatch.setattr(sources, "_google_news", down)
    monkeypatch.setattr(sources, "_crypto_feed_news", lambda s, n: [{"title": "t"}])
    assert sources.news(BTC, "Bitcoin") == ([{"title": "t"}], "CoinDesk/Cointelegraph")


def test_stocks_have_no_crypto_feed_fallback(monkeypatch):
    monkeypatch.setattr(sources, "_yahoo_news", lambda s: [])
    monkeypatch.setattr(sources, "_google_news", lambda q: [])
    monkeypatch.setattr(sources, "_crypto_feed_news", lambda s, n: [{"title": "t"}])
    assert sources.news(NVDA) == (None, None)
    assert sources.news(ISIN) == (None, None)


def test_stock_universe_symbols(monkeypatch):
    import universe
    monkeypatch.setattr(universe, "load", lambda: {"tickers": ["DASH", "STX", "BRK.B"]})
    assert sources.stock_universe_symbols() == {"DASH", "STX", "BRK.B"}

    def down():
        raise ConnectionError("x")
    monkeypatch.setattr(universe, "load", down)
    assert sources.stock_universe_symbols() == set()


@pytest.mark.parametrize("used,empty,note", [
    ("Yahoo", "недоступно сейчас", None),
    (None, "недоступно сейчас", "цены: недоступно сейчас"),
    (None, "нет данных", "цены: нет данных"),
    ("Binance", "недоступно сейчас", "цены: Binance (Yahoo недоступен)"),
])
def test_source_note(used, empty, note):
    assert sources.source_note("цены", used, "Yahoo", empty=empty) == note
