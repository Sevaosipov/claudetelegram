"""Buy-cluster finders: several distinct insiders or members of Congress buying
the same security within a window, per disclosure source."""

from __future__ import annotations

import datetime as dt

import fx
import sweden

from .common import (
    BAFIN_MIN_BUYERS,
    BAFIN_SOLO_THRESHOLD,
    BAFIN_WINDOW_DAYS,
    ClusterSignal,
    FIRST_BUY_MIN_HISTORY_DAYS,
    HOUSE_CLUSTER_SPAN_DAYS,
    HOUSE_MIN_BUYERS,
    HOUSE_SOLO_THRESHOLD,
    HOUSE_WINDOW_DAYS,
    MIN_CLUSTER_VALUE,
    NORWAY_MIN_BUYERS,
    NORWAY_SOLO_THRESHOLD,
    NORWAY_WINDOW_DAYS,
    SEC_MIN_BUYERS,
    SEC_SOLO_THRESHOLD,
    SEC_WINDOW_DAYS,
    SENATE_CLUSTER_SPAN_DAYS,
    SENATE_MIN_BUYERS,
    SENATE_WINDOW_DAYS,
    SWEDEN_MIN_BUYERS,
    SWEDEN_SOLO_THRESHOLD,
    SWEDEN_WINDOW_DAYS,
    _ASSET_SUFFIX_RE,
    _JUNK_TICKER_SQL,
    parse_amount_low,
)
from .alerts import should_alert


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
