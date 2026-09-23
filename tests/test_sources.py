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


def _stub_history(monkeypatch, **results):
    """Each history provider returns its given value, raises it if it's an exception,
    or returns None when not given."""
    for name in ("_yahoo_history", "_nasdaq_history", "_binance_history",
                 "_bybit_history", "_kraken_history"):
        value = results.get(name)

        def fake(*args, _v=value):
            if isinstance(_v, Exception):
                raise _v
            return _v
        monkeypatch.setattr(sources, name, fake)


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


def test_crypto_current_price_falls_back_to_binance(monkeypatch):
    _stub_history(monkeypatch, _yahoo_history=None)
    monkeypatch.setattr(sources, "_tradingview_close", lambda asset: None)
    monkeypatch.setattr(sources.crypto, "price_usd", lambda conn, sym: None)
    monkeypatch.setattr(sources, "_binance_price", lambda sym: 86000.0)
    assert sources.current_price(BTC) == (86000.0, "Binance")


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
