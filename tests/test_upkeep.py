"""upkeep.py: the dated copy of the database, the alerts said once, the sign-ins, a buyout of a holding;
and the three small additions to a buy signal and the watch that came with it."""
from __future__ import annotations

import datetime as dt
import sqlite3
import types

import pytest

import analyst
import bot
import db
import positions
import signal_context as sc
import takeover
import telegram_notify as tn
import upkeep
import watch
import weekly

TODAY = dt.date(2026, 10, 12)


# ------------------------------------------------------------------ the backup
def _database(tmp_path):
    path = tmp_path / "disclosures.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE t (x)")
    c.execute("INSERT INTO t VALUES (42)")
    c.commit()
    c.close()
    return path


def test_the_backup_is_a_dated_whole_copy_made_once_a_day(tmp_path):
    path = _database(tmp_path)
    copy = upkeep.backup(path, TODAY)
    assert copy == tmp_path / "backups" / "disclosures-2026-10-12.db"
    assert sqlite3.connect(copy).execute("SELECT x FROM t").fetchone() == (42,)
    stamp = copy.stat().st_mtime_ns
    assert upkeep.backup(path, TODAY) == copy and copy.stat().st_mtime_ns == stamp      # not copied again
    assert not list((tmp_path / "backups").glob("*.tmp"))


def test_only_the_newest_copies_are_kept_and_no_database_is_no_copy(tmp_path):
    path = _database(tmp_path)
    for d in range(5):
        upkeep.backup(path, TODAY + dt.timedelta(days=d), keep=3)
    assert sorted(p.name for p in (tmp_path / "backups").iterdir()) == [
        "disclosures-2026-10-14.db", "disclosures-2026-10-15.db", "disclosures-2026-10-16.db"]
    assert upkeep.backup(tmp_path / "nope.db", TODAY) is None


# ------------------------------------------------------------------ said once
def test_an_alert_goes_out_once_in_its_days_and_a_failed_send_is_tried_again(conn):
    sent = []
    assert upkeep.alert_once(conn, "k", "text", send=lambda t: False) is False
    assert upkeep.alert_once(conn, "k", "text", send=lambda t: sent.append(t) or True) is True
    assert upkeep.alert_once(conn, "k", "text", send=lambda t: sent.append(t) or True) is False
    assert upkeep.alert_once(conn, "other", "text", send=lambda t: sent.append(t) or True) is True
    assert sent == ["text", "text"]


# ------------------------------------------------------------------ the sign-ins
def _cli(out):
    return lambda *a, **k: types.SimpleNamespace(stdout=out, stderr="")


def test_a_lapsed_tradingview_sign_in_is_told_with_the_command_to_renew_it(conn):
    sent = []
    send = lambda t: sent.append(t) or True                                    # noqa: E731
    assert upkeep.tradingview_login(conn, run=_cli("tv:\n  Status: ✔ Connected"), send=send) is True
    assert upkeep.tradingview_login(conn, run=_cli("tv:\n  Status: ! Needs authentication"), send=send) is False
    assert sent == [upkeep.TV_LOGIN_ALERT] and "claude mcp login tv" in sent[0]
    upkeep.tradingview_login(conn, run=_cli("Status: ! Needs authentication"), send=send)
    assert len(sent) == 1                                                       # not every day


def test_a_check_that_cannot_run_says_nothing(conn, capsys):
    def boom(*a, **k):
        raise FileNotFoundError("claude")
    assert upkeep.tradingview_login(conn, run=boom, send=lambda t: 1 / 0) is None
    assert upkeep.tradingview_login(conn, run=_cli("No MCP server found"), send=lambda t: 1 / 0) is None


@pytest.mark.parametrize("text, failed", [("Failed to authenticate. API Error: 401 Invalid bearer token", True),
                                          ("Please run /login", True), ("usage limit reached", False), ("", False)])
def test_a_failed_claude_sign_in_is_recognised(conn, text, failed):
    sent = []
    assert upkeep.claude_login_failed(conn, text, send=lambda t: sent.append(t) or True) is failed
    assert sent == ([upkeep.CLAUDE_LOGIN_ALERT] if failed else [])


def test_the_analyst_tells_a_failed_sign_in(conn, monkeypatch):
    seen = []
    monkeypatch.setattr(upkeep, "claude_login_failed", lambda c, text: seen.append(text))
    monkeypatch.setattr(analyst, "context", lambda conn, ticker: "x")
    db.enqueue_analysis(conn, "NVDA")
    proc = types.SimpleNamespace(returncode=1, stdout="Failed to authenticate. API Error: 401", stderr="")
    analyst.process_queue(conn, run=lambda *a, **k: proc, send=lambda t: True)
    assert seen and "Failed to authenticate" in seen[0]


# ------------------------------------------------------------------ a buyout of something held
def test_a_holding_under_offer_is_told_once_and_looked_up_once_a_week(conn):
    positions.open_position(conn, "RXO", 28.0, closes_fn=lambda t, s: [])
    positions.open_position(conn, "AAPL", 250.0, closes_fn=lambda t, s: [])
    asked, sent = [], []

    def check(ticker, source, today):
        asked.append(ticker)
        return takeover.Finding(takeover.TARGET, "идёт выкуп компании (форма 425 ×21)") if ticker == "RXO" else None
    send = lambda t: sent.append(t) or True                                     # noqa: E731
    assert upkeep.takeover_alerts(conn, TODAY, check=check, send=send) == ["RXO"]
    assert sent == ["<pre>⚠️ RXO · takeover pending</pre>\n"
                    "Идёт выкуп компании (форма 425 ×21): цена привязана к оферте, риск — срыв сделки."]
    assert upkeep.takeover_alerts(conn, TODAY, check=check, send=send) == [] and asked == ["RXO", "AAPL"]


def test_a_party_to_a_merger_that_is_not_the_target_is_not_an_alert(conn):
    positions.open_position(conn, "CHRW", 100.0, closes_fn=lambda t, s: [])
    deal = lambda t, s, d: takeover.Finding(takeover.DEAL, "компания участвует в слиянии")     # noqa: E731
    assert upkeep.takeover_alerts(conn, TODAY, check=deal, send=lambda t: 1 / 0) == []


# ------------------------------------------------------------------ the earnings date on a signal
def test_a_report_within_thirty_days_is_a_row_of_the_block():
    cal = lambda s: {"Earnings Date": [dt.date(2026, 10, 20), dt.date(2026, 10, 22)]}       # noqa: E731
    assert sc.report_date("NVDA", "SEC", TODAY, calendar_fn=cal) == "2026-10-20"
    pick = {"ticker": "NVDA", "stop_pct": 0.1, "report": "2026-10-20", "chart": "График за — x"}
    assert weekly.buy_text(pick) == "<pre>NVDA Buy · Invest\nStop  -10%\nChart For\nEarn  20.10</pre>"


@pytest.mark.parametrize("cal", [lambda s: {"Earnings Date": [dt.date(2026, 12, 1)]}, lambda s: {},
                                 lambda s: {"Earnings Date": [dt.date(2026, 10, 1)]}, lambda s: 1 / 0])
def test_a_far_past_unknown_or_failed_date_is_no_row(cal):
    assert sc.report_date("NVDA", "SEC", TODAY, calendar_fn=cal) is None
    assert sc.report_date("CRYPTO:BTC", None, TODAY, calendar_fn=lambda s: 1 / 0) is None


# ------------------------------------------------------------------ the budget carried over
def test_a_week_with_no_signal_carries_its_budget_and_a_week_with_one_spends_it_all(conn):
    sc.handle_command(conn, "/size budget 30")
    assert sc.settle_week(conn, 0) == 30.0 and sc.settle_week(conn, 0) == 60.0
    assert sc.amount_eur(conn, 0.1, TODAY, share=0.5) == 45.0              # (30 + 60) shared out
    assert "Перенесено с недель без сигналов: €60." in sc.settings_text(conn)
    assert sc.settle_week(conn, 2) == 0.0 and sc.amount_eur(conn, 0.1, TODAY) == 30.0
    assert "Перенесено" not in sc.settings_text(conn)


def test_without_a_budget_nothing_is_carried(conn):
    assert sc.settle_week(conn, 0) == 0.0 and sc.carry(conn) == 0.0


def test_picking_the_week_settles_its_budget(conn, monkeypatch):
    sc.handle_command(conn, "/size budget 30")
    monkeypatch.setattr(bot.signals_weekly, "pick_buys", lambda *a, **k: [])
    bot._pick_week(conn, dt.date(2026, 10, 9), types.SimpleNamespace(scored=[]))
    assert sc.carry(conn) == 30.0


# ------------------------------------------------------------------ a level broken during the day
def test_a_level_clearly_broken_during_the_day_is_noted_once_and_stays_watched(conn):
    watch.record(conn, "CRYPTO:XRP", "Wait   close above 1.43", TODAY)
    sent = []
    send = lambda t: sent.append(t) or True                                     # noqa: E731
    assert watch.intraday(conn, price_fn=lambda t, s: 1.44, send=send) == 0     # under 1 % beyond: not yet
    assert watch.intraday(conn, price_fn=lambda t, s: 1.45, send=send) == 1
    assert sent == ["<pre>🔔 XRP above 1.43 now (1.45)</pre>"]
    assert watch.intraday(conn, price_fn=lambda t, s: 1.50, send=send) == 0     # said once
    assert len(watch.active(conn)) == 1                                         # the verdict waits for the close


def test_a_level_below_and_a_missing_price(conn):
    watch.record(conn, "$NVDA", "Wait   close below 222.08", TODAY)
    sent = []
    assert watch.intraday(conn, price_fn=lambda t, s: None, send=lambda t: sent.append(t) or True) == 0
    assert watch.intraday(conn, price_fn=lambda t, s: 219.0, send=lambda t: sent.append(t) or True) == 1
    assert sent == ["<pre>🔔 NVDA below 222.08 now (219)</pre>"]


# ------------------------------------------------------------------ the daily run
def test_the_housekeeping_runs_last_and_the_backup_even_without_telegram(conn, monkeypatch):
    done = []
    args = types.SimpleNamespace(no_telegram=False)
    monkeypatch.setattr(bot, "_filtered_run", lambda a: False)
    monkeypatch.setattr(bot.upkeep, "backup", lambda path: done.append("backup"))
    monkeypatch.setattr(bot.upkeep, "takeover_alerts", lambda c: done.append("takeover"))
    monkeypatch.setattr(bot.upkeep, "tradingview_login", lambda c: done.append("login"))
    monkeypatch.setattr(bot.signal_record, "monthly", lambda c: done.append("record"))
    bot._upkeep_pass(conn, args)
    assert done == ["backup", "takeover", "login", "record"]
    done.clear()
    bot._upkeep_pass(conn, types.SimpleNamespace(no_telegram=True))
    assert done == ["backup"]
    assert tn.format_takeover_alert("RXO", "идёт выкуп", html=False).startswith("⚠️ RXO · takeover pending\n")


# ------------------------------------------------------------------ the backup's second place
def test_the_days_copy_is_gzipped_into_the_second_place_and_only_the_newest_kept(tmp_path, monkeypatch):
    import gzip
    path = _database(tmp_path)
    away = tmp_path / "away"
    monkeypatch.setenv(upkeep.MIRROR_ENV, str(away))
    for d in range(4):
        upkeep.backup(path, TODAY + dt.timedelta(days=d))
    assert len(list(away.iterdir())) == 4
    newest = away / "disclosures-2026-10-15.db.gz"
    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.open(newest).read())
    assert sqlite3.connect(restored).execute("SELECT x FROM t").fetchone() == (42,)
    copy = tmp_path / "backups" / "disclosures-2026-10-15.db"
    assert upkeep.mirror(copy, folder=away, keep=2).name == "disclosures-2026-10-15.db.gz"
    assert sorted(p.name for p in away.iterdir()) == ["disclosures-2026-10-14.db.gz", "disclosures-2026-10-15.db.gz"]


def test_the_second_place_is_the_named_folder_else_icloud_drive_else_nowhere(tmp_path, monkeypatch):
    assert upkeep.mirror_dir() is None                                      # no cloud drive on this "Mac"
    cloud = tmp_path / "cloud"
    cloud.mkdir()
    monkeypatch.setattr(upkeep, "ICLOUD_DRIVE", cloud)
    assert upkeep.mirror_dir() == cloud / "disclosure-bot-backups"
    monkeypatch.setenv(upkeep.MIRROR_ENV, str(tmp_path / "usb"))
    assert upkeep.mirror_dir() == tmp_path / "usb"


def test_a_failing_second_copy_leaves_the_first(tmp_path, monkeypatch, capsys):
    path = _database(tmp_path)
    monkeypatch.setattr(upkeep, "mirror", lambda copy: 1 / 0)
    assert upkeep.backup(path, TODAY).exists() and "not copied off this disk" in capsys.readouterr().err

