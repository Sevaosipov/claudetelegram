"""The crypto sources: parsing, the three finders, and how their signals flow through
scoring, formatting, journaling and alert state. Offline like the rest of the suite --
the parser tests read excerpts of real filings saved in tests/fixtures/."""
from __future__ import annotations

import datetime as dt

import pytest

import bot
import cluster
import crypto
import crypto_etf
import crypto_treasury as ct
import db
import house_ptr
import telegram_notify
import tradingview
from conftest import add_house_txn, fixture_text

TODAY = dt.date.today()


def _days_ago(n: int) -> str:
    return (TODAY - dt.timedelta(days=n)).isoformat()


# ------------------------------------------------------------------ crypto.py
@pytest.mark.parametrize("text,symbol", [
    ("Bitcoin [CT]", "BTC"), ("BTC [CT]", "BTC"), ("Bitcoin (BTC) [CT]", "BTC"),
    ("Bitcoin (CRYPTO:BTC) [CT]", "BTC"), ("Ethereum [CT]", "ETH"),
    ("Bitcoin Cash [CT]", "BCH"),      # not bitcoin
    ("Solana (SOL) [CT]", "SOL"), ("Some Token [CT]", None),
])
def test_symbol_for_text(text, symbol):
    assert crypto.symbol_for_text(text) == symbol


def test_yf_symbol_maps_crypto_and_share_classes():
    assert crypto.yf_symbol("CRYPTO:BTC") == "BTC-USD"
    assert crypto.yf_symbol("BRK.B") == "BRK-B"


def test_price_is_read_from_the_cache_without_network(conn, monkeypatch):
    db.save_cached_value(conn, "crypto_price_usd_BTC", 80_000.0)
    monkeypatch.setattr(crypto.requests, "get", lambda *a, **k: pytest.fail("network"))
    assert crypto.price_usd(conn, "BTC") == 80_000.0


# --------------------------------------------------------- House crypto rows
def _house_row(asset: str):
    cur = {"asset_parts": [asset], "amount_parts": ["$50,001 - $100,000"], "owner": "",
           "type": "P", "date1": "09/01/2026", "date2": "09/05/2026"}
    return house_ptr._finalize(cur, "doc-1", {"name": "Hon. Someone"}, "http://u")


@pytest.mark.parametrize("asset", ["Bitcoin [CT]", "Bitcoin (BTC) [CT]", "BTC [CT]"])
def test_house_crypto_rows_get_a_crypto_ticker(asset):
    """"Bitcoin (BTC)" used to become ticker BTC -- a US-listed ETF's symbol."""
    assert _house_row(asset).ticker == "CRYPTO:BTC"


def test_house_crypto_is_in_the_purchase_feed():
    assert house_ptr.is_feed_worthy("Bitcoin [CT]")


def test_congressional_crypto_buys_cluster_like_stocks(conn):
    for member in ("Member One", "Member Two"):
        add_house_txn(conn, "CRYPTO:BTC", member, "$250,001 - $500,000",
                      date=(TODAY - dt.timedelta(days=3)).strftime("%m/%d/%Y"))
    sigs = cluster.find_house_clusters(conn)
    assert [s.ticker for s in sigs] == ["CRYPTO:BTC"] and sigs[0].buyer_count == 2


# ------------------------------------------------------- treasury: parsing
def test_parses_strategy_weekly_table():
    trades = ct.parse_text(fixture_text("crypto_8k_strategy.txt"))
    assert trades == [{"coin": "BTC", "side": "P", "units": 950.0, "avg": 79670.0,
                       "total": 75_700_000.0}]


def test_parses_prose_purchase():
    [t] = ct.parse_text(fixture_text("crypto_8k_strive.txt"))
    assert (t["coin"], t["side"], t["units"], t["avg"]) == ("BTC", "P", 1355.0, 79475.0)


def test_parses_a_sale_as_a_sale():
    """KULR's filing talks about 'purchasers' -- the counterparties, not KULR."""
    [t] = ct.parse_text(fixture_text("crypto_8k_kulr.txt"))
    assert (t["side"], t["units"], t["total"]) == ("S", 764.0, 58_600_000.0)


@pytest.mark.parametrize("text", [
    "The Company acquired 1,000 bitcoin mining machines from Bitmain.",
    "We may purchase up to 500 bitcoin under the program.",
    "The Company holds 12,000 bitcoin.",
    # a miner selling production, not a treasury decision
    "During the nine months, the Company mined 291.53 BTC, received other additions of "
    "27.93 BTC, sold 207.32 BTC, and settled 78.73 BTC in respect of services received.",
])
def test_not_a_treasury_trade(text):
    assert ct.parse_text(text) == []


def test_implausible_price_is_rejected():
    """A dropped thousands separator: $79 per bitcoin is a misread, not a bargain."""
    assert ct.parse_text("Acme purchased 100 bitcoin at an average price of $79 per bitcoin.") == []


def test_total_with_a_multiplier():
    [t] = ct.parse_text("Acme bought approximately 120 BTC for approximately $9.6 million.")
    assert t["total"] == pytest.approx(9_600_000)


def test_a_trade_stated_twice_in_one_document_is_one_trade():
    text = ("Acme purchased 50 bitcoin at an average price of $80,000. "
            "As noted, Acme purchased 50 bitcoin at an average price of $80,000.")
    assert len(ct.parse_text(text)) == 1


def test_primary_ticker_comes_from_the_cik_map():
    hit = {"_id": "0001-26-1:ex99.htm", "_source": {
        "ciks": ["0001829311"], "display_names": ["BITMINE  (BMNP, BMNR)  (CIK 0001829311)"],
        "file_date": "2026-09-21", "form": "8-K"}}

    class Lookup:
        def ticker(self, cik):
            return "BMNR" if cik == "0001829311" else None
    text = "Over the past week, we acquired 27,562 ETH."
    assert ct.txns_from_hit(hit, text)[0].ticker == "BMNP"          # alphabetical first
    assert ct.txns_from_hit(hit, text, Lookup())[0].ticker == "BMNR"


def test_company_and_ticker_from_display_name():
    assert ct._company("Strive, Inc.  (ASST, SATA)  (CIK 0001920406)") == ("Strive, Inc.", "ASST")
    assert ct._company("Private Co  (CIK 0000000001)") == ("Private Co  (CIK 0000000001)", None)


# ------------------------------------------------------ treasury: signals
def _add_treasury(conn, units, avg=80_000.0, total=None, side="P", filed=None, acc="acc-1",
                  coin="BTC"):
    db.save_crypto_treasury_txn(conn, ct.TreasuryTxn(
        accession=acc, company="Acme Corp", ticker="ACME", cik="1", coin=coin, side=side,
        units=units, avg_price_usd=avg, total_usd=total, filed_date=filed or _days_ago(1),
        form="8-K", source_url="https://sec.test/doc"))
    conn.commit()


def test_treasury_purchase_is_a_signal(conn):
    _add_treasury(conn, 100)                    # $8m
    [sig] = cluster.find_treasury_signals(conn)
    assert sig.ticker == "CRYPTO:BTC" and sig.bullish and sig.company == "Acme Corp (ACME)"
    assert sig.total_value == pytest.approx(8_000_000 / 1.16)


def test_small_treasury_purchase_is_not(conn):
    _add_treasury(conn, 1)                      # $80k
    assert cluster.find_treasury_signals(conn) == []


def test_treasury_purchase_with_no_price_is_kept(conn):
    """Unknown is not small: it is valued at spot later, by enrich_signals."""
    _add_treasury(conn, 100, avg=None)
    [sig] = cluster.find_treasury_signals(conn)
    assert sig.total_value is None and sig.units == 100


def test_old_treasury_filing_is_not_resurfaced(conn):
    _add_treasury(conn, 100, filed=_days_ago(60))
    assert cluster.find_treasury_signals(conn) == []


def test_treasury_signal_fires_once(conn):
    _add_treasury(conn, 100)
    [sig] = cluster.find_treasury_signals(conn)
    bot._commit_signals(conn, [sig])
    assert cluster.find_treasury_signals(conn) == []


# ------------------------------------------------------------ ETF flows
def test_ishares_page_parses():
    snap = crypto_etf.parse_page("IBIT", fixture_text("ishares_ibit_snippet.html"))
    assert (snap.coin, snap.as_of, snap.shares_outstanding, snap.nav_usd) == \
        ("BTC", "2026-09-22", 1_396_640_000, 48.93)


def test_ishares_page_without_figures_is_none():
    assert crypto_etf.parse_page("IBIT", "<html>redesigned</html>") is None


def test_flow_is_share_change_times_nav():
    [(prev, day, flow)] = crypto_etf.flows([("2026-09-22", 1_010, 50.0), ("2026-09-21", 1_000, 40.0)])
    assert (prev, day, flow) == ("2026-09-21", "2026-09-22", 500.0)


def _add_etf(conn, fund, days_ago, shares, nav=50.0):
    coin = crypto_etf.FUNDS[fund][0]
    db.save_crypto_etf_snapshot(conn, crypto_etf.Snapshot(fund, coin, _days_ago(days_ago), shares, nav))
    conn.commit()


def test_big_etf_day_is_a_signal(conn):
    _add_etf(conn, "IBIT", 2, 1_000_000_000)
    _add_etf(conn, "IBIT", 1, 1_010_000_000)          # +10m shares * $50 = $500m
    [sig] = cluster.find_etf_flow_signals(conn)
    assert sig.bullish and sig.ticker == "CRYPTO:BTC" and "IBIT" in sig.company


def test_ordinary_etf_day_is_not(conn):
    _add_etf(conn, "IBIT", 2, 1_000_000_000)
    _add_etf(conn, "IBIT", 1, 1_002_000_000)          # $100m
    assert cluster.find_etf_flow_signals(conn) == []


def test_etf_outflow_streak_fires_once_per_streak(conn):
    shares = 1_000_000_000
    for i, d in enumerate(range(5, 0, -1)):
        _add_etf(conn, "IBIT", d, shares - i * 3_000_000)   # -$150m a day, four days
    [sig] = cluster.find_etf_flow_signals(conn)
    assert not sig.bullish and "4 дн. подряд оттока" in sig.details[1]
    bot._commit_signals(conn, [sig])
    _add_etf(conn, "IBIT", 0, shares - 5 * 3_000_000)       # the streak continues
    assert cluster.find_etf_flow_signals(conn) == []


def test_stale_etf_snapshot_is_ignored(conn):
    _add_etf(conn, "IBIT", 30, 1_000_000_000)
    _add_etf(conn, "IBIT", 29, 1_100_000_000)
    assert cluster.find_etf_flow_signals(conn) == []


# ------------------------------------------------------------- on-chain
def _add_wallet(conn, address, balance, hours_ago, coin="BTC"):
    taken = (dt.datetime.now() - dt.timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    db.save_crypto_wallet_snapshot(conn, type("B", (), {
        "coin": coin, "address": address, "label": f"Exchange {address}", "balance": balance})(), taken)
    conn.commit()


def test_exchange_outflow_is_a_bullish_signal(conn):
    _add_wallet(conn, "a", 100_000, 24)
    _add_wallet(conn, "b", 50_000, 25)
    _add_wallet(conn, "a", 98_000, 0)
    _add_wallet(conn, "b", 49_000, 0)
    [sig] = cluster.find_onchain_signals(conn)
    assert sig.bullish and sig.units == 3_000 and sig.total_value is None


def test_small_exchange_move_is_not_a_signal(conn):
    _add_wallet(conn, "a", 100_000, 24)
    _add_wallet(conn, "a", 99_500, 0)
    assert cluster.find_onchain_signals(conn) == []


def test_wallet_without_a_day_old_snapshot_is_left_out(conn):
    _add_wallet(conn, "a", 100_000, 2)          # only two hours of history
    _add_wallet(conn, "a", 90_000, 0)
    assert cluster.find_onchain_signals(conn) == []


# ----------------------------------------------- scoring, output, corroboration
def test_crypto_score_grows_with_size_and_is_capped(conn):
    small = cluster.CryptoSignal("CRYPTO_ETF", "etf_flow", "CRYPTO:BTC", "x", True, None, 1e6,
                                 "", "", [], None, ["k"])
    big = cluster.CryptoSignal("CRYPTO_ETF", "etf_flow", "CRYPTO:BTC", "x", True, None, 1e12,
                               "", "", [], None, ["k"])
    assert cluster.W_CRYPTO_BASE < cluster.score_signal(small) < cluster.score_signal(big)
    assert cluster.score_signal(big) == cluster.W_CRYPTO_BASE + cluster.CAP_CRYPTO_SIZE


@pytest.mark.parametrize("value_eur,passes", [(66e6, False), (94e6, True), (345e6, True)])
def test_crypto_scores_against_the_daily_bar(value_eur, passes):
    """run_daily.sh sends at --min-score 35: a EUR 94m treasury buy (Strive's 1,355
    BTC) clears it, BitMine's EUR 66m week doesn't, a $400m ETF day does."""
    sig = cluster.CryptoSignal("CRYPTO_TREASURY", "treasury", "CRYPTO:BTC", "x", True, None,
                               value_eur, "", "", [], None, ["k"])
    assert (cluster.score_signal(sig) >= 35) is passes


def test_congressional_crypto_cluster_is_not_penalised_for_unknown_size(conn):
    for member in ("Member One", "Member Two"):
        add_house_txn(conn, "CRYPTO:BTC", member, "$250,001 - $500,000",
                      date=(TODAY - dt.timedelta(days=3)).strftime("%m/%d/%Y"))
        add_house_txn(conn, "AAA", member, "$250,001 - $500,000",
                      date=(TODAY - dt.timedelta(days=3)).strftime("%m/%d/%Y"))
    by_ticker = {s.ticker: s for s in cluster.find_house_clusters(conn)}
    for s in by_ticker.values():
        s.market_cap_eur = s.avg_daily_value = s.value_pct_of_mcap = None
    assert (cluster.score_signal(by_ticker["CRYPTO:BTC"])
            - cluster.score_signal(by_ticker["AAA"])) == -cluster.P_UNKNOWN_SIZE


def test_units_only_signal_is_valued_at_spot(conn):
    db.save_cached_value(conn, "crypto_price_usd_BTC", 116_000.0)   # = EUR 100k at 1.16
    sig = cluster.CryptoSignal("CRYPTO_ONCHAIN", "exchange_flow", "CRYPTO:BTC", "x", True,
                               3_000, None, "", "", [], None, ["k"])
    cluster.enrich_signals(conn, [sig])
    assert sig.total_value == pytest.approx(300_000_000)


def test_politician_and_company_buying_the_same_coin_corroborate(conn):
    for member in ("Member One", "Member Two"):
        add_house_txn(conn, "CRYPTO:BTC", member, "$250,001 - $500,000",
                      date=(TODAY - dt.timedelta(days=3)).strftime("%m/%d/%Y"))
    _add_treasury(conn, 100)
    sigs = cluster.find_house_clusters(conn) + cluster.find_treasury_signals(conn)
    cluster.find_corroboration(conn, sigs)
    assert {s.source: s.corroborated_by for s in sigs} == {
        "HOUSE": ["CRYPTO_TREASURY"], "CRYPTO_TREASURY": ["HOUSE"]}


@pytest.mark.parametrize("html", [False, True])
def test_crypto_signal_formats(conn, html):
    _add_treasury(conn, 100)
    [sig] = cluster.find_treasury_signals(conn)
    text = telegram_notify.format_any_signal(sig, html=html)
    assert "КОМПАНИЯ КУПИЛА: CRYPTO:BTC" in text and "100 BTC" in text
    assert "1 крипто" in telegram_notify.format_signals_digest([sig])


def test_onchain_alert_says_it_is_not_a_disclosure(conn):
    sig = cluster.CryptoSignal("CRYPTO_ONCHAIN", "exchange_flow", "CRYPTO:BTC", "кошельки бирж",
                               True, 3_000, 2e8, "2026-09-22T08:00", "2026-09-23T08:00", [], None, ["k"])
    assert "не раскрытие" in telegram_notify.format_crypto_signal(sig)


def test_crypto_signal_journals_with_its_kind(conn):
    _add_treasury(conn, 100)
    [sig] = cluster.find_treasury_signals(conn)
    row = bot._signal_features(sig)
    assert (row["source"], row["kind"], row["ticker"]) == ("CRYPTO_TREASURY", "treasury", "CRYPTO:BTC")


def test_tradingview_quotes_the_usd_pair():
    seen = {}

    class Session:
        def get(self, url, params=None, **k):
            seen["symbol"] = params["symbol"]
            raise tradingview.requests.RequestException("offline")
    tradingview.fetch_snapshot("CRYPTO:BTC", Session())
    assert seen["symbol"] == "CRYPTO:BTCUSD"


# --------------------------------------------------------------- price trend
def _closes(monkeypatch, series):
    calls = []
    monkeypatch.setattr(crypto, "_daily_closes", lambda sym: calls.append(sym) or series)
    return calls


def test_rising_price_above_its_average_confirms(conn, monkeypatch):
    _closes(monkeypatch, [100.0] * 20 + [101, 102, 103, 104, 105, 106, 107, 110])
    trend = crypto.price_trend(conn, "BTC")
    assert trend["ret_7d"] == pytest.approx((110 / 101 - 1) * 100)
    assert trend["above_ma20"] and crypto.trend_confirms(trend)


def test_falling_price_does_not_confirm(conn, monkeypatch):
    _closes(monkeypatch, [100.0] * 20 + [99, 98, 97, 96, 95, 94, 93, 92])
    assert not crypto.trend_confirms(crypto.price_trend(conn, "BTC"))


def test_trend_is_cached_between_calls(conn, monkeypatch):
    calls = _closes(monkeypatch, [100.0] * 28)
    crypto.price_trend(conn, "ETH")
    crypto.price_trend(conn, "ETH")
    assert calls == ["ETH"]


def test_no_price_history_means_no_trend(conn, monkeypatch):
    _closes(monkeypatch, None)
    assert crypto.price_trend(conn, "BTC") is None and not crypto.trend_confirms(None)
