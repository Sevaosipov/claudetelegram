"""cfd/live.py, storage and settings: the cfd_signals table (spec 2026-10-08-cfd-live-crypto-breakout.md,
"Storage") and the four kv settings. In-memory database, nothing else."""
from __future__ import annotations

import sqlite3

import pytest

from cfd import live


def _new(conn, coin="SOL", signal_date="2026-10-06", **kw):
    base = dict(coin=coin, symbol=f"{coin}-USD", side="long", signal_date=signal_date, entry=121.5,
                stop0=108.2, r=13.3, last_bar=signal_date)
    base.update(kw)
    return live.insert_signal(conn, **base)


def test_the_table_has_the_columns_of_the_spec(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(cfd_signals)")]
    assert cols == ["id", "coin", "symbol", "side", "signal_date", "entry", "stop0", "stop", "r",
                    "checkpoint", "last_bar", "status", "closed_date", "exit_price", "result_r",
                    "risk_pct", "risk_eur", "qty", "created"]


def test_a_new_signal_is_open_with_its_stop_at_the_initial_stop(conn):
    sig = live.get_signal(conn, _new(conn))
    assert (sig.coin, sig.symbol, sig.side, sig.signal_date) == ("SOL", "SOL-USD", "long", "2026-10-06")
    assert (sig.entry, sig.stop0, sig.stop, sig.r) == (121.5, 108.2, 108.2, 13.3)
    assert (sig.checkpoint, sig.last_bar, sig.status) == (0, "2026-10-06", "open")
    assert (sig.closed_date, sig.exit_price, sig.result_r) == (None, None, None)
    assert (sig.risk_pct, sig.risk_eur, sig.qty) == (None, None, None)       # no balance set
    assert sig.created


def test_the_sizing_is_kept_when_given(conn):
    sig = live.get_signal(conn, _new(conn, risk_pct=1.0, risk_eur=5.0, qty=0.43))
    assert (sig.risk_pct, sig.risk_eur, sig.qty) == (1.0, 5.0, 0.43)


def test_one_signal_per_coin_and_signal_date(conn):
    _new(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _new(conn)
    _new(conn, signal_date="2026-10-07")
    _new(conn, coin="BTC")


def test_open_and_closed_lists(conn):
    a, b = _new(conn), _new(conn, coin="BTC")
    live.update_signal(conn, b, status="closed", closed_date="2026-10-09", exit_price=100.0,
                       result_r=-1.02)
    assert [s.id for s in live.open_signals(conn)] == [a]
    assert [s.id for s in live.closed_signals(conn)] == [b]
    assert live.has_signal(conn, "SOL", "2026-10-06") and not live.has_signal(conn, "SOL", "2026-10-07")
    assert live.get_signal(conn, 999) is None


def test_update_changes_only_the_named_columns(conn):
    sid = _new(conn)
    live.update_signal(conn, sid, checkpoint=2, stop=119.4, last_bar="2026-10-08")
    sig = live.get_signal(conn, sid)
    assert (sig.checkpoint, sig.stop, sig.last_bar, sig.stop0, sig.status) == (2, 119.4, "2026-10-08", 108.2, "open")
    with pytest.raises(ValueError):
        live.update_signal(conn, sid, nonsense=1)


# ------------------------------------------------------------------ settings
def test_the_defaults(conn):
    s = live.get_settings(conn)
    assert (s.balance_eur, s.risk_pct, s.max_open_risk_pct, s.paused) == (None, 1.0, 3.0, False)


def test_settings_are_kept_in_kv_under_the_spec_names(conn):
    live.set_setting(conn, "balance", 500.0)
    live.set_setting(conn, "risk", 0.5)
    live.set_setting(conn, "maxrisk", 4.0)
    live.set_setting(conn, "paused", True)
    s = live.get_settings(conn)
    assert (s.balance_eur, s.risk_pct, s.max_open_risk_pct, s.paused) == (500.0, 0.5, 4.0, True)
    keys = {r[0] for r in conn.execute("SELECT key FROM kv_cache WHERE key LIKE 'cfd_%'")}
    assert keys == {"cfd_balance_eur", "cfd_risk_pct", "cfd_max_open_risk_pct", "cfd_paused"}
    live.set_setting(conn, "paused", False)
    assert live.get_settings(conn).paused is False


def test_a_zero_or_missing_balance_means_no_balance(conn):
    live.set_setting(conn, "balance", 500.0)
    live.set_setting(conn, "balance", None)
    assert live.get_settings(conn).balance_eur is None


def test_settings_do_not_expire(conn):
    live.set_setting(conn, "risk", 2.0)
    conn.execute("UPDATE kv_cache SET computed_at = '2001-01-01T00:00:00' WHERE key = 'cfd_risk_pct'")
    assert live.get_settings(conn).risk_pct == 2.0
