"""weekly.py: the week's Telegram messages -- one short message per signal, then a short summary of the
user's own Trading 212 account (spec 2026-10-04-remove-model-portfolio.md, which builds on the style spec
2026-10-04-signal-message-style.md).

The bot holds no portfolio. What it sends once a week is:

  * each buy signal it picked (signals_weekly.pick_buys; buy_text words one), in the order picked;
  * each group of insiders that bought a ticker together and started selling it this week (week_exits);
  * last, the summary (format_summary): the account, what is held and how the held names stand, and how
    many signals the week had. The summary is made from the database alone -- no network call.

week_signals gives the signals as (key, html text); the key names a signal for good, so that bot._send_weekly
can tell which went out already when a send fails half-way. The week is today - 6 days ... today.
"""
from __future__ import annotations

import datetime as dt
import json

import crypto
import positions
import t212_account
import telegram_notify
from telegram_notify import DOT_GREEN, DOT_RED, signed_pct

WEEK_DAYS = 6               # the week is today - 6 days ... today
MAX_REASONS = 2             # a buy names this many of its reasons
MAX_SELLERS = 5             # a group exit names this many sellers, then «и ещё N»
BEST_WORST = 3              # the summary names this many best and this many worst positions
SCORING_FAILED_WARNING = "⚠️ Оценка сигналов на этой неделе не отработала — покупок не было."


# ----------------------------------------------------------------- the signals
def buy_text(pick: dict) -> str:
    """«🟢 GME!: покупка — 2 инсайдера из руководства; CEO среди покупателей; балл 70, стоп −10%»: a picked
    buy (signals_weekly.pick_record) -- its first two reasons, its score and its stop, and «нет на
    Trading 212» when the score says the broker does not list it. A coin is named by its symbol. What the
    pick does not have is left out. There is no money in it: the bot holds nothing."""
    reasons = [r for r in (pick.get("reasons") or []) if r][:MAX_REASONS]
    score = pick.get("score")
    head = reasons + ([f"балл {score:.0f}"] if score is not None else [])
    tail = []
    if pick.get("stop_pct"):
        tail.append(f"стоп −{pick['stop_pct'] * 100:.0f}%")
    if pick.get("t212") is False:
        tail.append("нет на Trading 212")
    details = ", ".join((["; ".join(head)] if head else []) + tail)
    return telegram_notify.signal_line(DOT_GREEN, crypto.symbol_of(pick["ticker"]), "покупка", details or None)


def week_exits(conn, start: str, end: str) -> list[tuple[int, str, list[str]]]:
    """The group exits journaled from `start` to `end` (ISO dates, local time): one per ticker, its latest row,
    as (journal id, ticker, who sold), by journal id."""
    latest: dict[str, tuple[int, str | None]] = {}
    for id_, ticker, members in conn.execute(
            "SELECT id, ticker, members FROM signal_journal WHERE kind = 'exit' "
            "AND date(emitted_at, 'localtime') BETWEEN ? AND ? ORDER BY id", (start, end)):
        latest[ticker] = (id_, members)
    rows = []
    for ticker, (id_, members) in latest.items():
        try:
            named = json.loads(members or "[]")
        except ValueError:
            named = []
        rows.append((id_, ticker, [str(n) for n in named] if isinstance(named, list) else []))
    return sorted(rows)


def exit_text(ticker: str, names: list[str]) -> str:
    """«🔴 XYZ!: продают те, кто покупал — Name A, Name B» (the first five, then «и ещё N»)."""
    who = ", ".join(names[:MAX_SELLERS])
    if len(names) > MAX_SELLERS:
        who += f" и ещё {len(names) - MAX_SELLERS}"
    return telegram_notify.signal_line(DOT_RED, crypto.symbol_of(ticker), "продают те, кто покупал", who or None)


def week_signals(conn, today: dt.date, picks: list[dict]) -> list[tuple[str, str]]:
    """The week's signals, one Telegram message each, as (key, html text) in the order they go out: the
    picked buys as given (best score first), then the group exits (by journal id). The key is
    `buy:<ticker>` or `exit:<journal id>` and names the signal for good. Nothing in a quiet week."""
    start = (today - dt.timedelta(days=WEEK_DAYS)).isoformat()
    signals = [(f"buy:{p['ticker']}", buy_text(p)) for p in picks]
    signals += [(f"exit:{id_}", exit_text(ticker, names))
                for id_, ticker, names in week_exits(conn, start, today.isoformat())]
    return signals


# ----------------------------------------------------------------- the summary
def _account_line(conn, today: dt.date) -> str | None:
    """«счёт Trading 212 €12 346 (за неделю +2,9%)»: the account's value from the newest snapshot the Trading
    212 sync stored on or before `today`, against the newest one on or before a week earlier (left out when
    there is none). A snapshot more than t212_account.STALE_DAYS older than the day it stands for is not used:
    with no fresh value the sync has not got through and there is no line, and with no fresh one a week ago
    no week change. None with no value."""
    stale = t212_account.STALE_DAYS
    now = t212_account.account_value(conn, today, max_age_days=stale)
    if now is None:
        return None
    value, currency = now
    line = f"счёт Trading 212 {telegram_notify.money(value, currency)}"
    before = t212_account.account_value(conn, today - dt.timedelta(days=7), max_age_days=stale)
    if before and before[0]:
        line += f" (за неделю {signed_pct(value / before[0] - 1)})"
    return line


def _result(conn, pos: positions.Position, today: dt.date) -> float | None:
    """How a position stands since it was bought: the last price the Trading 212 sync stored for it against
    its entry price. The stored data only, no network call; None for a position with no fresh stored price
    (positions.t212_price) -- and for a /bought one, which the sync stores nothing for (the prices left
    under its ticker by a holding since sold are not its own)."""
    if pos.origin != positions.T212 or not pos.entry_price:
        return None
    price = positions.t212_price(conn, pos.ticker, today)
    return None if price is None else price / pos.entry_price - 1


def _positions_line(conn, today: dt.date) -> str:
    """«Позиций 43: лучшие — SMCI +31,0%, BBD +12,4%, GME +8,1%; худшие — XYZ −22,5%, ABC −9,0%, DEF −4,2%»:
    how many positions are open (the Trading 212 account's and /bought ones) and the three best and three
    worst of those with a result (_result), the best first and the worst first. With fewer than six of them
    all are listed once, the best first. «Позиций нет.» with none open."""
    held = positions.open_positions(conn)
    if not held:
        return "Позиций нет."
    priced = sorted(((positions.display_name(pos), result) for pos in held
                     if (result := _result(conn, pos, today)) is not None),
                    key=lambda row: (-row[1], row[0]))

    def shown(rows) -> str:
        return ", ".join(f"{name} {signed_pct(result)}" for name, result in rows)
    head = f"Позиций {len(held)}"
    if not priced:
        return f"{head}: цен пока нет"
    if len(priced) < 2 * BEST_WORST:
        return f"{head}: {shown(priced)}"
    return f"{head}: лучшие — {shown(priced[:BEST_WORST])}; худшие — {shown(priced[:-BEST_WORST - 1:-1])}"


def _signals_line(conn, today: dt.date) -> str:
    """«Сигналов за неделю: покупок 2, на продажу 1, групповых выходов 1» -- the buy signals sent (buy_signals
    rows), the sell alerts (positions whose close_alerted_at is in the week) and the group exits journaled
    (week_exits). «Сигналов за неделю не было.» when all three are none."""
    start, end = (today - dt.timedelta(days=WEEK_DAYS)).isoformat(), today.isoformat()
    buys = conn.execute("SELECT COUNT(*) FROM buy_signals WHERE sent_at BETWEEN ? AND ?", (start, end)).fetchone()[0]
    sells = conn.execute("SELECT COUNT(*) FROM positions WHERE close_alerted_at BETWEEN ? AND ?",
                         (start, end)).fetchone()[0]
    exits = len(week_exits(conn, start, end))
    if not (buys or sells or exits):
        return "Сигналов за неделю не было."
    return f"Сигналов за неделю: покупок {buys}, на продажу {sells}, групповых выходов {exits}"


def format_summary(conn, today: dt.date, *, html: bool = True, scoring_failed: bool = False) -> str:
    """The weekly summary, sent last: one short message about the user's own account, a line each

        📊 Неделя 03.10–09.10: счёт Trading 212 €12 346 (за неделю +2,9%)
        Позиций 43: лучшие — SMCI +31,0%, BBD +12,4%, GME +8,1%; худшие — XYZ −22,5%, ABC −9,0%, DEF −4,2%
        Сигналов за неделю: покупок 2, на продажу 1, групповых выходов 1
        ⚠️ Оценка сигналов на этой неделе не отработала — покупок не было.

    -- the account (_account_line; with no fresh Trading 212 data the line is the title alone), the positions
    (_positions_line), the week's signals (_signals_line) and, with `scoring_failed` -- the week's scoring
    never got through (the Sunday message goes out regardless) -- the warning. Always a message, however quiet
    the week. Made from the database alone: no call to Trading 212, Yahoo or Telegram."""
    first = today - dt.timedelta(days=WEEK_DAYS)
    title = telegram_notify._b(f"Неделя {first:%d.%m}–{today:%d.%m}", html)
    account = _account_line(conn, today)
    lines = [f"📊 {title}" + (f": {telegram_notify._e(account, html)}" if account else ""),
             telegram_notify._e(_positions_line(conn, today), html),
             telegram_notify._e(_signals_line(conn, today), html)]
    if scoring_failed:
        lines.append(telegram_notify._e(SCORING_FAILED_WARNING, html))
    return "\n".join(lines)
