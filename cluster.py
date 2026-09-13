"""Cluster-buying signal detection.

The raw purchase feed (universe-filtered SEC + House purchases) is stored in full
in SQLite/CSV for browsing from the terminal. Telegram, however, only gets
*signals*: a ticker where multiple distinct people (insiders, or separately,
members of Congress) bought within a rolling window -- the idea being that several
unrelated buyers converging on the same stock is a stronger, rarer, more legible
signal than any single purchase.

A signal only re-fires when the cluster *grows* past its previously-alerted size
(tracked in db.cluster_alert_state), so the same cluster isn't re-sent every day.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass, field

import datefmt
import db
import fx
import sec_edgar
import sweden

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


def _clean_asset_name(asset: str) -> str:
    return _ASSET_SUFFIX_RE.sub("", asset).strip()


def _sec_role(o: dict) -> str:
    """Display role for a Form 4 filer, most specific first. The 10%-owner case
    matters: an institution adding to a stake (Cascade buying $50M of RSG) is a
    materially different event from an officer or director buying, and used to
    render identically as a bare "Insider"."""
    if o["title"]:
        return o["title"]
    if o["is_director"]:
        return "Director"
    if o["is_officer"]:
        return "Officer"
    if o["is_ten_pct"]:
        return "10%+ Owner"
    return "Insider"


def find_sec_clusters(conn, window_days: int = SEC_WINDOW_DAYS, min_buyers: int = SEC_MIN_BUYERS,
                       min_value: float = MIN_CLUSTER_VALUE, solo_threshold: float = SEC_SOLO_THRESHOLD,
                       ignore_alert_state: bool = False, include_derivatives: bool = False,
                       insiders_only: bool = False, include_10b5_1: bool = False) -> list[ClusterSignal]:
    """`include_derivatives` folds derivative-table code-P rows (warrants, options,
    convertibles) into the money totals. Off by default: those are priced at a
    strike, not at what the stock costs, so summing them alongside common stock
    inflates or deflates a cluster's headline value for no gain in meaning.

    `insiders_only` drops filers who are neither officers nor directors, i.e. keeps
    the people who run the company and drops passive >10% holders.

    `include_10b5_1` keeps purchases made under a Rule 10b5-1 plan. Off by default:
    those are arranged months in advance on a fixed schedule, so they say nothing
    about what the insider thinks now -- which is the only thing this tool is
    looking for. Form 4 has always carried the flag; it was simply never read.
    """
    since = (dt.date.today() - dt.timedelta(days=window_days)).isoformat()
    where = ["transaction_date >= ?", "ticker IS NOT NULL", "ticker != ''", _JUNK_TICKER_SQL]
    if not include_derivatives:
        where.append("derivative = 0")
    if insiders_only:
        where.append("(is_officer = 1 OR is_director = 1)")
    if not include_10b5_1:
        where.append("COALESCE(is_10b5_1, 0) = 0")
    rows = conn.execute(
        """SELECT ticker, issuer_name, owner_name, officer_title, is_director,
                  transaction_date, value, is_officer, is_ten_pct_owner,
                  shares, shares_owned_after, filed_date
           FROM sec_purchases WHERE """ + " AND ".join(where),
        (since,),
    ).fetchall()

    by_ticker: dict[str, list] = {}
    for r in rows:
        by_ticker.setdefault(r[0], []).append(r)

    signals = []
    for ticker, group in by_ticker.items():
        # Aggregate ALL of each owner's purchases in the window (not just their
        # latest one) -- a single insider buying repeatedly should have those
        # summed, both for the solo-whale check and for the displayed total.
        by_owner: dict[str, dict] = {}
        for (_, issuer_name, owner_name, title, is_director, txn_date, value,
             is_officer, is_ten_pct, shares, owned_after, filed_date) in group:
            slot = by_owner.setdefault(owner_name, {
                "issuer_name": issuer_name, "title": title, "is_director": is_director,
                "is_officer": is_officer, "is_ten_pct": is_ten_pct, "total": 0.0,
                "shares": 0.0, "owned_after": None, "lags": [],
            })
            slot["total"] += value or 0
            slot["shares"] += shares or 0
            # The largest reported post-transaction holding is the most reliable
            # anchor for "what did they hold before this".
            if owned_after and (slot["owned_after"] is None or owned_after > slot["owned_after"]):
                slot["owned_after"] = owned_after
            lag = _lag_days(txn_date, filed_date)
            if lag is not None:
                slot["lags"].append(lag)
            if title:
                slot["title"] = title
            # Relationship flags are per-filing; keep the union across the window.
            slot["is_director"] = slot["is_director"] or is_director
            slot["is_officer"] = slot["is_officer"] or is_officer
            slot["is_ten_pct"] = slot["is_ten_pct"] or is_ten_pct

        # SEC reports USD; signals are shown (and thresholded) in EUR. Convert
        # once per person after aggregating, not per row -- same result, far
        # fewer rate lookups.
        for o in by_owner.values():
            o["total"] = fx.to_eur(o["total"], "USD", conn)

        total_value = sum(o["total"] for o in by_owner.values())
        biggest_owner = max(by_owner.values(), key=lambda o: o["total"])
        is_cluster = len(by_owner) >= min_buyers
        is_solo_whale = biggest_owner["total"] >= solo_threshold
        if not is_cluster and not is_solo_whale:
            continue
        if total_value < min_value:
            continue
        member_names = list(by_owner)
        if not ignore_alert_state and not should_alert(conn, "SEC", ticker, member_names, total_value):
            continue

        company = next(iter(by_owner.values()))["issuer_name"]
        members = [f"{name} ({_sec_role(o)}) €{o['total']:,.0f}" for name, o in by_owner.items()]

        dates = [r[5] for r in group]
        signals.append(ClusterSignal(
            source="SEC", ticker=ticker, company=company, buyer_count=len(by_owner),
            total_value=total_value or None, members=members,
            window_start=min(dates), window_end=max(dates),
            reason="cluster" if is_cluster else "solo",
            member_names=member_names,
            holder_only=not any(o["is_officer"] or o["is_director"] for o in by_owner.values()),
            position_increase_pct=max(
                (_position_increase(o) for o in by_owner.values()),
                key=lambda v: (v is not None, v or 0), default=None),
            first_buy=_is_first_buy(conn, ticker, member_names, since),
            lag_days=_median([l for o in by_owner.values() for l in o["lags"]]),
        ))
    return signals


def _lag_days(txn_date: str, filed_date: str | None) -> float | None:
    """Days between the trade and its disclosure.

    This is the difference between sources that the Telegram message never showed:
    a Form 4 lands about two days after the trade, while a House PTR averages two
    weeks and is allowed forty-five. Both used to render identically.
    """
    if not txn_date or not filed_date:
        return None
    try:
        return (dt.date.fromisoformat(filed_date) - dt.date.fromisoformat(txn_date)).days
    except ValueError:
        return None


def _median(values: list[float]) -> float | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


def _position_increase(owner: dict) -> float | None:
    """The purchase as a percentage of what the buyer already held.

    An insider adding half a percent to an existing stake and one doubling their
    holding were previously the same event. A buyer who held nothing before is
    reported as 100%: a brand-new position, not an infinite increase.
    """
    bought, after = owner.get("shares") or 0, owner.get("owned_after")
    if not bought or after is None:
        return None
    before = after - bought
    if before <= 0:
        return 100.0
    return min(bought / before * 100, 1000.0)


def _is_first_buy(conn, ticker: str, member_names: list[str], since: str) -> bool:
    """True when nobody in this cluster had bought this ticker before the window.

    Only meaningful once the database has some depth -- on a fresh one every
    purchase is a first purchase, so this returns False until at least
    FIRST_BUY_MIN_HISTORY_DAYS of history exist rather than firing on everything.
    """
    earliest = conn.execute(
        "SELECT min(transaction_date) FROM sec_purchases WHERE transaction_date != ''"
    ).fetchone()[0]
    if not earliest:
        return False
    try:
        covered = (dt.date.fromisoformat(since) - dt.date.fromisoformat(earliest)).days
    except ValueError:
        return False
    if covered < FIRST_BUY_MIN_HISTORY_DAYS:
        return False
    placeholders = ",".join("?" * len(member_names))
    prior = conn.execute(
        f"""SELECT count(*) FROM sec_purchases
            WHERE ticker = ? AND transaction_date < ? AND owner_name IN ({placeholders})""",
        (ticker, since, *member_names),
    ).fetchone()[0]
    return prior == 0


def _tight_purchase_window(dated_members: list[tuple], span_days: int,
                            min_buyers: int):
    """Does some sub-window of width <= span_days contain purchases from at least
    min_buyers distinct members? `dated_members` is a list of (date, member_name)
    pairs, one per purchase -- need not be sorted or deduplicated; a member with
    several purchases in the scan contributes one pair per purchase, which is fine
    since only the *set* of names in a window is what gets counted.

    Returns (window_start, window_end, member_names) for the earliest such window
    the scan finds, or None if no window of that width ever reaches min_buyers
    distinct members. Two members buying 40 days apart inside a wider lookback
    scan must not qualify -- see find_house_clusters.
    """
    items = sorted(dated_members, key=lambda dm: dm[0])
    span = dt.timedelta(days=span_days)
    left = 0
    for right in range(len(items)):
        while items[right][0] - items[left][0] > span:
            left += 1
        names = {name for _, name in items[left:right + 1]}
        if len(names) >= min_buyers:
            return items[left][0], items[right][0], names
    return None


def find_house_clusters(conn, window_days: int = HOUSE_WINDOW_DAYS, min_buyers: int = HOUSE_MIN_BUYERS,
                         min_value: float = MIN_CLUSTER_VALUE, solo_threshold: float = HOUSE_SOLO_THRESHOLD,
                         cluster_span_days: int = HOUSE_CLUSTER_SPAN_DAYS,
                         ignore_alert_state: bool = False, table: str = "house_purchases",
                         source: str = "HOUSE", date_format: str | None = "%m/%d/%Y") -> list[ClusterSignal]:
    """Also serves the Senate, via find_senate_clusters. Both chambers disclose under
    the same STOCK Act rules in the same amount brackets, so the only differences are
    the table, the alert-state source label, and the stored date format -- the Senate
    table keeps ISO dates (date_format=None) rather than the House's M/D/YYYY.

    `window_days` (wide, 45 days) is how far back to scan for source rows at all --
    a PTR can legally be filed up to 45 days after the trade, so a trade from a
    month ago can be the first time this scan ever sees it. It is not how close
    together a cluster's members must have bought: that is `cluster_span_days`
    (14 by default) -- two members whose purchases both merely land somewhere in
    the wide scan, weeks apart, do not read as coordinated buying.
    """
    cutoff = dt.date.today() - dt.timedelta(days=window_days)
    rows = conn.execute(
        # These tables hold every PTR txn type (P/S/S (partial)/E) -- a *buy* cluster
        # must only count txn_type == 'P', otherwise sellers get miscounted as buyers.
        f"SELECT ticker, asset, member_name, txn_date, amount_range FROM {table} "
        "WHERE ticker IS NOT NULL AND ticker != '' AND txn_type = 'P' AND " + _JUNK_TICKER_SQL
    ).fetchall()

    by_ticker: dict[str, list] = {}
    for ticker, asset, member_name, txn_date, amount_range in rows:
        try:
            # Parsed once, here -- min()/max() on raw "M/D/YYYY" strings sorts
            # lexicographically, not chronologically ("01/15/2026" < "12/20/2025").
            d = (dt.datetime.strptime(txn_date, date_format).date() if date_format
                 else dt.date.fromisoformat(txn_date))
        except ValueError:
            continue
        if d < cutoff:
            continue
        by_ticker.setdefault(ticker, []).append((d, asset, member_name, amount_range))

    signals = []
    for ticker, group in by_ticker.items():
        # Whole-scan totals, for the solo-whale check only: one member's total
        # spend across the full window_days lookback, regardless of clustering.
        by_member_all: dict[str, dict] = {}
        for d, asset, member_name, amount_range in group:
            slot = by_member_all.setdefault(member_name, {"asset": asset, "total": 0.0})
            slot["total"] += parse_amount_low(amount_range)
        for m in by_member_all.values():
            m["total"] = fx.to_eur(m["total"], "USD", conn)
        biggest_member = max(by_member_all.values(), key=lambda m: m["total"])
        is_solo_whale = biggest_member["total"] >= solo_threshold

        window = _tight_purchase_window(
            [(d, member_name) for d, _asset, member_name, _amt in group],
            cluster_span_days, min_buyers)
        is_cluster = window is not None

        if not is_cluster and not is_solo_whale:
            continue

        if is_cluster:
            win_start, win_end, in_window_members = window
            rows_in_window = [row for row in group
                              if win_start <= row[0] <= win_end and row[2] in in_window_members]
        else:
            # Solo whale, no tight cluster: report just that one member's own
            # purchases across the full window_days lookback -- not the whole
            # ticker group, which can hold other members' unrelated small
            # purchases that merely happened to land in the same wide scan.
            whale_name = next(name for name, m in by_member_all.items() if m is biggest_member)
            rows_in_window = [row for row in group if row[2] == whale_name]

        by_member: dict[str, dict] = {}
        for d, asset, member_name, amount_range in rows_in_window:
            slot = by_member.setdefault(member_name, {"asset": asset, "total": 0.0})
            slot["total"] += parse_amount_low(amount_range)
        # Brackets are USD; signals are shown (and thresholded) in EUR.
        for m in by_member.values():
            m["total"] = fx.to_eur(m["total"], "USD", conn)

        total_value = sum(m["total"] for m in by_member.values())
        if total_value < min_value:
            continue
        member_names = list(by_member)
        if not ignore_alert_state and not should_alert(conn, source, ticker, member_names, total_value):
            continue

        company = _clean_asset_name(next(iter(by_member.values()))["asset"])
        members = [f"{name} (от €{m['total']:,.0f})" for name, m in by_member.items()]
        dates_in_window = [row[0] for row in rows_in_window]

        signals.append(ClusterSignal(
            source=source, ticker=ticker, company=company, buyer_count=len(by_member),
            total_value=total_value, members=members,
            window_start=min(dates_in_window), window_end=max(dates_in_window),
            reason="cluster" if is_cluster else "solo",
            member_names=member_names,
        ))
    return signals


def find_senate_clusters(conn, **kwargs) -> list[ClusterSignal]:
    """Senate PTR buy clusters. Same logic as the House -- see find_house_clusters."""
    kwargs.setdefault("window_days", SENATE_WINDOW_DAYS)
    kwargs.setdefault("min_buyers", SENATE_MIN_BUYERS)
    kwargs.setdefault("cluster_span_days", SENATE_CLUSTER_SPAN_DAYS)
    return find_house_clusters(conn, table="senate_purchases", source="SENATE",
                                date_format=None, **kwargs)


def find_bafin_clusters(conn, window_days: int = BAFIN_WINDOW_DAYS, min_buyers: int = BAFIN_MIN_BUYERS,
                         min_value: float = MIN_CLUSTER_VALUE, solo_threshold: float = BAFIN_SOLO_THRESHOLD,
                         ignore_alert_state: bool = False) -> list[ClusterSignal]:
    cutoff = dt.date.today() - dt.timedelta(days=window_days)
    rows = conn.execute(
        "SELECT isin, issuer_name, notifier_name, position, txn_date, volume_eur FROM bafin_purchases "
        "WHERE txn_type = 'P' AND isin IS NOT NULL AND isin != ''"
    ).fetchall()

    by_isin: dict[str, list] = {}
    for isin, issuer_name, notifier_name, position, txn_date, volume in rows:
        try:
            d = dt.datetime.strptime(txn_date, "%d.%m.%Y").date()
        except ValueError:
            continue
        if d < cutoff:
            continue
        by_isin.setdefault(isin, []).append((issuer_name, notifier_name, position, d, volume))

    signals = []
    for isin, group in by_isin.items():
        # Aggregate ALL of each notifier's purchases in the window -- same
        # rationale as the SEC/House solo-whale aggregation above.
        by_notifier: dict[str, dict] = {}
        for issuer_name, notifier_name, position, d, volume in group:
            slot = by_notifier.setdefault(notifier_name, {"issuer_name": issuer_name, "position": position, "total": 0.0})
            slot["total"] += volume or 0
            if position:
                slot["position"] = position

        total_value = sum(o["total"] for o in by_notifier.values())
        biggest = max(by_notifier.values(), key=lambda o: o["total"])
        is_cluster = len(by_notifier) >= min_buyers
        is_solo_whale = biggest["total"] >= solo_threshold
        if not is_cluster and not is_solo_whale:
            continue
        if total_value < min_value:
            continue
        member_names = list(by_notifier)
        if not ignore_alert_state and not should_alert(conn, "BAFIN", isin, member_names, total_value):
            continue

        company = next(iter(by_notifier.values()))["issuer_name"]
        members = []
        for name, o in by_notifier.items():
            role = o["position"] or "Insider"
            members.append(f"{name} ({role}) €{o['total']:,.0f}")

        dates = [item[3] for item in group]
        signals.append(ClusterSignal(
            source="BAFIN", ticker=isin, company=company, buyer_count=len(by_notifier),
            total_value=total_value or None, members=members,
            window_start=min(dates), window_end=max(dates),
            reason="cluster" if is_cluster else "solo",
            member_names=member_names,
        ))
    return signals


def find_norway_clusters(conn, window_days: int = NORWAY_WINDOW_DAYS, min_buyers: int = NORWAY_MIN_BUYERS,
                          min_value: float = MIN_CLUSTER_VALUE, solo_threshold: float = NORWAY_SOLO_THRESHOLD,
                          ignore_alert_state: bool = False) -> list[ClusterSignal]:
    """Unlike SEC/House/BaFin (one fixed currency each), Norway's disclosures are
    mostly NOK but occasionally SEK/EUR/USD/DKK for cross-listed issuers -- so
    conversion has to happen per row, on that row's own currency, before anything
    is summed."""
    since = (dt.date.today() - dt.timedelta(days=window_days)).isoformat()
    rows = conn.execute(
        """SELECT ticker, issuer_name, person, txn_date, value, currency
           FROM norway_purchases
           WHERE txn_type = 'P' AND txn_date >= ? AND ticker IS NOT NULL AND ticker != ''
             AND """ + _JUNK_TICKER_SQL,
        (since,),
    ).fetchall()

    by_ticker: dict[str, list] = {}
    for r in rows:
        by_ticker.setdefault(r[0], []).append(r)

    signals = []
    for ticker, group in by_ticker.items():
        by_person: dict[str, dict] = {}
        for _, issuer_name, person, txn_date, value, currency in group:
            slot = by_person.setdefault(person, {"issuer_name": issuer_name, "total": 0.0})
            slot["total"] += fx.to_eur(value, currency, conn)

        total_value = sum(o["total"] for o in by_person.values())
        biggest = max(by_person.values(), key=lambda o: o["total"])
        is_cluster = len(by_person) >= min_buyers
        is_solo_whale = biggest["total"] >= solo_threshold
        if not is_cluster and not is_solo_whale:
            continue
        if total_value < min_value:
            continue
        member_names = list(by_person)
        if not ignore_alert_state and not should_alert(conn, "NORWAY", ticker, member_names, total_value):
            continue

        company = next(iter(by_person.values()))["issuer_name"]
        members = [f"{name} €{o['total']:,.0f}" for name, o in by_person.items()]

        dates = [r[3] for r in group]
        signals.append(ClusterSignal(
            source="NORWAY", ticker=ticker, company=company, buyer_count=len(by_person),
            total_value=total_value or None, members=members,
            window_start=min(dates), window_end=max(dates),
            reason="cluster" if is_cluster else "solo",
            member_names=member_names,
        ))
    return signals


@dataclass
class StakeSignal:
    """A 5%+ beneficial owner declaring or increasing a position (Schedule 13D/G).

    Unlike a ClusterSignal this is one filer, and the headline number is a share of
    the company rather than an amount of money -- which is the point: €500k means
    something entirely different in a €50m company than in a €500bn one, and until
    these filings were added nothing here could tell the difference.
    """
    source: str          # always "SEC13DG"
    ticker: str
    company: str
    person: str
    form_type: str
    percent: float
    prev_percent: float | None
    amount_owned: float | None
    event_date: str
    url: str
    corroborated_by: list[str] = field(default_factory=list)

    @property
    def is_activist(self) -> bool:
        return self.form_type.startswith("SCHEDULE 13D")


def find_stake_signals(conn, min_percent: float = STAKE_MIN_PERCENT,
                        min_increase_pp: float = STAKE_MIN_INCREASE_PP,
                        activist_only: bool = False,
                        ignore_alert_state: bool = False) -> list[StakeSignal]:
    """Newly declared or materially increased 5%+ stakes.

    What counts as news:
      - a first 13D/13G for a holder in an issuer (a stake that wasn't there before);
      - an amendment showing the stake up by at least `min_increase_pp` points.
    What doesn't: the constant drizzle of 13G/A amendments where an index fund's
    holding moved a tenth of a point, and anything below the 5% filing trigger,
    which can only be a position being wound down.

    Alert state is keyed per (issuer, holder) rather than per issuer -- two
    unrelated funds building stakes in the same company are two separate pieces of
    news, and one must not mask the other.
    """
    rows = conn.execute(
        """SELECT ticker, issuer_cik, issuer_name, person_name, form_type, event_date,
                  percent_of_class, amount_owned, source_url, accession, found_at
           FROM sec_stakes
           WHERE percent_of_class IS NOT NULL
           ORDER BY event_date, found_at""",
    ).fetchall()

    # (issuer, holder) -> filings in chronological order, so "did it grow" compares
    # against that holder's own previous filing rather than anyone else's.
    history: dict[tuple, list] = {}
    for r in rows:
        ticker, issuer_cik, issuer_name, person, form_type, event_date, pct, amount, url, acc, found = r
        history.setdefault((ticker or issuer_cik, person), []).append(r)

    signals = []
    for (key, person), filings in history.items():
        latest = filings[-1]
        (ticker, issuer_cik, issuer_name, person_name, form_type, event_date,
         pct, amount, url, acc, found) = latest
        if pct is None or pct < min_percent:
            continue
        if activist_only and not form_type.startswith("SCHEDULE 13D"):
            continue

        prev_pct = filings[-2][6] if len(filings) > 1 else None
        if prev_pct is not None and pct - prev_pct < min_increase_pp:
            continue

        state_key = f"{key}|{person}"
        if not ignore_alert_state:
            prev = db.get_alert_state(conn, "SEC13DG", state_key)
            if prev is not None:
                last_pct = prev["total_value"]
                if last_pct is not None and pct - last_pct < min_increase_pp:
                    continue

        signals.append(StakeSignal(
            source="SEC13DG", ticker=ticker or issuer_cik, company=issuer_name,
            person=person_name, form_type=form_type, percent=pct, prev_percent=prev_pct,
            amount_owned=amount, event_date=event_date, url=url,
        ))
    return signals


def commit_stake_alert(conn, signal: StakeSignal) -> None:
    db.save_cluster_alert_state(conn, "SEC13DG", f"{signal.ticker}|{signal.person}",
                                 1, [signal.person], signal.percent)


def score_signal(sig, corroborated_by: list[str] | None = None) -> float:
    """A number for ORDERING signals by how much attention they deserve.

    This is emphatically not a prediction of return, and a higher score is not a
    claim that something is a better trade. It exists because the last run emitted
    252 signals as a flat list, which is the same as emitting none: whatever was
    most notable was buried among things that were not.

    The components restate the reasoning the tool was built on -- several unrelated
    insiders converging beats one; the people who run a company know more about it
    than a passive holder does; a given sum means more against a small company than
    a large one; a stale disclosure is worth less than a fresh one; an independent
    disclosure regime noticing the same ticker is more than that regime noticing it
    twice (see find_corroboration) -- and the weights are a starting point, not a
    finding. backtest.py exists to check them.
    """
    if hasattr(sig, "percent"):   # StakeSignal
        # For a 13D/G the share of the company IS the headline, so it carries the
        # score directly rather than being one input among several.
        score = min(sig.percent, 50.0) * 2.0
        if sig.is_activist:
            score += 25.0   # 13D means the holder may seek to influence control
        if sig.prev_percent is not None:
            score += min(max(sig.percent - sig.prev_percent, 0.0), 20.0) * 2.0
    elif hasattr(sig, "seller_count"):   # ExitSignal
        # Exit signals carry none of the buy-side context (officer status, % of
        # market cap, freshness) the general path below scores on -- they are a
        # different kind of event. Score on what they do carry: how many sellers,
        # and whether every buyer in the cluster has now sold.
        score = min(W_PER_EXTRA_BUYER * max(0, sig.seller_count - 1), CAP_BUYERS)
        if sig.total_buyers and sig.seller_count >= sig.total_buyers:
            score += W_FULL_UNWIND
    else:
        score = 0.0
        score += min(W_PER_EXTRA_BUYER * max(0, (sig.buyer_count or 1) - 1), CAP_BUYERS)
        if not getattr(sig, "holder_only", False):
            score += W_HAS_OFFICER
        pct_mcap = sig.value_pct_of_mcap
        if pct_mcap and pct_mcap <= MAX_CREDIBLE_PCT_OF_MCAP:
            score += min(W_PCT_OF_MARKET_CAP * (pct_mcap / 0.1), CAP_MARKET_CAP)
        if sig.position_increase_pct:
            score += min(W_POSITION_INCREASE * (sig.position_increase_pct / 25.0), CAP_POSITION)
        if getattr(sig, "first_buy", False):
            score += W_FIRST_BUY
        if sig.lag_days is not None:
            score += W_FRESH * max(0.0, 1.0 - sig.lag_days / FRESH_DECAY_DAYS)
        if sig.market_cap_eur is None:
            score += P_UNKNOWN_SIZE
        if sig.avg_daily_value is not None and sig.avg_daily_value < ILLIQUID_BELOW_EUR:
            score += P_ILLIQUID

    if corroborated_by:
        score += min(W_PER_CORROBORATING_SOURCE * len(corroborated_by), CAP_CORROBORATION)
    return round(score, 1)


def find_corroboration(conn, signals: list, window_days: int = CORROBORATION_WINDOW_DAYS) -> None:
    """Which OTHER disclosure-source regimes also show activity on each
    signal's ticker within `window_days` -- this run's own batch, plus
    signal_journal history. Sets `.corroborated_by` on every signal in place
    to the sorted list of those other sources ([] when there are none).

    Not a claim the sources agree: any signal kind counts on either side, so a
    buy-side cluster and an exit signal on the same ticker corroborate each
    other just as two buy-side clusters would -- "another independent regime
    had activity here", nothing about direction. Pure SQL + set logic, no
    network, so it runs before the market-cap/liquidity loop in
    enrich_signals() that does need the network.
    """
    tickers = sorted({sig.ticker for sig in signals})
    if not tickers:
        return

    batch_sources: dict[str, set[str]] = {t: set() for t in tickers}
    for sig in signals:
        batch_sources[sig.ticker].add(sig.source)

    since = (dt.date.today() - dt.timedelta(days=window_days)).isoformat()
    placeholders = ",".join("?" * len(tickers))
    rows = conn.execute(
        f"SELECT DISTINCT ticker, source FROM signal_journal "
        f"WHERE ticker IN ({placeholders}) AND emitted_at >= ?",
        (*tickers, since),
    ).fetchall()
    journal_sources: dict[str, set[str]] = {t: set() for t in tickers}
    for ticker, source in rows:
        journal_sources[ticker].add(source)

    for sig in signals:
        others = (batch_sources[sig.ticker] | journal_sources[sig.ticker]) - {sig.source}
        sig.corroborated_by = sorted(others)


def enrich_signals(conn, signals: list) -> list:
    """Attach company context to signals and score them, newest-first by score.

    Split out from the finders on purpose: this is the only part that needs the
    network (market cap and traded volume), so the finders stay pure SQL and the
    test suite stays offline and fast.
    """
    import marketcap

    find_corroboration(conn, signals)

    for sig in signals:
        cap = marketcap.market_cap_eur(conn, sig.ticker)
        facts = marketcap.facts(conn, sig.ticker) or {}
        sig.market_cap_eur = cap
        sig.avg_daily_value = (fx.to_eur(facts["avg_daily_value"], facts.get("currency") or "USD", conn)
                                if facts.get("avg_daily_value") else None)
        total = getattr(sig, "total_value", None)
        pct = (total / cap * 100) if (cap and total) else None
        sig.value_pct_of_mcap = pct
        sig.score = score_signal(sig, sig.corroborated_by)
    signals.sort(key=lambda x: getattr(x, "score", 0.0), reverse=True)
    return signals


def should_alert(conn, source: str, ticker: str, member_names, total_value: float | None) -> bool:
    """Whether this signal is new enough to send, given what was last alerted for
    this (source, ticker).

    Headcount alone is not enough. The cluster window is rolling, so old buyers age
    out of it: once a ticker has been alerted at 4 buyers, a fresh cluster of 3
    *different* insiders months later would be suppressed forever under a
    count-only rule. So a signal re-fires when it contains a name that wasn't in the
    last alert, or when its total value has grown by ALERT_VALUE_GROWTH -- the
    latter being the only thing that can ever re-fire a solo whale, whose count is
    permanently 1.
    """
    prev = db.get_alert_state(conn, source, ticker)
    if prev is None:
        return True
    if set(member_names) - prev["members"]:
        return True
    if prev["total_value"] and total_value and total_value >= prev["total_value"] * ALERT_VALUE_GROWTH:
        return True
    # Pre-existing rows written before last_total_value was recorded have no value to
    # compare against; fall back to the old count rule so they aren't stuck forever.
    if prev["total_value"] is None and len(member_names) > prev["count"]:
        return True
    return False


def should_alert_exit(conn, source: str, ticker: str, seller_names) -> bool:
    """Same idea as should_alert, for exit signals: re-fire when someone who hadn't
    sold before now has. Value growth is not a useful trigger here -- the event is a
    person crossing from holding to selling, not the size of the sale."""
    prev = db.get_alert_state(conn, f"{source}_EXIT", ticker)
    if prev is None:
        return True
    return bool(set(seller_names) - prev["members"])


def find_sweden_clusters(conn, window_days: int = SWEDEN_WINDOW_DAYS, min_buyers: int = SWEDEN_MIN_BUYERS,
                          min_value: float = MIN_CLUSTER_VALUE, solo_threshold: float = SWEDEN_SOLO_THRESHOLD,
                          ignore_alert_state: bool = False, include_share_programs: bool = False,
                          include_derivatives: bool = False) -> list[ClusterSignal]:
    """Keyed on ISIN rather than a ticker, like BaFin -- Sweden discloses no ticker.
    Currency is converted per row like Norway's, since Swedish filings are mostly
    SEK but cross-listed issuers report in CAD/CHF/EUR.

    Two filters no other source can offer, both on by default:
      - share-programme transactions are excluded. Shares handed over by a comp plan
        are not a decision to buy, and FI is the only register here that says which
        is which.
      - non-share instruments (options, warrants, bonds) are excluded, for the same
        reason SEC derivative rows are: they're priced off a strike or a face value,
        not off what the share costs. The largest Swedish "purchase" in a sample week
        was a bond issue.
    Corrected/withdrawn filings ("Reviderad") never count -- only the live version.
    """
    since = (dt.date.today() - dt.timedelta(days=window_days)).isoformat()
    where = ["txn_type = 'P'", "txn_date >= ?", "isin IS NOT NULL", "isin != ''",
             "status = 'Aktuell'"]
    if not include_share_programs:
        where.append("share_program = 0")
    if not include_derivatives:
        where.append("instrument_type IN (" + ",".join("?" * len(sweden.SHARE_INSTRUMENTS)) + ")")
    params = [since] + ([] if include_derivatives else sorted(sweden.SHARE_INSTRUMENTS))
    rows = conn.execute(
        """SELECT isin, issuer_name, person, position, txn_date, value, currency
           FROM sweden_purchases WHERE """ + " AND ".join(where),
        params,
    ).fetchall()

    by_isin: dict[str, list] = {}
    for r in rows:
        by_isin.setdefault(r[0], []).append(r)

    signals = []
    for isin, group in by_isin.items():
        by_person: dict[str, dict] = {}
        for _, issuer_name, person, position, txn_date, value, currency in group:
            slot = by_person.setdefault(person, {"issuer_name": issuer_name, "position": position, "total": 0.0})
            slot["total"] += fx.to_eur(value, currency, conn)
            if position:
                slot["position"] = position

        total_value = sum(o["total"] for o in by_person.values())
        biggest = max(by_person.values(), key=lambda o: o["total"])
        is_cluster = len(by_person) >= min_buyers
        is_solo_whale = biggest["total"] >= solo_threshold
        if not is_cluster and not is_solo_whale:
            continue
        if total_value < min_value:
            continue
        member_names = list(by_person)
        if not ignore_alert_state and not should_alert(conn, "SWEDEN", isin, member_names, total_value):
            continue

        company = next(iter(by_person.values()))["issuer_name"]
        members = [f"{name} ({o['position'] or 'Insider'}) €{o['total']:,.0f}"
                   for name, o in by_person.items()]

        dates = [r[4] for r in group]
        signals.append(ClusterSignal(
            source="SWEDEN", ticker=isin, company=company, buyer_count=len(by_person),
            total_value=total_value or None, members=members,
            window_start=min(dates), window_end=max(dates),
            reason="cluster" if is_cluster else "solo",
            member_names=member_names,
        ))
    return signals


def commit_alert(conn, signal: ClusterSignal) -> None:
    db.save_cluster_alert_state(conn, signal.source, signal.ticker, signal.buyer_count,
                                 signal.member_names, signal.total_value)


@dataclass
class ExitSignal:
    source: str  # "SEC", "HOUSE", "BAFIN" or "NORWAY"
    ticker: str
    company: str
    total_buyers: int
    seller_count: int
    lines: list[str]  # already-formatted "Name: bought $X D1 -> sold $Y D2 (url)" lines
    # Plain seller names, for the same reason ClusterSignal carries member_names.
    seller_names: list[str] = field(default_factory=list)
    corroborated_by: list[str] = field(default_factory=list)


def find_sec_exit_signals(conn, lookback_months: int = EXIT_LOOKBACK_MONTHS,
                           min_buyers: int = EXIT_MIN_BUYERS, min_sellers: int = EXIT_MIN_SELLERS,
                           sell_fraction: float = EXIT_SELL_FRACTION,
                           ignore_alert_state: bool = False, include_form_144: bool = True,
                           min_144_value: float = FORM_144_MIN_VALUE) -> list[ExitSignal]:
    """`include_form_144` also counts a notice of intent to sell (Form 144) as an
    exit. That is the point of collecting those: a 144 is filed *before* the sale,
    and therefore before the Form 4 that records it, so an unwinding cluster shows
    up here weeks earlier than sales alone would reveal it. The signal line says
    which kind of evidence each person contributed, because an intention is not a
    completed sale -- filers do abandon them.
    """
    since = (dt.date.today() - dt.timedelta(days=lookback_months * 30)).isoformat()
    buy_rows = conn.execute(
        "SELECT ticker, issuer_name, owner_name, transaction_date, value, source_url FROM sec_purchases "
        "WHERE transaction_date >= ? AND ticker IS NOT NULL AND ticker != ''",
        (since,),
    ).fetchall()
    sell_rows = conn.execute(
        "SELECT ticker, owner_name, transaction_date, value, source_url FROM sec_sales "
        "WHERE transaction_date >= ? AND ticker IS NOT NULL AND ticker != ''",
        (since,),
    ).fetchall()

    # ticker -> owner_name -> (company, earliest_buy_date, buy_value, buy_url)
    buys: dict[str, dict[str, tuple]] = {}
    for ticker, issuer_name, owner_name, txn_date, value, url in buy_rows:
        d = buys.setdefault(ticker, {})
        if owner_name not in d or txn_date < d[owner_name][1]:
            d[owner_name] = (issuer_name, txn_date, value, url)

    # ticker -> name_key -> [(sell_date, sell_value, sell_url, kind), ...]
    #
    # Keyed by name_key rather than the raw name because Form 4 and Form 144 write
    # the same person's name differently ("CHEN SEAN" vs "Sean Chen"); an exact
    # match would find essentially nothing across the two forms.
    sells: dict[str, dict[str, list]] = {}
    for ticker, owner_name, txn_date, value, url in sell_rows:
        sells.setdefault(ticker, {}).setdefault(name_key(owner_name), []).append(
            (txn_date, value, url, "sale"))

    if include_form_144:
        notice_rows = conn.execute(
            """SELECT ticker, person_name, approx_sale_date, market_value, source_url
               FROM sec_proposed_sales
               WHERE approx_sale_date >= ? AND ticker IS NOT NULL AND ticker != ''
                 AND market_value >= ?""",
            (since, min_144_value),
        ).fetchall()
        for ticker, person, sale_date, value, url in notice_rows:
            sells.setdefault(ticker, {}).setdefault(name_key(person), []).append(
                (sale_date, value, url, "notice"))

    signals = []
    for ticker, buyers in buys.items():
        if len(buyers) < min_buyers:
            continue
        sellers_for_ticker = sells.get(ticker, {})
        matched = []
        for owner_name, (issuer_name, buy_date, buy_value, buy_url) in buyers.items():
            later_sells = sorted(s for s in sellers_for_ticker.get(name_key(owner_name), [])
                                 if s[0] > buy_date)
            if later_sells:
                sell_date, sell_value, sell_url, sell_kind = later_sells[0]
                matched.append((owner_name, buy_date, buy_value, buy_url,
                                 sell_date, sell_value, sell_url, sell_kind))
        if len(matched) < min_sellers or len(matched) < math.ceil(len(buyers) * sell_fraction):
            continue

        seller_names = [m[0] for m in matched]
        if not ignore_alert_state and not should_alert_exit(conn, "SEC", ticker, seller_names):
            continue

        company = next(iter(buyers.values()))[0]
        lines = []
        for owner_name, buy_date, buy_value, buy_url, sell_date, sell_value, sell_url, sell_kind in matched:
            bv = f"€{fx.to_eur(buy_value, 'USD', conn):,.0f}" if buy_value else "?"
            sv = f"€{fx.to_eur(sell_value, 'USD', conn):,.0f}" if sell_value else "?"
            verb = "заявил о продаже (Form 144)" if sell_kind == "notice" else "продал"
            label = "уведомление" if sell_kind == "notice" else "продажа"
            lines.append(f"{owner_name}: купил {bv} {datefmt.fmt(buy_date)} -> {verb} {sv} "
                         f"{datefmt.fmt(sell_date)}\n     покупка: {buy_url}\n     {label}: {sell_url}")

        signals.append(ExitSignal(
            source="SEC", ticker=ticker, company=company, total_buyers=len(buyers),
            seller_count=len(matched), lines=lines, seller_names=seller_names,
        ))
    return signals


def find_house_exit_signals(conn, lookback_months: int = EXIT_LOOKBACK_MONTHS,
                             min_buyers: int = EXIT_MIN_BUYERS, min_sellers: int = EXIT_MIN_SELLERS,
                             sell_fraction: float = EXIT_SELL_FRACTION,
                             ignore_alert_state: bool = False, table: str = "house_purchases",
                             source: str = "HOUSE", date_format: str | None = "%m/%d/%Y") -> list[ExitSignal]:
    """Also serves the Senate, via find_senate_exit_signals -- see find_house_clusters
    for why the two chambers share this code."""
    cutoff = dt.date.today() - dt.timedelta(days=lookback_months * 30)
    rows = conn.execute(
        f"SELECT ticker, asset, member_name, txn_type, txn_date, amount_range, source_url FROM {table} "
        "WHERE ticker IS NOT NULL AND ticker != ''"
    ).fetchall()

    def parse_date(s):
        try:
            return (dt.datetime.strptime(s, date_format).date() if date_format
                    else dt.date.fromisoformat(s))
        except ValueError:
            return None

    # ticker -> member_name -> (asset, earliest_buy_date, buy_amount, buy_url)
    buys: dict[str, dict[str, tuple]] = {}
    # ticker -> member_name -> [(sell_date, sell_amount, sell_url), ...]
    sells: dict[str, dict[str, list]] = {}

    for ticker, asset, member_name, txn_type, txn_date, amount_range, url in rows:
        d = parse_date(txn_date)
        if d is None or d < cutoff:
            continue
        if txn_type == "P":
            slot = buys.setdefault(ticker, {})
            if member_name not in slot or d < slot[member_name][1]:
                slot[member_name] = (asset, d, amount_range, url)
        elif txn_type in ("S", "S (partial)"):
            sells.setdefault(ticker, {}).setdefault(member_name, []).append((d, amount_range, url))

    signals = []
    for ticker, buyers in buys.items():
        if len(buyers) < min_buyers:
            continue
        sellers_for_ticker = sells.get(ticker, {})
        matched = []
        for member_name, (asset, buy_date, buy_amount, buy_url) in buyers.items():
            later_sells = sorted(s for s in sellers_for_ticker.get(member_name, []) if s[0] > buy_date)
            if later_sells:
                sell_date, sell_amount, sell_url = later_sells[0]
                matched.append((member_name, buy_date, buy_amount, buy_url, sell_date, sell_amount, sell_url))
        if len(matched) < min_sellers or len(matched) < math.ceil(len(buyers) * sell_fraction):
            continue

        seller_names = [m[0] for m in matched]
        if not ignore_alert_state and not should_alert_exit(conn, source, ticker, seller_names):
            continue

        company = _clean_asset_name(next(iter(buyers.values()))[0])
        lines = []
        for member_name, buy_date, buy_amount, buy_url, sell_date, sell_amount, sell_url in matched:
            lines.append(
                f"{member_name}: купил {_bracket_to_eur(buy_amount, conn)} {datefmt.fmt(buy_date)} -> "
                f"продал {_bracket_to_eur(sell_amount, conn)} {datefmt.fmt(sell_date)}"
                f"\n     покупка: {buy_url}\n     продажа: {sell_url}"
            )

        signals.append(ExitSignal(
            source=source, ticker=ticker, company=company, total_buyers=len(buyers),
            seller_count=len(matched), lines=lines, seller_names=seller_names,
        ))
    return signals


def find_senate_exit_signals(conn, **kwargs) -> list[ExitSignal]:
    """Senate PTR exit signals. Same logic as the House -- see find_house_exit_signals."""
    return find_house_exit_signals(conn, table="senate_purchases", source="SENATE",
                                    date_format=None, **kwargs)


def find_bafin_exit_signals(conn, lookback_months: int = EXIT_LOOKBACK_MONTHS,
                             min_buyers: int = EXIT_MIN_BUYERS, min_sellers: int = EXIT_MIN_SELLERS,
                             sell_fraction: float = EXIT_SELL_FRACTION,
                             ignore_alert_state: bool = False) -> list[ExitSignal]:
    cutoff = dt.date.today() - dt.timedelta(days=lookback_months * 30)
    rows = conn.execute(
        "SELECT isin, issuer_name, notifier_name, txn_type, txn_date, volume_eur, source_url FROM bafin_purchases "
        "WHERE isin IS NOT NULL AND isin != ''"
    ).fetchall()

    def parse_date(s):
        try:
            return dt.datetime.strptime(s, "%d.%m.%Y").date()
        except ValueError:
            return None

    # isin -> notifier_name -> (issuer_name, earliest_buy_date, buy_value, buy_url)
    buys: dict[str, dict[str, tuple]] = {}
    # isin -> notifier_name -> [(sell_date, sell_value, sell_url), ...]
    sells: dict[str, dict[str, list]] = {}

    for isin, issuer_name, notifier_name, txn_type, txn_date, volume, url in rows:
        d = parse_date(txn_date)
        if d is None or d < cutoff:
            continue
        if txn_type == "P":
            slot = buys.setdefault(isin, {})
            if notifier_name not in slot or d < slot[notifier_name][1]:
                slot[notifier_name] = (issuer_name, d, volume, url)
        elif txn_type == "S":
            sells.setdefault(isin, {}).setdefault(notifier_name, []).append((d, volume, url))

    signals = []
    for isin, buyers in buys.items():
        if len(buyers) < min_buyers:
            continue
        sellers_for_isin = sells.get(isin, {})
        matched = []
        for notifier_name, (issuer_name, buy_date, buy_value, buy_url) in buyers.items():
            later_sells = sorted(s for s in sellers_for_isin.get(notifier_name, []) if s[0] > buy_date)
            if later_sells:
                sell_date, sell_value, sell_url = later_sells[0]
                matched.append((notifier_name, buy_date, buy_value, buy_url, sell_date, sell_value, sell_url))
        if len(matched) < min_sellers or len(matched) < math.ceil(len(buyers) * sell_fraction):
            continue

        seller_names = [m[0] for m in matched]
        if not ignore_alert_state and not should_alert_exit(conn, "BAFIN", isin, seller_names):
            continue

        company = next(iter(buyers.values()))[0]
        lines = []
        for notifier_name, buy_date, buy_value, buy_url, sell_date, sell_value, sell_url in matched:
            bv = f"€{buy_value:,.0f}" if buy_value else "?"
            sv = f"€{sell_value:,.0f}" if sell_value else "?"
            lines.append(
                f"{notifier_name}: купил {bv} {datefmt.fmt(buy_date)} -> продал {sv} {datefmt.fmt(sell_date)}"
                f"\n     покупка: {buy_url}\n     продажа: {sell_url}"
            )

        signals.append(ExitSignal(
            source="BAFIN", ticker=isin, company=company, total_buyers=len(buyers),
            seller_count=len(matched), lines=lines, seller_names=seller_names,
        ))
    return signals


def find_norway_exit_signals(conn, lookback_months: int = EXIT_LOOKBACK_MONTHS,
                              min_buyers: int = EXIT_MIN_BUYERS, min_sellers: int = EXIT_MIN_SELLERS,
                              sell_fraction: float = EXIT_SELL_FRACTION,
                              ignore_alert_state: bool = False) -> list[ExitSignal]:
    # txn_date is already ISO here (unlike House's M/D/Y or BaFin's D.M.Y), so no
    # parse-to-date step is needed before comparing/sorting -- same as SEC's.
    since = (dt.date.today() - dt.timedelta(days=lookback_months * 30)).isoformat()
    rows = conn.execute(
        """SELECT ticker, issuer_name, person, txn_type, txn_date, value, currency, source_url
           FROM norway_purchases
           WHERE txn_date >= ? AND ticker IS NOT NULL AND ticker != ''""",
        (since,),
    ).fetchall()

    # ticker -> person -> (issuer_name, earliest_buy_date, buy_value, buy_currency, buy_url)
    buys: dict[str, dict[str, tuple]] = {}
    # ticker -> person -> [(sell_date, sell_value, sell_currency, sell_url), ...]
    sells: dict[str, dict[str, list]] = {}

    for ticker, issuer_name, person, txn_type, txn_date, value, currency, url in rows:
        if txn_type == "P":
            slot = buys.setdefault(ticker, {})
            if person not in slot or txn_date < slot[person][1]:
                slot[person] = (issuer_name, txn_date, value, currency, url)
        elif txn_type == "S":
            sells.setdefault(ticker, {}).setdefault(person, []).append((txn_date, value, currency, url))

    signals = []
    for ticker, buyers in buys.items():
        if len(buyers) < min_buyers:
            continue
        sellers_for_ticker = sells.get(ticker, {})
        matched = []
        for person, (issuer_name, buy_date, buy_value, buy_currency, buy_url) in buyers.items():
            later_sells = sorted(s for s in sellers_for_ticker.get(person, []) if s[0] > buy_date)
            if later_sells:
                sell_date, sell_value, sell_currency, sell_url = later_sells[0]
                matched.append((person, buy_date, buy_value, buy_currency, buy_url,
                                 sell_date, sell_value, sell_currency, sell_url))
        if len(matched) < min_sellers or len(matched) < math.ceil(len(buyers) * sell_fraction):
            continue

        seller_names = [m[0] for m in matched]
        if not ignore_alert_state and not should_alert_exit(conn, "NORWAY", ticker, seller_names):
            continue

        company = next(iter(buyers.values()))[0]
        lines = []
        for person, buy_date, buy_value, buy_currency, buy_url, sell_date, sell_value, sell_currency, sell_url in matched:
            bv = f"€{fx.to_eur(buy_value, buy_currency, conn):,.0f}" if buy_value else "?"
            sv = f"€{fx.to_eur(sell_value, sell_currency, conn):,.0f}" if sell_value else "?"
            lines.append(f"{person}: купил {bv} {datefmt.fmt(buy_date)} -> продал {sv} {datefmt.fmt(sell_date)}"
                         f"\n     покупка: {buy_url}\n     продажа: {sell_url}")

        signals.append(ExitSignal(
            source="NORWAY", ticker=ticker, company=company, total_buyers=len(buyers),
            seller_count=len(matched), lines=lines, seller_names=seller_names,
        ))
    return signals


def find_sweden_exit_signals(conn, lookback_months: int = EXIT_LOOKBACK_MONTHS,
                              min_buyers: int = EXIT_MIN_BUYERS, min_sellers: int = EXIT_MIN_SELLERS,
                              sell_fraction: float = EXIT_SELL_FRACTION,
                              ignore_alert_state: bool = False) -> list[ExitSignal]:
    """txn_date is already ISO here, so no parse-to-date step is needed before
    comparing or sorting -- same as SEC's and Norway's."""
    since = (dt.date.today() - dt.timedelta(days=lookback_months * 30)).isoformat()
    rows = conn.execute(
        """SELECT isin, issuer_name, person, txn_type, txn_date, value, currency, source_url
           FROM sweden_purchases
           WHERE txn_date >= ? AND isin IS NOT NULL AND isin != '' AND status = 'Aktuell'""",
        (since,),
    ).fetchall()

    # isin -> person -> (issuer_name, earliest_buy_date, buy_value, buy_currency, buy_url)
    buys: dict[str, dict[str, tuple]] = {}
    # isin -> person -> [(sell_date, sell_value, sell_currency, sell_url), ...]
    sells: dict[str, dict[str, list]] = {}

    for isin, issuer_name, person, txn_type, txn_date, value, currency, url in rows:
        if txn_type == "P":
            slot = buys.setdefault(isin, {})
            if person not in slot or txn_date < slot[person][1]:
                slot[person] = (issuer_name, txn_date, value, currency, url)
        elif txn_type == "S":
            sells.setdefault(isin, {}).setdefault(person, []).append((txn_date, value, currency, url))

    signals = []
    for isin, buyers in buys.items():
        if len(buyers) < min_buyers:
            continue
        sellers_for_isin = sells.get(isin, {})
        matched = []
        for person, (issuer_name, buy_date, buy_value, buy_currency, buy_url) in buyers.items():
            later_sells = sorted(s for s in sellers_for_isin.get(person, []) if s[0] > buy_date)
            if later_sells:
                sell_date, sell_value, sell_currency, sell_url = later_sells[0]
                matched.append((person, buy_date, buy_value, buy_currency, buy_url,
                                 sell_date, sell_value, sell_currency, sell_url))
        if len(matched) < min_sellers or len(matched) < math.ceil(len(buyers) * sell_fraction):
            continue

        seller_names = [m[0] for m in matched]
        if not ignore_alert_state and not should_alert_exit(conn, "SWEDEN", isin, seller_names):
            continue

        company = next(iter(buyers.values()))[0]
        lines = []
        for person, buy_date, buy_value, buy_currency, buy_url, sell_date, sell_value, sell_currency, sell_url in matched:
            bv = f"€{fx.to_eur(buy_value, buy_currency, conn):,.0f}" if buy_value else "?"
            sv = f"€{fx.to_eur(sell_value, sell_currency, conn):,.0f}" if sell_value else "?"
            lines.append(f"{person}: купил {bv} {datefmt.fmt(buy_date)} -> продал {sv} {datefmt.fmt(sell_date)}"
                         f"\n     покупка: {buy_url}\n     продажа: {sell_url}")

        signals.append(ExitSignal(
            source="SWEDEN", ticker=isin, company=company, total_buyers=len(buyers),
            seller_count=len(matched), lines=lines, seller_names=seller_names,
        ))
    return signals


def commit_exit_alert(conn, signal: ExitSignal) -> None:
    db.save_cluster_alert_state(conn, f"{signal.source}_EXIT", signal.ticker, signal.seller_count,
                                 signal.seller_names)
