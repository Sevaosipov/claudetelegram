"""cfd/instruments.py: the universe table and the cost table are the pre-registration's own
numbers (spec 2026-10-05-cfd-signals.md §1.1 and §1.4), so the tests pin every row of both."""
from __future__ import annotations

import pytest

from cfd import instruments as ins

# spec §1.1, row by row: Yahoo symbol -> name shown
EXPECTED_NAMES = {
    "EURUSD=X": "EURUSD", "GBPUSD=X": "GBPUSD", "USDJPY=X": "USDJPY", "AUDUSD=X": "AUDUSD",
    "USDCAD=X": "USDCAD", "USDCHF=X": "USDCHF", "NZDUSD=X": "NZDUSD",
    "EURGBP=X": "EURGBP", "EURJPY=X": "EURJPY", "GBPJPY=X": "GBPJPY", "EURCHF=X": "EURCHF",
    "GC=F": "XAUUSD", "SI=F": "XAGUSD",
    "CL=F": "WTI", "BZ=F": "BRENT",
    "^GSPC": "US500", "^NDX": "US100", "^DJI": "US30", "^GDAXI": "DE40", "^FTSE": "UK100",
    "^N225": "JP225",
    "BTC-USD": "BTCUSD", "ETH-USD": "ETHUSD",
}


def test_the_universe_is_the_23_instruments_of_the_spec():
    assert len(ins.UNIVERSE) == 23
    assert {i.symbol: i.name for i in ins.UNIVERSE} == EXPECTED_NAMES


def test_symbols_and_names_are_unique():
    assert len({i.symbol for i in ins.UNIVERSE}) == 23
    assert len({i.name for i in ins.UNIVERSE}) == 23


def test_class_sizes_match_the_spec_table():
    counts: dict[str, int] = {}
    for i in ins.UNIVERSE:
        counts[i.klass] = counts.get(i.klass, 0) + 1
    assert counts == {"FX major": 7, "FX cross": 4, "Metals": 2, "Energy": 2, "Indices": 6,
                      "Crypto": 2}


def test_the_gate_merges_fx_majors_and_crosses_into_one_class():
    # spec §1.6: the classes are FX (majors and crosses together), Metals, Energy, Indices, Crypto
    assert ins.GATE_CLASSES == ("FX", "Metals", "Energy", "Indices", "Crypto")
    assert ins.by_symbol("EURUSD=X").gate_class == "FX"
    assert ins.by_symbol("EURGBP=X").gate_class == "FX"
    assert ins.by_symbol("GC=F").gate_class == "Metals"
    assert ins.by_symbol("CL=F").gate_class == "Energy"
    assert ins.by_symbol("^GSPC").gate_class == "Indices"
    assert ins.by_symbol("BTC-USD").gate_class == "Crypto"
    assert {i.gate_class for i in ins.UNIVERSE} == set(ins.GATE_CLASSES)


@pytest.mark.parametrize("symbol, quote", [
    ("EURUSD=X", "USD"), ("GBPUSD=X", "USD"), ("AUDUSD=X", "USD"), ("NZDUSD=X", "USD"),
    ("USDJPY=X", "JPY"), ("USDCAD=X", "CAD"), ("USDCHF=X", "CHF"),
    ("EURGBP=X", "GBP"), ("EURJPY=X", "JPY"), ("GBPJPY=X", "JPY"), ("EURCHF=X", "CHF"),
    ("GC=F", "USD"), ("SI=F", "USD"), ("CL=F", "USD"), ("BZ=F", "USD"),
    ("^GSPC", "USD"), ("^NDX", "USD"), ("^DJI", "USD"),
    ("^GDAXI", "EUR"), ("^FTSE", "GBP"), ("^N225", "JPY"),
    ("BTC-USD", "USD"), ("ETH-USD", "USD"),
])
def test_quote_currency(symbol, quote):
    assert ins.by_symbol(symbol).quote == quote


def test_unit_labels_per_class():
    assert {i.unit for i in ins.UNIVERSE if i.gate_class == "FX"} == {"ед."}
    assert {i.unit for i in ins.UNIVERSE if i.klass == "Metals"} == {"унц."}
    assert {i.unit for i in ins.UNIVERSE if i.klass == "Energy"} == {"барр."}
    assert {i.unit for i in ins.UNIVERSE if i.klass == "Indices"} == {"контр."}
    assert {i.unit for i in ins.UNIVERSE if i.klass == "Crypto"} == {"монет"}


# ------------------------------------------------------------------ costs (spec §1.4)
@pytest.mark.parametrize("symbol, round_trip, fin_long, fin_short", [
    ("EURUSD=X", 0.015, 0.010, 0.010),     # FX major
    ("USDJPY=X", 0.015, 0.010, 0.010),
    ("EURGBP=X", 0.030, 0.010, 0.010),     # FX cross
    ("GBPJPY=X", 0.030, 0.010, 0.010),
    ("GC=F", 0.030, 0.015, 0.015),         # XAUUSD
    ("SI=F", 0.060, 0.015, 0.015),         # XAGUSD
    ("CL=F", 0.060, 0.020, 0.020),         # energy
    ("BZ=F", 0.060, 0.020, 0.020),
    ("^GSPC", 0.030, 0.020, 0.005),        # indices: long 0.020, short 0.005
    ("^N225", 0.030, 0.020, 0.005),
    ("BTC-USD", 0.250, 0.060, 0.060),      # crypto
    ("ETH-USD", 0.250, 0.060, 0.060),
])
def test_cost_table_rows(symbol, round_trip, fin_long, fin_short):
    c = ins.by_symbol(symbol).costs
    assert c.round_trip_pct == round_trip
    assert c.financing_long_pct == fin_long
    assert c.financing_short_pct == fin_short


def test_every_instrument_resolves_its_cost_keys():
    for i in ins.UNIVERSE:
        assert i.cost_key in ins.ROUND_TRIP_PCT
        assert i.financing_key in ins.FINANCING_PCT
        assert i.costs.round_trip_pct > 0


def test_financing_follows_the_side_for_indices_only():
    idx = ins.by_symbol("^GDAXI").costs
    assert idx.financing_pct("long") == 0.020
    assert idx.financing_pct("short") == 0.005
    fx = ins.by_symbol("EURUSD=X").costs
    assert fx.financing_pct("long") == fx.financing_pct("short") == 0.010
    with pytest.raises(ValueError):
        idx.financing_pct("sideways")


def test_gold_intraday_pays_the_xauusd_round_trip_and_no_financing():
    # spec §1.4: PB-H1-GOLD pays the XAUUSD round trip and no financing (flat the same day)
    c = ins.gold_intraday_costs()
    assert c.round_trip_pct == 0.030
    assert c.financing_long_pct == 0.0 and c.financing_short_pct == 0.0


def test_lookup_by_symbol_and_by_name():
    assert ins.by_symbol("GC=F").name == "XAUUSD"
    assert ins.by_name("XAUUSD").symbol == "GC=F"
    with pytest.raises(KeyError):
        ins.by_symbol("AAPL")
    with pytest.raises(KeyError):
        ins.by_name("AAPL")


def test_the_gold_symbol_constant_is_the_gold_future():
    assert ins.GOLD_SYMBOL == "GC=F"
    assert ins.by_symbol(ins.GOLD_SYMBOL).klass == "Metals"
