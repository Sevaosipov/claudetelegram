"""cfd/sizing.py: the lot size from /risk and the stop's distance."""
from __future__ import annotations

import pytest

import fx
import telegram_bot as tb
import telegram_notify as tn
from cfd import sizing, sk_live


def test_no_setting_no_lots(conn):
    assert sizing.lots(conn, "EURUSD", 1.1200, 1.1150) is None
    assert sizing.handle_command(conn, "/risk").startswith("Риск на CFD-сделку не задан")


def test_lots_are_the_risk_over_what_a_lot_loses(conn):
    assert "€10" in sizing.handle_command(conn, "/risk 10")
    usd = fx.per_eur("USD", conn)
    # EURUSD: 50 pips x 100,000 = $500 a lot
    assert sizing.lots(conn, "EURUSD", 1.1200, 1.1150) == f"{int(10 / (500 / usd) / 0.01) * 0.01:.2f}"
    # gold: $20 x 100 oz = $2,000 a lot -> under the smallest lot: 0.01 and what it risks
    assert sizing.lots(conn, "XAUUSD", 4160.0, 4140.0) == f"0.01 = €{20 / usd:.0f}"
    assert sizing.lots(conn, "USDJPY", 157.80, 160.20) is not None           # the loss is in yen, converted
    assert sizing.lots(conn, "EURUSD", 1.12, 1.12) is None and sizing.lots(conn, "NOPE", 1.0, 0.9) is None
    assert sizing.handle_command(conn, "/risk off") and sizing.lots(conn, "EURUSD", 1.12, 1.115) is None
    assert sizing.handle_command(conn, "/risk x").startswith("Команды")
    assert sizing.handle_command(conn, "/risk 0").startswith("Риск — число")


def test_the_sk_order_shows_the_lots(conn, monkeypatch):
    t = sk_live.Trade(1, "EURUSD", "r", "long", 1.1205, 1.118, 1.124, "2026-10-12T12:00:00+00:00", "pending",
                      None, None, None, None, 1)
    assert "Lots" not in tn.format_sk_order(t, html=False)
    assert tn.format_sk_order(t, lots="0.04", html=False).endswith("TP    1.12400\nLots  0.04")
    told = []
    monkeypatch.setattr(tn, "send_text", lambda text, **k: told.append(text) or True)
    assert tb._handle_risk_command(conn, "/risk 25") and not tb._handle_risk_command(conn, "/sk")
    assert "€25" in told[0] and sizing.risk_eur(conn) == pytest.approx(25.0)
