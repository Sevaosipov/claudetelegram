"""cfd/instruments.py: the 23-instrument universe of the CFD research and its cost table -- the
numbers of spec 2026-10-05-cfd-signals.md §1.1 and §1.4, written once so the backtest and the
live signals can't drift apart.

The classes of §1.1 (FX major, FX cross, Metals, Energy, Indices, Crypto) set the cost row of an
instrument; the gate of §1.6 judges fewer classes (FX majors and crosses together), which is
`gate_class`. An instrument carries two cost keys -- one into the round-trip table, one into the
financing table -- because gold and silver pay different round trips but the same financing, and
only indices charge by side.

Costs are percentages of price (0.015 means 0.015 %). Single-stock CFDs are out of scope (§1.1).

Round 2 (docs/cfd/PREREGISTRATION_R2.md) adds, without moving a round-1 value: the eleven alt coins
of H3 (`ALT_COINS`, a round trip of 0.40 % instead of bitcoin's 0.25 %, the same financing), the
index and FX universes of H4 and H5 (`INDEX_UNIVERSE`, `FX_UNIVERSE`, both taken from the round-1
table), the base currency of an FX pair, and H5's own financing of 0.004 % a night
(`carry_fx_costs`). The alts are not in `UNIVERSE`: round 1 iterates it, and its 23 instruments are
frozen.
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
    "crypto_alt": 0.400,        # round 2, H3: alts trade wider than bitcoin
}
# (long, short) per night held
FINANCING_PCT = {
    "fx_major": (0.010, 0.010),
    "fx_cross": (0.010, 0.010),
    "metals": (0.015, 0.015),
    "energy": (0.020, 0.020),
    "indices": (0.020, 0.005),
    "crypto": (0.060, 0.060),
    # round 2, H5: about 1.5 % a year, the broker's markup alone (the carry earned is not credited)
    "fx_carry": (0.004, 0.004),
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

    @property
    def base(self) -> str:
        """The base currency of an FX pair (EUR of EURUSD, USD of USDJPY); `quote` is the other.
        Only FX pairs have one: H5 (CARRY-FX) compares the interest rates of the two."""
        if self.gate_class != "FX":
            raise ValueError(f"{self.name} is not an FX pair: it has no base currency")
        return self.name[:3]


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

# ---------------------------------------------------------------- round 2 (PREREGISTRATION_R2.md)
def _alt(symbol: str, ticker: str) -> Instrument:
    return Instrument(symbol, f"{ticker}USD", CRYPTO, "crypto_alt", "crypto", "USD", UNIT_CRYPTO)


# H3 (CR-BO): the eleven coins the bot follows besides BTC and ETH, in the pre-registration's order
ALT_COINS: tuple[Instrument, ...] = (
    _alt("SOL-USD", "SOL"),
    _alt("XRP-USD", "XRP"),
    _alt("BNB-USD", "BNB"),
    _alt("DOGE-USD", "DOGE"),
    _alt("AVAX-USD", "AVAX"),
    _alt("LTC-USD", "LTC"),
    _alt("LINK-USD", "LINK"),
    _alt("TRX-USD", "TRX"),
    _alt("ENA-USD", "ENA"),
    _alt("HYPE32196-USD", "HYPE"),
    _alt("SUI20947-USD", "SUI"),
)
# H4 (IDX-DIP): the six indices; H5 (CARRY-FX): the eleven FX pairs (majors and crosses)
INDEX_UNIVERSE: tuple[Instrument, ...] = tuple(i for i in UNIVERSE if i.klass == INDICES)
FX_UNIVERSE: tuple[Instrument, ...] = tuple(i for i in UNIVERSE if i.gate_class == "FX")

_BY_SYMBOL = {i.symbol: i for i in UNIVERSE + ALT_COINS}
_BY_NAME = {i.name: i for i in UNIVERSE + ALT_COINS}


def by_symbol(symbol: str) -> Instrument:
    return _BY_SYMBOL[symbol]


def by_name(name: str) -> Instrument:
    return _BY_NAME[name]


def gold_intraday_costs() -> Costs:
    """PB-H1-GOLD pays the XAUUSD round trip and no financing: it is flat the same day (§1.4)."""
    return Costs(ROUND_TRIP_PCT["xauusd"], 0.0, 0.0)


def carry_fx_costs(inst: Instrument) -> Costs:
    """H5 (CARRY-FX): the pair's round-1 round trip (0.015 % a major, 0.030 % a cross) and its own
    financing of 0.004 % a night on either side. The trade holds the higher-yielding currency, so it
    in practice earns the rate difference less the broker's markup; charging the markup with no credit
    for the carry is the conservative reading (PREREGISTRATION_R2.md)."""
    if inst.gate_class != "FX":
        raise ValueError(f"{inst.name} is not an FX pair: H5 trades FX pairs only")
    long_, short = FINANCING_PCT["fx_carry"]
    return Costs(ROUND_TRIP_PCT[inst.cost_key], long_, short)
