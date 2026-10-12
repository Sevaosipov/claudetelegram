"""An ISIN is priced through the listing Trading 212 sells it as (marketcap.ISIN_LISTINGS)."""
from __future__ import annotations

import pytest

import fx
import marketcap
import positions
import prices
import weekly


@pytest.fixture(autouse=True)
def empty_listings():
    marketcap.ISIN_LISTINGS.clear()
    yield
    marketcap.ISIN_LISTINGS.clear()


def _instruments(conn, rows):
    conn.executemany("INSERT OR REPLACE INTO t212_instruments (ticker, isin, type, short_name, currency) "
                     "VALUES (?,?,?,?,?)", rows)


def test_the_listing_of_an_isin_comes_from_trading_212s_list(conn):
    _instruments(conn, [
        ("SAPd_EQ", "DE0007164600", "STOCK", "SAP", "EUR"),
        ("DGEl_EQ", "GB0002374006", "STOCK", "DGE", "GBX"), ("GUId_EQ", "GB0002374006", "STOCK", "GUI", "EUR"),
        ("BT.Al_EQ", "GB0030913577", "STOCK", "BT.A", "GBX"),
        ("VOL1d_EQ", "SE0000115446", "STOCK", "VOL1", "EUR"),
        ("VUSAl_EQ", "IE00B3XXRP09", "ETF", "VUSA", "GBX"),                 # not a stock
        ("XYZ_US_EQ", "US0000000001", "STOCK", "XYZ", "USD")])              # a US line: no venue letter
    assert marketcap.load_isin_listings(conn) == 4
    assert marketcap.ISIN_LISTINGS["DE0007164600"] == ("SAP.DE", "EUR", "SAP")
    assert marketcap.ISIN_LISTINGS["GB0002374006"] == ("DGE.L", "GBX", "DGE")          # London first for a GB ISIN
    assert marketcap.ISIN_LISTINGS["GB0030913577"][0] == "BT-A.L"
    assert positions.yahoo_symbol("DE0007164600", "BAFIN") == "SAP.DE"
    assert prices.listing("GB0002374006", "UK") == ("DGE.L", "GBX")
    assert positions.yahoo_symbol("FR0000000001", "BAFIN") is None                     # an ISIN it does not sell
    assert "DGE Buy · " in weekly.buy_text({"ticker": "GB0002374006", "price": 1673.0, "stop_pct": 0.1})


def test_without_the_list_an_isin_stays_unpriced(conn):
    assert marketcap.load_isin_listings(conn) == 0
    assert positions.yahoo_symbol("DE0007164600", "BAFIN") is None and prices.listing("DE0007164600", "BAFIN") is None


def test_pence_are_a_hundredth_of_a_pound(conn):
    assert fx.per_eur("GBX", conn) == pytest.approx(100 * fx.per_eur("GBP", conn))
    assert fx.to_eur(1673.0, "GBX", conn) == pytest.approx(fx.to_eur(16.73, "GBP", conn))
