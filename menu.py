"""Interactive terminal menu for browsing disclosure-bot's collected data --
no need to remember script names/flags, just run this and pick a number.

Usage:
    python menu.py
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import analyst
import db
import model
import paper_report
import positions
import research
import telegram_notify
import termstyle

# Absolute, like bot.py's -- a relative path silently opened (and CREATED) an empty
# database whenever the menu was launched from anywhere but the project directory.
DB_PATH = Path(__file__).parent / "data" / "disclosures.db"


def show_signals(conn) -> None:
    """The model's score for every fresh signal and both coins (model.py): the scores the
    daily run kept today, else computed now -- a browse, not a digest: signals already sent
    show up too -- then the pending close alerts and the open positions (reported with /bought,
    or read from the Trading 212 account)."""
    print()
    try:
        scored = model.cached_scores(conn, dt.date.today())
        if scored is None:
            print("Считаю оценки — новости и цены, может занять минуту...")
            scored = model.score_today(conn)
        print(telegram_notify.format_scored(scored))
    except Exception as e:  # a failed source must not end the menu
        print(f"Ошибка: {type(e).__name__}: {e}")
    closes = positions.check_exits(conn)
    if closes:
        print()
        print("\n".join(telegram_notify.format_close_alert(a, html=False) for a in closes))
    print()
    print(telegram_notify.format_positions(
        positions.open_positions(conn), lambda ticker, source=None: positions.last_price(conn, ticker, source)))


def show_research(conn) -> None:
    """Сводка по одному тикеру, монете или ISIN. Намеренно без вердикта «покупать
    или нет» — см. пояснение в конце самого отчёта и в research.py."""
    key = input("Тикер, монета или ISIN (NVDA, BTC, EQNR.OL, $BTC): ").strip().upper()
    if not key:
        return
    print("Собираю: база бота, цены, отчётность SEC, новости — может занять минуту...")
    try:
        print(research.format_report(research.build(conn, key)))
    except research.NotATicker:
        print("Не похоже на тикер. Примеры: NVDA, BTC, EQNR.OL, $BTC (акция), BTC-USD (монета).")
    except Exception as e:  # any other failure: say so, and keep the menu running
        print(f"Ошибка: {type(e).__name__}: {e}")


def show_paper(conn) -> None:
    """The model portfolio against the 70/30 mix. Details of one book: python paper.py MODEL-S."""
    print()
    print(paper_report.format_summary(conn, dt.date.today()))
    print("\nПодробно: python paper.py MODEL-S (или MODEL-C, R1-E1 …)")


def ask_analyst() -> None:
    """A free-form question to the Claude analyst: the bot's own data plus the chart in the
    user's TradingView Desktop. analyst.ask prints the answer; nothing goes to Telegram."""
    question = input("Вопрос аналитику: ").strip()
    if not question:
        return
    print("Спрашиваю… (1–5 мин, нужен открытый TradingView)")
    try:
        analyst.ask(question)
    except Exception as e:  # a failed run must not end the menu
        print(f"Ошибка: {type(e).__name__}: {e}")


def main() -> None:
    conn = db.connect(DB_PATH)
    while True:
        print()
        print(termstyle.header("disclosure-bot"))
        print("1) Сигналы")
        print("2) Досье по тикеру или монете")
        print("3) Модельный портфель")
        print("4) Спросить аналитика")
        print("0) Выход")
        choice = input("Выбор: ").strip()

        if choice == "1":
            show_signals(conn)
        elif choice == "2":
            show_research(conn)
        elif choice == "3":
            show_paper(conn)
        elif choice == "4":
            ask_analyst()
        elif choice == "0":
            break
        else:
            print("Не понял выбор, введите 0, 1, 2, 3 или 4.")


if __name__ == "__main__":
    main()
