"""cfd/mt5_bridge.py: the MetaTrader 5 terminal's files as a price source. No terminal: the files are written
by the test."""
from __future__ import annotations

import datetime as dt
import os

import pytest

from cfd import mt5_bridge as bridge

UTC = dt.timezone.utc
HEAD = "time,open,high,low,close,tick_volume,spread_points\n"


def test_the_server_clock_is_seven_hours_ahead_of_new_york():
    assert bridge.server_to_utc("2026.10.12 12:00") == dt.datetime(2026, 10, 12, 9, 0, tzinfo=UTC)     # US summer
    assert bridge.server_to_utc("2026.01.12 12:00") == dt.datetime(2026, 1, 12, 10, 0, tzinfo=UTC)     # US winter


def test_fresh_files_are_read_and_anything_else_falls_back(tmp_path):
    (tmp_path / "EURUSD_M15_recent.csv").write_text(
        HEAD + "2026.10.12 12:00,1.12000,1.12100,1.11900,1.12050,340,2\nbroken row\n")
    at = dt.datetime(2026, 10, 12, 9, 20, tzinfo=UTC).timestamp()
    os.utime(tmp_path / "EURUSD_M15_recent.csv", (at, at))
    got = bridge.rows("EURUSD=X", "15m", folder=tmp_path, now=at)
    assert got == [("2026-10-12T09:00:00+00:00", 1.12, 1.121, 1.119, 1.1205)]
    assert bridge.rows("EURUSD=X", "1h", folder=tmp_path) is None            # no such file
    assert bridge.rows("BTC-USD", "15m", folder=tmp_path) is None            # a symbol the bridge does not carry
    assert bridge.rows("EURUSD=X", "15m", folder=tmp_path, now=at + bridge.MAX_AGE + 5) is None    # stopped writing
    late = at + 3600
    os.utime(tmp_path / "EURUSD_M15_recent.csv", (late, late))
    assert bridge.rows("EURUSD=X", "15m", folder=tmp_path, now=late) is None  # a fresh file of old bars


def test_fetch_or_prefers_the_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv("MT5_BRIDGE_DIR", str(tmp_path))
    fetch = bridge.fetch_or(lambda symbol, interval: [("yahoo",)])
    assert fetch("EURUSD=X", "15m") == [("yahoo",)]
    stamp = (dt.datetime.now(UTC).astimezone(bridge.NEW_YORK) + bridge.SERVER_AHEAD_OF_NEW_YORK).strftime("%Y.%m.%d %H:%M")
    (tmp_path / "EURUSD_M15_recent.csv").write_text(HEAD + f"{stamp},1.12,1.121,1.119,1.1205,340,2\n")
    assert fetch("EURUSD=X", "15m")[0][1:] == (1.12, 1.121, 1.119, 1.1205)


def test_the_history_file_gives_bars_and_spreads(tmp_path):
    (tmp_path / "XAUUSD_H1_history.csv").write_text(
        HEAD + "2026.10.09 10:00,4200.10,4210.00,4195.50,4205.00,900,15\n2026.10.09 11:00,4205.00,4207.00,4201.00,4202.00,800,12\n")
    bars = bridge.history("XAUUSD", "H1", folder=tmp_path)
    assert [b.close for b in bars] == [4205.0, 4202.0] and bars[0].ts == dt.datetime(2026, 10, 9, 7, 0, tzinfo=UTC)
    assert bridge.spread_points("XAUUSD", "H1", folder=tmp_path) == [15, 12]
    assert bridge.history("EURUSD", "H1", folder=tmp_path) == []


def test_the_account_positions_and_closed_trades(tmp_path):
    import telegram_notify as tn
    (tmp_path / "account.csv").write_text(
        "balance,equity,margin,free_margin,currency,leverage,server_time\n1000.00,1012.40,37.30,975.10,EUR,30,2026.10.12 19:00:00\n")
    (tmp_path / "positions.csv").write_text(
        "ticket,time,symbol,side,volume,price_open,sl,tp,profit,swap\n"
        "1,2026.10.12 11:30:00,EURUSD,sell,0.05,1.11932,1.12076,1.11499,12.90,-0.50\n")
    (tmp_path / "deals.csv").write_text(
        "ticket,time,symbol,side,entry,volume,price,profit,commission,swap,position\n"
        "10,2026.10.09 10:00:00,XAUUSD,buy,in,0.01,4200.00,0.00,-0.04,0.00,7\n"
        "11,2026.10.09 15:00:00,XAUUSD,sell,out,0.01,4210.00,8.60,-0.04,-0.10,7\n"
        "12,2026.08.01 10:00:00,EURUSD,sell,in,0.02,1.16000,0.00,-0.07,0.00,8\n"
        "13,2026.08.02 10:00:00,EURUSD,buy,out,0.02,1.16500,-9.00,-0.07,0.00,8\n")
    acc = bridge.account(tmp_path)
    assert (acc["balance"], acc["equity"], acc["currency"]) == (1000.0, 1012.4, "EUR")
    [pos] = bridge.open_positions(tmp_path)
    assert (pos["symbol"], pos["side"], pos["volume"], pos["profit"]) == ("EURUSD", "sell", 0.05, pytest.approx(12.4))
    closed = bridge.closed_trades(tmp_path)
    assert [(c["symbol"], c["side"], round(c["net"], 2)) for c in closed] == [("XAUUSD", "buy", 8.42), ("EURUSD", "sell", -9.14)]
    now = dt.datetime(2026, 10, 12, 16, 0, tzinfo=UTC)
    assert tn.format_mt5(acc, [pos], closed, now, html=False).splitlines() == [
        "MT5 EUR 1,000.00 · equity 1,012.40", "Open",
        "EURUSD sell 0.05  1.11932  SL 1.12076  TP 1.11499  +12.40",
        "Closed  7d  +8.42  1 trades, 1 won", "Closed 30d  +8.42  1 trades, 1 won"]
    assert "no account data" in tn.format_mt5(None, [], [], now, html=False)
    assert bridge.account(tmp_path / "nowhere") is None and bridge.closed_trades(tmp_path / "nowhere") == []


def test_the_measured_cost_is_the_median_day_spread_and_the_commission(tmp_path):
    (tmp_path / "quotes.csv").write_text(
        "symbol,bid,ask,point,server_time,gmt_time\nEURUSD,1.11707,1.11708,0.00001,2026.10.12 17:17:52,2026.10.12 14:17:52\n")
    rows = "".join(f"2026.10.{d:02d} {h:02d}:{m:02d},1.12,1.121,1.119,1.12,300,{20 if 9 <= h < 18 else 90}\n"
                   for d in (5, 6, 7, 8, 9) for h in range(24) for m in (0, 15, 30, 45))
    (tmp_path / "EURUSD_M15_history.csv").write_text(HEAD + rows)
    cost = bridge.measured_cost("EURUSD", tmp_path)
    assert cost["spread"] == pytest.approx(0.0002) and cost["commission"] == pytest.approx(0.00007)
    assert cost["round_trip"] == pytest.approx(0.00027) and cost["live"] == pytest.approx(0.00001)
    assert bridge.measured_cost("XAUUSD", tmp_path) is None
    assert cost["floor"] is True


def test_live_samples_replace_the_bars_once_there_are_enough(tmp_path, conn):
    (tmp_path / "quotes.csv").write_text(
        "symbol,bid,ask,point,server_time,gmt_time\nEURUSD,1.11700,1.11712,0.00001,x,y\nXAUUSD,0,0,0.01,x,y\n")
    at = dt.datetime(2026, 10, 12, 9, 0, tzinfo=UTC).timestamp()
    os.utime(tmp_path / "quotes.csv", (at, at))
    assert bridge.sample_spreads(conn, tmp_path, now=at) == 1                 # gold has no quote: not stored
    assert bridge.sample_spreads(conn, tmp_path, now=at + 3600) == 0          # the quotes file is an hour old
    for k in range(1, 25):
        conn.execute("INSERT INTO mt5_spreads VALUES (?,?,?)",
                     (dt.datetime(2026, 10, 13, 6 + k % 9, k, tzinfo=UTC).isoformat(), "EURUSD", 0.00010))
    cost = bridge.measured_cost("EURUSD", tmp_path, conn=conn)
    assert cost["floor"] is False and cost["spread"] == pytest.approx(0.0001)
    assert cost["round_trip"] == pytest.approx(0.00017) and cost["live"] == pytest.approx(0.00012)
