"""Interactive terminal menu for browsing disclosure-bot's collected data --
no need to remember script names/flags, just run this and pick a number.

Usage:
    python menu.py
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import backtest
import cluster
import congress_score
import datefmt
import db
import model_eval
import politician_report
import research
import telegram_notify

# Absolute, like bot.py's -- a relative path silently opened (and CREATED) an empty
# database whenever the menu was launched from anywhere but the project directory.
DB_PATH = Path(__file__).parent / "data" / "disclosures.db"


# Only emit ANSI color codes to a real terminal -- piped/redirected output would
# otherwise show the raw escape sequences as garbage text.
GREEN = "\033[92m" if sys.stdout.isatty() else ""
RESET = "\033[0m" if sys.stdout.isatty() else ""


def show_insiders(conn) -> None:
    ticker = input("Тикер/ISIN (Enter = все): ").strip().upper()
    where = "WHERE ticker = ?" if ticker else ""
    params = (ticker,) if ticker else ()

    buys = conn.execute(
        f"SELECT transaction_date, owner_name, officer_title, is_director, ticker, value, source_url "
        f"FROM sec_purchases {where} ORDER BY transaction_date DESC LIMIT 200", params
    ).fetchall()
    sells = conn.execute(
        f"SELECT transaction_date, owner_name, officer_title, is_director, ticker, value, source_url "
        f"FROM sec_sales {where} ORDER BY transaction_date DESC LIMIT 200", params
    ).fetchall()
    rows = [(*r, "P", "$") for r in buys] + [(*r, "S", "$") for r in sells]

    bafin_where = "WHERE isin = ?" if ticker else ""
    bafin_rows = conn.execute(
        f"SELECT txn_date, notifier_name, position, 0, isin, volume_eur, source_url, txn_type "
        f"FROM bafin_purchases {bafin_where} ORDER BY txn_date DESC LIMIT 200", params
    ).fetchall()
    # BaFin dates are DD.MM.YYYY, not sortable as plain strings like SEC's ISO
    # dates -- normalize to ISO here so min()/max()/sort() below stay correct.
    for txn_date, name, position, _zero, isin, volume, url, code in bafin_rows:
        try:
            iso_date = dt.datetime.strptime(txn_date, "%d.%m.%Y").date().isoformat()
        except ValueError:
            iso_date = txn_date
        rows.append((iso_date, name, position, 0, isin, volume, url, code if code in ("P", "S") else "P", "€"))

    # Norway's txn_date is already ISO (from Newsweb's publishedTime), unlike
    # BaFin's DD.MM.YYYY -- no conversion needed before it's sorted/displayed.
    norway_where = "WHERE ticker = ?" if ticker else ""
    norway_rows = conn.execute(
        f"SELECT txn_date, person, '', 0, ticker, value, source_url, txn_type, currency "
        f"FROM norway_purchases {norway_where} ORDER BY txn_date DESC LIMIT 200", params
    ).fetchall()
    for txn_date, name, position, _zero, tkr, value, url, code, currency in norway_rows:
        rows.append((txn_date, name, position, 0, tkr, value, url, code if code in ("P", "S") else "P", currency))

    # Sweden, like BaFin, is keyed on ISIN -- there is no ticker in the register.
    # Dates are already ISO, so no conversion is needed before sorting.
    # status = 'Aktuell' keeps corrected/superseded versions of a filing out of the
    # list, so one transaction shows once rather than once per revision.
    sweden_where = "isin = ? AND status = 'Aktuell'" if ticker else "status = 'Aktuell'"
    sweden_rows = conn.execute(
        f"SELECT txn_date, person, position, 0, isin, value, source_url, txn_type, currency "
        f"FROM sweden_purchases WHERE {sweden_where} ORDER BY txn_date DESC LIMIT 200", params
    ).fetchall()
    for txn_date, name, position, _zero, isin, value, url, code, currency in sweden_rows:
        rows.append((txn_date, name, position, 0, isin, value, url, code if code in ("P", "S") else "P", currency))

    if not rows:
        print("Пока ничего не найдено в базе. Соберите данные: python bot.py --once")
        return

    def role_of(title, is_director):
        return title or ("директор" if is_director else "инсайдер")

    # Group every purchase/sale by (person, ticker/ISIN, direction, currency)
    # regardless of date -- one line per insider per stock, not one line per lot.
    # Currency is part of the key so a $ total and a € total never get summed
    # together into a meaningless number.
    groups: dict[tuple, dict] = {}
    for date, name, title, is_director, tkr, value, url, code, currency in rows:
        key = (name, tkr, code, currency)
        g = groups.setdefault(key, {
            "title": title, "is_director": is_director, "total": 0.0, "n": 0,
            "first": date, "last": date,
        })
        g["total"] += value or 0
        g["n"] += 1
        g["first"] = min(g["first"], date)
        g["last"] = max(g["last"], date)
        if title:  # prefer a real title over the director-only fallback
            g["title"] = title

    # Most recently active groups at the bottom, like a running feed. Quota per
    # currency/source so BaFin (much lower daily volume than SEC) doesn't just get
    # crowded out of a single shared top-30 by SEC's fresher, higher-volume activity.
    by_currency: dict[str, list] = {}
    for item in groups.items():
        by_currency.setdefault(item[0][3], []).append(item)
    per_currency_limit = max(10, 30 // max(1, len(by_currency)))
    ordered = []
    for currency, items in by_currency.items():
        ordered += sorted(items, key=lambda kv: kv[1]["last"])[-per_currency_limit:]
    ordered.sort(key=lambda kv: kv[1]["last"])

    ticker_w = max(len(tkr) for (name, tkr, code, cur), g in ordered)
    name_w = max(len(f"{name} ({role_of(g['title'], g['is_director'])})") for (name, tkr, code, cur), g in ordered)

    print(f"\nПоследние сделки ({len(ordered)} инсайдер×тикер):")
    for (name, tkr, code, currency), g in ordered:
        icon = "🟢" if code == "P" else "🔴"
        name_role = f"{name} ({role_of(g['title'], g['is_director'])})"
        first, last = datefmt.fmt(g["first"]), datefmt.fmt(g["last"])
        date_range = first if first == last else f"{first}..{last}"
        times = f" ×{g['n']}" if g["n"] > 1 else ""
        print(f"  {icon} {date_range:<22}  {tkr:<{ticker_w}}  {name_role:<{name_w}}{times}  {GREEN}{currency}{g['total']:,.0f}{RESET}")


def show_politicians(conn) -> None:
    """Top 10 members of Congress ranked by estimated profit on their PTR
    purchases (see congress_score.py -- House never discloses exact price/shares,
    so this is a return-based estimate, not exact accounting)."""
    print(f"Считаю доходность по сделкам за последние {congress_score.LOOKBACK_MONTHS} мес. "
          f"(историческая цена на дату покупки vs текущая) — может занять минуту...")

    def progress(msg):
        print(f"  {msg}")

    top = congress_score.top_politicians(conn, n=10, progress=progress)
    if not top:
        print("Недостаточно данных для рейтинга (нужны покупки с определяемым тикером в базе).")
        return

    print()
    print(f"=== Топ-{len(top)} самых прибыльных (оценка) ===")
    for i, r in enumerate(top, 1):
        print(f"{i:2}. {r['name']} ({r['state_district']})")
        print(f"     прибыль ~${r['total_profit']:,.0f}  ·  {r['num_trades']} сделок  ·  "
              f"средняя доходность {r['avg_return_pct']:+.1f}%  ·  вложено ~${r['total_invested']:,.0f}")

    choice = input("\nПоказать детальный отчёт по номеру (Enter — назад): ").strip()
    if choice.isdigit() and 1 <= int(choice) <= len(top):
        picked = top[int(choice) - 1]
        years_raw = input(f"Годы через запятую (Enter = {politician_report.CURRENT_YEAR}): ").strip()
        years = [int(y) for y in years_raw.split(",")] if years_raw else [politician_report.CURRENT_YEAR]
        politician_report.run(picked["name"], years)


def show_signals(conn) -> None:
    """Shows every currently-qualifying signal, including ones already sent to
    Telegram earlier (unlike bot.py, which only alerts on growth since last time)."""
    signals = (
        cluster.find_sec_clusters(conn, ignore_alert_state=True)
        + cluster.find_house_clusters(conn, ignore_alert_state=True)
        + cluster.find_senate_clusters(conn, ignore_alert_state=True)
        + cluster.find_bafin_clusters(conn, ignore_alert_state=True)
        + cluster.find_norway_clusters(conn, ignore_alert_state=True)
        + cluster.find_sweden_clusters(conn, ignore_alert_state=True)
        + cluster.find_stake_signals(conn, ignore_alert_state=True)
        + cluster.find_sec_exit_signals(conn, ignore_alert_state=True)
        + cluster.find_house_exit_signals(conn, ignore_alert_state=True)
        + cluster.find_senate_exit_signals(conn, ignore_alert_state=True)
        + cluster.find_bafin_exit_signals(conn, ignore_alert_state=True)
        + cluster.find_norway_exit_signals(conn, ignore_alert_state=True)
        + cluster.find_sweden_exit_signals(conn, ignore_alert_state=True)
    )
    if not signals:
        print("Сейчас нет ни одного сигнала, удовлетворяющего порогам (см. cluster.py).")
        return
    for s in signals:
        print(telegram_notify.format_any_signal(s))
        print()


def show_research(conn) -> None:
    """Сводка по одному тикеру или ISIN. Намеренно без вердикта «покупать или
    нет» — см. пояснение в конце самого отчёта и в research.py."""
    key = input("Тикер или ISIN: ").strip().upper()
    if not key:
        return
    print("Собираю: база бота, цены, отчётность SEC, новости — может занять минуту...")
    print(research.format_report(research.build(conn, key)))


def show_backtest(conn) -> None:
    """Что происходило с ценой после сигналов и после отдельных покупок.

    Это диагностика инструмента, а не рекомендация: маленькие выборки здесь
    ничего не значат, и отчёт прямо помечает такие группы."""
    print("Считаю доходность после сигналов — нужны цены с Yahoo Finance, "
          "первый раз может занять минуту...")
    horizon_raw = input("Горизонт в торговых днях (Enter = 21): ").strip()
    horizon = int(horizon_raw) if horizon_raw.isdigit() else 21

    data = backtest.collect_signals(conn, (horizon,))
    if data:
        backtest._report("все сигналы", {"все": data}, horizon)
        backtest._report("по типу", backtest._group(data, lambda r: r["kind"]), horizon)
        backtest._report("по числу покупателей",
                          backtest._group(data, lambda r: "1 (соло)" if r["buyers"] <= 1
                                          else "2" if r["buyers"] == 2 else "3+"), horizon)
    else:
        print("\nЖурнал сигналов пока пуст — он заполняется по мере отправки сигналов.")

    purchases = backtest.collect_purchases(conn, (horizon,))
    if purchases:
        backtest._report("отдельные покупки (SEC)", {"все": purchases}, horizon)
        backtest._report("обычные акции vs деривативы",
                          backtest._group(purchases, lambda r: "дериватив" if r["derivative"]
                                          else "обычные акции"), horizon)
    print(f"\nГруппы меньше n={backtest.MIN_MEANINGFUL_N} — это шум. Наблюдения здесь "
          f"не независимы\n(покупки скапливаются в одних и тех же бумагах и в одном "
          f"периоде рынка), поэтому p\nзавышает уверенность. Прошлое поведение — не "
          f"прогноз, и это не инвестиционный совет.")


def show_model_eval(conn) -> None:
    """Walk-forward оценка того, есть ли у признаков предсказательная сила.
    Не прогноз и не рекомендация — см. подпись внизу отчёта."""
    corpus = (input("Корпус [politicians / insiders / both] (Enter = both): ").strip()
              or "both")
    if corpus not in ("politicians", "insiders", "both"):
        print("Не понял корпус.")
        return
    h = input("Горизонт в торговых днях (Enter = 21): ").strip()
    horizon = int(h) if h.isdigit() else 21
    print("Считаю (нужны исторические цены — политический прогон может занять минуту)...")
    print(model_eval.run(conn, corpus, horizon))


def main() -> None:
    conn = db.connect(DB_PATH)
    while True:
        print()
        print("=== disclosure-bot ===")
        print("1) Инсайдеры (SEC/BaFin/Осло/Швеция) — сделки из базы")
        print("2) Политики (Конгресс) — топ-10 самых прибыльных")
        print("3) Текущие сигналы (кластеры покупок + выходы + крупные доли 13D/G)")
        print("4) Проверка сигналов на истории (что было с ценой после)")
        print("5) Досье по тикеру (всё, что известно + новости и отчётность)")
        print("6) Оценка предсказательной силы (walk-forward модель)")
        print("0) Выход")
        choice = input("Выбор: ").strip()

        if choice == "1":
            show_insiders(conn)
        elif choice == "2":
            show_politicians(conn)
        elif choice == "3":
            show_signals(conn)
        elif choice == "4":
            show_backtest(conn)
        elif choice == "5":
            show_research(conn)
        elif choice == "6":
            show_model_eval(conn)
        elif choice == "0":
            break
        else:
            print("Не понял выбор, введите 0, 1, 2, 3, 4, 5 или 6.")


if __name__ == "__main__":
    main()
