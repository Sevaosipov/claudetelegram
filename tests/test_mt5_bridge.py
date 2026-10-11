"""cfd/mt5_bridge.py: the MetaTrader 5 terminal's files as a price source. No terminal: the files are written
by the test."""
from __future__ import annotations

import datetime as dt
import os

from cfd import mt5_bridge as bridge

UTC = dt.timezone.utc
HEAD = "time,open,high,low,close,tick_volume,spread_points\n"


def test_the_server_clock_is_seven_hours_ahead_of_new_york():
    assert bridge.server_to_utc("2026.10.12 12:00") == dt.datetime(2026, 10, 12, 9, 0, tzinfo=UTC)     # US summer
    assert bridge.server_to_utc("2026.01.12 12:00") == dt.datetime(2026, 1, 12, 10, 0, tzinfo=UTC)     # US winter


def test_fresh_files_are_read_and_anything_else_falls_back(tmp_path):
    (tmp_path / "EURUSD_M15_recent.csv").write_text(
        HEAD + "2026.10.12 12:00,1.12000,1.12100,1.11900,1.12050,340,2\nbroken row\n")
    got = bridge.rows("EURUSD=X", "15m", folder=tmp_path)
    assert got == [("2026-10-12T09:00:00+00:00", 1.12, 1.121, 1.119, 1.1205)]
    assert bridge.rows("EURUSD=X", "1h", folder=tmp_path) is None            # no such file
    assert bridge.rows("BTC-USD", "15m", folder=tmp_path) is None            # a symbol the bridge does not carry
    old = os.path.getmtime(tmp_path / "EURUSD_M15_recent.csv") + bridge.MAX_AGE + 5
    assert bridge.rows("EURUSD=X", "15m", folder=tmp_path, now=old) is None  # the terminal has stopped writing


def test_fetch_or_prefers_the_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv("MT5_BRIDGE_DIR", str(tmp_path))
    fetch = bridge.fetch_or(lambda symbol, interval: [("yahoo",)])
    assert fetch("EURUSD=X", "15m") == [("yahoo",)]
    (tmp_path / "EURUSD_M15_recent.csv").write_text(HEAD + "2026.10.12 12:00,1.12,1.121,1.119,1.1205,340,2\n")
    assert fetch("EURUSD=X", "15m")[0][0] == "2026-10-12T09:00:00+00:00"


def test_the_history_file_gives_bars_and_spreads(tmp_path):
    (tmp_path / "XAUUSD_H1_history.csv").write_text(
        HEAD + "2026.10.09 10:00,4200.10,4210.00,4195.50,4205.00,900,15\n2026.10.09 11:00,4205.00,4207.00,4201.00,4202.00,800,12\n")
    bars = bridge.history("XAUUSD", "H1", folder=tmp_path)
    assert [b.close for b in bars] == [4205.0, 4202.0] and bars[0].ts == dt.datetime(2026, 10, 9, 7, 0, tzinfo=UTC)
    assert bridge.spread_points("XAUUSD", "H1", folder=tmp_path) == [15, 12]
    assert bridge.history("EURUSD", "H1", folder=tmp_path) == []
