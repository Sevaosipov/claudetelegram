"""Menu option 4 (ask the analyst): reads a question and hands it to analyst.ask. Offline --
input and analyst.ask are replaced; nothing runs Claude."""
from __future__ import annotations

import analyst
import menu


def _inputs(monkeypatch, *answers):
    queue = list(answers)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or queue.pop(0))
    return prompts


def test_ask_analyst_passes_the_question_to_the_analyst(monkeypatch, capsys):
    asked = []
    _inputs(monkeypatch, "  что с NVDA?  ")
    monkeypatch.setattr(analyst, "ask", lambda q: asked.append(q) or 0)
    menu.ask_analyst()
    assert asked == ["что с NVDA?"]
    assert "Спрашиваю… (1–5 мин, нужен открытый TradingView)" in capsys.readouterr().out


def test_an_empty_question_returns_without_asking(monkeypatch, capsys):
    asked = []
    _inputs(monkeypatch, "   ")
    monkeypatch.setattr(analyst, "ask", lambda q: asked.append(q) or 0)
    menu.ask_analyst()
    assert asked == [] and "Спрашиваю" not in capsys.readouterr().out


def test_a_failing_analyst_does_not_end_the_menu(monkeypatch, capsys):
    _inputs(monkeypatch, "вопрос")

    def boom(q):
        raise OSError("no claude")
    monkeypatch.setattr(analyst, "ask", boom)
    menu.ask_analyst()
    assert "Ошибка: OSError" in capsys.readouterr().out


def test_the_menu_lists_option_4_and_dispatches_it(conn, monkeypatch, capsys):
    asked = []
    _inputs(monkeypatch, "4", "вопрос аналитику", "0")
    monkeypatch.setattr(menu.db, "connect", lambda path: conn)
    monkeypatch.setattr(analyst, "ask", lambda q: asked.append(q) or 0)
    menu.main()
    assert asked == ["вопрос аналитику"]
    assert "4) Спросить аналитика" in capsys.readouterr().out


def test_an_invalid_choice_lists_0_to_5(conn, monkeypatch, capsys):
    _inputs(monkeypatch, "9", "0")
    monkeypatch.setattr(menu.db, "connect", lambda path: conn)
    menu.main()
    assert "Не понял выбор, введите 0, 1, 2, 3, 4 или 5." in capsys.readouterr().out
