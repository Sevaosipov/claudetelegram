"""What the model portfolio shows: its statistics against the 70/30 mix, the success line,
the menu/CLI text and the monthly Telegram report. Spec:
docs/superpowers/specs/2026-09-30-model-portfolio-and-analyst-design.md, section 6. The
paper books of the 2026-09-28 design stay in the database as an archive: format_book shows
any of them, but the summary and the statistics are the model's."""
from __future__ import annotations

import datetime as dt

import db
import model
import paper
import telegram_notify
from telegram_notify import money_eur, signed_pct

_REPORT_KEY_TTL = 40 * 86400
_SLEEVES = ((model.STOCK_BOOK, "Акции", "S&P 500"), (model.CRYPTO_BOOK, "Крипто", "BTC"))


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
    row = f"{p['ticker']:<12} {days:>3} дн.  {signed_pct(value / p['cost_eur'] - 1):>7}"
    if p["stop_pct"] is not None:
        row += f"  стоп −{p['stop_pct'] * 100:.0f}%"
    return row


def _archived(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT code FROM paper_books WHERE code NOT IN (?, ?) ORDER BY rowid", model.BOOKS)]


def format_summary(conn, today: dt.date, *, monthly: bool = False, html: bool = False) -> str:
    stats = model_stats(conn, today)
    if stats is None:
        return "Модельный портфель ещё не запущен — стартует с первого ежедневного прогона."
    esc = telegram_notify._esc if html else str

    def table(rows: list[str]) -> list[str]:
        if html:        # Telegram draws <pre> in a fixed-width font, so the padded columns line up
            return ["<pre>" + "\n".join(esc(r) for r in rows) + "</pre>"]
        return rows

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
