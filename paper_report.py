"""What the paper portfolio shows: per-book statistics against the benchmark, the
success line, the menu/CLI text and the monthly Telegram report. Spec:
docs/superpowers/specs/2026-09-28-paper-portfolio-design.md, sections 2-3."""
from __future__ import annotations

import datetime as dt

import db
import paper
import telegram_notify

_BENCH_NAME = {"stock": "S&P 500", "crypto": "BTC"}
_REPORT_KEY_TTL = 40 * 86400


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:+.1f}%"


def _book_row(conn, code: str):
    return conn.execute("SELECT start_date, start_eur FROM paper_books WHERE code = ?",
                        (code,)).fetchone()


def _value_on_or_before(conn, code: str, day: dt.date) -> float | None:
    row = conn.execute("SELECT value FROM paper_equity WHERE book = ? AND date <= ? "
                       "ORDER BY date DESC LIMIT 1", (code, day.isoformat())).fetchone()
    return row[0] if row else None


def stats(conn, book: paper.Book, today: dt.date) -> dict:
    start_date, start_eur = _book_row(conn, book.code)
    eq = conn.execute("SELECT value, bench FROM paper_equity WHERE book = ? ORDER BY date",
                      (book.code,)).fetchall()
    values = [v for v, _b in eq]
    benches = [b for _v, b in eq if b is not None]
    value = values[-1] if values else start_eur
    ret = value / start_eur - 1
    bench_ret = benches[-1] / start_eur - 1 if benches else None
    dd = paper.max_drawdown(values)
    bench_dd = paper.max_drawdown(benches) if benches else None
    trades = len(paper.closed_positions(conn, book.code))
    day = (today - dt.date.fromisoformat(start_date)).days
    if book.analyst:
        status = "тень"
    elif day < paper.SUCCESS_DAYS:
        status = "идёт"
    elif book.sleeve == "stock" and trades < paper.MIN_STOCK_TRADES:
        # A stock book turns a slot over only about twice in 182 days, so the verdict
        # waits for the trade minimum rather than failing on the count alone.
        status = f"идёт (сделок {trades} из {paper.MIN_STOCK_TRADES})"
    else:
        ok = (bench_ret is not None and ret > bench_ret and bench_dd is not None and dd > bench_dd)
        status = "пройдено" if ok else "не пройдено"
    return {"day": day, "start": start_date, "ret": ret, "bench_ret": bench_ret, "dd": dd,
            "bench_dd": bench_dd, "trades": trades, "open": len(paper.open_positions(conn, book.code)),
            "status": status}


def _month_return(conn, code: str, today: dt.date) -> float | None:
    """The previous calendar month: value at its end / value at the end of the month
    before it (or the start money)."""
    end = today.replace(day=1) - dt.timedelta(days=1)
    before = end.replace(day=1) - dt.timedelta(days=1)
    v_end = _value_on_or_before(conn, code, end)
    v_begin = _value_on_or_before(conn, code, before) or _book_row(conn, code)[1]
    return v_end / v_begin - 1 if v_end else None


def format_summary(conn, today: dt.date, *, monthly: bool = False, html: bool = False) -> str:
    if conn.execute("SELECT COUNT(*) FROM paper_books").fetchone()[0] == 0:
        return "Бумажный портфель ещё не запущен — он стартует с первого ежедневного прогона."
    first = stats(conn, paper.BOOKS[0], today)
    start = dt.date.fromisoformat(first["start"]).strftime("%d.%m.%Y")
    lines = [telegram_notify._b(f"Бумажный портфель — день {first['day']} из {paper.SUCCESS_DAYS} "
                                f"(с {start})", html)]
    for sleeve, title in (("stock", "АКЦИИ"), ("crypto", "КРИПТО")):
        books = [b for b in paper.BOOKS if b.sleeve == sleeve]
        head = stats(conn, books[0], today)
        lines.append("")
        lines.append(telegram_notify._b(
            f"{title} ({_BENCH_NAME[sleeve]}: {_pct(head['bench_ret'])}, "
            f"худшая просадка {_pct(head['bench_dd'])})", html))
        for b in books:
            s = stats(conn, b, today)
            diff = ("—" if s["bench_ret"] is None
                    else f"{(s['ret'] - s['bench_ret']) * 100:+.1f} п.п.")
            line = (f"{b.label:<16} {_pct(s['ret'])}  ({diff})  просадка {_pct(s['dd'])}  "
                    f"сделок {s['trades']}  позиций {s['open']}  {s['status']}")
            if monthly:
                line += f"  за месяц {_pct(_month_return(conn, b.code, today))}"
            lines.append(telegram_notify._esc(line) if html else line)
    return "\n".join(lines)


def format_book(conn, code: str) -> str:
    book = paper.BOOK_BY_CODE[code]
    lines = [f"{book.label} — открытые позиции:"]
    for p in paper.open_positions(conn, code):
        value = p["last_value"] if p["last_value"] is not None else p["net_eur"]
        lines.append(f"  {p['ticker']:<14} с {p['fill_date']}  €{p['cost_eur']:,.0f} → €{value:,.0f} "
                     f"({_pct(value / p['cost_eur'] - 1)})  {p['reason']}")
    if len(lines) == 1:
        lines.append("  нет")
    lines.append("Сделки:")
    closed = paper.closed_positions(conn, code)
    for p in closed:
        lines.append(f"  {p['ticker']:<14} {p['fill_date']} → {p['closed_date']}  "
                     f"{_pct(p['proceeds_eur'] / p['cost_eur'] - 1)}  куплено: {p['reason']}  "
                     f"продано: {p['close_reason']}")
    if not closed:
        lines.append("  нет")
    skipped = [o for o in paper.orders(conn, code) if o["status"] in ("skipped", "cancelled")]
    if skipped:
        lines.append("Пропущено:")
        lines += [f"  {o['created']}  {o['ticker']:<14} {o['note']}" for o in skipped]
    return "\n".join(lines)


def maybe_send_monthly_report(conn, today: dt.date, send=None) -> bool:
    """The first run of each month sends the summary, with each book's month, once.
    Nothing in the month the books started. True when it was sent."""
    row = conn.execute("SELECT MIN(start_date) FROM paper_books").fetchone()
    if not row or not row[0] or row[0][:7] == today.strftime("%Y-%m"):
        return False
    key = f"paper_report_{today:%Y-%m}"
    if db.get_cached_value(conn, key, _REPORT_KEY_TTL) is not None:
        return False
    send = send or telegram_notify.send_text
    if not send(format_summary(conn, today, monthly=True, html=True)):
        return False
    db.save_cached_value(conn, key, 1.0)
    return True
