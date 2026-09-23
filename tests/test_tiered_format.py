"""The three-section message: 🔥 Сильные / 👀 Кандидаты / 🚪 Закрыть."""
from __future__ import annotations

import cluster
import positions
import strategy
import telegram_notify


def _sig(ticker="AAA"):
    return cluster.ClusterSignal(source="SEC", ticker=ticker, company="Test Corp", buyer_count=3,
                                 total_value=900_000.0, members=["A (Director) €300,000"],
                                 window_start="2026-09-21", window_end="2026-09-22")


def _pos(ticker="AAA", entry=100.0):
    return positions.Position(1, ticker, "SEC", "2026-09-01", entry, ["A"], None, None, None, None)


def _selection(t212=True):
    strong = [strategy.Tiered(_sig("AAA"), strategy.STRONG, ["3 инсайдера(ов) из руководства"], [])]
    cand = [strategy.Tiered(_sig("BBB"), strategy.CANDIDATE, [], ["размер неизвестен"])]
    return strategy.Selection(strong, cand, t212)


def test_sections_rules_and_closes_appear():
    close = positions.CloseAlert(_pos("CCC"), "stop_loss", "−16.0% от входа", 84.0)
    text = telegram_notify.format_tiered_digest(_selection(), [close])
    assert text.index("Сильные") < text.index("Кандидаты") < text.index("Закрыть")
    assert "✓ 3 инсайдера(ов) из руководства" in text and "✗ размер неизвестен" in text
    assert "CCC" in text and "стоп-лосс" in text


def test_missing_trading_212_check_is_announced():
    assert "Trading 212 не проверялся" in telegram_notify.format_tiered_digest(_selection(False), [])


def test_exits_section_appears_after_closes():
    exit_sig = cluster.ExitSignal(source="SEC", ticker="ZZZ", company="Exit Corp", total_buyers=3,
                                  seller_count=2, lines=["A: bought $1 -> sold $2"],
                                  seller_names=["A", "B"])
    sel = strategy.Selection([], [], True, exits=[exit_sig])
    close = positions.CloseAlert(_pos("CCC"), "stop_loss", "−16.0% от входа", 84.0)
    text = telegram_notify.format_tiered_digest(sel, [close])
    assert text.index("Закрыть") < text.index("Выходы")
    assert "ZZZ" in text and "🚨" in text


def test_exits_alone_count_as_something_to_say():
    exit_sig = cluster.ExitSignal(source="SEC", ticker="ZZZ", company="Exit Corp", total_buyers=3,
                                  seller_count=2, lines=[], seller_names=["A", "B"])
    sel = strategy.Selection([], [], True, exits=[exit_sig])
    text = telegram_notify.format_tiered_digest(sel, [])
    assert "сигналов нет" not in text and "ZZZ" in text


def test_empty_selection_says_there_is_nothing():
    empty = strategy.Selection([], [], True)
    assert "сигналов нет" in telegram_notify.format_tiered_digest(empty, [], html=False)


def test_plain_text_has_no_html_tags():
    assert "<b>" not in telegram_notify.format_tiered_digest(_selection(), [], html=False)


def test_positions_show_return_and_days():
    text = telegram_notify.format_positions([_pos(entry=100.0)], lambda t, s: 112.0)
    assert "AAA" in text and "+12.0%" in text
