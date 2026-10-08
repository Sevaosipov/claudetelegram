"""cfd/instruments.py, round 3: the eight FX pairs of H6 (FX-REV) with their own round trips and the
USD pairs H7 (CARRY-BASKET) reads -- every number is PREREGISTRATION_R3.md's. Rounds 1 and 2 must not
move, so the tests pin them too."""
from __future__ import annotations

import pytest

from cfd import instruments as ins

H6_SYMBOLS = ["EURCHF=X", "EURGBP=X", "AUDNZD=X", "AUDCAD=X", "NZDCAD=X", "USDCAD=X",
              "EURNOK=X", "EURSEK=X"]
H6_ROUND_TRIP = {"EURCHF=X": 0.030, "EURGBP=X": 0.030, "AUDNZD=X": 0.030, "AUDCAD=X": 0.030,
                 "NZDCAD=X": 0.030, "USDCAD=X": 0.015, "EURNOK=X": 0.060, "EURSEK=X": 0.060}


def test_the_h6_universe_is_the_eight_pairs_in_the_preregistered_order():
    assert [i.symbol for i in ins.H6_UNIVERSE] == H6_SYMBOLS
    assert [i.name for i in ins.H6_UNIVERSE] == [s.removesuffix("=X") for s in H6_SYMBOLS]


@pytest.mark.parametrize("symbol", H6_SYMBOLS)
def test_each_h6_pair_pays_its_own_round_trip_and_ten_thousandths_a_night(symbol):
    inst = next(i for i in ins.H6_UNIVERSE if i.symbol == symbol)
    c = inst.costs
    assert c.round_trip_pct == H6_ROUND_TRIP[symbol]
    assert c.financing_long_pct == 0.010 and c.financing_short_pct == 0.010


def test_h6_pairs_are_fx_pairs_with_their_base_and_quote_currencies():
    by = {i.name: i for i in ins.H6_UNIVERSE}
    for name, (base, quote) in {"EURCHF": ("EUR", "CHF"), "EURGBP": ("EUR", "GBP"),
                                "AUDNZD": ("AUD", "NZD"), "AUDCAD": ("AUD", "CAD"),
                                "NZDCAD": ("NZD", "CAD"), "USDCAD": ("USD", "CAD"),
                                "EURNOK": ("EUR", "NOK"), "EURSEK": ("EUR", "SEK")}.items():
        assert by[name].base == base and by[name].quote == quote
        assert by[name].gate_class == "FX" and by[name].unit == "ед."


def test_the_h6_pairs_that_round_one_already_has_are_the_same_instruments():
    for symbol in ("EURCHF=X", "EURGBP=X", "USDCAD=X"):
        assert ins.by_symbol(symbol) in ins.UNIVERSE
        assert next(i for i in ins.H6_UNIVERSE if i.symbol == symbol) is ins.by_symbol(symbol)


def test_lookup_finds_the_new_pairs_too():
    for symbol in H6_SYMBOLS:
        assert ins.by_symbol(symbol).symbol == symbol
        assert ins.by_name(symbol.removesuffix("=X")).symbol == symbol


def test_the_h7_usd_pairs_give_every_currency_but_usd_a_price_in_usd():
    assert ins.H7_CURRENCIES == ("USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD")
    assert ins.H7_PAIRS == {
        "EUR": ("EURUSD=X", False), "GBP": ("GBPUSD=X", False), "AUD": ("AUDUSD=X", False),
        "NZD": ("NZDUSD=X", False), "JPY": ("USDJPY=X", True), "CAD": ("USDCAD=X", True),
        "CHF": ("USDCHF=X", True)}
    assert [i.symbol for i in ins.H7_UNIVERSE] == [s for s, _ in ins.H7_PAIRS.values()]
    for ccy, (symbol, inverted) in ins.H7_PAIRS.items():
        inst = ins.by_symbol(symbol)
        assert inst.base == (ccy if not inverted else "USD")
        assert inst.quote == ("USD" if not inverted else ccy)


def test_rounds_one_and_two_do_not_move():
    assert len(ins.UNIVERSE) == 23 and len(ins.FX_UNIVERSE) == 11 and len(ins.ALT_COINS) == 11
    assert ins.ROUND_TRIP_PCT["fx_major"] == 0.015 and ins.ROUND_TRIP_PCT["fx_cross"] == 0.030
    assert ins.FINANCING_PCT["fx_major"] == (0.010, 0.010) and ins.FINANCING_PCT["fx_cross"] == (0.010, 0.010)
    assert not {i.symbol for i in ins.UNIVERSE} & {"AUDNZD=X", "AUDCAD=X", "NZDCAD=X", "EURNOK=X", "EURSEK=X"}
    assert "AUDNZD=X" not in {i.symbol for i in ins.FX_UNIVERSE}
