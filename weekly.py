"""weekly.py: the week's Telegram messages -- one short message per signal, then a short summary of the
user's own Trading 212 account (spec 2026-10-04-remove-model-portfolio.md, which builds on the style spec
2026-10-04-signal-message-style.md).

The bot holds no portfolio. What it sends once a week is:

  * each buy signal it picked (signals_weekly.pick_buys; buy_text words one), in the order picked -- the
    stocks by score first, then the coins;
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
RISK_TAG = "High risk"      # a line of an alt's buy block
MAX_SELLERS = 5             # a group exit names this many sellers, then «и ещё N»
BEST_WORST = 3              # the summary names this many best and this many worst positions
LEFT_OUT_KEY = "weekly_left_out_{week}"     # kv: [{"ticker", "why"}] the week's pick left out (bot._pick_week)
SCORING_FAILED_WARNING = "⚠️ Оценка сигналов на этой неделе не отработала — покупок не было."


# ----------------------------------------------------------------- the signals
def _plain(x: float) -> str:
    """A price as a trading terminal writes it: a point, no spaces; two decimals from 1, more below."""
    return f"{x:.{2 if x >= 1 else 4 if x >= 0.01 else 6}f}"


def buy_text(pick: dict) -> str:
    """A picked buy (signals_weekly.pick_record), bare: a fixed-width block with nothing but the trade --

        RXO Buy
        Price 15.20
        Stop  13.68
        Size  €22

    (the last close, the stop that far below it, the euros to buy: signal_context) -- and under it why it was
    picked (its first two reasons and its score) and, for a company, what it is and how it is valued. A coin
    is named by its symbol. Without a price the stop is its percent. «Not on Trading 212» and, for an alt,
    «High risk» are lines of the block: both decide whether the trade can or should be placed; so is
    «Merger pending», for a company that is a party to a merger without being its target. The last
    line, «Claude: …», is the analyst's read of the chart (signal_context.refresh). What the pick does not
    have is left out."""
    rows = []
    price, stop_pct = pick.get("price"), pick.get("stop_pct")
    if price:
        rows.append(("Price", _plain(price)))
    if stop_pct:
        rows.append(("Stop", _plain(price * (1 - stop_pct)) if price else f"-{stop_pct * 100:.0f}%"))
    if pick.get("amount_eur"):
        rows.append(("Size", f"€{pick['amount_eur']:.0f}"))
    block = [f"{crypto.symbol_of(pick['ticker'])} Buy"] + [f"{name:<6}{value}" for name, value in rows]
    if pick.get("t212") is False:
        block.append("Not on Trading 212")
    if pick.get("risk"):
        block.append(RISK_TAG)
    if pick.get("deal"):
        block.append("Merger pending")
    lines = [f"<pre>{telegram_notify._esc(chr(10).join(block))}</pre>"]
    reasons = [r for r in (pick.get("reasons") or []) if r][:MAX_REASONS]
    score = pick.get("score")
    why = "; ".join(reasons + ([f"балл {score:.0f}"] if score is not None else []))
    if why:
        lines.append(telegram_notify._esc(why))
    if pick.get("about"):
        lines.append(telegram_notify._esc(pick["about"]))
    if pick.get("claude"):
        lines.append(telegram_notify._esc(f"Claude: {pick['claude']}"))
    return "\n".join(lines)


def facts(pick: dict) -> str:
    """What the bot knows of a pick, as plain lines for the analyst's chart check (analyst.signal_note)."""
    lines = [f"Тикер: {pick['ticker']}" + (f" ({pick['company']})" if pick.get("company") else "")]
    if pick.get("reasons"):
        lines.append("Причины сигнала: " + "; ".join(r for r in pick["reasons"] if r))
    if pick.get("score") is not None:
        lines.append(f"Балл: {pick['score']:.0f} (покупка от 60)")
    if pick.get("price"):
        lines.append(f"Цена: {_plain(pick['price'])}")
    if pick.get("stop_pct"):
        lines.append(f"Стоп: −{pick['stop_pct'] * 100:.0f}% от максимума после покупки")
    if pick.get("about"):
        lines.append(f"Компания: {pick['about']}")
    return "\n".join(lines)


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
    picked buys as given (the stocks by score, then the coins: signals_weekly.pick_buys), then the group
    exits (by journal id). The key is
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


def _left_out_line(conn, today: dt.date) -> str | None:
    """«Не вошли в сигналы: RXO, SSTI — идёт выкуп компании»: the buys the week's pick left out because the
    company is being bought out (takeover.py); None when there are none."""
    import db
    iso = today.isocalendar()
    kept = db.get_cached_json(conn, LEFT_OUT_KEY.format(week=f"{iso.year}-W{iso.week:02d}"))
    names = [d["ticker"] for d in kept if isinstance(d, dict) and d.get("ticker")] if isinstance(kept, list) else []
    return f"Не вошли в сигналы: {', '.join(names)} — идёт выкуп компании" if names else None


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
    left_out = _left_out_line(conn, today)
    if left_out:
        lines.append(telegram_notify._e(left_out, html))
    if scoring_failed:
        lines.append(telegram_notify._e(SCORING_FAILED_WARNING, html))
    return "\n".join(lines)
