"""calibrate_strategy.replay: tiers as they would have been on past days, with rows
disclosed after that day hidden."""
from __future__ import annotations

import datetime as dt

import calibrate_strategy
import cluster
import cluster.scoring
import db
from conftest import add_sec_purchase


def _sized(monkeypatch):
    def fake(conn, signals):
        for s in signals:
            s.score, s.market_cap_eur, s.avg_daily_value = 50.0, 5e9, 5e7
        return signals
    monkeypatch.setattr(cluster, "enrich_signals", fake)


def test_a_strong_signal_is_counted_once_in_the_week_it_appeared(conn, monkeypatch):
    _sized(monkeypatch)
    day = dt.date(2026, 9, 9)                       # ISO week 37
    for o in ("A", "B", "C"):
        add_sec_purchase(conn, "AAA", o, 116_000, day.isoformat(), filed_date=day.isoformat())
    conn.execute("UPDATE sec_purchases SET found_at = ?", (f"{day.isoformat()} 06:00:00",))
    weeks = calibrate_strategy.replay(conn, day - dt.timedelta(days=2), day + dt.timedelta(days=6), None)
    assert weeks["2026-W37"]["strong"] == ["AAA"]
    assert sum(len(w["strong"]) for w in weeks.values()) == 1


def test_rows_disclosed_after_the_replayed_day_are_invisible(conn, monkeypatch):
    _sized(monkeypatch)
    day = dt.date(2026, 9, 9)
    for o in ("A", "B", "C"):
        add_sec_purchase(conn, "AAA", o, 116_000, day.isoformat(), filed_date=day.isoformat())
    conn.execute("UPDATE sec_purchases SET found_at = '2026-09-20 06:00:00'")
    weeks = calibrate_strategy.replay(conn, day, day, None)
    assert all(not w["strong"] for w in weeks.values())


def test_signal_journal_and_scoring_are_frozen_per_day(conn, monkeypatch):
    """signal_journal rows emitted after the replayed day are hidden; cluster.scoring.dt
    is frozen during replay and restored afterward."""
    day = dt.date(2026, 9, 9)

    # Seed signal_journal row emitted AFTER the replayed day
    db.journal_signal(conn, {"source": "HOUSE", "kind": "cluster", "ticker": "AAA"})
    conn.execute("UPDATE signal_journal SET emitted_at = '2026-09-20 06:00:00'")

    # Seed three SEC purchases found ON the replayed day
    for o in ("A", "B", "C"):
        add_sec_purchase(conn, "AAA", o, 116_000, day.isoformat(), filed_date=day.isoformat())
    conn.execute("UPDATE sec_purchases SET found_at = ?", (f"{day.isoformat()} 06:00:00",))

    # Store what dt is visible inside replay (via fake enrich)
    recorded = {}

    def fake_enrich(conn, signals):
        # Record the frozen dt.date.today() and visible signal_journal count inside replay
        recorded["today"] = cluster.scoring.dt.date.today()
        recorded["journal_count"] = conn.execute("SELECT count(*) FROM signal_journal").fetchone()[0]
        for s in signals:
            s.score, s.market_cap_eur, s.avg_daily_value = 50.0, 5e9, 5e7
        return signals

    monkeypatch.setattr(cluster, "enrich_signals", fake_enrich)

    # Remember the original dt before replay
    original_dt = cluster.scoring.dt

    # Replay a single day
    calibrate_strategy.replay(conn, day, day, None)

    # Verify that dt was frozen to the replayed day during replay
    assert recorded["today"] == day, f"dt.date.today() inside replay was {recorded['today']}, not {day}"
    # Verify that signal_journal was invisible (emitted after replayed day)
    assert recorded["journal_count"] == 0, f"signal_journal had {recorded['journal_count']} rows, expected 0"
    # Verify that dt is restored after replay
    assert cluster.scoring.dt is original_dt, "cluster.scoring.dt was not restored"
    assert cluster.scoring.dt is dt, "cluster.scoring.dt is not the real datetime module"
