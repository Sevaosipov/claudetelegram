"""calibrate_strategy.replay: tiers as they would have been on past days, with rows
disclosed after that day hidden."""
from __future__ import annotations

import datetime as dt

import calibrate_strategy
import cluster
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
