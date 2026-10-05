"""cfd/instruments.py, round 2: the eleven alt coins of H3 (CR-BO) with their own round trip, the
index and FX universes the other two hypotheses run on, the base currency of an FX pair, and H5's own
financing -- every number is the pre-registration's own (docs/cfd/PREREGISTRATION_R2.md). Round 1's
universe and costs must not move, so the tests pin both."""
from __future__ import annotations

import pytest

from cfd import instruments as ins

# PREREGISTRATION_R2.md, H3: coin -> Yahoo symbol
H3_COINS = {
    "SOL": "SOL-USD", "XRP": "XRP-USD", "BNB": "BNB-USD", "DOGE": "DOGE-USD", "AVAX": "AVAX-USD",
    "LTC": "LTC-USD", "LINK": "LINK-USD", "TRX": "TRX-USD", "ENA": "ENA-USD",
    "HYPE": "HYPE32196-USD", "SUI": "SUI20947-USD",
}


# ------------------------------------------------------------------ H3: the eleven alts
def test_the_alt_coins_are_the_eleven_of_the_preregistration_in_its_order():
    assert [i.symbol for i in ins.ALT_COINS] == list(H3_COINS.values())
    assert len(ins.ALT_COINS) == 11


def test_alt_coin_names_are_the_ticker_plus_usd_like_btcusd():
    assert {i.symbol: i.name for i in ins.ALT_COINS} == {s: f"{t}USD" for t, s in H3_COINS.items()}


def test_every_alt_is_a_usd_crypto_in_the_crypto_gate_class():
    for i in ins.ALT_COINS:
        assert (i.klass, i.gate_class, i.quote, i.unit) == (ins.CRYPTO, "Crypto", "USD", "монет")


def test_alts_pay_a_round_trip_of_0_40_percent_and_the_crypto_financing():
    for i in ins.ALT_COINS:
        c = i.costs
        assert c.round_trip_pct == 0.40
        assert c.financing_long_pct == 0.06 and c.financing_short_pct == 0.06


def test_bitcoin_and_ether_keep_their_round_one_costs():
    for symbol in ("BTC-USD", "ETH-USD"):
        c = ins.by_symbol(symbol).costs
        assert (c.round_trip_pct, c.financing_long_pct, c.financing_short_pct) == (0.250, 0.060, 0.060)


def test_the_round_one_universe_is_unchanged_by_round_two():
    assert len(ins.UNIVERSE) == 23
    assert not {i.symbol for i in ins.ALT_COINS} & {i.symbol for i in ins.UNIVERSE}
    assert ins.ROUND_TRIP_PCT["crypto"] == 0.250 and ins.ROUND_TRIP_PCT["fx_major"] == 0.015
    assert ins.FINANCING_PCT["crypto"] == (0.060, 0.060) and ins.FINANCING_PCT["indices"] == (0.020, 0.005)


def test_symbols_and_names_stay_unique_across_both_rounds():
    both = ins.UNIVERSE + ins.ALT_COINS
    assert len({i.symbol for i in both}) == len(both) == 34
    assert len({i.name for i in both}) == 34


def test_lookup_finds_the_alts_too():
    assert ins.by_symbol("HYPE32196-USD").name == "HYPEUSD"
    assert ins.by_name("SUIUSD").symbol == "SUI20947-USD"
    assert ins.by_symbol("SOL-USD") is ins.ALT_COINS[0]
    with pytest.raises(KeyError):
        ins.by_symbol("PEPE-USD")


# ------------------------------------------------------------------ H4: the six indices
def test_the_index_universe_is_the_six_indices_of_the_preregistration():
    assert [i.symbol for i in ins.INDEX_UNIVERSE] == [
        "^GSPC", "^NDX", "^DJI", "^GDAXI", "^FTSE", "^N225"]
    assert all(i.klass == ins.INDICES for i in ins.INDEX_UNIVERSE)


def test_h4_pays_the_indices_row_of_the_round_one_table():
    for i in ins.INDEX_UNIVERSE:
        c = i.costs
        assert (c.round_trip_pct, c.financing_long_pct, c.financing_short_pct) == (0.030, 0.020, 0.005)


# ------------------------------------------------------------------ H5: the eleven FX pairs
FX_PAIRS = {            # symbol -> (base, quote)
    "EURUSD=X": ("EUR", "USD"), "GBPUSD=X": ("GBP", "USD"), "USDJPY=X": ("USD", "JPY"),
    "AUDUSD=X": ("AUD", "USD"), "USDCAD=X": ("USD", "CAD"), "USDCHF=X": ("USD", "CHF"),
    "NZDUSD=X": ("NZD", "USD"), "EURGBP=X": ("EUR", "GBP"), "EURJPY=X": ("EUR", "JPY"),
    "GBPJPY=X": ("GBP", "JPY"), "EURCHF=X": ("EUR", "CHF"),
}


def test_the_fx_universe_is_the_eleven_pairs_of_round_one():
    assert {i.symbol for i in ins.FX_UNIVERSE} == set(FX_PAIRS)
    assert len(ins.FX_UNIVERSE) == 11
    assert all(i.gate_class == "FX" for i in ins.FX_UNIVERSE)


@pytest.mark.parametrize("symbol, pair", sorted(FX_PAIRS.items()))
def test_an_fx_pair_knows_its_base_and_quote_currency(symbol, pair):
    inst = ins.by_symbol(symbol)
    assert (inst.base, inst.quote) == pair


def test_only_fx_pairs_have_a_base_currency():
    for symbol in ("GC=F", "CL=F", "^GSPC", "BTC-USD", "SOL-USD"):
        with pytest.raises(ValueError):
            ins.by_symbol(symbol).base


@pytest.mark.parametrize("symbol, round_trip", [
    ("EURUSD=X", 0.015), ("USDJPY=X", 0.015), ("NZDUSD=X", 0.015),      # majors
    ("EURGBP=X", 0.030), ("GBPJPY=X", 0.030), ("EURCHF=X", 0.030),      # crosses
])
def test_h5_pays_the_round_one_fx_round_trip_and_its_own_financing(symbol, round_trip):
    c = ins.carry_fx_costs(ins.by_symbol(symbol))
    assert c.round_trip_pct == round_trip
    assert c.financing_long_pct == 0.004 and c.financing_short_pct == 0.004
    assert c.financing_pct("long") == c.financing_pct("short") == 0.004


def test_h5_financing_does_not_change_what_round_one_charges_fx():
    c = ins.by_symbol("EURUSD=X").costs
    assert (c.round_trip_pct, c.financing_long_pct, c.financing_short_pct) == (0.015, 0.010, 0.010)


def test_h5_costs_exist_only_for_fx_pairs():
    with pytest.raises(ValueError):
        ins.carry_fx_costs(ins.by_symbol("GC=F"))
    with pytest.raises(ValueError):
        ins.carry_fx_costs(ins.by_symbol("SOL-USD"))
