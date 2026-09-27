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
import strategy
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


def test_backfill_walks_the_range_in_weekly_slices_and_stores_trades(conn):
    slices = []

    def scan(start, end, seen, cik_lookup=None):
        slices.append((start, end))
        if start == dt.date(2026, 9, 1):
            yield "doc-1", [ct.TreasuryTxn("acc-b", "Acme", "ACME", "1", "BTC", "P", 10, 80_000.0,
                                           None, "2026-09-02", "8-K", "u")]
    assert ct.backfill(conn, 20, today=dt.date(2026, 9, 21), scan=scan) == 1
    assert slices == [(dt.date(2026, 9, 1), dt.date(2026, 9, 7)),
                      (dt.date(2026, 9, 8), dt.date(2026, 9, 14)),
                      (dt.date(2026, 9, 15), dt.date(2026, 9, 21))]
    assert "doc-1" in db.crypto_treasury_seen(conn)


def test_backfill_resumes_past_documents_already_read(conn):
    db.mark_crypto_treasury_seen(conn, "doc-1")
    conn.commit()
    seen_args = []

    def scan(start, end, seen, cik_lookup=None):
        seen_args.append(set(seen))
        return iter(())
    ct.backfill(conn, 6, today=dt.date(2026, 9, 21), scan=scan)
    assert seen_args == [{"doc-1"}]


def test_backfill_uses_the_paced_scanner_by_default(conn, monkeypatch):
    calls = []
    monkeypatch.setattr(ct, "scan_new_filings",
                        lambda s, e, seen, cik_lookup=None: calls.append((s, e)) or iter(()))
    ct.backfill(conn, 3, today=dt.date(2026, 9, 21))
    assert calls == [(dt.date(2026, 9, 18), dt.date(2026, 9, 21))]


def test_backfill_command(conn, monkeypatch):
    got = {}
    monkeypatch.setattr(ct, "backfill", lambda c, days, cik_lookup=None: got.setdefault("days", days) and 0)
    monkeypatch.setattr(db, "connect", lambda path: conn)
    monkeypatch.setattr("cik_map.CikMap", lambda: None)
    assert ct.main(["--backfill", "365"]) == 0 and got["days"] == 365


# ------------------------------------------------------ treasury: signals
def _add_treasury(conn, units, avg=80_000.0, total=None, side="P", filed=None, acc="acc-1",
                  coin="BTC", company="Acme Corp", cik="1", co_ticker="ACME"):
    db.save_crypto_treasury_txn(conn, ct.TreasuryTxn(
        accession=acc, company=company, ticker=co_ticker, cik=cik, coin=coin, side=side,
        units=units, avg_price_usd=avg, total_usd=total, filed_date=filed or TODAY.isoformat(),
        form="8-K", source_url="https://sec.test/doc"))
    conn.commit()


def _weeks_ago(n: int) -> str:
    """A day inside the calendar week n weeks before this one."""
    return (TODAY - dt.timedelta(weeks=n)).isoformat()


def test_a_big_week_of_company_buying_is_a_signal(conn):
    _add_treasury(conn, 1_000)                                   # $80m this week, no history yet
    [sig] = cluster.find_treasury_signals(conn, today=TODAY)
    assert sig.bullish and sig.ticker == "CRYPTO:BTC" and sig.company == "компании: Acme Corp"
    assert sig.total_value == pytest.approx(80_000_000 / 1.16)
    assert sig.details[0] == "$80.0 млн за неделю"
    assert "порог по умолчанию: мало истории" in sig.details


def test_a_small_week_is_not(conn):
    _add_treasury(conn, 500)                                     # $40m, under €50m
    assert cluster.find_treasury_signals(conn, today=TODAY) == []


def test_a_routine_week_for_a_weekly_buyer_is_not(conn):
    for k in range(1, 21):                                       # $160m every week for 20 weeks
        _add_treasury(conn, 2_000, filed=_weeks_ago(k), acc=f"w{k}")
    _add_treasury(conn, 2_000, acc="now")                        # the same again this week
    assert cluster.find_treasury_signals(conn, today=TODAY) == []


def test_a_week_above_the_usual_says_how_unusual(conn):
    for k in range(1, 21):
        _add_treasury(conn, 2_000, filed=_weeks_ago(k), acc=f"w{k}")
    _add_treasury(conn, 6_000, acc="now")                        # three times the usual week
    [sig] = cluster.find_treasury_signals(conn, today=TODAY)
    assert sig.details[0] == "$480.0 млн за неделю — больше, чем в 100% из 20 недель"


def test_buyers_are_listed_largest_first_and_a_first_time_buyer_is_marked(conn):
    _add_treasury(conn, 1, filed=(TODAY - dt.timedelta(days=400)).isoformat(), acc="old",
                  company="Strategy Inc", cik="2", co_ticker="MSTR")      # history reaches a year back
    _add_treasury(conn, 900, acc="s", company="Strategy Inc", cik="2", co_ticker="MSTR")
    _add_treasury(conn, 300, acc="a")                                     # Acme's first purchase ever
    [sig] = cluster.find_treasury_signals(conn, today=TODAY)
    assert sig.details[1] == "Strategy Inc (MSTR) $72.0 млн · Acme Corp (ACME) $24.0 млн (впервые)"
    assert sig.member_names == ["Strategy Inc", "Acme Corp"]


def test_no_first_time_mark_before_a_year_of_history(conn):
    _add_treasury(conn, 1_000)
    [sig] = cluster.find_treasury_signals(conn, today=TODAY)
    assert "впервые" not in sig.details[1]


def test_units_only_purchase_is_valued_at_the_latest_stated_price(conn):
    _add_treasury(conn, 1, acc="priced")                          # states $80,000 a coin
    _add_treasury(conn, 1_000, avg=None, acc="unpriced")          # no price in the filing
    [sig] = cluster.find_treasury_signals(conn, today=TODAY)
    assert sig.total_value == pytest.approx(1_001 * 80_000 / 1.16)


def test_a_week_alerts_once(conn):
    _add_treasury(conn, 1_000)
    [sig] = cluster.find_treasury_signals(conn, today=TODAY)
    bot._commit_signals(conn, [sig])
    _add_treasury(conn, 1_000, acc="acc-2")                       # more buying the same week
    assert cluster.find_treasury_signals(conn, today=TODAY) == []


def test_last_weeks_buying_is_not_this_weeks_signal(conn):
    _add_treasury(conn, 1_000, filed=_weeks_ago(1))
    assert cluster.find_treasury_signals(conn, today=TODAY) == []


def test_a_large_company_sale_is_a_caution(conn):
    _add_treasury(conn, 200, side="S", filed=_days_ago(3))        # $16m
    [sig] = cluster.find_treasury_signals(conn, today=TODAY)
    assert not sig.bullish and sig.company == "Acme Corp (ACME)"
    assert sig.window_end == _days_ago(3)


def test_a_small_company_sale_is_not(conn):
    _add_treasury(conn, 100, side="S")                            # $8m, under €10m
    assert cluster.find_treasury_signals(conn, today=TODAY) == []


def test_an_old_sale_is_not_resurfaced(conn):
    _add_treasury(conn, 200, side="S", filed=_days_ago(30))
    assert cluster.find_treasury_signals(conn, today=TODAY) == []


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


def test_farside_page_parses():
    flows = crypto_etf.parse_farside("BTC", fixture_text("farside_btc_snippet.html"))
    got = sorted((f.coin, f.date, f.fund, f.flow_usd) for f in flows)
    want = sorted([("BTC", "2026-09-24", "IBIT", 162.6e6), ("BTC", "2026-09-24", "FBTC", 12.9e6),
                   ("BTC", "2026-09-24", "GBTC", -4.0e6), ("BTC", "2026-09-25", "IBIT", 1_097.0e6),
                   ("BTC", "2026-09-25", "FBTC", -49.3e6)])
    assert [g[:3] for g in got] == [w[:3] for w in want]
    assert [g[3] for g in got] == pytest.approx([w[3] for w in want])


def test_farside_page_without_a_table_is_empty():
    assert crypto_etf.parse_farside("BTC", "<html>redesigned</html>") == []


def test_etf_flows_are_stored_and_the_newest_day_is_rewritten(conn):
    db.save_etf_flows(conn, [crypto_etf.Flow("BTC", "2026-09-25", "IBIT", 1e6)])
    db.save_etf_flows(conn, [crypto_etf.Flow("BTC", "2026-09-25", "IBIT", 5e6)])   # late funds filled in
    assert conn.execute("SELECT flow_usd FROM crypto_etf_flows").fetchall() == [(5e6,)]
    assert db.etf_flow_count(conn, "BTC") == 1 and db.etf_flow_count(conn, "ETH") == 0


def test_etf_pass_loads_the_full_history_once(conn, monkeypatch):
    import passes
    calls = []
    monkeypatch.setattr(crypto_etf, "fetch_snapshots", lambda: [])

    def fake(coin, full_history=False, session=None):
        calls.append((coin, full_history))
        return [crypto_etf.Flow(coin, "2026-09-25", "IBIT" if coin == "BTC" else "ETHA", 1e6)]
    monkeypatch.setattr(crypto_etf, "fetch_farside", fake)
    passes.run_crypto_etf_pass(conn, None)
    passes.run_crypto_etf_pass(conn, None)
    assert calls == [("BTC", True), ("ETH", True), ("BTC", False), ("ETH", False)]


def test_etf_pass_survives_farside_being_down(conn, monkeypatch):
    import passes
    monkeypatch.setattr(crypto_etf, "fetch_snapshots", lambda: [])

    def down(coin, full_history=False, session=None):
        raise crypto_etf.requests.RequestException("down")
    monkeypatch.setattr(crypto_etf, "fetch_farside", down)
    assert passes.run_crypto_etf_pass(conn, None) == 0
    assert db.etf_flow_count(conn, "BTC") == 0


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


def _add_farside(conn, coin, flows_musd, fund="IBIT", newest_days_ago=1):
    """One stored day per value (in $m), oldest first, the last one `newest_days_ago` ago."""
    n = len(flows_musd)
    db.save_etf_flows(conn, [crypto_etf.Flow(coin, _days_ago(newest_days_ago + n - 1 - i), fund, m * 1e6)
                             for i, m in enumerate(flows_musd)])


def test_unusual_etf_day_is_measured_against_its_own_history(conn):
    _add_farside(conn, "BTC", [50, -40] * 30 + [300])        # 60 ordinary days, then $300m
    [sig] = cluster.find_etf_flow_signals(conn)
    assert sig.bullish and sig.company == "спот-ETF США, фондов: 1"
    assert "больше, чем в 100% из 60 дней" in sig.details[0]
    assert not any("IBIT/ETHA" in d for d in sig.details)


def test_ordinary_day_in_a_busy_market_is_not(conn):
    _add_farside(conn, "BTC", [300, -250] * 30 + [120])
    assert cluster.find_etf_flow_signals(conn) == []


def test_etf_day_floor_applies_in_a_quiet_market(conn):
    _add_farside(conn, "BTC", [5, -4] * 30 + [60])            # top of its history, but under $100m
    assert cluster.find_etf_flow_signals(conn) == []


def test_unusual_outflow_streak_is_a_bearish_signal(conn):
    _add_farside(conn, "ETH", [-30, 40] * 30 + [-90, -90, -90])
    [sig] = cluster.find_etf_flow_signals(conn)
    assert not sig.bullish and "3 дн. подряд оттока" in sig.details[1]


def test_under_30_days_of_history_uses_the_fixed_thresholds(conn):
    _add_farside(conn, "BTC", [10, -10] * 5 + [450])
    [sig] = cluster.find_etf_flow_signals(conn)
    assert "порог по умолчанию: мало истории" in sig.details


def test_stale_farside_falls_back_to_the_issuer_snapshots(conn):
    _add_farside(conn, "BTC", [50, -40] * 30 + [900], newest_days_ago=10)
    _add_etf(conn, "IBIT", 2, 1_000_000_000)
    _add_etf(conn, "IBIT", 1, 1_010_000_000)                  # +$500m on the issuer page
    [sig] = cluster.find_etf_flow_signals(conn)
    assert "IBIT" in sig.company and "только IBIT/ETHA (Farside недоступен)" in sig.details


def test_dossier_flows_are_summed_across_farside_funds(conn):
    _add_farside(conn, "BTC", [10, 20])
    db.save_etf_flows(conn, [crypto_etf.Flow("BTC", _days_ago(1), "FBTC", 5e6)])
    days = cluster.daily_etf_flows(conn)["BTC"]
    assert days[-1][1] == pytest.approx(25e6) and days[-1][2] == ["FBTC", "IBIT"]


def test_calibration_hides_future_etf_flows():
    import calibrate_strategy
    assert calibrate_strategy._VISIBLE["crypto_etf_flows"] == "date <= '{d}'"


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
    """A manual-run --min-score 35 (the daily digest sorts by strategy.py's tiers,
    not this score): a EUR 94m treasury buy (Strive's 1,355 BTC) clears it,
    BitMine's EUR 66m week doesn't, a $400m ETF day does."""
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
    _add_treasury(conn, 1_000)
    sigs = cluster.find_house_clusters(conn) + cluster.find_treasury_signals(conn)
    cluster.find_corroboration(conn, sigs)
    assert {s.source: s.corroborated_by for s in sigs} == {
        "HOUSE": ["CRYPTO_TREASURY"], "CRYPTO_TREASURY": ["HOUSE"]}


@pytest.mark.parametrize("html", [False, True])
def test_crypto_signal_formats(conn, html):
    _add_treasury(conn, 1_000)
    [sig] = cluster.find_treasury_signals(conn)
    text = telegram_notify.format_any_signal(sig, html=html)
    assert "ПОКУПКИ КОМПАНИЙ ЗА НЕДЕЛЮ: CRYPTO:BTC" in text and "1,000 BTC" in text
    assert "1 крипто" in telegram_notify.format_signals_digest([sig])


def test_onchain_alert_says_it_is_not_a_disclosure(conn):
    sig = cluster.CryptoSignal("CRYPTO_ONCHAIN", "exchange_flow", "CRYPTO:BTC", "кошельки бирж",
                               True, 3_000, 2e8, "2026-09-22T08:00", "2026-09-23T08:00", [], None, ["k"])
    assert "не раскрытие" in telegram_notify.format_crypto_signal(sig)


def test_crypto_signal_journals_with_its_kind(conn):
    _add_treasury(conn, 1_000)
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


def test_falling_price_below_its_average_confirms_a_caution(conn, monkeypatch):
    _closes(monkeypatch, [100.0] * 20 + [99, 98, 97, 96, 95, 94, 93, 92])
    assert crypto.trend_confirms_down(crypto.price_trend(conn, "BTC"))


def test_rising_price_does_not_confirm_a_caution(conn, monkeypatch):
    _closes(monkeypatch, [100.0] * 20 + [101, 102, 103, 104, 105, 106, 107, 110])
    assert not crypto.trend_confirms_down(crypto.price_trend(conn, "BTC"))
    assert not crypto.trend_confirms_down(None)


def test_cautions_are_journaled_and_marked_even_though_never_sent(conn):
    _add_treasury(conn, 200, side="S")
    [sig] = cluster.find_treasury_signals(conn, today=TODAY)
    sig.tier = strategy.CAUTION
    bot._record_cautions(conn, strategy.Selection([], [], True, cautions=[strategy.Tiered(sig, "caution")]))
    assert conn.execute("SELECT tier, kind, ticker FROM signal_journal").fetchall() == [
        ("caution", "treasury", "CRYPTO:BTC")]
    assert cluster.find_treasury_signals(conn, today=TODAY) == []
