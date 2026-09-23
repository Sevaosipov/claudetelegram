"""`python research.py TICKER` and the menu's option 2 -- the two terminal entry points
of the dossier. Offline: research.build is stubbed, and so is db.connect (main() would
otherwise open the real database)."""
from __future__ import annotations

import sys

import pytest

import menu
import research


@pytest.fixture
def cli(conn, monkeypatch):
    """Run research.main() on argv; returns what it printed."""
    monkeypatch.setattr(research.db, "connect", lambda path: conn)

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["research.py", *argv])
        research.main()
    return run


def _raise(exc):
    def build(conn, text):
        raise exc
    return build


def test_not_a_ticker_is_its_own_error(conn, monkeypatch):
    import sources
    monkeypatch.setattr(sources, "cached_coin_symbols", lambda conn: set())
    with pytest.raises(research.NotATicker):
        research.build(conn, "!!!")
    assert issubclass(research.NotATicker, ValueError)


def test_cli_lets_other_errors_through(cli, monkeypatch):
    """Only "not a ticker" gets the hint; a bug deep inside build must not be
    disguised as one."""
    monkeypatch.setattr(research, "build", _raise(ValueError("a parsing bug")))
    with pytest.raises(ValueError, match="a parsing bug"):
        cli("NVDA")


def test_menu_reports_other_errors_and_keeps_running(conn, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt="": "NVDA")
    monkeypatch.setattr(research, "build", _raise(ValueError("a parsing bug")))
    menu.show_research(conn)
    assert "Ошибка: ValueError: a parsing bug" in capsys.readouterr().out
    monkeypatch.setattr(research, "build", _raise(RuntimeError("network")))
    menu.show_research(conn)
    out = capsys.readouterr().out
    assert "Ошибка: RuntimeError: network" in out and "Не похоже на тикер" not in out
