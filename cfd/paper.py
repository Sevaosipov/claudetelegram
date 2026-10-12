"""cfd/paper.py: every paper test on one board, and the rule that retires an idea.

The bot runs several paper tests of CFD rules -- the daily league's ideas, the day league, the SK sequence --
and the live crypto breakout signals. `board` puts their results in one message (/paper, and every Monday).
`retire` stops an idea making new trades once its closed trades sum to STOP_R or worse: at that depth a
recovery within the test is implausible, and the answer is already known. A retired idea's open trades run to
their end; nothing is deleted."""
from __future__ import annotations

import datetime as dt

import db

STOP_R = -10.0
_FOREVER = 100 * 365 * 24 * 3600
STOPPED_KEY = "paper_stopped_{name}"
WEEK_KEY = "paper_board_{week}"


def stopped(conn, name: str) -> bool:
    return bool(db.get_cached_value(conn, STOPPED_KEY.format(name=name), _FOREVER))


def rows(conn, today: dt.date) -> list[tuple[str, str, float, int, int]]:
    """(group, name, sum R, closed, open) of every paper test, in the order shown."""
    from cfd import intraday, league, live, sk_live
    import telegram_notify
    out = []
    for s in league.scoreboard(conn, today).scores:
        out.append(("League", telegram_notify.LEAGUE_NAMES.get(s.idea, s.idea), s.net, s.closed, s.open))
    if db.get_cached_value(conn, intraday.START_KEY, _FOREVER) is not None:
        for s in intraday.scoreboard(conn, today).scores:
            if s.closed or s.open:
                out.append(("Day", intraday.IDEAS[s.idea], s.net, s.closed, s.open))
    sk = sk_live.trades(conn, "WHERE scored = 1")
    for pair in sk_live.MARKETS:
        mine = [t for t in sk if t.pair == pair]
        out.append(("SK", pair, sum(t.result_r for t in mine if t.status == "closed"),
                    sum(1 for t in mine if t.status == "closed"), sum(1 for t in mine if t.status == "open")))
    done = live.closed_signals(conn)
    out.append(("Crypto", "Breakout", sum(s.result_r or 0.0 for s in done), len(done), len(live.open_signals(conn))))
    return out


def key_of(group: str, name: str) -> str:
    return f"{group}:{name}"


def retire(conn, today: dt.date, send=None) -> list[str]:
    """Stop every paper idea whose closed trades sum to STOP_R or worse; tell each once. Returns their keys.
    The crypto breakout is live signals, not a paper test: it is shown, never retired here."""
    out = []
    for group, name, net, closed, _open in rows(conn, today):
        key = key_of(group, name)
        if group == "Crypto" or net > STOP_R or stopped(conn, key):
            continue
        db.save_cached_value(conn, STOPPED_KEY.format(name=key), 1.0)
        out.append(key)
        if send:
            send(f"<pre>{group} · {name} stopped at {net:+.1f}R after {closed} trades</pre>")
    return out


def board(conn, today: dt.date, *, html: bool = True) -> str:
    import telegram_notify
    lines = [f"Paper tests {today:%d.%m}", f"{'':<19}{'R':>6} {'Cl':>3} {'Op':>2}"]
    last = None
    for group, name, net, closed, open_ in rows(conn, today):
        label = f"{group if group != last else '':<7}{name:<12}"
        last = group
        mark = " stopped" if stopped(conn, key_of(group, name)) else ""
        lines.append(f"{label}{net:>+6.2f} {closed:>3} {open_:>2}{mark}")
    text = "\n".join(lines)
    return f"<pre>{telegram_notify._esc(text)}</pre>" if html else text


def weekly(conn, today: dt.date, send) -> bool:
    """The board once a week, at the first pass from Monday on. True when it was sent."""
    iso = today.isocalendar()
    key = WEEK_KEY.format(week=f"{iso.year}-W{iso.week:02d}")
    if db.get_cached_value(conn, key, _FOREVER):
        return False
    if send(board(conn, today)):
        db.save_cached_value(conn, key, 1.0)
        return True
    return False
