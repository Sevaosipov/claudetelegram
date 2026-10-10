"""upkeep.py: what keeps the bot trustworthy between signals, run once a day after everything else.

  * backup(): a dated copy of the database (SQLite's own online backup), the last KEEP kept. Everything
    the bot knows -- positions, signals, the league, the watched levels -- is in that one file.
  * takeover_alerts(): the user's own holdings checked for a pending buyout (takeover.check), each told
    once: the price of a company under offer stands at the offer, and the bot's stop and trend rules say
    nothing about that.
  * tradingview_login(): whether the analyst's sign-in to TradingView's own server still holds, told
    when it does not (alert_once). The Claude sign-in is told from the analyst, when a run fails on it.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import db

KEEP = 14                       # dated copies kept
BACKUP_DIR = "backups"          # under the database's folder
ALERT_EVERY_DAYS = 3            # a standing problem is said again this often, not every day
TAKEOVER_RECHECK_DAYS = 7       # a holding is looked up at the SEC this often
_FOREVER = 100 * 365 * 24 * 3600
CLAUDE_LOGIN_ALERT = ("⚠️ Вход Claude истёк — аналитик не отвечает. В терминале: "
                      "~/.local/bin/claude setup-token, затем новый токен в .env "
                      "(CLAUDE_CODE_OAUTH_TOKEN=…) и перезапуск бота.")
TV_LOGIN_ALERT = ("⚠️ Вход TradingView истёк — аналитик работает без собственных данных TradingView. "
                  "В терминале: ~/.local/bin/claude mcp login tv")


# ------------------------------------------------------------------ the backup
def backup(db_path: Path | str, today: dt.date | None = None, *, keep: int = KEEP) -> Path | None:
    """A copy of the database dated `today` in <its folder>/backups, unless the day has one; then the
    oldest copies beyond `keep` are removed. Returns the day's copy, or None when there is no database."""
    db_path = Path(db_path)
    if not db_path.exists():
        return None
    folder = db_path.parent / BACKUP_DIR
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{db_path.stem}-{(today or dt.date.today()).isoformat()}.db"
    if not target.exists():
        tmp = target.with_suffix(".tmp")
        tmp.unlink(missing_ok=True)
        src, dst = sqlite3.connect(db_path), sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()
        tmp.replace(target)                         # a copy cut short never looks like a whole one
    for old in sorted(folder.glob(f"{db_path.stem}-*.db"))[:-keep]:
        old.unlink()
    return target


# ------------------------------------------------------------------ saying a thing once
def alert_once(conn, key: str, text: str, *, send=None, days: float = ALERT_EVERY_DAYS) -> bool:
    """Send `text` unless the same alert (`key`) went out in the last `days` days. True when sent."""
    stamp = f"alert_{key}"
    if db.get_cached_value(conn, stamp, days * 86400) is not None:
        return False
    if send is None:
        import telegram_notify
        send = telegram_notify.send_text
    if not send(text):
        return False
    db.save_cached_value(conn, stamp, time.time())
    return True


# ------------------------------------------------------------------ the sign-ins
def tradingview_login(conn, *, run=None, send=None) -> bool | None:
    """Whether the analyst is signed in to TradingView's own server: `claude mcp get tv` (no model is
    called). True, False (and the alert is sent, once in ALERT_EVERY_DAYS days), or None when it cannot be
    told -- no claude binary, no such server, a failure of the command."""
    import analyst
    try:
        proc = (run or subprocess.run)([str(analyst.CLAUDE_BIN), "mcp", "get", analyst.TV_OFFICIAL_NAME],
                                       capture_output=True, text=True, timeout=90, env=analyst.claude_env(),
                                       cwd=str(Path.home()))
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[upkeep] the TradingView sign-in was not checked: {type(e).__name__}", file=sys.stderr)
        return None
    out = (proc.stdout or "") + (proc.stderr or "")
    if "Needs authentication" in out:
        alert_once(conn, "tv_login", TV_LOGIN_ALERT, send=send)
        return False
    return True if "Connected" in out else None


def claude_login_failed(conn, text: str, *, send=None) -> bool:
    """Called by the analyst with what a failed run printed: when it is the sign-in that failed, the
    alert goes out (once in ALERT_EVERY_DAYS days). True when it was the sign-in."""
    low = (text or "").lower()
    if not any(mark in low for mark in ("failed to authenticate", "invalid bearer token", "401", "/login",
                                        "oauth token has expired")):
        return False
    alert_once(conn, "claude_login", CLAUDE_LOGIN_ALERT, send=send)
    return True


# ------------------------------------------------------------------ a buyout of something held
def takeover_alerts(conn, today: dt.date | None = None, *, check=None, send=None) -> list[str]:
    """Each open position of a company that files with the SEC, looked up once in TAKEOVER_RECHECK_DAYS
    days; a holding found to be a takeover target is told once, for good. Returns the tickers told."""
    import positions
    import takeover
    import telegram_notify
    today = today or dt.date.today()
    check = check or takeover.check
    send = send or telegram_notify.send_text
    told = []
    for pos in positions.open_positions(conn):
        said, looked = f"takeover_told_{pos.ticker}", f"takeover_checked_{pos.ticker}"
        if (db.get_cached_value(conn, said, _FOREVER) is not None
                or db.get_cached_value(conn, looked, TAKEOVER_RECHECK_DAYS * 86400) is not None):
            continue
        found = check(pos.ticker, pos.source, today)
        db.save_cached_value(conn, looked, time.time())
        if found is None or found.kind != takeover.TARGET:
            continue
        if send(telegram_notify.format_takeover_alert(positions.display_name(pos), found.text)):
            db.save_cached_value(conn, said, time.time())
            told.append(pos.ticker)
    return told
