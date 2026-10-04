"""What the model portfolio shows: its statistics against the 70/30 mix, the success line,
the menu/CLI text, the weekly Telegram signals and summary (week_signals, format_week_summary)
and the monthly report. Spec: docs/superpowers/specs/2026-09-30-model-portfolio-and-analyst-design.md,
section 6; for the weekly messages docs/superpowers/specs/2026-10-01-weekly-model-message.md and,
for their style (one short message per signal, a short summary), 2026-10-04-signal-message-style.md.
The paper books of the 2026-09-28 design stay in the database as an archive: format_book shows
any of them, but the summary and the statistics are the model's."""
from __future__ import annotations

import datetime as dt
import json
import re

import crypto
import db
import model
import paper
import t212_account
import telegram_notify
from telegram_notify import DOT_GREEN, DOT_RED, money_eur, signed_pct

_REPORT_KEY_TTL = 40 * 86400
_SLEEVES = ((model.STOCK_BOOK, "Акции", "S&P 500"), (model.CRYPTO_BOOK, "Крипто", "BTC"))
_NOT_STARTED = "Модельный портфель ещё не запущен — стартует с первого ежедневного прогона."


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:+.1f}%"


def _eur(x: float) -> str:
    return f"{'-' if x < 0 else '+'}€{abs(x):,.0f}"


def _book_row(conn, code: str):
    return conn.execute("SELECT start_date, start_eur FROM paper_books WHERE code = ?",
                        (code,)).fetchone()


def _value_on_or_before(conn, code: str, day: dt.date, column: str = "value") -> float | None:
    """The book's `column` (value, or bench: its benchmark) on the last day stored
    on or before `day`."""
    assert column in ("value", "bench")
    row = conn.execute(f"SELECT {column} FROM paper_equity WHERE book = ? AND date <= ? "
                       f"AND {column} IS NOT NULL ORDER BY date DESC LIMIT 1",
                       (code, day.isoformat())).fetchone()
    return row[0] if row else None


def _equity_by_day(conn, code: str, today: dt.date) -> dict[str, tuple[float, float | None]]:
    return {d: (v, b) for d, v, b in conn.execute(
        "SELECT date, value, bench FROM paper_equity WHERE book = ? AND date <= ? ORDER BY date",
        (code, today.isoformat()))}


def model_stats(conn, today: dt.date) -> dict | None:
    """The model against the 70/30 mix: both books' values added up on the days both have
    one. None before the model's first run. With no such day yet, the start money and zeros."""
    rows = {code: _book_row(conn, code) for code in model.BOOKS}
    if any(row is None for row in rows.values()):
        return None
    start_date = rows[model.STOCK_BOOK][0]
    start_eur = sum(row[1] for row in rows.values())
    stock, coin = (_equity_by_day(conn, code, today) for code in model.BOOKS)
    days = sorted(set(stock) & set(coin))
    values = [stock[d][0] + coin[d][0] for d in days]
    benches = [stock[d][1] + coin[d][1] for d in days
               if stock[d][1] is not None and coin[d][1] is not None]
    if not days:
        values = benches = [start_eur]
    value = values[-1]
    bench = benches[-1] if benches else None
    ret = value / start_eur - 1
    bench_ret = bench / start_eur - 1 if bench is not None else None
    dd = paper.max_drawdown(values)
    bench_dd = paper.max_drawdown(benches) if benches else None
    trades = sum(len(paper.closed_positions(conn, code)) for code in model.BOOKS)
    day = (today - dt.date.fromisoformat(start_date)).days
    if day < paper.SUCCESS_DAYS:
        status = f"идёт (день {day} из {paper.SUCCESS_DAYS})"
    elif trades < paper.MIN_STOCK_TRADES:
        status = f"идёт (сделок {trades} из {paper.MIN_STOCK_TRADES})"
    else:
        ok = bench_ret is not None and ret > bench_ret and dd > bench_dd
        status = "пройдено" if ok else "не пройдено"
    return {"day": day, "start": start_date, "value": value, "bench": bench, "ret": ret,
            "bench_ret": bench_ret, "dd": dd, "bench_dd": bench_dd, "trades": trades,
            "open": sum(len(paper.open_positions(conn, code)) for code in model.BOOKS),
            "status": status}


def _sleeve(conn, code: str, today: dt.date) -> dict:
    """One book's value, cash and return, and its benchmark's return, as of `today`."""
    start_eur = _book_row(conn, code)[1]
    value = _value_on_or_before(conn, code, today)
    bench = _value_on_or_before(conn, code, today, "bench")
    return {"value": start_eur if value is None else value, "cash": paper.cash(conn, code),
            "ret": (start_eur if value is None else value) / start_eur - 1,
            "bench_ret": None if bench is None else bench / start_eur - 1}


def _month_return(conn, code: str, today: dt.date, column: str = "value") -> float | None:
    """The previous calendar month: value at its end / value at the end of the month
    before it (or the start money). column="bench" gives the benchmark's month -- it
    is indexed from the same start money."""
    end = today.replace(day=1) - dt.timedelta(days=1)
    before = end.replace(day=1) - dt.timedelta(days=1)
    v_end = _value_on_or_before(conn, code, end, column)
    v_begin = _value_on_or_before(conn, code, before, column) or _book_row(conn, code)[1]
    return v_end / v_begin - 1 if v_end else None


def _pp(x: float) -> str:
    """A difference of two fractions in percentage points: «+3,6 п.п.»."""
    return f"{signed_pct(x)[:-1]} п.п."


def _position_row(p: dict, today: dt.date) -> str:
    value = p["last_value"] if p["last_value"] is not None else p["net_eur"]
    days = (today - dt.date.fromisoformat(p["fill_date"])).days
    row = f"{crypto.symbol_of(p['ticker']):<12} {days:>3} дн.  {signed_pct(value / p['cost_eur'] - 1):>7}"
    if p["stop_pct"] is not None:
        row += f"  стоп −{p['stop_pct'] * 100:.0f}%"
    return row


def _esc_for(html: bool):
    return telegram_notify._esc if html else str


def _table(rows: list[str], html: bool) -> list[str]:
    """Rows of a padded table. Telegram draws <pre> in a fixed-width font, so the columns line up."""
    if html:
        return ["<pre>" + "\n".join(telegram_notify._esc(r) for r in rows) + "</pre>"]
    return rows


def _archived(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT code FROM paper_books WHERE code NOT IN (?, ?) ORDER BY rowid", model.BOOKS)]


def format_summary(conn, today: dt.date, *, monthly: bool = False, html: bool = False) -> str:
    stats = model_stats(conn, today)
    if stats is None:
        return _NOT_STARTED
    esc = _esc_for(html)

    def table(rows: list[str]) -> list[str]:
        return _table(rows, html)

    start = dt.date.fromisoformat(stats["start"]).strftime("%d.%m.%Y")
    lines = [telegram_notify._b(f"Модельный портфель — день {stats['day']} (с {start})", html)]
    diff = "" if stats["bench_ret"] is None else f" ({_pp(stats['ret'] - stats['bench_ret'])})"
    lines += [esc(t) for t in (
        f"Итого: {money_eur(stats['value'])} ({signed_pct(stats['ret'])}) — смесь 70/30 "
        f"(S&P 500 / BTC): {signed_pct(stats['bench_ret'])}{diff}",
        f"Худшая просадка: {signed_pct(stats['dd'])} — смесь: {signed_pct(stats['bench_dd'])}",
        f"Закрытых сделок: {stats['trades']} · открытых позиций: {stats['open']}",
        f"Статус: {stats['status']}")]

    rows = []
    for code, name, bench_name in _SLEEVES:
        sl = _sleeve(conn, code, today)
        bench = f"{bench_name} {signed_pct(sl['bench_ret'])}"
        row = (f"{name:<7}{code:<8}{money_eur(sl['value']):>9}  {signed_pct(sl['ret']):>6}  "
               f"{bench:<15}свободно {money_eur(sl['cash'])}")
        if monthly:
            row += (f"  за месяц {signed_pct(_month_return(conn, code, today))} "
                    f"({bench_name} {signed_pct(_month_return(conn, code, today, 'bench'))})")
        rows.append(row)
    lines += [""] + table(rows)

    held = [_position_row(p, today) for code in model.BOOKS for p in paper.open_positions(conn, code)]
    lines.append("")
    if held:
        lines += [esc("Открытые позиции:")] + table(held)
    else:
        lines.append(esc("Открытых позиций нет."))

    archived = _archived(conn)
    if archived:
        n = len(archived)
        what = telegram_notify._plural(n, "прежняя книга остановлена", "прежние книги остановлены",
                                       "прежних книг остановлены")
        lines += ["", esc(f"Архив: {n} {what} — python paper.py {archived[0]}")]
    return "\n".join(lines)


def format_book(conn, code: str) -> str:
    lines = [f"{code} — открытые позиции:"]
    for p in paper.open_positions(conn, code):
        value = p["last_value"] if p["last_value"] is not None else p["net_eur"]
        lines.append(f"  {p['ticker']:<14} с {p['fill_date']}  вход {p['entry_close']:,.2f}  "
                     f"€{p['cost_eur']:,.0f} → €{value:,.0f} ({_pct(value / p['cost_eur'] - 1)})  "
                     f"{p['reason']}")
    if len(lines) == 1:
        lines.append("  нет")
    lines.append("Сделки:")
    closed = paper.closed_positions(conn, code)
    for p in closed:
        lines.append(f"  {p['ticker']:<14} {p['fill_date']} → {p['closed_date']}  "
                     f"{_pct(p['proceeds_eur'] / p['cost_eur'] - 1)} "
                     f"({_eur(p['proceeds_eur'] - p['cost_eur'])})  куплено: {p['reason']}  "
                     f"продано: {p['close_reason']}")
    if not closed:
        lines.append("  нет")
    skipped = [o for o in paper.orders(conn, code) if o["status"] in ("skipped", "cancelled")]
    if skipped:
        lines.append("Пропущено:")
        lines += [f"  {o['created']}  {o['ticker']:<14} {o['note']}" for o in skipped]
    return "\n".join(lines)


# ------------------------------------------------------ the weekly signals and the weekly summary
_WEEK_DAYS = 6              # the week is today - 6 days ... today
_MAX_REASONS = 2            # a buy names this many of its reasons
_MAX_SELLERS = 5
MODEL_FAILED_WARNING = "⚠️ Модель на этой неделе не отработала — покупок не было."
_SCORE_PREFIX = re.compile(r"^балл \d+:\s*")      # an order's reason opens with its score, shown apart
_UNPRICED = " (по последней цене: нет котировок)"   # paper._close_at_last_value adds it to a reason
_STOP_SIZE = re.compile(r"[−-]\s?\d+(?:[.,]\d+)?%")
# (how a model sale's reason begins, the close-alert trigger whose wording it takes)
_SALE_TRIGGERS = (("продаёт инсайдер", "insider_sell"), ("активист", "activist_cut"),
                  ("стоит на месте", "dead_money"), ("год в позиции", "time"), ("новости", "news"),
                  ("тренд вниз", "trend_down"), ("осторожно", "caution"))


def _started(conn) -> bool:
    """Both model books exist: the model has had its first run."""
    return all(_book_row(conn, code) is not None for code in model.BOOKS)


def _week_buys(conn, start: str, end: str) -> list[dict]:
    """The model's buy orders created from `start` to `end` that went ahead (still pending
    or filled), from both books, the best score first (no score last; then as they were placed)."""
    rows = [o for code in model.BOOKS for o in paper.orders(conn, code)
            if o["side"] == "buy" and o["status"] in ("pending", "filled") and start <= o["created"] <= end]
    return sorted(rows, key=lambda o: (o["score"] is None, -(o["score"] or 0.0), o["id"]))


def _fill_of(conn, order: dict) -> tuple[str, float, str] | None:
    """(fill date, entry close, currency) of a filled buy order: the book's first position in the
    ticker opened after the order was placed (an order fills at the first close after it)."""
    return conn.execute(
        "SELECT fill_date, entry_close, currency FROM paper_positions WHERE book = ? AND ticker = ? "
        "AND fill_date > ? ORDER BY fill_date, id LIMIT 1",
        (order["book"], order["ticker"], order["created"])).fetchone()


def _t212_flags(conn, first: dt.date, today: dt.date, report) -> dict[str, bool]:
    """ticker -> whether Trading 212 lists it, as the week's scores say (newest day first): today's
    `report`, else the scores a daily run kept that day (model.cached_scores). A score that does not
    say (t212 None: a coin, no key, no instrument list) leaves the ticker out."""
    flags: dict[str, bool] = {}
    day = today
    while day >= first:
        scored = report.scored if day == today and report is not None else model.cached_scores(conn, day)
        for s in scored or ():
            known = getattr(s, "t212", None)
            if known is not None:
                flags.setdefault(s.ticker, known)
        day -= dt.timedelta(days=1)
    return flags


def _buy_text(conn, order: dict, t212: dict[str, bool]) -> str:
    """«🟢 GME!: покупка — 2 инсайдера из руководства; CEO среди покупателей; балл 70, стоп −10%»: the
    order's reason without its «балл N: » (its first two parts), its score and its stop; then «нет на
    Trading 212» when the model's score says so and, for an order that has filled, «вход 07.10 по 23,10»."""
    reasons = [r for r in _SCORE_PREFIX.sub("", order["reason"] or "").strip().split("; ") if r]
    head = reasons[:_MAX_REASONS] + ([f"балл {order['score']:.0f}"] if order["score"] is not None else [])
    tail = []
    if order["stop_pct"] is not None:
        tail.append(f"стоп −{order['stop_pct'] * 100:.0f}%")
    if t212.get(order["ticker"]) is False:
        tail.append("нет на Trading 212")
    fill = _fill_of(conn, order) if order["status"] == "filled" else None
    if fill:
        tail.append(f"вход {dt.date.fromisoformat(fill[0]):%d.%m} по {telegram_notify._price(fill[1])}")
    details = ", ".join((["; ".join(head)] if head else []) + tail)
    return telegram_notify.signal_line(DOT_GREEN, crypto.symbol_of(order["ticker"]), "покупка", details or None)


def _sale_event(reason: str | None) -> tuple[str, bool]:
    """(what a model sale's reason says happened, worded as the close alerts word it; whether it was
    sold at the last price for want of quotes). «стоп: −10% от максимума» is «сработал стоп −10%»;
    a reason nobody worded is shown as it is, and none is «продажа»."""
    text = (reason or "").strip()
    unpriced = text.endswith(_UNPRICED)
    text = text.removesuffix(_UNPRICED).strip()
    if text.startswith("стоп"):
        size = _STOP_SIZE.search(text)
        return "сработал стоп" + (f" {size.group().replace('-', '−')}" if size else ""), unpriced
    for prefix, trigger in _SALE_TRIGGERS:
        if text.startswith(prefix):
            return telegram_notify.CLOSE_EVENT[trigger], unpriced
    return text or "продажа", unpriced


def _dot(pct: float | None) -> str:
    """Green for a result of zero or more as shown (to a tenth of a percent), red for a loss -- and
    for a result nobody knows."""
    return DOT_GREEN if pct is not None and round(pct * 100, 1) >= 0 else DOT_RED


def _week_sales(conn, start: str, end: str) -> list[dict]:
    """The positions closed from `start` to `end`, in the order they closed."""
    return sorted((p for code in model.BOOKS for p in paper.closed_positions(conn, code)
                   if start <= p["closed_date"] <= end), key=lambda p: (p["closed_date"], p["id"]))


def _pending_sales(conn) -> list[dict]:
    """The sell orders still waiting for their close."""
    return sorted((o for code in model.BOOKS for o in paper.pending_orders(conn, code)
                   if o["side"] == "sell"), key=lambda o: o["id"])


def _sale_text(p: dict) -> str:
    """«🔴 GME!: сработал стоп −10% — продано 07.10, итог −9,8%»: the sale of a closed position, its
    result in percent only -- the model's money is virtual and is not shown."""
    event, unpriced = _sale_event(p["close_reason"])
    when = f"продано {dt.date.fromisoformat(p['closed_date']):%d.%m}" + (" по последней цене" if unpriced else "")
    pct = None if p["proceeds_eur"] is None else p["proceeds_eur"] / p["cost_eur"] - 1
    return telegram_notify.signal_line(_dot(pct), crypto.symbol_of(p["ticker"]), event, when,
                                       result=None if pct is None else signed_pct(pct))


def _pending_sale_text(conn, order: dict) -> str:
    """«🔴 GME!: сработал стоп −10% — продажа по ближайшему закрытию, сейчас −9,8%»: a sell order still
    waiting for its close, with how the position stands now (none: it has no value yet)."""
    row = conn.execute("SELECT cost_eur, last_value FROM paper_positions WHERE id = ?",
                       (order["position_id"],)).fetchone()
    pct = row[1] / row[0] - 1 if row and row[1] is not None else None
    event, _unpriced = _sale_event(order["reason"])
    return telegram_notify.signal_line(_dot(pct), crypto.symbol_of(order["ticker"]), event,
                                       "продажа по ближайшему закрытию", label="сейчас",
                                       result=None if pct is None else signed_pct(pct))


def _week_exits(conn, start: str, end: str) -> list[tuple[int, str, list[str]]]:
    """The group exits journaled from `start` to `end`: one per ticker, its latest row, as (journal
    id, ticker, who sold), by journal id."""
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


def _exit_text(ticker: str, names: list[str]) -> str:
    """«🔴 XYZ!: продают те, кто покупал — Name A, Name B» (the first five, then «и ещё N»)."""
    who = ", ".join(names[:_MAX_SELLERS])
    if len(names) > _MAX_SELLERS:
        who += f" и ещё {len(names) - _MAX_SELLERS}"
    return telegram_notify.signal_line(DOT_RED, crypto.symbol_of(ticker), "продают те, кто покупал", who or None)


def week_signals(conn, today: dt.date, report) -> list[tuple[str, str]]:
    """The week's signals, one Telegram message each (spec 2026-10-04-signal-message-style.md),
    as (key, html text) in the order they go out: the model's buys (best score first), its sales,
    the sales still waiting, the group exits. The week is today-6 ... today. The key names the signal
    for good -- `buy:<order id>`, `sell:<position id>`, `sellpending:<order id>`, `exit:<journal id>`
    -- so that bot._send_weekly can tell which went out already. Nothing before the model's first
    run, and nothing in a quiet week.

    `report` is model.DayReport (None: the model did not run): its scores, and the ones the daily runs
    kept on the days of the week, say which bought names Trading 212 does not list."""
    if not _started(conn):
        return []
    first = today - dt.timedelta(days=_WEEK_DAYS)
    start, end = first.isoformat(), today.isoformat()
    t212 = _t212_flags(conn, first, today, report)
    signals = [(f"buy:{o['id']}", _buy_text(conn, o, t212)) for o in _week_buys(conn, start, end)]
    signals += [(f"sell:{p['id']}", _sale_text(p)) for p in _week_sales(conn, start, end)]
    signals += [(f"sellpending:{o['id']}", _pending_sale_text(conn, o)) for o in _pending_sales(conn)]
    signals += [(f"exit:{id_}", _exit_text(ticker, names)) for id_, ticker, names in _week_exits(conn, start, end)]
    return signals


def _week_return(conn, today: dt.date, value: float) -> float | None:
    """`value` against the books' values on the last day stored on or before a week ago; None
    when either book has no value that early."""
    before = [_value_on_or_before(conn, code, today - dt.timedelta(days=7)) for code in model.BOOKS]
    if any(v is None for v in before) or not sum(before):
        return None
    return value / sum(before) - 1


def _t212_line(conn, today: dt.date) -> str | None:
    """«Ваш счёт Trading 212: €X (за неделю ±Y%)»: the owner's own account, from the snapshots the
    Trading 212 sync stores -- its value on the last day stored on or before `today`, against the
    last one on or before a week earlier (left out when there is none). A snapshot more than
    t212_account.STALE_DAYS older than the day it stands for is not used: with no fresh value there
    is no line (the sync has not got through), and with no fresh one a week ago no week change.
    None with no value yet."""
    stale = t212_account.STALE_DAYS
    now = t212_account.account_value(conn, today, max_age_days=stale)
    if now is None:
        return None
    value, currency = now
    line = f"Ваш счёт Trading 212: {telegram_notify.money(value, currency)}"
    before = t212_account.account_value(conn, today - dt.timedelta(days=7), max_age_days=stale)
    if before and before[0]:
        line += f" (за неделю {signed_pct(value / before[0] - 1)})"
    return line


def format_week_summary(conn, today: dt.date, report, *, html: bool = True,
                        model_failed: bool = False) -> str:
    """The weekly summary, sent last: one short message, a line each (spec 2026-10-04-signal-message-style.md)

        📊 Модель, неделя 26.09–02.10: €101 230 (+1,2% с начала, за неделю +0,4%); смесь 70/30 +0,8%
        В портфеле (9): BBD +1,2%, TRMD −0,4%, …
        Сигналов за неделю: покупок 2, продаж 1
        Ваш счёт Trading 212: €186 (за неделю +0,3%)
        ⚠️ Модель на этой неделе не отработала — покупок не было.

    -- where the portfolio stands, what it holds (the best result first), how many signals the week
    sent (week_signals; a sale waiting counts as a sale, group exits are counted apart; none: «Сигналов
    за неделю не было.»), the owner's Trading 212 account when the bot tracks it (_t212_line), and
    `model_failed` -- the week's model pass never got through (the Sunday message goes out regardless)
    -- as the warning. A part that cannot be computed is left out: no week return before the model is
    a week old, no mix without its benchmark. Always a message, however quiet the week.
    `report` is model.DayReport (None: the model did not run)."""
    stats = model_stats(conn, today)
    if stats is None:
        return _NOT_STARTED
    esc = _esc_for(html)
    first = today - dt.timedelta(days=_WEEK_DAYS)
    since = [f"{signed_pct(stats['ret'])} с начала"]
    week = _week_return(conn, today, stats["value"])
    if week is not None:
        since.append(f"за неделю {signed_pct(week)}")
    title = telegram_notify._b(f"Модель, неделя {first:%d.%m}–{today:%d.%m}", html)
    head = f"📊 {title}: {esc(money_eur(stats['value']))} ({esc(', '.join(since))})"
    if stats["bench_ret"] is not None:
        head += esc(f"; смесь 70/30 {signed_pct(stats['bench_ret'])}")
    lines = [head]

    held = sorted(((crypto.symbol_of(p["ticker"]),
                    (p["last_value"] if p["last_value"] is not None else p["net_eur"]) / p["cost_eur"] - 1)
                   for code in model.BOOKS for p in paper.open_positions(conn, code)),
                  key=lambda row: (-row[1], row[0]))
    lines.append(esc(f"В портфеле ({len(held)}): " + ", ".join(f"{name} {signed_pct(result)}"
                                                              for name, result in held))
                 if held else esc("В портфеле: пусто — всё в деньгах"))

    kinds = [key.split(":")[0] for key, _text in week_signals(conn, today, report)]
    sales, exits = kinds.count("sell") + kinds.count("sellpending"), kinds.count("exit")
    lines.append(esc(f"Сигналов за неделю: покупок {kinds.count('buy')}, продаж {sales}"
                     + (f", групповых выходов {exits}" if exits else "") if kinds
                     else "Сигналов за неделю не было."))
    if account := _t212_line(conn, today):
        lines.append(esc(account))
    if model_failed:
        lines.append(esc(MODEL_FAILED_WARNING))
    return "\n".join(lines)


def maybe_send_monthly_report(conn, today: dt.date, send=None) -> bool:
    """The first run of each month sends the summary, with each sleeve's month, once.
    Nothing until the model has started (both books, with statistics), and nothing in the
    month it started. True when it was sent."""
    row = _book_row(conn, model.STOCK_BOOK)
    if row is None or row[0][:7] == today.strftime("%Y-%m"):
        return False
    if model_stats(conn, today) is None:
        return False
    key = f"paper_report_{today:%Y-%m}"
    if db.get_cached_value(conn, key, _REPORT_KEY_TTL) is not None:
        return False
    send = send or telegram_notify.send_text
    if not send(format_summary(conn, today, monthly=True, html=True)):
        return False
    db.save_cached_value(conn, key, 1.0)
    return True
