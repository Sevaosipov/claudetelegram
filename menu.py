"""Interactive terminal menu for browsing disclosure-bot's collected data --
no need to remember script names/flags, just run this and pick a number.

Usage:
    python menu.py
"""
from __future__ import annotations

from pathlib import Path

import db
import positions
import research
import strategy
import telegram_notify
import termstyle
import trading212

# Absolute, like bot.py's -- a relative path silently opened (and CREATED) an empty
# database whenever the menu was launched from anywhere but the project directory.
DB_PATH = Path(__file__).parent / "data" / "disclosures.db"


def _find_signals(conn) -> list:
    """Buy-side signals only: strategy.buy_side_signals (the one shared finder list
    also used by bot.run_cluster_pass and calibrate_strategy._signals), with
    on-chain included so a snapshot shows up if the bot has ever been run with
    --onchain. Exit signals are left out -- this is a list of things to consider
    buying."""
    return strategy.buy_side_signals(conn, ignore_alert_state=True, onchain=True)


def show_signals(conn) -> None:
    """The same selection the daily Telegram digest sends (strategy.py), computed
    fresh and including signals already sent -- this is a browse, not a digest --
    plus pending close alerts and the positions reported with /bought."""
    print()
    print(termstyle.header("СИГНАЛЫ"))
    signals = _find_signals(conn) + strategy.exit_signals(conn, ignore_alert_state=True)
    selection = strategy.select(conn, signals, trading212.availability(conn))
    closes = positions.check_exits(conn)
    print(telegram_notify.format_tiered_digest(selection, closes, html=False))
    print()
    print(telegram_notify.format_positions(positions.open_positions(conn), positions.last_close))


def show_research(conn) -> None:
    """Сводка по одному тикеру или ISIN. Намеренно без вердикта «покупать или
    нет» — см. пояснение в конце самого отчёта и в research.py."""
    key = input("Тикер или ISIN: ").strip().upper()
    if not key:
        return
    print("Собираю: база бота, цены, отчётность SEC, новости — может занять минуту...")
    print(research.format_report(research.build(conn, key)))


def main() -> None:
    conn = db.connect(DB_PATH)
    while True:
        print()
        print(termstyle.header("disclosure-bot"))
        print("1) Сигналы")
        print("2) Досье по тикеру")
        print("0) Выход")
        choice = input("Выбор: ").strip()

        if choice == "1":
            show_signals(conn)
        elif choice == "2":
            show_research(conn)
        elif choice == "0":
            break
        else:
            print("Не понял выбор, введите 0, 1 или 2.")


if __name__ == "__main__":
    main()
