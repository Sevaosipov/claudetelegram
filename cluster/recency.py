"""When a signal's newest piece became public -- what "a signal from the last N days"
is measured by.

Not the trade date: a member of Congress can disclose a trade 45 days after making
it, and that trade was news on the day it was disclosed, not the day it happened.
Each source's own publication date is used where it stores one (SEC's filing date,
Finansinspektionen's publication time); elsewhere, the day the bot first stored the
row, which for a daily-run bot is the disclosure day give or take one.
"""

from __future__ import annotations

# source -> (table, issuer column, person column, disclosure-date SQL, purchases-only clause)
_SPECS = {
    "SEC": ("sec_purchases", "ticker", "owner_name",
            "COALESCE(NULLIF(filed_date, ''), date(found_at))", ""),
    "HOUSE": ("house_purchases", "ticker", "member_name", "date(found_at)", "txn_type = 'P'"),
    "SENATE": ("senate_purchases", "ticker", "member_name", "date(found_at)", "txn_type = 'P'"),
    "BAFIN": ("bafin_purchases", "isin", "notifier_name", "date(found_at)", "txn_type = 'P'"),
    "NORWAY": ("norway_purchases", "ticker", "person", "date(found_at)", "txn_type = 'P'"),
    "SWEDEN": ("sweden_purchases", "isin", "person",
               "COALESCE(date(published), date(found_at))", "txn_type = 'P'"),
    "SEC13DG": ("sec_stakes", "ticker", "person_name", "date(found_at)", ""),
}


def disclosed_on(conn, sig) -> str | None:
    """ISO date the signal's most recently disclosed row became public, or None when
    it can't be told (exit signals, which this isn't used for)."""
    if hasattr(sig, "crypto_kind"):
        # Its own date already is the publication: a filing date, the issuer's
        # as-of date for an ETF snapshot, the moment a balance was read.
        return (sig.window_end or "")[:10] or None
    spec = _SPECS.get(sig.source)
    if spec is None or hasattr(sig, "seller_count"):
        return None
    table, key_col, person_col, expr, only_buys = spec
    if hasattr(sig, "percent"):   # StakeSignal
        people = [sig.person] + list(getattr(sig, "co_filer_names", []) or [])
    else:
        people = list(getattr(sig, "member_names", []) or [])
    where = [f"{key_col} = ?"]
    params: list = [sig.ticker]
    if people:
        where.append(f"{person_col} IN ({','.join('?' * len(people))})")
        params += people
    if only_buys:
        where.append(only_buys)
    row = conn.execute(f"SELECT max({expr}) FROM {table} WHERE {' AND '.join(where)}",
                       params).fetchone()
    return row[0] if row and row[0] else None
