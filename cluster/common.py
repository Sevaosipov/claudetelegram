"""Thresholds, scoring weights, and the helpers every finder shares."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import fx
import sec_edgar


SEC_WINDOW_DAYS = 14
SEC_MIN_BUYERS = 2

HOUSE_WINDOW_DAYS = 45  # STOCK Act PTRs are due 30-45 days after the trade -- how
                        # far back to scan for source rows at all, not how close
                        # together a cluster's members must have bought (see
                        # HOUSE_CLUSTER_SPAN_DAYS and find_house_clusters).
HOUSE_MIN_BUYERS = 2
HOUSE_CLUSTER_SPAN_DAYS = 14  # members must buy within ~2 weeks of EACH OTHER to
                              # read as coordinated, not just both land somewhere
                              # in the wider 45-day filing-lag scan above.

BAFIN_WINDOW_DAYS = 14  # same rationale as SEC -- corporate insiders, not politicians
BAFIN_MIN_BUYERS = 2

NORWAY_WINDOW_DAYS = 14  # same rationale as SEC/BaFin -- corporate insiders, not politicians
NORWAY_MIN_BUYERS = 2

SWEDEN_WINDOW_DAYS = 14  # same rationale again -- corporate insiders, not politicians
SWEDEN_MIN_BUYERS = 2

# The Senate files under the same STOCK Act deadline as the House, so it gets the
# same window rather than the corporate-insider one.
SENATE_WINDOW_DAYS = HOUSE_WINDOW_DAYS
SENATE_MIN_BUYERS = 2
SENATE_CLUSTER_SPAN_DAYS = HOUSE_CLUSTER_SPAN_DAYS

# Every threshold below, and every amount a signal displays, is in EUR -- source
# amounts are converted via fx.py before any comparison, so one constant means the
# same real bar for all four sources. (The raw purchase log keeps native currency.)
#
# Minimum combined value across all buyers in a cluster for it to be worth
# alerting on. SEC purchases report exact amounts; House PTRs only report a bracket
# (e.g. "$1,001 - $15,000"), so for House this is the sum of each buyer's *lower*
# bound -- a conservative, understating estimate rather than an exact figure.
MIN_CLUSTER_VALUE = 100_000

# A single person buying enough on their own also counts as a signal, even without
# a second distinct buyer -- we don't have shares-outstanding data to measure "% of
# the company", so this is a flat money bar for "clearly a large bet" instead.
SEC_SOLO_THRESHOLD = 500_000
HOUSE_SOLO_THRESHOLD = 500_000
BAFIN_SOLO_THRESHOLD = 500_000
NORWAY_SOLO_THRESHOLD = 500_000
SWEDEN_SOLO_THRESHOLD = 500_000

# Exit signal: fires when a meaningful fraction of the people who bought a ticker
# later sell it -- e.g. 5 people bought AAPL, 3 of those same 5 later sold it.
# A signal whose member set hasn't changed still re-fires once its total value has
# grown this many times over what was last alerted. Without this a solo whale can
# never re-fire at all (their buyer count is 1 and stays 1, however much more they
# buy), and a cluster that doubles its money without adding a name stays silent.
ALERT_VALUE_GROWTH = 1.5

# Schedule 13D/G stake signals (see sec_13dg.py). A holder crossing 5% must file, so
# STAKE_MIN_PERCENT below that would only catch positions being wound down.
STAKE_MIN_PERCENT = 5.0
# How much a stake must grow, in percentage points of the class, to be worth saying
# again. 13G/A amendments are filed constantly by index funds drifting a tenth of a
# point; those are noise, not news.
STAKE_MIN_INCREASE_PP = 1.0

# How long the database must already cover before "this person had never bought this
# ticker before" means anything. On a fresh database every purchase is a first
# purchase, which would make the feature fire on everything and mean nothing.
FIRST_BUY_MIN_HISTORY_DAYS = 180

# A Form 144 (notice of intent to sell) counts toward exit signals only if the
# intended sale is at least this large in USD -- routine vesting-and-selling is
# most of the volume. See sec_144.py.
FORM_144_MIN_VALUE = 100_000

EXIT_LOOKBACK_MONTHS = 12   # how far back to look for the original buyers
EXIT_MIN_BUYERS = 2         # need at least this many distinct buyers to track
EXIT_MIN_SELLERS = 2        # ...and at least this many of them selling later
EXIT_SELL_FRACTION = 0.5    # ...which must also be >= half the original buyers

# sec_edgar.clean_ticker nulls these at parse time now, but rows collected before
# that still carry them, and House/Norway tickers come from other parsers entirely
# -- so every ticker-keyed query filters them out in SQL as well.
_JUNK_TICKER_SQL = ("UPPER(TRIM(ticker)) NOT IN ("
                    + ",".join(f"'{t}'" for t in sec_edgar.JUNK_TICKERS) + ")")

# Suffixes that carry no identity, dropped before comparing names across forms.
_NAME_SUFFIXES = {"JR", "SR", "II", "III", "IV", "MD", "PHD", "ESQ"}
_NAME_PUNCT_RE = re.compile(r"[.,'\"()]")


def name_key(name: str) -> str:
    """Comparison key for a person's name across two SEC forms that write names
    differently. Form 4 files them surname-first and upper-cased ("CROOK NATHANIEL
    GLENN"); Form 144 files whatever the filer typed, often given-name first ("SEAN
    CHEN", "Bobak Azamian"). Matching those literally would almost never succeed, so
    the key is case-folded, stripped of punctuation and honorific suffixes, and
    token-sorted -- making "Chen Sean" and "SEAN CHEN" the same key.

    Single-letter tokens are deliberately KEPT. Dropping middle initials would let
    "Smith John A" and "Smith John B" -- two different people -- collapse into one
    key, and a false match here invents an exit signal claiming someone sold. The
    cost is missing a match when one form writes a middle initial and the other
    doesn't; a miss is much cheaper than a fabricated alert.

    Deliberately used only for cross-form matching, never for display or for the
    distinct-buyer count, where collapsing two different people would be worse than
    missing a match.
    """
    tokens = _NAME_PUNCT_RE.sub(" ", (name or "").upper()).split()
    tokens = [t for t in tokens if t not in _NAME_SUFFIXES]
    return " ".join(sorted(tokens))


_ASSET_SUFFIX_RE = re.compile(r"\s*\([A-Za-z.]{1,6}\)\s*\[\w+\]\s*$")
_AMOUNT_RE = re.compile(r"\$([\d,]+)")


def parse_amount_low(amount_range: str) -> float:
    """Lower bound of a House PTR amount range/value, e.g. '$1,001 - $15,000' -> 1001.0.
    Stays in the source's USD -- callers convert if they need EUR."""
    matches = _AMOUNT_RE.findall(amount_range)
    if not matches:
        return 0.0
    return float(matches[0].replace(",", ""))


def _bracket_to_eur(amount_range: str, conn=None) -> str:
    """Rewrite every dollar figure inside a House PTR bracket string as EUR,
    keeping the bracket shape: '$1,001 - $15,000' -> '€864 - €12,942'. House
    never discloses an exact amount, so the range is all there is to show."""
    def repl(m):
        usd = float(m.group(1).replace(",", ""))
        return f"€{fx.to_eur(usd, 'USD', conn):,.0f}"

    return _AMOUNT_RE.sub(repl, amount_range)


# Ranking weights. These decide the ORDER signals are presented in, nothing else --
# they are not a prediction of return, and no claim is made that a higher-scoring
# signal is a better trade. They encode the reasoning the tool was built on (several
# unrelated insiders converging is more notable than one; the people who run a
# company are better informed about it than a passive holder; a purchase is more
# notable relative to a small company than a large one), and they are a starting
# point to be checked against backtest.py rather than trusted.
# Every component is CAPPED. An early version left the market-cap term unbounded
# and a single nano-cap whose purchase came out at 226%% of its market cap scored
# 18,161 against a normal range of 20-60 -- one bad data point sorted the entire
# list. Ranking has to degrade gracefully when an input is wrong, because sooner or
# later an input is wrong.
W_PER_EXTRA_BUYER = 12.0     # beyond the first
CAP_BUYERS = 48.0
W_HAS_OFFICER = 10.0         # an officer or director, not only >10% holders
W_PCT_OF_MARKET_CAP = 8.0    # per 0.1%% of the company bought
CAP_MARKET_CAP = 40.0
W_POSITION_INCREASE = 6.0    # per 25%% added to the buyer's existing holding
CAP_POSITION = 24.0
W_FIRST_BUY = 8.0            # nobody involved had bought this name before
W_FRESH = 10.0               # full marks same-day, decaying to zero over FRESH_DECAY_DAYS
FRESH_DECAY_DAYS = 30.0
P_UNKNOWN_SIZE = -5.0        # size couldn't be resolved: less is known, not less is true
P_ILLIQUID = -12.0           # trades less than ILLIQUID_BELOW_EUR of value per day
ILLIQUID_BELOW_EUR = 250_000
W_FULL_UNWIND = 20.0         # every buyer in the cluster has now sold, not just some

# A single insider cannot buy more of a company than the company is worth. Past
# this, the market cap or the reported value is wrong -- usually a stale quote on a
# nano-cap after a split -- so the ratio is discarded rather than trusted, the same
# way sweden.py discards an implausible notional.
MAX_CREDIBLE_PCT_OF_MCAP = 100.0

# Corroboration: does another, independent disclosure regime also show activity
# on this ticker recently? Not a claim that sources agree -- an exit on one
# regime and a buy on another both count, see find_corroboration below and the
# README's "Ранжирование сигналов" section.
CORROBORATION_WINDOW_DAYS = 30
W_PER_CORROBORATING_SOURCE = 15.0   # per distinct other source active on this ticker
CAP_CORROBORATION = 30.0            # caps at 2 corroborating sources' worth

# SEC13DG (a 13D/G stake filing) and SEC (a Form 4 insider cluster) can both
# come from the same >10%-holder's single position change, so they don't count
# as independent corroboration of each other. HOUSE and SENATE are both
# STOCK Act PTR filings -- different chambers, same regulatory framework --
# so they're collapsed too, a weaker case (genuinely different filers) but
# the same regime for this purpose. Everything else maps to itself.
_REGIME = {"SEC13DG": "SEC", "SENATE": "HOUSE"}


def _regime(source: str) -> str:
    return _REGIME.get(source, source)


@dataclass
class ClusterSignal:
    source: str  # "SEC", "HOUSE", "BAFIN" or "NORWAY"
    ticker: str
    company: str
    buyer_count: int
    total_value: float | None
    members: list[str]  # already-formatted "Name (role) $amount" lines
    window_start: str
    window_end: str
    reason: str = "cluster"  # "cluster" (2+ distinct buyers) or "solo" (one big buyer)
    # Plain buyer names, parallel to `members` but unformatted. The alert-state
    # dedup compares these as a set, so they must not be re-parsed back out of the
    # display strings above (a name containing " (" would split wrong).
    member_names: list[str] = field(default_factory=list)
    # True when nobody in this cluster is an officer or director -- i.e. it's built
    # entirely from >10% holders. An institution adding to a stake is a different
    # thing from the people who run the company buying, and shouldn't read the same.
    holder_only: bool = False
    # Context attached later by enrich_signals(), which needs the network. Kept off
    # the finders so they stay pure SQL and the tests stay offline.
    position_increase_pct: float | None = None  # biggest buyer's buy vs what they already held
    first_buy: bool = False        # nobody here had bought this name before
    lag_days: float | None = None  # trade date -> disclosure date
    market_cap_eur: float | None = None
    value_pct_of_mcap: float | None = None
    avg_daily_value: float | None = None
    score: float = 0.0
    corroborated_by: list[str] = field(default_factory=list)
