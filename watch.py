"""watch.py: a level an analysis said to wait for, watched until the price closes beyond it.

An analysis whose conclusion is «wait for a close above 1.43» carries that level as a row of its block,
«Wait   close above 1.43» (analyst_method.txt). When such an answer is delivered, the level is kept here;
the daily run then reads each watched asset's last completed daily close, and when it is beyond the level
it says so and has the analyst write a fresh analysis -- which may set the next level, and so the watch
goes on by itself. A level not reached in EXPIRE_DAYS days is dropped.

One level per asset: a new analysis replaces the old one (and an analysis with no «Wait» row ends the
watch -- its conclusion no longer hinges on a level). The bot places no orders; this only decides when
to look again.
"""
from __future__ import annotations

import datetime as dt
import re
import sys
from dataclasses import dataclass

EXPIRE_DAYS = 60
MAX_ACTIVE = 20                 # each level reached costs one analysis: a bound on what a run can start
ABOVE, BELOW = "above", "below"
_WAIT = re.compile(r"^\s*Wait\s+close\s+(above|below)\s+(\d[\d ]*(?:[.,]\d+)?)\s*(?:</pre>)?\s*$", re.I | re.M)


@dataclass(frozen=True)
class Level:
    id: int
    ticker: str                 # the asset as the analysis queue names it: CRYPTO:XRP, $NVDA, EQNR.OL
    direction: str              # above | below
    level: float
    created: str                # ISO date the analysis was delivered


def parse(answer: str) -> tuple[str, float] | None:
    """(direction, level) of an answer's «Wait   close above 1.43» row; None when it has none."""
    m = _WAIT.search(answer or "")
    if not m:
        return None
    try:
        level = float(m.group(2).replace(" ", "").replace(",", "."))
    except ValueError:
        return None
    return (m.group(1).lower(), level) if level > 0 else None


def active(conn) -> list[Level]:
    return [Level(*row) for row in conn.execute(
        "SELECT id, ticker, direction, level, created FROM watch_levels WHERE status = 'active' ORDER BY id")]


def _end(conn, ticker: str, status: str) -> int:
    cur = conn.execute("UPDATE watch_levels SET status = ? WHERE ticker = ? AND status = 'active'",
                       (status, ticker))
    return cur.rowcount


def record(conn, ticker: str, answer: str, today: dt.date | None = None) -> Level | None:
    """Keep what a delivered analysis of `ticker` says to wait for: its level replaces the asset's
    earlier one; an answer with no «Wait» row ends the watch. Returns the level now watched, if any.
    With MAX_ACTIVE assets watched already, a new asset is not added (said in the log)."""
    found = parse(answer)
    watched = {lv.ticker for lv in active(conn)}
    _end(conn, ticker, "replaced")
    if found is None:
        conn.commit()
        return None
    if ticker not in watched and len(watched) >= MAX_ACTIVE:
        conn.commit()
        print(f"[watch] {ticker}: not watched, {MAX_ACTIVE} levels are watched already", file=sys.stderr)
        return None
    direction, level = found
    cur = conn.execute("INSERT INTO watch_levels (ticker, direction, level, created, status) VALUES (?,?,?,?,'active')",
                       (ticker, direction, level, (today or dt.date.today()).isoformat()))
    conn.commit()
    return Level(cur.lastrowid, ticker, direction, level, (today or dt.date.today()).isoformat())


def cancel(conn, name: str) -> bool:
    """Stop watching the asset the user names (XRP, NVDA, CRYPTO:XRP, $NVDA). True when there was one."""
    wanted = _plain(name)
    ended = sum(_end(conn, lv.ticker, "cancelled") for lv in active(conn) if _plain(lv.ticker) == wanted)
    conn.commit()
    return ended > 0


def _plain(ticker: str) -> str:
    return ticker.upper().removeprefix("$").removeprefix("CRYPTO:")


def last_close(conn, ticker: str, today: dt.date, closes_fn=None) -> tuple[str, float] | None:
    """(date, close) of the asset's last completed daily bar, or None."""
    import positions
    key = ticker.removeprefix("$")
    listed = positions.split_venue(key)
    name, source = listed if listed else (key, positions.position_source(conn, key))
    bars = positions._completed_bars((closes_fn or positions.daily_closes)(name, source), today)
    return bars[-1] if bars else None


def reached(level: Level, bar: tuple[str, float] | None) -> bool:
    """Whether a completed bar dated after the analysis closed beyond the level."""
    if bar is None or bar[0] <= level.created:
        return False
    return bar[1] > level.level if level.direction == ABOVE else bar[1] < level.level


def run(conn, *, today: dt.date | None = None, closes_fn=None, send=None, analyse=None) -> int:
    """The daily pass: drop the levels older than EXPIRE_DAYS; for each one reached, say so, mark it and
    queue a fresh analysis of the asset; then have the analyst answer the queue (it sends each analysis
    itself, and records the next level). One asset failing is logged and the others go on. Returns the
    number of levels reached."""
    import db
    import telegram_notify
    today = today or dt.date.today()
    send = send or telegram_notify.send_text
    cutoff = (today - dt.timedelta(days=EXPIRE_DAYS)).isoformat()
    conn.execute("UPDATE watch_levels SET status = 'expired' WHERE status = 'active' AND created < ?", (cutoff,))
    conn.commit()
    fired = 0
    for level in active(conn):
        try:
            bar = last_close(conn, level.ticker, today, closes_fn)
            if not reached(level, bar):
                continue
            if not send(telegram_notify.format_watch_reached(level, bar[1])):
                continue                            # not said: the next run tries again
            conn.execute("UPDATE watch_levels SET status = 'reached', fired_at = ?, fired_close = ? WHERE id = ?",
                         (today.isoformat(), bar[1], level.id))
            conn.commit()
            db.enqueue_analysis(conn, level.ticker)
            fired += 1
        except Exception as e:
            print(f"[watch] {level.ticker}: {type(e).__name__}: {e}", file=sys.stderr)
    if fired:
        try:
            if analyse is None:
                import analyst
                analyse = analyst.process_queue
            analyse(conn)
        except Exception as e:
            print(f"[watch] the fresh analyses did not run: {type(e).__name__}: {e}", file=sys.stderr)
    return fired


INTRADAY_MARGIN = 0.01          # during the day a level counts as broken when the price is this far beyond it


def intraday(conn, *, price_fn=None, send=None) -> int:
    """Between the daily runs (the Telegram agent, every quarter of an hour): a level the price is now
    clearly beyond -- by INTRADAY_MARGIN or more -- is told once, «🔔 XRP above 1.43 now (1.45)». It is a
    note, not a verdict: the fresh analysis still comes with the daily close (run), since a price that
    goes through a level during the day often comes back. Returns the number of notes sent."""
    import positions
    import telegram_notify
    send = send or telegram_notify.send_text
    told = {r[0] for r in conn.execute("SELECT id FROM watch_levels WHERE intraday_at IS NOT NULL")}
    sent = 0
    for level in active(conn):
        if level.id in told:
            continue
        try:
            key = level.ticker.removeprefix("$")
            listed = positions.split_venue(key)
            name, source = listed if listed else (key, positions.position_source(conn, key))
            price = (price_fn or positions.last_close)(name, source)
            if not price:
                continue
            beyond = (price >= level.level * (1 + INTRADAY_MARGIN) if level.direction == ABOVE
                      else price <= level.level * (1 - INTRADAY_MARGIN))
            if beyond and send(telegram_notify.format_watch_intraday(level, price)):
                conn.execute("UPDATE watch_levels SET intraday_at = ? WHERE id = ?",
                             (dt.datetime.now().isoformat(timespec="minutes"), level.id))
                conn.commit()
                sent += 1
        except Exception as e:
            print(f"[watch] {level.ticker}: intraday: {type(e).__name__}: {e}", file=sys.stderr)
    return sent


USAGE = ("/watch — уровни, которых бот ждёт после разборов: при закрытии за уровнем придёт свежий разбор\n"
         "/watch off XRP — перестать ждать по этому активу")


def handle_command(conn, text: str) -> str:
    import telegram_notify
    args = text.split()[1:]
    if not args:
        return telegram_notify.format_watch_list(active(conn))
    if len(args) == 2 and args[0].lower() == "off":
        return (f"{_plain(args[1])}: больше не жду." if cancel(conn, args[1])
                else f"{_plain(args[1])}: уровня и не было.")
    return USAGE
