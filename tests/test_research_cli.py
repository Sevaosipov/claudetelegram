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


# ---------------------------------------------------------- every form, both entry points
@pytest.fixture
def stub_build(monkeypatch):
    """research.build resolves for real; the dossiers behind it are stubbed."""
    import crypto_research
    import sources
    built = []
    monkeypatch.setattr(sources, "cached_coin_symbols", lambda conn: {"BTC", "ETH"})
    monkeypatch.setattr(sources, "stock_universe_symbols", lambda: set())
    monkeypatch.setattr(research, "_build_stock", lambda conn, a: built.append(a) or {"kind": "stock"})
    monkeypatch.setattr(crypto_research, "build", lambda conn, a: built.append(a) or {"kind": "crypto"})
    monkeypatch.setattr(research, "format_report", lambda rep: f"REPORT {built[-1].kind} {built[-1].symbol}")
    return built


@pytest.mark.parametrize("text,printed", [("NVDA", "REPORT stock NVDA"), ("BTC", "REPORT crypto BTC"),
                                          ("EQNR.OL", "REPORT stock EQNR.OL"),
                                          ("$BTC", "REPORT stock BTC")])
def test_cli_takes_every_form(cli, stub_build, capsys, text, printed):
    cli(text)
    assert capsys.readouterr().out.strip() == printed


def test_cli_rejects_a_non_ticker_with_the_hint(cli, stub_build, capsys):
    with pytest.raises(SystemExit) as exit_:
        cli("!!!")
    assert exit_.value.code == 2
    assert "Не похоже на тикер. Примеры: NVDA, BTC, EQNR.OL" in capsys.readouterr().out
    assert stub_build == []


def test_menu_option_2_prints_the_report_or_the_hint(conn, stub_build, monkeypatch, capsys):
    typed = iter(["btc", "!!!"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(typed))
    menu.show_research(conn)
    assert "REPORT crypto BTC" in capsys.readouterr().out
    menu.show_research(conn)
    out = capsys.readouterr().out
    assert "Не похоже на тикер" in out and "REPORT" not in out
