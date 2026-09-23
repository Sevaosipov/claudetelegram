"""The crypto dossier. Offline: every source and the outlook are stubbed."""
from __future__ import annotations

import datetime as dt

import pytest

import assets
import crypto_research
import crypto_treasury as ct
import db
from conftest import add_house_txn

BTC = assets.crypto_asset("BTC")
TODAY = dt.date.today()


@pytest.fixture
def offline(monkeypatch):
    closes = [50_000.0 * (1.001 ** i) for i in range(400)]
    bars = [((TODAY - dt.timedelta(days=399 - i)).isoformat(), c) for i, c in enumerate(closes)]
    monkeypatch.setattr(crypto_research.sources, "price_history", lambda a, days=800: (bars, "Binance"))
    monkeypatch.setattr(crypto_research.sources, "current_price", lambda a: (None, None))
    monkeypatch.setattr(crypto_research.sources, "indicators", lambda a, closes=None: (None, None))
    monkeypatch.setattr(crypto_research.sources, "news", lambda a, name=None: (
        [{"title": "Bitcoin climbs", "publisher": "CoinDesk", "published": "2026-09-22", "url": "u"}],
        "CoinDesk/Cointelegraph"))
    monkeypatch.setattr(crypto_research.sources, "coin_name", lambda conn, s: "Bitcoin")
    monkeypatch.setattr(crypto_research.outlook, "lookup", lambda conn, a: {"status": "no_table"})
    return closes


def test_build_collects_price_trend_and_the_bots_own_data(conn, offline):
    db.save_crypto_treasury_txn(conn, ct.TreasuryTxn(
        "acc", "Strategy Inc", "MSTR", "1", "BTC", "P", 950, 79670.0, 75.7e6,
        (TODAY - dt.timedelta(days=2)).isoformat(), "8-K", "https://sec.test/x"))
    add_house_txn(conn, "CRYPTO:BTC", "Hon. Someone", "$50,001 - $100,000")
    rep = crypto_research.build(conn, BTC)
    assert rep["kind"] == "crypto" and rep["found"] and rep["name"] == "Bitcoin"
    assert rep["current"] == pytest.approx(offline[-1])
    assert rep["changes"]["30 дней"] == pytest.approx((1.001 ** 30 - 1) * 100)
    assert rep["trend"] == "up" and rep["sources"]["prices"] == "Binance"
    assert rep["treasury"][0][1] == "Strategy Inc" and rep["political"]


def test_report_texts(conn, offline):
    rep = crypto_research.build(conn, BTC)
    report = crypto_research.format_report(rep)
    assert "BTC — Bitcoin" in report and "$BTC" in report and "📈 Прогноз на месяц" in report
    assert "цены: Binance" in report and "новости: CoinDesk/Cointelegraph" in report
    brief = crypto_research.format_brief(rep)
    assert brief.startswith("АКТИВ: BTC (Bitcoin, криптовалюта)") and "---NEWS---" in brief
    html = crypto_research.format_condensed(rep)
    assert html.startswith("<b>BTC — Bitcoin</b>") and "📈 Прогноз на месяц" in html


def test_nothing_found_when_no_source_has_a_price(conn, offline, monkeypatch):
    monkeypatch.setattr(crypto_research.sources, "price_history", lambda a, days=800: (None, None))
    rep = crypto_research.build(conn, BTC)
    assert not rep["found"] and "цена: недоступно сейчас" in crypto_research.format_report(rep)
