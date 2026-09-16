"""claude_analysis_queue helpers (db.enqueue_analysis / pending_analysis /
mark_analysis_processed) -- pure DB logic, no network."""
from __future__ import annotations

import db


def test_enqueue_adds_a_pending_row(conn):
    db.enqueue_analysis(conn, "AAPL")
    assert db.pending_analysis(conn) == [(1, "AAPL")]


def test_enqueue_dedupes_against_an_existing_pending_row(conn):
    db.enqueue_analysis(conn, "AAPL")
    db.enqueue_analysis(conn, "AAPL")
    assert len(db.pending_analysis(conn)) == 1


def test_enqueue_allows_a_new_row_once_the_old_one_is_processed(conn):
    db.enqueue_analysis(conn, "AAPL")
    (queue_id, _) = db.pending_analysis(conn)[0]
    db.mark_analysis_processed(conn, queue_id)
    db.enqueue_analysis(conn, "AAPL")
    assert len(db.pending_analysis(conn)) == 1  # the new one, not the old processed one


def test_mark_processed_removes_it_from_pending(conn):
    db.enqueue_analysis(conn, "AAPL")
    db.enqueue_analysis(conn, "MSFT")
    (aapl_id, _) = next(r for r in db.pending_analysis(conn) if r[1] == "AAPL")
    db.mark_analysis_processed(conn, aapl_id)
    remaining = [t for _, t in db.pending_analysis(conn)]
    assert remaining == ["MSFT"]


def test_pending_analysis_orders_oldest_first(conn):
    db.enqueue_analysis(conn, "AAA")
    db.enqueue_analysis(conn, "BBB")
    db.enqueue_analysis(conn, "CCC")
    assert [t for _, t in db.pending_analysis(conn)] == ["AAA", "BBB", "CCC"]
