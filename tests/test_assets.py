"""assets.resolve: what the user typed -> which asset. Offline: the coin list is
passed in."""
from __future__ import annotations

import pytest

import assets

COINS = {"BTC", "ETH", "SOL", "PEPE", "IP"}


@pytest.mark.parametrize("text,kind,symbol,yahoo,key", [
    ("BTC", "crypto", "BTC", "BTC-USD", "CRYPTO:BTC"),
    ("btc", "crypto", "BTC", "BTC-USD", "CRYPTO:BTC"),
    ("$BTC", "stock", "BTC", "BTC", "$BTC"),
    ("btc-usd", "crypto", "BTC", "BTC-USD", "CRYPTO:BTC"),
    ("CRYPTO:SOL", "crypto", "SOL", "SOL-USD", "CRYPTO:SOL"),
    ("SOL", "crypto", "SOL", "SOL-USD", "CRYPTO:SOL"),
    ("PEPE", "crypto", "PEPE", "PEPE-USD", "CRYPTO:PEPE"),
    ("NVDA", "stock", "NVDA", "NVDA", "$NVDA"),
    ("$aapl", "stock", "AAPL", "AAPL", "$AAPL"),
    ("aapl buy now", "stock", "AAPL", "AAPL", "$AAPL"),
    ("BRK.B", "stock", "BRK.B", "BRK-B", "$BRK.B"),
    ("EQNR.OL", "stock", "EQNR.OL", "EQNR.OL", "EQNR.OL"),
    ("volv-b.st", "stock", "VOLV-B.ST", "VOLV-B.ST", "VOLV-B.ST"),
    ("SAP.DE", "stock", "SAP.DE", "SAP.DE", "SAP.DE"),
])
def test_resolve(text, kind, symbol, yahoo, key):
    a = assets.resolve(text, COINS)
    assert (a.kind, a.symbol, a.yahoo, a.key) == (kind, symbol, yahoo, key)


def test_crypto_assets_carry_their_tradingview_pair():
    assert assets.resolve("BTC", COINS).tradingview == "CRYPTO:BTCUSD"
    assert assets.resolve("NVDA", COINS).tradingview is None


def test_key_resolves_back_to_the_same_asset():
    """The analysis queue stores `key`; resolving it again must give the same asset."""
    for text in ("BTC", "$BTC", "NVDA", "EQNR.OL", "DE0007164600", "BRK.B"):
        a = assets.resolve(text, COINS)
        assert assets.resolve(a.key, COINS) == a


def test_isin_is_a_stock_without_market_symbols():
    a = assets.resolve("DE0007164600", COINS)
    assert a.is_isin and a.kind == "stock" and a.yahoo is None and a.key == "DE0007164600"


@pytest.mark.parametrize("text", ["", "   ", "$", "CRYPTO:", "-USD", "!!!", "A" * 20, "#$%"])
def test_not_a_ticker(text):
    assert assets.resolve(text, COINS) is None


def test_coin_list_is_only_consulted_when_needed():
    calls = []

    def coins():
        calls.append(1)
        return COINS
    for text in ("$BTC", "EQNR.OL", "DE0007164600", "BTC-USD", "CRYPTO:ETH"):
        assets.resolve(text, coins)
    assert calls == []
    assert assets.resolve("BTC", coins).kind == "crypto" and calls == [1]


def test_default_coins_are_the_builtin_list():
    assert assets.resolve("ETH").kind == "crypto" and assets.resolve("NVDA").kind == "stock"


def test_signal_ticker_matches_the_signal_tables():
    assert assets.resolve("BTC", COINS).signal_ticker == "CRYPTO:BTC"
    assert assets.resolve("NVDA", COINS).signal_ticker == "NVDA"
