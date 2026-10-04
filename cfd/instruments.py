"""cfd/instruments.py: the 23-instrument universe of the CFD research and its cost table -- the
numbers of spec 2026-10-05-cfd-signals.md §1.1 and §1.4, written once so the backtest and the
live signals can't drift apart.

The classes of §1.1 (FX major, FX cross, Metals, Energy, Indices, Crypto) set the cost row of an
instrument; the gate of §1.6 judges fewer classes (FX majors and crosses together), which is
`gate_class`. An instrument carries two cost keys -- one into the round-trip table, one into the
financing table -- because gold and silver pay different round trips but the same financing, and
only indices charge by side.

Costs are percentages of price (0.015 means 0.015 %). Single-stock CFDs are out of scope (§1.1).
"""
from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------- classes (spec §1.1)
FX_MAJOR = "FX major"
FX_CROSS = "FX cross"
METALS = "Metals"
ENERGY = "Energy"
INDICES = "Indices"
CRYPTO = "Crypto"

# The classes the gate judges (§1.6): FX majors and crosses together.
GATE_CLASSES = ("FX", "Metals", "Energy", "Indices", "Crypto")
_GATE_CLASS = {FX_MAJOR: "FX", FX_CROSS: "FX", METALS: "Metals", ENERGY: "Energy",
               INDICES: "Indices", CRYPTO: "Crypto"}

# ---------------------------------------------------------------- costs (spec §1.4), in % of price
ROUND_TRIP_PCT = {
    "fx_major": 0.015,
    "fx_cross": 0.030,
    "xauusd": 0.030,
    "xagusd": 0.060,
    "energy": 0.060,
    "indices": 0.030,
    "crypto": 0.250,
}
# (long, short) per night held
FINANCING_PCT = {
    "fx_major": (0.010, 0.010),
    "fx_cross": (0.010, 0.010),
    "metals": (0.015, 0.015),
    "energy": (0.020, 0.020),
    "indices": (0.020, 0.005),
    "crypto": (0.060, 0.060),
}

# ---------------------------------------------------------------- unit labels (live messages)
UNIT_FX = "ед."
UNIT_METAL = "унц."
UNIT_ENERGY = "барр."
UNIT_INDEX = "контр."
UNIT_CRYPTO = "монет"

GOLD_SYMBOL = "GC=F"


@dataclass(frozen=True)
class Costs:
    """What a trade pays, in % of its entry price: one round trip, plus a financing charge for
    every night it is held (indices charge longs and shorts differently)."""
    round_trip_pct: float
    financing_long_pct: float
    financing_short_pct: float

    def financing_pct(self, side: str) -> float:
        if side == "long":
            return self.financing_long_pct
        if side == "short":
            return self.financing_short_pct
        raise ValueError(f"side must be 'long' or 'short', not {side!r}")


@dataclass(frozen=True)
class Instrument:
    symbol: str          # Yahoo symbol
    name: str            # the name shown to the user
    klass: str           # the class of spec §1.1
    cost_key: str        # row of ROUND_TRIP_PCT
    financing_key: str   # row of FINANCING_PCT
    quote: str           # the currency its price is quoted in
    unit: str            # the unit label of a quantity

    @property
    def gate_class(self) -> str:
        return _GATE_CLASS[self.klass]

    @property
    def costs(self) -> Costs:
        long_, short = FINANCING_PCT[self.financing_key]
        return Costs(ROUND_TRIP_PCT[self.cost_key], long_, short)


def _fx(symbol: str, klass: str, quote: str) -> Instrument:
    key = "fx_major" if klass == FX_MAJOR else "fx_cross"
    return Instrument(symbol, symbol.removesuffix("=X"), klass, key, key, quote, UNIT_FX)


UNIVERSE: tuple[Instrument, ...] = (
    # FX major
    _fx("EURUSD=X", FX_MAJOR, "USD"),
    _fx("GBPUSD=X", FX_MAJOR, "USD"),
    _fx("USDJPY=X", FX_MAJOR, "JPY"),
    _fx("AUDUSD=X", FX_MAJOR, "USD"),
    _fx("USDCAD=X", FX_MAJOR, "CAD"),
    _fx("USDCHF=X", FX_MAJOR, "CHF"),
    _fx("NZDUSD=X", FX_MAJOR, "USD"),
    # FX cross
    _fx("EURGBP=X", FX_CROSS, "GBP"),
    _fx("EURJPY=X", FX_CROSS, "JPY"),
    _fx("GBPJPY=X", FX_CROSS, "JPY"),
    _fx("EURCHF=X", FX_CROSS, "CHF"),
    # Metals
    Instrument("GC=F", "XAUUSD", METALS, "xauusd", "metals", "USD", UNIT_METAL),
    Instrument("SI=F", "XAGUSD", METALS, "xagusd", "metals", "USD", UNIT_METAL),
    # Energy
    Instrument("CL=F", "WTI", ENERGY, "energy", "energy", "USD", UNIT_ENERGY),
    Instrument("BZ=F", "BRENT", ENERGY, "energy", "energy", "USD", UNIT_ENERGY),
    # Indices
    Instrument("^GSPC", "US500", INDICES, "indices", "indices", "USD", UNIT_INDEX),
    Instrument("^NDX", "US100", INDICES, "indices", "indices", "USD", UNIT_INDEX),
    Instrument("^DJI", "US30", INDICES, "indices", "indices", "USD", UNIT_INDEX),
    Instrument("^GDAXI", "DE40", INDICES, "indices", "indices", "EUR", UNIT_INDEX),
    Instrument("^FTSE", "UK100", INDICES, "indices", "indices", "GBP", UNIT_INDEX),
    Instrument("^N225", "JP225", INDICES, "indices", "indices", "JPY", UNIT_INDEX),
    # Crypto
    Instrument("BTC-USD", "BTCUSD", CRYPTO, "crypto", "crypto", "USD", UNIT_CRYPTO),
    Instrument("ETH-USD", "ETHUSD", CRYPTO, "crypto", "crypto", "USD", UNIT_CRYPTO),
)

_BY_SYMBOL = {i.symbol: i for i in UNIVERSE}
_BY_NAME = {i.name: i for i in UNIVERSE}


def by_symbol(symbol: str) -> Instrument:
    return _BY_SYMBOL[symbol]


def by_name(name: str) -> Instrument:
    return _BY_NAME[name]


def gold_intraday_costs() -> Costs:
    """PB-H1-GOLD pays the XAUUSD round trip and no financing: it is flat the same day (§1.4)."""
    return Costs(ROUND_TRIP_PCT["xauusd"], 0.0, 0.0)
