"""signal_context.py: the company line and the euro amount of a weekly buy signal, and /size."""
from __future__ import annotations

import datetime as dt

import pytest

import signal_context as sc
import telegram_bot as tb
import weekly

TODAY = dt.date(2026, 10, 9)
RXO = {"industry": "Trucking", "sector": "Industrials", "marketCap": 2.84e9, "currency": "USD",
       "trailingPE": 31.4, "forwardPE": 18.2, "trailingEps": 0.6, "priceToSalesTrailing12Months": 0.62,
       "revenueGrowth": -0.04, "profitMargins": 0.012}


def account(conn, value=200.0, day=TODAY, currency="EUR"):
    conn.execute("INSERT OR REPLACE INTO t212_equity (date, total_value, currency) VALUES (?,?,?)",
                 (day.isoformat(), value, currency))
    conn.commit()


# ------------------------------------------------------------------ the company line
def test_the_company_line():
    assert sc.about_line(RXO) == ("Trucking (Industrials) · кап. $2,8 млрд · P/E 31 (прогноз 18,2) · P/S 0,6 · "
                                  "выручка −4% г/г · маржа 1,2%")


def test_a_loss_is_said_instead_of_a_pe_and_what_is_missing_is_left_out():
    info = {"sector": "Technology", "marketCap": 4.5e11, "currency": "EUR", "trailingEps": -1.2,
            "forwardPE": 45.0, "profitMargins": -0.08, "trailingPE": float("nan")}
    assert sc.about_line(info) == "Technology · кап. €450,0 млрд · убыточна (прогноз P/E 45) · маржа −8,0%"
    assert sc.about_line({}) is None and sc.about_line({"marketCap": "n/a"}) is None


def test_a_price_to_sales_across_two_currencies_is_left_out():
    info = {"industry": "Oil & Gas Integrated", "currency": "NOK", "financialCurrency": "USD",
            "priceToSalesTrailing12Months": 8.7, "trailingPE": 11.6}
    assert sc.about_line(info) == "Oil & Gas Integrated · P/E 11,6"


def test_a_coin_and_a_failed_fetch_have_no_line(capsys):
    def boom(symbol):
        raise RuntimeError("offline")
    assert sc.about("CRYPTO:BTC", info_fn=lambda s: RXO) is None
    assert sc.about("RXO", info_fn=boom) is None and "no company summary" in capsys.readouterr().err
    assert sc.about("RXO", info_fn=lambda s: RXO).startswith("Trucking")


# ------------------------------------------------------------------ the amount
def test_the_amount_is_the_risk_over_the_stop_capped_by_the_largest_share(conn):
    account(conn, 2000.0)
    assert sc.amount_eur(conn, 0.20, TODAY) == pytest.approx(100.0)       # 1% of 2000 over a 20% stop
    assert sc.amount_eur(conn, 0.05, TODAY) == pytest.approx(200.0)       # 400 by the risk, 10% of the account
    sc.handle_command(conn, "/size risk 2")
    sc.handle_command(conn, "/size max 50")
    assert sc.amount_eur(conn, 0.10, TODAY) == pytest.approx(400.0)


def test_no_amount_without_a_stop_a_fresh_account_or_euros(conn):
    assert sc.amount_eur(conn, 0.10, TODAY) is None
    account(conn, 2000.0, day=TODAY - dt.timedelta(days=30))
    assert sc.amount_eur(conn, 0.10, TODAY) is None
    account(conn, 2000.0)
    assert sc.amount_eur(conn, None, TODAY) is None
    account(conn, 2000.0, currency="USD")
    assert sc.amount_eur(conn, 0.10, TODAY) is None


# ------------------------------------------------------------------ /size
def test_size_shows_and_changes_the_settings(conn, monkeypatch):
    sent = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    monkeypatch.setattr(sc.dt, "date", type("D", (dt.date,), {"today": classmethod(lambda cls: TODAY)}))
    account(conn, 192.0)
    tb._handle_message(conn, "/size")
    assert sent[-1] == ("Риск 1% счёта на покупку, не больше 10% счёта в одной. Сейчас: счёт €192 — "
                        "при стопе −10% это €19.\n" + "\n".join(sc.USAGE.values()))
    tb._handle_message(conn, "/size risk 0,5")
    assert sent[-1].startswith("Риск 0,5% счёта на покупку, не больше 10% счёта в одной.")
    assert sc.settings(conn) == {"risk": 0.5, "max": 10.0}


@pytest.mark.parametrize("text, answer", [("/size risk 9", sc.USAGE["risk"]), ("/size max 0", sc.USAGE["max"]),
                                          ("/size risk x", sc.USAGE["risk"]),
                                          ("/size what", "\n".join(sc.USAGE.values()))])
def test_a_bad_size_gets_the_usage_and_changes_nothing(conn, text, answer):
    assert sc.handle_command(conn, text) == answer and sc.settings(conn) == sc.DEFAULTS


# ------------------------------------------------------------------ in the signal
def test_a_pick_gets_its_company_line_and_amount_and_the_message_says_them(conn):
    account(conn, 192.0)
    pick = {"ticker": "RXO", "source": "SEC13DG", "score": 65.0, "stop_pct": 0.10,
            "reasons": ["активист 13D: 19,3%", "6 мес. +81%"], "t212": True}
    sc.enrich(conn, pick, TODAY, info_fn=lambda s: RXO)
    assert pick["amount_eur"] == 19.2 and pick["about"].startswith("Trucking (Industrials)")
    assert weekly.buy_text(pick) == (
        "🟢 <b>RXO!</b>: покупка — активист 13D: 19,3%; 6 мес. +81%; балл 65, стоп −10%, купить на €19\n"
        "Компания: Trucking (Industrials) · кап. $2,8 млрд · P/E 31 (прогноз 18,2) · P/S 0,6 · "
        "выручка −4% г/г · маржа 1,2%")


def test_a_pick_with_no_context_reads_as_before(conn):
    pick = {"ticker": "CRYPTO:SOL", "score": 61.0, "stop_pct": 0.2, "reasons": ["тренд"], "risk": True}
    sc.enrich(conn, pick, TODAY, info_fn=lambda s: RXO)
    assert "about" not in pick and "amount_eur" not in pick
    assert weekly.buy_text(pick) == "🟢 <b>SOL!</b>: покупка — тренд; балл 61, стоп −20%, высокий риск"
