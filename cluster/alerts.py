"""Alert state: whether a signal is new enough to send, and recording that it was."""

from __future__ import annotations

import db

from .common import ALERT_VALUE_GROWTH, ClusterSignal


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


def commit_alert(conn, signal: ClusterSignal) -> None:
    db.save_cluster_alert_state(conn, signal.source, signal.ticker, signal.buyer_count,
                                 signal.member_names, signal.total_value)
