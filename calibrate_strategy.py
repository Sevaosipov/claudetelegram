"""Replay strategy.py's tiers day by day over stored history, to check the rules give
roughly the intended volume (2-3 Сильный, 5-10 Кандидат a week) before trusting them.

For each replayed day D, every source table is shadowed by a TEMP VIEW of the same
name that hides rows disclosed after D (SQLite resolves unqualified names to the temp
schema first), the finders' "today" is frozen to D in cluster.buys, cluster.stakes,
cluster.crypto, and cluster.scoring, and corroboration dates are likewise frozen.
Market caps come from today's cache -- an approximation, and the report says so.
Runs on an in-memory copy of the database; the file on disk is never written.

    python calibrate_strategy.py              # last 35 days
    python calibrate_strategy.py --days 60
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import types
from pathlib import Path

import cluster
import cluster.buys
import cluster.crypto
import cluster.scoring
import cluster.stakes
import db
import strategy
import trading212

DB_PATH = Path(__file__).parent / "data" / "disclosures.db"

# table -> SQL condition (with {d} = the replayed ISO date) for "known by then".
_VISIBLE = {
    "sec_purchases": "date(found_at) <= '{d}' AND COALESCE(NULLIF(filed_date, ''), '{d}') <= '{d}'",
    "house_purchases": "date(found_at) <= '{d}'",
    "senate_purchases": "date(found_at) <= '{d}'",
    "bafin_purchases": "date(found_at) <= '{d}'",
    "norway_purchases": "date(found_at) <= '{d}'",
    "sweden_purchases": "date(found_at) <= '{d}'",
    "sec_stakes": "date(found_at) <= '{d}'",
    "signal_journal": "date(emitted_at) <= '{d}'",
    "crypto_treasury_txns": "filed_date <= '{d}'",
    "crypto_etf_snapshots": "as_of <= '{d}'",
    "crypto_wallet_snapshots": "date(taken_at) <= '{d}'",
}
_FROZEN_MODULES = (cluster.buys, cluster.stakes, cluster.crypto, cluster.scoring)


def _frozen_dt(day: dt.date):
    class _Date(dt.date):
        @classmethod
        def today(cls):
            return day
    return types.SimpleNamespace(date=_Date, datetime=dt.datetime, timedelta=dt.timedelta)


def _shadow(conn, day: dt.date) -> None:
    for table, cond in _VISIBLE.items():
        conn.execute(f"DROP VIEW IF EXISTS temp.{table}")
        conn.execute(f"CREATE TEMP VIEW {table} AS SELECT * FROM main.{table} "
                     f"WHERE {cond.format(d=day.isoformat())}")


def _unshadow(conn) -> None:
    for table in _VISIBLE:
        conn.execute(f"DROP VIEW IF EXISTS temp.{table}")


def _signals(conn) -> list:
    """strategy.buy_side_signals -- the same finder list bot.run_cluster_pass and
    menu._find_signals use -- with on-chain included, like menu's."""
    return strategy.buy_side_signals(conn, ignore_alert_state=True, onchain=True)


def replay(conn, start: dt.date, end: dt.date, t212) -> dict[str, dict]:
    """Replay tiers from start to end, returning {ISO week: {tier: [tickers]}}.

    Each (source, ticker, tier) is counted once, in the ISO week it first appears --
    the digest shows one entry per source, so results measure what the user receives.
    """
    weeks: dict[str, dict] = {}
    seen: set[tuple] = set()
    originals = [(m, m.dt) for m in _FROZEN_MODULES]
    try:
        day = start
        while day <= end:
            for m in _FROZEN_MODULES:
                m.dt = _frozen_dt(day)
            _shadow(conn, day)
            sel = strategy.select(conn, _signals(conn), t212, today=day)
            iso = day.isocalendar()
            week = weeks.setdefault(f"{iso.year}-W{iso.week:02d}", {"strong": [], "candidates": []})
            for tier, items in (("strong", sel.strong), ("candidates", sel.candidates)):
                for t in items:
                    key = (t.signal.source, t.signal.ticker, tier)
                    if key not in seen:
                        seen.add(key)
                        week[tier].append(t.signal.ticker)
            day += dt.timedelta(days=1)
    finally:
        for m, original in originals:
            m.dt = original
        _unshadow(conn)
    return weeks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=35)
    args = ap.parse_args()
    mem = db.connect(":memory:")
    src = sqlite3.connect(DB_PATH)
    src.backup(mem)
    src.close()
    t212 = trading212.availability(mem)
    end = dt.date.today()
    weeks = replay(mem, end - dt.timedelta(days=args.days), end, t212)
    print(f"Replay {args.days} days; Trading 212 filter: {'on' if t212 else 'OFF (no key)'}; "
          f"market caps are today's (approximation).\n")
    print(f"{'week':<10} {'strong':>6} {'cand.':>6}  strong tickers")
    for week, w in sorted(weeks.items()):
        print(f"{week:<10} {len(w['strong']):>6} {len(w['candidates']):>6}  "
              f"{', '.join(w['strong'])}")
    print("\nTarget: 2-3 strong and 5-10 candidates a week.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
