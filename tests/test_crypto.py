"""The crypto sources: parsing, the three finders, and how their signals flow through
scoring, formatting, journaling and alert state. Offline like the rest of the suite --
the parser tests read excerpts of real filings saved in tests/fixtures/."""
from __future__ import annotations

import datetime as dt
import json

import pytest
import requests

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


THIRTEEN = ("BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "AVAX", "HYPE", "LTC", "ENA", "LINK", "TRX", "SUI")


@pytest.mark.parametrize("text,symbol", [
    ("Hyperliquid [CT]", "HYPE"), ("HYPE [CT]", "HYPE"), ("Hyperliquid (HYPE) [CT]", "HYPE"),
    ("Ethena [CT]", "ENA"), ("ENA [CT]", "ENA"),
    ("Binance Coin [CT]", "BNB"), ("BNB [CT]", "BNB"), ("Binance Coin (BNB) [CT]", "BNB"),
    ("Tron [CT]", "TRX"), ("TRX [CT]", "TRX"),
    ("Sui [CT]", "SUI"), ("SUI [CT]", "SUI"),
    ("XRP [CT]", "XRP"), ("Ripple [CT]", "XRP"), ("Dogecoin [CT]", "DOGE"), ("Litecoin (LTC) [CT]", "LTC"),
    ("Chainlink [CT]", "LINK"), ("Avalanche [CT]", "AVAX"), ("AVAX [CT]", "AVAX"),
    ("Bitcoin Cash [CT]", "BCH"),                     # the longer name still wins over bitcoin
])
def test_the_new_coins_resolve_from_asset_text(text, symbol):
    assert crypto.symbol_for_text(text) == symbol


@pytest.mark.parametrize("text", [
    "Electronic Arts Inc [ST]",          # "tron" is inside "electronic"
    "Patron Holdings [ST]",
    "Suite Property Trust [ST]",         # "sui" is inside "suite"
    "Pursuit Holdings [ST]",
    "Hyperlink Systems [ST]", "Venom Inc [ST]",
])
def test_a_coin_name_inside_another_word_is_not_a_coin(text):
    assert crypto.symbol_for_text(text) is None


def test_every_scored_coin_is_known_to_crypto():
    """The thirteen scored coins resolve as tickers and have a CoinGecko id for spot prices."""
    for sym in THIRTEEN:
        assert sym in crypto.SYMBOLS and sym in crypto.COINGECKO_IDS, sym
        assert crypto.is_crypto(crypto.ticker(sym)) and crypto.symbol_of(crypto.ticker(sym)) == sym
        assert crypto.yf_symbol(crypto.ticker(sym)) == f"{sym}-USD"
    assert {"ADA", "DOT", "BCH", "ETC"} <= crypto.SYMBOLS        # the earlier extras stay


@pytest.mark.parametrize("symbol,cg_id", [("BNB", "binancecoin"), ("HYPE", "hyperliquid"), ("ENA", "ethena"),
                                          ("TRX", "tron"), ("SUI", "sui")])
def test_the_new_coins_have_their_coingecko_ids(symbol, cg_id):
    assert crypto.COINGECKO_IDS[symbol] == cg_id


def test_a_new_coins_spot_price_asks_coingecko_for_its_id():
    asked = []

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"hyperliquid": {"usd": 38.5}}

    class Session:
        def get(self, url, params=None, timeout=None):
            asked.append(params)
            return Resp()
    assert crypto.price_usd(None, "HYPE", Session()) == 38.5
    assert asked == [{"ids": "hyperliquid", "vs_currencies": "usd"}]


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


# ---- the alts: one positive and one negative per coin
ALTS = ("SOL", "XRP", "BNB", "DOGE", "AVAX", "HYPE", "LTC", "ENA", "LINK", "TRX", "SUI")


def test_the_queries_cover_the_thirteen_coins():
    assert ct.QUERIES == {
        "BTC": '"bitcoin"', "ETH": '"ether" OR "ethereum"', "SOL": '"solana"', "XRP": '"XRP"', "BNB": '"BNB"',
        "DOGE": '"dogecoin"', "AVAX": '"AVAX"', "HYPE": '"hyperliquid"', "LTC": '"litecoin"',
        "ENA": '"ethena"', "LINK": '"chainlink"', "TRX": '"TRX" OR "TRON"', "SUI": '"SUI"'}
    assert list(ct.QUERIES) == list(THIRTEEN)


def test_every_coin_has_a_query_words_and_price_bounds():
    assert set(ct.PLAUSIBLE_PRICE_USD) == set(ct.QUERIES) == set(ct._COIN_WORDS.values()) == set(THIRTEEN)


def test_the_plausible_price_bounds():
    assert ct.PLAUSIBLE_PRICE_USD == {
        "BTC": (1_000, 1_000_000), "ETH": (50, 100_000), "SOL": (5, 5_000), "XRP": (0.05, 100),
        "BNB": (20, 20_000), "DOGE": (0.005, 20), "AVAX": (1, 2_000), "HYPE": (1, 5_000),
        "LTC": (5, 5_000), "ENA": (0.02, 100), "LINK": (1, 2_000), "TRX": (0.01, 20), "SUI": (0.1, 500)}


@pytest.mark.parametrize("text,coin,units,avg", [
    ("purchased 1,250,000 SOL at an average price of $182.40", "SOL", 1_250_000, 182.40),
    ("acquired 25,000,000 XRP at an average price of $2.45 per XRP", "XRP", 25_000_000, 2.45),
    ("purchased 480,000 BNB at an average price of approximately $850.25", "BNB", 480_000, 850.25),
    ("acquired 500,000,000 DOGE at an average price of $0.24", "DOGE", 500_000_000, 0.24),
    ("purchased 2,000,000 AVAX at an average price of $31.40", "AVAX", 2_000_000, 31.40),
    ("acquired 5,000,000 HYPE tokens", "HYPE", 5_000_000, None),
    ("acquired 5,000,000 HYPE tokens at an average price of $38.20", "HYPE", 5_000_000, 38.20),
    ("purchased 929,548 LTC at an average price of $95.10", "LTC", 929_548, 95.10),
    ("acquired 400,000,000 ENA at an average price of $0.62", "ENA", 400_000_000, 0.62),
    ("purchased 1,500,000 LINK at an average price of $18.30", "LINK", 1_500_000, 18.30),
    ("acquired 365,000,000 TRX at an average price of $0.31", "TRX", 365_000_000, 0.31),
    ("acquired 365,000,000 TRON at an average price of $0.31", "TRX", 365_000_000, 0.31),
    ("purchased 108,000,000 SUI tokens at an average price of $3.62", "SUI", 108_000_000, 3.62),
    ("purchased 20,000 LINK coins", "LINK", 20_000, None),
])
def test_an_alt_purchase_is_parsed(text, coin, units, avg):
    [t] = ct.parse_text(f"Acme Corp. {text}.")
    assert (t["coin"], t["side"], t["units"], t["avg"]) == (coin, "P", units, avg)


@pytest.mark.parametrize("text,coin", [
    ("purchased 1,000 Solana", "SOL"), ("acquired 1,000 solana tokens", "SOL"),
    ("bought 7,000 Dogecoin", "DOGE"), ("purchased 1,000 LITECOIN", "LTC"), ("purchased 90 Avalanche", "AVAX"),
    ("acquired 3,000 Chainlink", "LINK"), ("acquired 5,000 Hyperliquid", "HYPE"), ("acquired 800 Ethena", "ENA"),
])
def test_a_full_name_matches_in_any_case(text, coin):
    [t] = ct.parse_text(text)
    assert t["coin"] == coin


def test_an_alt_sale_is_a_sale():
    [t] = ct.parse_text("Acme sold 250,000 SOL at an average price of $190.00 for total consideration of $47.5 million.")
    assert (t["coin"], t["side"], t["units"], t["avg"], t["total"]) == ("SOL", "S", 250_000, 190.0, 47_500_000)


@pytest.mark.parametrize("text", [
    "purchased 10,000 sol",                              # a ticker is upper case only
    "purchased 1,250,000 Sol at an average price of $182.40",
    "bought 5,000 xrp", "purchased 100 bnb", "acquired 100 doge", "purchased 100 avax",
    "purchased 5 hype", "purchased 100 ltc", "acquired 100 ena", "bought 100 link", "bought 100 Link",
    "acquired 100 trx", "acquired 100 tron", "acquired 100 Tron", "purchased 100 sui", "purchased 100 Sui",
    "generated hype", "the hype around the token",
    "the link to the press release", "a link between the two", "sold sui generis rights",
    "purchased 100 shares of SOL Corp", "purchased up to 500,000 SOL", "may purchase 500 SOL",
    "holds 2,000,000 SOL", "mined 100 DOGE", "purchased 5,000 SOL mining rigs",
    "acquired 1,000 LTC miners", "acquired 5 LINK machines",
    "the Company has purchased SOL and other tokens",    # no unit count
])
def test_not_an_alt_purchase(text):
    assert ct.parse_text(text) == []


@pytest.mark.parametrize("text", [
    "Acme purchased 1,250,000 SOL at an average price of $18,240.",      # a misread thousands separator
    "Acme purchased 1,250,000 SOL at an average price of $1.82.",
    "Acme acquired 500,000,000 DOGE at an average price of $240.",
    "Acme acquired 25,000,000 XRP at an average price of $245.",
    "Acme purchased 480,000 BNB at an average price of $8.50.",
    "Acme purchased 5,000,000 HYPE at an average price of $0.38.",
    "Acme purchased 108,000,000 SUI at an average price of $3,620.",
])
def test_an_alt_price_outside_its_plausible_bounds_is_rejected(text):
    assert ct.parse_text(text) == []


def test_an_implausible_alt_total_is_dropped_but_the_trade_is_kept():
    [t] = ct.parse_text("Acme acquired 1,000,000 SOL for approximately $1 million.")        # $1 a coin
    assert (t["coin"], t["units"], t["total"]) == ("SOL", 1_000_000, None)
    [t] = ct.parse_text("Acme acquired 1,000,000 SOL for approximately $182 million.")      # $182 a coin
    assert t["total"] == pytest.approx(182e6)


def test_bitcoin_and_ether_words_are_still_found_in_any_case():
    assert [t["coin"] for t in ct.parse_text("Acme bought 5 btc. Acme purchased 6 ETH. Acme bought 7 Ether.")] == [
        "BTC", "ETH", "ETH"]


def test_two_alts_in_one_document_are_two_trades():
    text = "Acme purchased 1,000 SOL at an average price of $180.00. Acme purchased 5,000 LINK at an average price of $18.00."
    assert [(t["coin"], t["units"]) for t in ct.parse_text(text)] == [("SOL", 1000), ("LINK", 5000)]


_SOL_TABLE = ("SOL Update On October 1, 2026, Acme announced updates with respect to its holdings: During Period "
              "September 24, 2026 to September 30, 2026 SOL Purchased (1) Aggregate Purchase Price (in millions) (2) "
              "Average Purchase Price (2) Aggregate SOL Holdings 120,000 $ 21.9 $ 182.40 2,100,000 $ 380.0 $ 181.00 "
              "(1) The purchases were made using USD Cash.")


def test_a_weekly_table_for_an_alt_is_parsed_like_strategys():
    [t] = ct.parse_text(_SOL_TABLE)
    assert (t["coin"], t["side"], t["units"], t["avg"], t["total"]) == ("SOL", "P", 120_000, 182.40, 21_900_000)


def test_a_table_head_ticker_is_upper_case_only():
    assert ct.parse_text(_SOL_TABLE.replace("SOL Purchased", "sol Purchased")) == []
    assert ct.parse_text(_SOL_TABLE.replace("SOL Purchased", "Solana Purchased"))[0]["coin"] == "SOL"


def test_a_table_for_an_alt_with_a_price_out_of_bounds_is_rejected():
    assert ct.parse_text(_SOL_TABLE.replace("$ 182.40", "$ 18,240").replace("21.9", "2,188,800")) == []


def test_a_table_that_disagrees_with_itself_is_rejected_for_an_alt_too():
    assert ct.parse_text(_SOL_TABLE.replace("$ 21.9", "$ 90.0")) == []


# ---- V4: an upper-case ticker followed by a company or security noun is not a coin
@pytest.mark.parametrize("text", [
    "The Company purchased 500,000 SOL Strategies common shares.",
    "The Company purchased 10 LTC properties.",
    "The Company acquired 100 TRON Inc shares.",
    "acquired 1,000 SOL Inc", "acquired 1,000 SOL Corp", "acquired 1,000 SOL Corporation", "acquired 1,000 SOL Ltd",
    "acquired 1,000 SOL LLC", "acquired 1,000 SOL Holdings", "acquired 1,000 SOL Group",
    "bought 1,000 SUI shares", "bought 1,000 SUI common stock", "bought 1,000 SUI stock", "bought 1,000 AVAX warrants",
    "bought 1,000 LINK notes", "bought 1,000 XRP units", "bought 1,000 HYPE common shares",
    "purchased 1,000 BNB Network Company shares",             # a capitalised word after the ticker
    "purchased 1,000 ENA Foundation tokens", "purchased 1,000 DOGE Labs",
    "purchased 1,000 SOL tokens Inc",                        # after the optional «tokens» word too
])
def test_an_upper_case_ticker_followed_by_a_company_or_security_noun_is_not_a_coin(text):
    assert ct.parse_text(text) == []


@pytest.mark.parametrize("text,coin,units", [
    ("Acme acquired 5,000,000 HYPE tokens", "HYPE", 5_000_000),
    ("Acme purchased 1,250,000 SOL at an average price of $182.40", "SOL", 1_250_000),
    ("Acme acquired 2,000 SOL, bringing its total holdings to 2,170,000 SOL.", "SOL", 2_000),
    ("Acme acquired 2,000 SOL. The Company now holds more.", "SOL", 2_000),
    ("Acme acquired 2,000 SOL (as defined below) for cash.", "SOL", 2_000),
    ("Acme acquired 2,000 SOL tokens for approximately $380,000.", "SOL", 2_000),
    ("Acme purchased 7,000 LINK coins and staked them.", "LINK", 7_000),
])
def test_a_ticker_followed_by_anything_else_is_still_a_coin(text, coin, units):
    [t] = ct.parse_text(text)
    assert (t["coin"], t["units"]) == (coin, units)


def test_the_noun_rule_is_for_the_alt_tickers_bitcoin_ether_and_the_full_names_parse_as_before():
    assert [t["coin"] for t in ct.parse_text("Acme purchased 50 BTC Holdings units.")] == ["BTC"]
    assert [t["coin"] for t in ct.parse_text("Acme purchased 50 ETH Group shares.")] == ["ETH"]
    assert [t["coin"] for t in ct.parse_text("Acme purchased 50 Solana Group shares.")] == ["SOL"]


def test_a_rejected_mention_does_not_hide_a_real_trade_after_it():
    text = "Acme acquired 100 TRON Inc shares and purchased 1,250,000 SOL at an average price of $182.40."
    assert [(t["coin"], t["units"]) for t in ct.parse_text(text)] == [("SOL", 1_250_000)]


# ---- V5: the unit count accepts a multiplier word
@pytest.mark.parametrize("text,coin,units", [
    ("Acme acquired 12.6 million HYPE tokens", "HYPE", 12_600_000),
    ("Acme purchased 2.2 million SOL", "SOL", 2_200_000),
    ("Acme purchased 1.5 billion DOGE", "DOGE", 1_500_000_000),
    ("Acme purchased 300 thousand LINK", "LINK", 300_000),
    ("Acme purchased 2.2m SOL", "SOL", 2_200_000),
    ("Acme purchased 1.5bn DOGE", "DOGE", 1_500_000_000),
    ("Acme purchased 4 mn XRP", "XRP", 4_000_000),
    ("Acme purchased approximately 12.6 million additional HYPE tokens", "HYPE", 12_600_000),
    ("Acme purchased an aggregate of 3.1 million Solana", "SOL", 3_100_000),
    ("Acme acquired 2.5 million ETH", "ETH", 2_500_000),
    ("Acme acquired 1.2 Million bitcoin", "BTC", 1_200_000),
])
def test_the_unit_count_accepts_a_multiplier_word(text, coin, units):
    [t] = ct.parse_text(text)
    assert (t["coin"], t["units"]) == (coin, units)


def test_a_multiplier_scales_the_units_the_price_is_read_after_it():
    [t] = ct.parse_text("Acme acquired 12.6 million HYPE tokens at an average price of $38.20 per HYPE.")
    assert (t["units"], t["avg"]) == (12_600_000, 38.20)
    [t] = ct.parse_text("Acme purchased 2.2 million SOL for approximately $400 million.")
    assert t["total"] == 400e6 and t["units"] == 2_200_000       # $182 a coin: plausible, so the total is kept
    assert ct.parse_text("Acme purchased 2.2 million SOL for approximately $4 million.")[0]["total"] is None   # $1.8 a coin


def test_a_multiplier_still_obeys_the_other_safeguards():
    assert ct.parse_text("Acme purchased 2.2 million SOL at an average price of $18,240.") == []      # a price out of bounds
    assert ct.parse_text("Acme may purchase up to 2.2 million SOL.") == []
    assert ct.parse_text("Acme purchased 2.2 million sol.") == []                                      # a ticker in lower case
    assert ct.parse_text("Acme purchased 2.2 million SOL Strategies shares.") == []                    # a company, not a coin
    assert ct.parse_text("Acme acquired 1.2 million bitcoin mining machines.") == []


@pytest.mark.parametrize("text,units", [
    ("Acme purchased 1,355 bitcoin at an average price of $79,475.", 1355),
    ("Acme purchased 12 more bitcoin.", 12), ("Acme purchased 12 additional bitcoin.", 12),
    ("Acme bought 3 BTC.", 3), ("Acme bought 3 BTC at an average price of $79,475.", 3),
    ("Acme acquired 27,562 ETH.", 27_562), ("Acme bought 1,000 million shares of stock and purchased 5 bitcoin.", 5),
])
def test_plain_numbers_before_bitcoin_and_ether_parse_exactly_as_before(text, units):
    """No word that merely starts with m or b (more, bitcoin, BTC) is read as a multiplier."""
    assert [t["units"] for t in ct.parse_text(text)] == [units]


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
    assert slices == [(dt.date(2026, 9, 15), dt.date(2026, 9, 21)),      # newest first
                      (dt.date(2026, 9, 8), dt.date(2026, 9, 14)),
                      (dt.date(2026, 9, 1), dt.date(2026, 9, 7))]
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


# ------------------------------------------------------ treasury: retries
_HIT = {"_id": "0001-26-1:ex99.htm", "_source": {
    "ciks": ["0001829311"], "display_names": ["BITMINE  (BMNR)  (CIK 0001829311)"],
    "file_date": "2026-09-21", "form": "8-K"}}


def _resp(status, body=""):
    """A real requests.Response, so raise_for_status behaves as it does live."""
    r = requests.Response()
    r.status_code = status
    r._content = (body if isinstance(body, str) else json.dumps(body)).encode()
    r.encoding = "utf-8"
    r.url = "https://example.test/"
    return r


class _Session:
    """Answers each GET with the next queued reply, raising it if it is an exception."""
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def sleeps(monkeypatch):
    """Every pause the scanner takes, recorded instead of waited out."""
    got = []
    monkeypatch.setattr(ct.time, "sleep", got.append)
    return got


@pytest.fixture
def two_queries(monkeypatch):
    """The two coin queries the scripted sessions below were written for: bitcoin, then ether."""
    monkeypatch.setattr(ct, "QUERIES", {"BTC": ct.QUERIES["BTC"], "ETH": ct.QUERIES["ETH"]})


def test_efts_search_retries_a_transient_server_error(sleeps, two_queries):
    session = _Session(_resp(500), _resp(200, {"hits": {"hits": [_HIT]}}),   # bitcoin
                       _resp(200, {"hits": {"hits": []}}))                   # ether
    assert ct.search(dt.date(2026, 9, 15), dt.date(2026, 9, 21), session) == [_HIT]
    # The retry waits first; the fair-access pause still follows every answered request.
    assert sleeps == [ct.RETRY_PAUSES_SECONDS[0], ct.REQUEST_PAUSE_SECONDS,
                      ct.REQUEST_PAUSE_SECONDS]


@pytest.mark.parametrize("failure", [
    _resp(500), _resp(429), _resp(503), requests.ConnectionError("reset"),
    requests.Timeout("read timed out"),
])
def test_document_fetch_retries_a_transient_failure(failure, sleeps, monkeypatch):
    monkeypatch.setattr(ct, "search", lambda start, end, session: [_HIT])
    session = _Session(failure, _resp(200, "Over the past week, we acquired 27,562 ETH."))
    [(doc_id, txns)] = ct.scan_new_filings(dt.date(2026, 9, 15), dt.date(2026, 9, 21),
                                           set(), session=session)
    assert doc_id == _HIT["_id"] and [t.units for t in txns] == [27_562]
    assert sleeps == [ct.RETRY_PAUSES_SECONDS[0], ct.REQUEST_PAUSE_SECONDS]


def test_a_missing_document_is_not_retried(sleeps, monkeypatch):
    monkeypatch.setattr(ct, "search", lambda start, end, session: [_HIT])
    session = _Session(_resp(404))
    assert list(ct.scan_new_filings(dt.date(2026, 9, 15), dt.date(2026, 9, 21), set(),
                                    session=session)) == [(_HIT["_id"], [])]
    assert session.calls == 1 and sleeps == [ct.REQUEST_PAUSE_SECONDS]


@pytest.mark.parametrize("failure,raised", [
    (_resp(500), requests.HTTPError), (requests.ConnectionError("reset"), requests.ConnectionError),
])
def test_a_persistent_failure_still_raises(failure, raised, sleeps):
    """So bot._run_source reports the source as failed rather than as quiet: the queries fail, each after its three
    attempts, and after MAX_CONSECUTIVE_FAILURES in a row the search gives up (EDGAR is down)."""
    n = ct.MAX_CONSECUTIVE_FAILURES
    session = _Session(*[failure] * (3 * n))
    with pytest.raises(raised):
        ct.search(dt.date(2026, 9, 15), dt.date(2026, 9, 21), session)
    assert session.calls == 3 * n
    assert sleeps == list(ct.RETRY_PAUSES_SECONDS) * n


# ------------------------------------------------------ treasury: one coin's query failing
_HIT_SOL = {"_id": "0002-26-2:ex99.htm", "_source": {
    "ciks": ["0001234567"], "display_names": ["DEFI DEV  (DFDV)  (CIK 0001234567)"],
    "file_date": "2026-09-22", "form": "8-K"}}
_NO_HITS = {"hits": {"hits": []}}
_START, _END = dt.date(2026, 9, 15), dt.date(2026, 9, 21)


class _Efts:
    """EFTS and the archive in one stand-in. `by_query` maps a query text to what EFTS answers for it --
    a payload, a Response, an exception to raise, or a function of the request params (for paging); any
    other query answers with no hits. `docs` maps a document URL to its text."""
    def __init__(self, by_query=None, docs=None):
        self.by_query, self.docs, self.asked, self.fetched = by_query or {}, docs or {}, [], []

    def get(self, url, params=None, timeout=None):
        if url != ct.EFTS_URL:
            self.fetched.append(url)
            return _resp(200, self.docs[url]) if url in self.docs else _resp(404)
        self.asked.append(params["q"])
        reply = self.by_query.get(params["q"], _NO_HITS)
        if callable(reply):
            reply = reply(params)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, requests.Response) else _resp(200, reply)


def _hits(*hits):
    return {"hits": {"hits": list(hits)}}


def test_every_coins_query_is_asked_and_the_hits_are_deduplicated(sleeps):
    session = _Efts({ct.QUERIES["BTC"]: _hits(_HIT), ct.QUERIES["SOL"]: _hits(_HIT, _HIT_SOL)})
    assert ct.search(_START, _END, session) == [_HIT, _HIT_SOL]            # one hit per document
    assert session.asked == list(ct.QUERIES.values())
    assert len(session.asked) == 13 and sleeps == [ct.REQUEST_PAUSE_SECONDS] * 13


def test_one_coins_query_failing_does_not_lose_the_other_coins_filings(sleeps, capsys):
    session = _Efts({ct.QUERIES["BTC"]: _hits(_HIT), ct.QUERIES["ETH"]: _resp(500),
                     ct.QUERIES["SOL"]: _hits(_HIT_SOL)})
    assert ct.search(_START, _END, session) == [_HIT, _HIT_SOL]
    assert "[crypto_treasury] ETH query failed: HTTPError" in capsys.readouterr().err
    assert session.asked.count(ct.QUERIES["ETH"]) == 3                      # its three attempts, then on
    assert session.asked[-1] == ct.QUERIES["SUI"]                           # the last coin was still asked


@pytest.mark.parametrize("failure", [_resp(503), requests.ConnectionError("reset"),
                                     requests.Timeout("read timed out"), _resp(200, "<html>not json</html>")])
def test_any_failure_of_one_query_is_skipped(failure, sleeps, capsys):
    session = _Efts({ct.QUERIES["DOGE"]: failure, ct.QUERIES["SOL"]: _hits(_HIT_SOL)})
    assert ct.search(_START, _END, session) == [_HIT_SOL]
    assert "DOGE query failed" in capsys.readouterr().err


def test_the_source_fails_when_every_query_failed(sleeps, two_queries):
    """With only two coins to ask (fewer than the streak that means "down"), failing both is failing every query."""
    down = {q: _resp(500) for q in ct.QUERIES.values()}
    with pytest.raises(requests.HTTPError):
        ct.search(_START, _END, _Efts(down))
    down[ct.QUERIES["ETH"]] = _hits(_HIT_SOL)                               # one coin still answers
    assert ct.search(_START, _END, _Efts(down)) == [_HIT_SOL]


# ---- V6: EDGAR down -- after three failed queries in a row, stop
def test_the_streak_that_means_edgar_is_down_is_three():
    assert ct.MAX_CONSECUTIVE_FAILURES == 3


def test_three_failed_queries_in_a_row_stop_the_search_and_fail_the_source(sleeps, capsys):
    session = _Efts({q: _resp(500) for q in ct.QUERIES.values()})
    with pytest.raises(requests.HTTPError):
        ct.search(_START, _END, session)
    first_three = [ct.QUERIES[c] for c in ("BTC", "ETH", "SOL")]
    assert session.asked == [q for q in first_three for _ in range(3)]       # nine requests, not 39
    assert sleeps == list(ct.RETRY_PAUSES_SECONDS) * 3                       # the retry pauses of three queries only
    err = capsys.readouterr().err
    assert "SOL query failed" in err and "3 queries in a row failed" in err and "EDGAR looks down" in err
    assert "XRP" not in err                                                  # the fourth was never asked


def test_the_streak_counts_failures_in_a_row_not_in_all(sleeps):
    """Every other coin fails: ten failures in all, never three together -- the search goes on to the end."""
    answers = {}
    for i, q in enumerate(ct.QUERIES.values()):
        answers[q] = _resp(503) if i % 2 == 0 else _hits({**_HIT_SOL, "_id": f"000{i}-26-9:ex99.htm"})
    session = _Efts(answers)
    hits = ct.search(_START, _END, session)
    assert len(hits) == 6 and len(set(session.asked)) == 13                  # every coin was asked


def test_two_failures_then_an_answer_start_the_streak_again(sleeps):
    queries = list(ct.QUERIES.values())
    answers = {queries[0]: _resp(500), queries[1]: _resp(500), queries[2]: _hits(_HIT_SOL),
               queries[3]: _resp(500), queries[4]: _resp(500), queries[5]: _hits(_HIT)}
    session = _Efts(answers)
    assert ct.search(_START, _END, session) == [_HIT_SOL, _HIT]
    assert len(set(session.asked)) == 13


def test_what_the_first_queries_found_is_given_up_with_the_source_when_edgar_goes_down(sleeps):
    """The source is reported failed, so the day's hits are not processed: the next run searches the window again."""
    queries = list(ct.QUERIES.values())
    answers = {queries[0]: _hits(_HIT), queries[1]: _resp(500), queries[2]: _resp(500), queries[3]: _resp(500)}
    with pytest.raises(requests.HTTPError):
        ct.search(_START, _END, _Efts(answers))


def test_a_connection_that_refuses_gives_up_after_three_coins_too(sleeps):
    session = _Efts({q: requests.ConnectionError("refused") for q in ct.QUERIES.values()})
    with pytest.raises(requests.ConnectionError):
        ct.search(_START, _END, session)
    assert len(session.asked) == 9 and len(sleeps) == 6


def test_a_failure_after_a_first_page_keeps_that_pages_hits(sleeps, monkeypatch):
    monkeypatch.setattr(ct, "PAGE_SIZE", 2)
    other = {"_id": "0003-26-3:ex99.htm", "_source": _HIT_SOL["_source"]}

    def paged(params):
        return _hits(_HIT_SOL, other) if params["from"] == 0 else _resp(500)
    session = _Efts({ct.QUERIES["SOL"]: paged, ct.QUERIES["BTC"]: _hits(_HIT)})
    assert ct.search(_START, _END, session) == [_HIT, _HIT_SOL, other]


def test_scanning_goes_on_with_the_coins_that_answered(sleeps, capsys):
    """The bitcoin query is down; a solana filing is still found, fetched and parsed."""
    doc = "Over the week the Company purchased 1,250,000 SOL at an average price of $182.40."
    session = _Efts({ct.QUERIES["BTC"]: _resp(500), ct.QUERIES["SOL"]: _hits(_HIT_SOL)},
                    docs={ct.doc_url(_HIT_SOL): doc})
    [(doc_id, txns)] = ct.scan_new_filings(_START, _END, set(), session=session)
    assert doc_id == _HIT_SOL["_id"]
    assert [(t.coin, t.side, t.units, t.avg_price_usd, t.ticker) for t in txns] == [
        ("SOL", "P", 1_250_000, 182.40, "DFDV")]


def test_the_backfill_reads_the_alts_too(conn, sleeps, monkeypatch):
    """`crypto_treasury.py --backfill N` needs no new interface: its slices ask every coin's query."""
    doc = "Over the week the Company purchased 1,250,000 SOL at an average price of $182.40."
    session = _Efts({ct.QUERIES["SOL"]: _hits(_HIT_SOL)}, docs={ct.doc_url(_HIT_SOL): doc})
    monkeypatch.setattr(ct, "new_session", lambda: session)
    assert ct.backfill(conn, 6, today=dt.date(2026, 9, 21)) == 1                  # one weekly slice
    assert session.asked == list(ct.QUERIES.values())
    assert conn.execute("SELECT coin, side, units FROM crypto_treasury_txns").fetchall() == [("SOL", "P", 1_250_000)]
    assert _HIT_SOL["_id"] in db.crypto_treasury_seen(conn)


# ---- V1: --reread
_OLD_DOC = ("Over the week Acme purchased 1,355 bitcoin at an average price of approximately $79,475 per bitcoin "
            "and purchased 1,250,000 SOL at an average price of $182.40.")


def _read_by_the_old_parser(conn):
    """The state a database is in after the BTC/ETH-only parser read _HIT_SOL: its bitcoin trade stored, the document seen."""
    db.save_crypto_treasury_txn(conn, ct.TreasuryTxn(
        _HIT_SOL["_id"].split(":")[0], "DEFI DEV", "DFDV", "0001234567", "BTC", "P", 1355.0, 79475.0, None,
        "2026-09-22", "8-K", ct.doc_url(_HIT_SOL)))
    db.mark_crypto_treasury_seen(conn, _HIT_SOL["_id"])
    conn.commit()


def _old_document_session(monkeypatch):
    session = _Efts({ct.QUERIES["BTC"]: _hits(_HIT_SOL), ct.QUERIES["SOL"]: _hits(_HIT_SOL)},
                    docs={ct.doc_url(_HIT_SOL): _OLD_DOC})
    monkeypatch.setattr(ct, "new_session", lambda: session)
    return session


def _stored(conn):
    return conn.execute("SELECT coin, side, units FROM crypto_treasury_txns ORDER BY coin").fetchall()


def test_a_seen_document_is_not_read_again_by_a_plain_backfill(conn, sleeps, monkeypatch):
    _read_by_the_old_parser(conn)
    session = _old_document_session(monkeypatch)
    assert ct.backfill(conn, 6, today=dt.date(2026, 9, 21)) == 0
    assert session.fetched == [] and _stored(conn) == [("BTC", "P", 1355.0)]


def test_reread_reads_a_seen_document_again_and_stores_its_alt_trade(conn, sleeps, monkeypatch):
    """The filing was read before the parser knew solana: with reread its SOL trade is stored now, the bitcoin
    trade is not stored twice, and the seen set loses nothing."""
    _read_by_the_old_parser(conn)
    session = _old_document_session(monkeypatch)
    assert ct.backfill(conn, 6, today=dt.date(2026, 9, 21), reread=True) == 1
    assert session.fetched == [ct.doc_url(_HIT_SOL)]
    assert _stored(conn) == [("BTC", "P", 1355.0), ("SOL", "P", 1_250_000.0)]
    assert db.crypto_treasury_seen(conn) == {_HIT_SOL["_id"]}


def test_a_second_reread_stores_nothing_new_and_duplicates_nothing(conn, sleeps, monkeypatch):
    _read_by_the_old_parser(conn)
    _old_document_session(monkeypatch)
    assert ct.backfill(conn, 6, today=dt.date(2026, 9, 21), reread=True) == 1
    assert ct.backfill(conn, 6, today=dt.date(2026, 9, 21), reread=True) == 0
    assert _stored(conn) == [("BTC", "P", 1355.0), ("SOL", "P", 1_250_000.0)]


def test_reread_still_marks_what_it_reads_and_deletes_no_seen_row(conn, sleeps, monkeypatch):
    other = "0003-26-3:old.htm"
    db.mark_crypto_treasury_seen(conn, other)                  # a document no query returns now
    _read_by_the_old_parser(conn)
    _old_document_session(monkeypatch)
    ct.backfill(conn, 6, today=dt.date(2026, 9, 21), reread=True)
    assert db.crypto_treasury_seen(conn) == {_HIT_SOL["_id"], other}


def test_the_daily_pass_still_skips_a_seen_document(conn, sleeps, monkeypatch):
    import passes
    _read_by_the_old_parser(conn)
    session = _old_document_session(monkeypatch)
    monkeypatch.setattr("cik_map.CikMap", lambda: None)
    assert passes.run_crypto_treasury_pass(conn, _Args()) == 0
    assert session.fetched == [] and _stored(conn) == [("BTC", "P", 1355.0)]


def test_the_backfill_command_takes_reread_and_is_called_as_before_without_it(conn, monkeypatch):
    got = {}
    monkeypatch.setattr(ct, "backfill", lambda c, days, cik_lookup=None, **kw: got.update(days=days, kw=kw) or 0)
    monkeypatch.setattr(db, "connect", lambda path: conn)
    monkeypatch.setattr("cik_map.CikMap", lambda: None)
    assert ct.main(["--backfill", "365", "--reread"]) == 0 and got == {"days": 365, "kw": {"reread": True}}
    got.clear()
    assert ct.main(["--backfill", "365"]) == 0 and got == {"days": 365, "kw": {}}


class _Args:
    crypto_days = 7


def test_the_daily_pass_stores_the_alt_trades_of_the_queries_that_answered(conn, sleeps, monkeypatch):
    import passes
    doc = "Over the week the Company purchased 1,250,000 SOL at an average price of $182.40."
    session = _Efts({ct.QUERIES["BTC"]: _resp(500), ct.QUERIES["SOL"]: _hits(_HIT_SOL)},
                    docs={ct.doc_url(_HIT_SOL): doc})
    monkeypatch.setattr(ct, "new_session", lambda: session)
    monkeypatch.setattr("cik_map.CikMap", lambda: None)
    assert bot._run_source("CRYPTO_TREASURY", passes.run_crypto_treasury_pass, conn, _Args()) == 1
    assert conn.execute("SELECT coin, side, units FROM crypto_treasury_txns").fetchall() == [("SOL", "P", 1_250_000)]
    assert _HIT_SOL["_id"] in db.crypto_treasury_seen(conn)


def test_the_daily_pass_is_reported_failed_when_edgar_is_down_after_three_coins(conn, sleeps, monkeypatch, capsys):
    import passes
    session = _Efts({q: requests.ConnectionError("reset") for q in ct.QUERIES.values()})
    monkeypatch.setattr(ct, "new_session", lambda: session)
    monkeypatch.setattr("cik_map.CikMap", lambda: None)
    assert bot._run_source("CRYPTO_TREASURY", passes.run_crypto_treasury_pass, conn, _Args()) is None
    assert "[CRYPTO_TREASURY] pass failed: ConnectionError" in capsys.readouterr().err
    assert len(session.asked) == 9                                          # three coins' attempts, not thirteen's


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


# ---- the floors are per coin: €50m (week) and €10m (sale) for BTC and ETH, €10m and €5m for the alts
def _add_worth(conn, coin, eur, side="P", filed=None, acc=None):
    """A trade of `coin` worth `eur` euros (at the fixture's 1.16 dollars) as the filing's own total."""
    _add_treasury(conn, 1_000, avg=None, total=eur * 1.16, side=side, filed=filed, coin=coin,
                  acc=acc or f"acc-{coin}-{side}-{filed}-{eur}")


def test_the_treasury_floors_are_per_coin_dicts_with_a_default():
    from cluster import crypto as cc
    assert cc.TREASURY_WEEK_FLOOR_EUR == {"BTC": 50e6, "ETH": 50e6, "default": 10e6}
    assert cc.TREASURY_SALE_MIN_EUR == {"BTC": 10e6, "ETH": 10e6, "default": 5e6}


@pytest.mark.parametrize("coin", ALTS)
def test_an_alt_week_of_12m_is_a_signal(conn, coin):
    _add_worth(conn, coin, 12e6)
    [sig] = cluster.find_treasury_signals(conn, today=TODAY)
    assert sig.bullish and sig.ticker == f"CRYPTO:{coin}" and sig.total_value == pytest.approx(12e6)
    assert sig.details[0] == "$13.9 млн за неделю"


@pytest.mark.parametrize("coin", ALTS)
def test_an_alt_week_of_8m_is_not(conn, coin):
    _add_worth(conn, coin, 8e6)
    assert cluster.find_treasury_signals(conn, today=TODAY) == []


def test_the_alt_floor_is_met_at_exactly_10m(conn):
    _add_worth(conn, "SOL", 10e6)
    assert [s.ticker for s in cluster.find_treasury_signals(conn, today=TODAY)] == ["CRYPTO:SOL"]


@pytest.mark.parametrize("coin", ["BTC", "ETH"])
def test_the_bitcoin_and_ether_week_floor_is_still_50m(conn, coin):
    _add_worth(conn, coin, 12e6)
    assert cluster.find_treasury_signals(conn, today=TODAY) == []
    _add_worth(conn, coin, 37e6, acc="more")                             # €49m in all
    assert cluster.find_treasury_signals(conn, today=TODAY) == []
    _add_worth(conn, coin, 1e6, acc="more2")                             # €50m
    assert [s.ticker for s in cluster.find_treasury_signals(conn, today=TODAY)] == [f"CRYPTO:{coin}"]


def test_each_coin_is_judged_against_its_own_floor(conn):
    _add_worth(conn, "BTC", 20e6)                                        # under bitcoin's floor
    _add_worth(conn, "SOL", 12e6)
    _add_worth(conn, "ETH", 60e6)
    assert sorted(s.ticker for s in cluster.find_treasury_signals(conn, today=TODAY)) == ["CRYPTO:ETH", "CRYPTO:SOL"]


def test_the_alt_floor_still_applies_when_the_week_is_judged_against_its_history(conn):
    for k in range(1, 21):
        _add_worth(conn, "SOL", 2e6, filed=_weeks_ago(k))                # twenty quiet weeks
    _add_worth(conn, "SOL", 9e6, acc="now")                              # the best week yet -- under €10m
    assert cluster.find_treasury_signals(conn, today=TODAY) == []
    _add_worth(conn, "SOL", 2e6, acc="now2")                             # €11m
    [sig] = cluster.find_treasury_signals(conn, today=TODAY)
    assert sig.details[0] == "$12.8 млн за неделю — больше, чем в 100% из 20 недель"


def test_a_routine_alt_week_for_a_weekly_buyer_is_not_a_signal(conn):
    for k in range(1, 21):
        _add_worth(conn, "SOL", 12e6, filed=_weeks_ago(k))
    _add_worth(conn, "SOL", 12e6, acc="now")                             # the same again
    assert cluster.find_treasury_signals(conn, today=TODAY) == []


@pytest.mark.parametrize("coin", ALTS)
def test_an_alt_sale_of_6m_is_a_caution(conn, coin):
    _add_worth(conn, coin, 6e6, side="S", filed=_days_ago(3))
    [sig] = cluster.find_treasury_signals(conn, today=TODAY)
    assert not sig.bullish and sig.ticker == f"CRYPTO:{coin}" and sig.total_value == pytest.approx(6e6)


@pytest.mark.parametrize("coin", ALTS)
def test_an_alt_sale_of_4m_is_not(conn, coin):
    _add_worth(conn, coin, 4e6, side="S", filed=_days_ago(3))
    assert cluster.find_treasury_signals(conn, today=TODAY) == []


@pytest.mark.parametrize("coin", ["BTC", "ETH"])
def test_the_bitcoin_and_ether_sale_floor_is_still_10m(conn, coin):
    _add_worth(conn, coin, 6e6, side="S", filed=_days_ago(3))
    assert cluster.find_treasury_signals(conn, today=TODAY) == []
    _add_worth(conn, coin, 10e6, side="S", filed=_days_ago(2))
    [sig] = cluster.find_treasury_signals(conn, today=TODAY)
    assert sig.ticker == f"CRYPTO:{coin}" and sig.window_end == _days_ago(2)


# ---- V3: a sale with no price is valued at the coin's current price before the floor
@pytest.fixture
def spot(monkeypatch):
    """The price helper (crypto.price_usd) as a seam: `spot.prices` is what it answers, `spot.asked` what it was asked."""
    import types as _types
    run = _types.SimpleNamespace(prices={}, asked=[])

    def price_usd(conn, symbol, session=None):
        run.asked.append(symbol)
        return run.prices.get(symbol)
    monkeypatch.setattr(crypto, "price_usd", price_usd)
    return run


def _unpriced_sale(conn, coin, units, acc=None, filed=None):
    _add_treasury(conn, units, avg=None, side="S", filed=filed or _days_ago(3), coin=coin, acc=acc or f"u-{coin}-{units}")


def test_an_unpriced_sale_of_400_link_is_no_caution(conn, spot):
    spot.prices["LINK"] = 20.0                                    # $8,000
    _unpriced_sale(conn, "LINK", 400)
    assert cluster.find_treasury_signals(conn, today=TODAY) == []
    assert spot.asked == ["LINK"]


def test_an_unpriced_alt_sale_worth_6m_euro_is_a_caution_and_is_valued(conn, spot):
    spot.prices["LINK"] = 20.0
    _unpriced_sale(conn, "LINK", 348_000)                         # $6.96m = EUR 6m at the fixture's 1.16
    [sig] = cluster.find_treasury_signals(conn, today=TODAY)
    assert not sig.bullish and sig.ticker == "CRYPTO:LINK" and sig.units == 348_000
    assert sig.total_value == pytest.approx(6e6)                  # valued here, not left for enrich_signals


def test_an_unpriced_alt_sale_worth_4m_euro_is_not(conn, spot):
    spot.prices["LINK"] = 20.0
    _unpriced_sale(conn, "LINK", 232_000)                         # EUR 4m: under the alt floor of 5m
    assert cluster.find_treasury_signals(conn, today=TODAY) == []


def test_an_unpriced_sale_with_no_price_available_is_dropped(conn, spot):
    _unpriced_sale(conn, "DOGE", 5_000_000_000)                   # a fortune in doge -- but nobody can say what it is worth
    assert cluster.find_treasury_signals(conn, today=TODAY) == [] and spot.asked == ["DOGE"]


@pytest.mark.parametrize("coin,units,caution", [("BTC", 100, False), ("BTC", 200, True), ("ETH", 3_000, False),
                                                ("ETH", 5_000, True)])
def test_an_unpriced_bitcoin_or_ether_sale_must_clear_its_own_floor_of_10m(conn, spot, coin, units, caution):
    spot.prices.update(BTC=80_000.0, ETH=3_000.0)                 # 100 BTC = EUR 6.9m, 200 = 13.8m; 3,000 ETH = 7.8m, 5,000 = 12.9m
    _unpriced_sale(conn, coin, units)
    assert [s.ticker for s in cluster.find_treasury_signals(conn, today=TODAY)] == ([f"CRYPTO:{coin}"] if caution else [])


def test_a_priced_sale_never_asks_for_the_price(conn, spot):
    _add_treasury(conn, 200, side="S", filed=_days_ago(3))        # $16m at its own stated $80,000
    assert len(cluster.find_treasury_signals(conn, today=TODAY)) == 1 and spot.asked == []


def test_a_filing_with_a_priced_and_an_unpriced_row_values_the_unpriced_one_at_spot(conn, spot):
    spot.prices["LINK"] = 20.0
    _add_treasury(conn, 100, avg=None, total=1_800.0, side="S", filed=_days_ago(3), coin="LINK", acc="mixed")   # its own total
    _add_treasury(conn, 290_000, avg=None, side="S", filed=_days_ago(3), coin="LINK", acc="mixed")              # $5.8m at spot
    [sig] = cluster.find_treasury_signals(conn, today=TODAY)
    assert sig.units == 290_100 and sig.total_value == pytest.approx((1_800 + 5_800_000) / 1.16)


def test_a_price_some_filing_stated_for_the_coin_is_still_used_before_the_current_price(conn, spot):
    """_treasury_rows' own fallback (the latest average price any filing stated) stays: the price helper is for sales no
    filing could put a value on."""
    spot.prices["LINK"] = 1_000.0                                  # would make it huge -- and must not be asked
    _add_treasury(conn, 10, avg=18.0, side="P", filed=_days_ago(40), coin="LINK", acc="p")       # a purchase that states $18
    _unpriced_sale(conn, "LINK", 232_000)                          # 232,000 x $18 = $4.2m = EUR 3.6m: under the floor
    assert cluster.find_treasury_signals(conn, today=TODAY) == [] and spot.asked == []


def test_the_price_of_a_coin_is_asked_once_however_many_sales_are_unpriced(conn, spot):
    spot.prices["LINK"] = 20.0
    _unpriced_sale(conn, "LINK", 400, acc="a")
    _unpriced_sale(conn, "LINK", 500, acc="b")
    _unpriced_sale(conn, "LINK", 348_000, acc="c")
    assert [s.ticker for s in cluster.find_treasury_signals(conn, today=TODAY)] == ["CRYPTO:LINK"]
    assert spot.asked == ["LINK"]


def test_the_real_price_helper_reads_its_hour_cache_so_no_network_is_needed(conn):
    db.save_cached_value(conn, "crypto_price_usd_LINK", 20.0)
    _unpriced_sale(conn, "LINK", 348_000)
    assert [s.ticker for s in cluster.find_treasury_signals(conn, today=TODAY)] == ["CRYPTO:LINK"]


def test_with_no_cache_and_no_network_an_unpriced_sale_is_dropped_not_raised(conn):
    _unpriced_sale(conn, "LINK", 348_000)                          # the offline guard refuses the CoinGecko call
    assert cluster.find_treasury_signals(conn, today=TODAY) == []


def test_a_sale_of_a_cheap_coin_names_its_average_price_in_cents(conn):
    _add_treasury(conn, 40_000_000, avg=0.24, side="S", filed=_days_ago(3), coin="DOGE")    # $9.6m
    [sig] = cluster.find_treasury_signals(conn, today=TODAY)
    assert sig.details == ["средняя цена $0.24 за DOGE"]
    _add_treasury(conn, 200, avg=80_000.0, side="S", filed=_days_ago(3), acc="btc")         # a bitcoin sale: whole dollars
    btc = next(s for s in cluster.find_treasury_signals(conn, today=TODAY) if s.ticker == "CRYPTO:BTC")
    assert btc.details == ["средняя цена $80,000 за BTC"]


# ---- V7b: one plain format for units, in the log line, the purchases CSV and the dossier
@pytest.mark.parametrize("units,text", [
    (1_250_000, "1,250,000"), (27_562, "27,562"), (1355, "1,355"), (1000, "1,000"), (999, "999"), (12.5, "12.5"),
    (0.4321, "0.4321"), (500_000_000, "500,000,000"), (1_500_000_000, "1,500,000,000"), (950.0, "950")])
def test_units_are_a_plain_number_never_scientific(units, text):
    assert crypto.units_text(units) == text and "e" not in crypto.units_text(units)


def test_the_purchases_csv_states_units_in_the_same_plain_format(conn, tmp_path, sleeps, monkeypatch):
    import csv

    import passes
    doc = ("Acme purchased 1,250,000 SOL at an average price of $182.40. Acme purchased 27,562 ETH at an average price "
           "of $3,100. Acme purchased 12.5 BTC at an average price of $79,475. Acme acquired 500,000,000 DOGE at an "
           "average price of $0.24.")
    session = _Efts({ct.QUERIES["SOL"]: _hits(_HIT_SOL)}, docs={ct.doc_url(_HIT_SOL): doc})
    monkeypatch.setattr(ct, "new_session", lambda: session)
    monkeypatch.setattr("cik_map.CikMap", lambda: None)
    monkeypatch.setattr(passes, "CSV_PATH", tmp_path / "purchases_log.csv")
    assert passes.run_crypto_treasury_pass(conn, _Args()) == 4
    rows = {r["issuer_or_asset"]: r["amount"] for r in csv.DictReader((tmp_path / "purchases_log.csv").open(encoding="utf-8"))}
    assert rows["SOL"] == "1,250,000 SOL ($228,000,000)"
    assert rows["ETH"].startswith("27,562 ETH") and rows["BTC"].startswith("12.5 BTC")
    assert rows["DOGE"] == "500,000,000 DOGE ($120,000,000)"
    assert not any("e+" in amount for amount in rows.values())


def test_the_dossier_states_units_in_the_same_plain_format(conn):
    import crypto_research
    db.save_crypto_treasury_txn(conn, ct.TreasuryTxn("acc", "DeFi Dev", "DFDV", "1", "SOL", "P", 1_250_000, 182.40, None,
                                                     (TODAY - dt.timedelta(days=2)).isoformat(), "8-K", "u"))
    db.save_crypto_treasury_txn(conn, ct.TreasuryTxn("acc2", "Acme", "ACME", "2", "SOL", "S", 12.5, None, None,
                                                     (TODAY - dt.timedelta(days=1)).isoformat(), "8-K", "u"))
    lines = crypto_research._bot_lines({"treasury": crypto_research._treasury(conn, "SOL"), "ticker": "SOL", "etf_flows": [],
                                        "political": [], "onchain": []})
    assert any("DeFi Dev (DFDV) купила 1,250,000 SOL" in line for line in lines)
    assert any("Acme (ACME) продала 12.5 SOL" in line for line in lines)
    assert not any("e+" in line for line in lines)


def test_the_treasury_log_line_reads_for_an_alt_as_it_does_for_bitcoin():
    def line(coin, units, avg):
        return telegram_notify.format_treasury_line(
            ct.TreasuryTxn("acc", "Acme Corp", "ACME", "1", coin, "P", units, avg, None, "2026-09-22", "8-K", "u"))
    assert line("SOL", 1_250_000, 182.40).startswith("🪙 Treasury: Acme Corp (ACME) bought 1,250,000 SOL @ $182 = $228,000,000\n")
    assert line("LINK", 1_500_000, 18.30).startswith("🪙 Treasury: Acme Corp (ACME) bought 1,500,000 LINK @ $18.30 = $27,450,000\n")
    assert line("DOGE", 500_000_000, 0.24).startswith("🪙 Treasury: Acme Corp (ACME) bought 500,000,000 DOGE @ $0.24 = $120,000,000\n")
    assert line("BTC", 1355, 79475.0).startswith("🪙 Treasury: Acme Corp (ACME) bought 1,355 BTC @ $79,475 = $107,688,625\n")
    assert line("BTC", 12.5, 79475.0).startswith("🪙 Treasury: Acme Corp (ACME) bought 12.5 BTC @ $79,475")


def test_a_trade_without_a_filing_date_is_ignored(conn):
    db.save_crypto_treasury_txn(conn, ct.TreasuryTxn(
        "acc-undated", "Other Co", "OTH", "2", "BTC", "P", 5_000, 80_000.0, None, "", "8-K", "u"))
    _add_treasury(conn, 1_000)
    [sig] = cluster.find_treasury_signals(conn, today=TODAY)
    assert sig.units == 1_000 and sig.company == "компании: Acme Corp"


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


SOL_FUNDS = {"BSOL", "FSOL", "GSOL", "MSOL", "SOEZ", "TSOL", "VSOL"}


def test_farside_sol_page_parses_its_seven_funds():
    flows = crypto_etf.parse_farside("SOL", fixture_text("farside_sol_snippet.html"))
    assert {f.coin for f in flows} == {"SOL"} and {f.fund for f in flows} == SOL_FUNDS
    assert len(flows) == 5 * 7 - 1                              # SOEZ shows "-" on 21 Sep: no figure yet
    by = {(f.date, f.fund): f.flow_usd for f in flows}
    assert ("2026-09-21", "SOEZ") not in by
    assert by[("2026-09-24", "BSOL")] == pytest.approx(-5.0e6)     # outflows are in parentheses
    assert by[("2026-09-25", "BSOL")] == pytest.approx(60.2e6)
    assert by[("2026-09-23", "GSOL")] == pytest.approx(-3.2e6)
    for day, total in (("2026-09-21", 16.2e6), ("2026-09-22", 37.6e6), ("2026-09-23", 64.6e6),
                       ("2026-09-24", -8.5e6), ("2026-09-25", 90.0e6)):
        assert sum(v for (d, _f), v in by.items() if d == day) == pytest.approx(total)
    assert sorted({f.date for f in flows}) == [f"2026-09-{d}" for d in (21, 22, 23, 24, 25)]   # no Total/Average rows


class _PageSession:
    """Answers every GET with the SOL fixture and remembers which URL was asked."""
    def __init__(self):
        self.urls = []

    def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        return _resp(200, fixture_text("farside_sol_snippet.html"))


def test_sol_is_a_farside_coin_with_no_all_data_page():
    assert crypto_etf.FARSIDE_URLS["SOL"] == "https://farside.co.uk/sol/"
    assert "SOL" not in crypto_etf.FARSIDE_ALL_URLS
    assert crypto_etf.FARSIDE_URLS["BTC"] == "https://farside.co.uk/btc/"
    assert crypto_etf.FARSIDE_URLS["ETH"] == "https://farside.co.uk/eth/"


def test_the_full_history_of_sol_is_the_recent_page():
    session = _PageSession()
    flows = crypto_etf.fetch_farside("SOL", full_history=True, session=session)
    assert session.urls == ["https://farside.co.uk/sol/"] and len(flows) == 34
    assert {f.coin for f in flows} == {"SOL"}


def test_the_recent_page_of_sol_is_the_recent_page():
    session = _PageSession()
    crypto_etf.fetch_farside("SOL", session=session)
    assert session.urls == ["https://farside.co.uk/sol/"]


def test_bitcoin_and_ether_still_have_their_all_data_pages():
    session = _PageSession()
    crypto_etf.fetch_farside("BTC", full_history=True, session=session)
    crypto_etf.fetch_farside("ETH", full_history=True, session=session)
    crypto_etf.fetch_farside("BTC", session=session)
    assert session.urls == ["https://farside.co.uk/bitcoin-etf-flow-all-data/",
                            "https://farside.co.uk/ethereum-etf-flow-all-data/", "https://farside.co.uk/btc/"]


def test_etf_flows_are_stored_and_the_newest_day_is_rewritten(conn):
    db.save_etf_flows(conn, [crypto_etf.Flow("BTC", "2026-09-25", "IBIT", 1e6)])
    db.save_etf_flows(conn, [crypto_etf.Flow("BTC", "2026-09-25", "IBIT", 5e6)])   # late funds filled in
    assert conn.execute("SELECT flow_usd FROM crypto_etf_flows").fetchall() == [(5e6,)]
    assert db.etf_flow_count(conn, "BTC") == 1 and db.etf_flow_count(conn, "ETH") == 0
    assert db.etf_flow_latest(conn, "BTC") == "2026-09-25" and db.etf_flow_latest(conn, "ETH") is None


_FUND = {"BTC": "IBIT", "ETH": "ETHA", "SOL": "BSOL"}


def _farside(calls=None, down=()):
    """A fetch_farside stand-in: one fund-day per coin, dated yesterday; a coin in
    `down` raises instead."""
    def fetch(coin, full_history=False, session=None):
        if calls is not None:
            calls.append((coin, full_history))
        if coin in down:
            raise crypto_etf.requests.RequestException("down")
        return [crypto_etf.Flow(coin, _days_ago(1), _FUND[coin], 1e6)]
    return fetch


def test_etf_pass_loads_the_full_history_once(conn, monkeypatch):
    import passes
    calls = []
    monkeypatch.setattr(crypto_etf, "fetch_farside", _farside(calls))
    passes.run_farside_pass(conn, None)
    passes.run_farside_pass(conn, None)
    assert calls == [("BTC", True), ("ETH", True), ("SOL", True),
                     ("BTC", False), ("ETH", False), ("SOL", False)]


def test_etf_pass_includes_sol(conn, monkeypatch):
    import passes
    monkeypatch.setattr(crypto_etf, "fetch_farside", _farside())
    assert list(crypto_etf.FARSIDE_URLS) == ["BTC", "ETH", "SOL"]
    assert passes.run_farside_pass(conn, None) == 3
    assert db.etf_flow_count(conn, "SOL") == 1 and db.etf_flow_latest(conn, "SOL") == _days_ago(1)


def test_the_first_sol_run_says_it_read_the_recent_page_not_the_full_history(conn, monkeypatch, capsys):
    """SOL has no all-data page: its history is built day by day, so the log must not claim a full one."""
    import passes
    monkeypatch.setattr(crypto_etf, "fetch_farside", _farside())
    passes.run_farside_pass(conn, None)
    out = capsys.readouterr().out
    assert "Farside BTC: 1 fund-day(s) (full history)" in out
    assert "Farside SOL: 1 fund-day(s)\n" in out and "Farside SOL: 1 fund-day(s) (full history)" not in out


def test_etf_pass_survives_farside_being_down(conn, monkeypatch):
    import passes
    monkeypatch.setattr(crypto_etf, "fetch_farside", _farside(down={"BTC"}))
    assert passes.run_farside_pass(conn, None) == 2                  # ETH and SOL still collected
    assert db.etf_flow_count(conn, "BTC") == 0 and db.etf_flow_count(conn, "ETH") == 1
    assert db.etf_flow_count(conn, "SOL") == 1


def test_farside_pass_fails_when_every_coin_is_down(conn, monkeypatch):
    import passes
    monkeypatch.setattr(crypto_etf, "fetch_farside", _farside(down=set(crypto_etf.FARSIDE_URLS)))
    with pytest.raises(RuntimeError, match="Farside unavailable for every coin"):
        passes.run_farside_pass(conn, None)


def test_farside_pass_reloads_the_history_after_a_gap(conn, monkeypatch):
    import passes
    calls = []
    db.save_etf_flows(conn, [crypto_etf.Flow("BTC", _days_ago(11), "IBIT", 1e6),
                             crypto_etf.Flow("ETH", _days_ago(10), "ETHA", 1e6)])
    monkeypatch.setattr(crypto_etf, "fetch_farside", _farside(calls))
    passes.run_farside_pass(conn, None)
    assert calls == [("BTC", True), ("ETH", False), ("SOL", True)]     # SOL has nothing stored yet


def test_farside_page_parsing_to_nothing_is_reported(conn, monkeypatch, capsys):
    import passes
    monkeypatch.setattr(crypto_etf, "fetch_farside", lambda coin, full_history=False, session=None: [])
    assert passes.run_farside_pass(conn, None) == 0
    assert "Farside BTC: 0 rows parsed — page layout may have changed" in capsys.readouterr().err


def test_an_ishares_failure_does_not_stop_farside(conn, monkeypatch):
    import passes

    def ishares_down(session=None):
        raise ValueError("IBIT: shares outstanding / NAV not found on the product page")
    monkeypatch.setattr(crypto_etf, "fetch_snapshots", ishares_down)
    monkeypatch.setattr(crypto_etf, "fetch_farside", _farside())
    assert bot._run_source("CRYPTO_ETF", passes.run_crypto_etf_pass, conn, None) is None
    assert bot._run_source("CRYPTO_ETF_FARSIDE", passes.run_farside_pass, conn, None) == 3
    assert db.etf_flow_count(conn, "BTC") == 1 and db.etf_flow_count(conn, "ETH") == 1


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


def test_etf_streak_floor_applies_in_a_quiet_market(conn):
    _add_farside(conn, "BTC", [-10, 10] * 30 + [-60, -60, -60])   # top 3-day total, but $180m < $250m
    assert cluster.find_etf_flow_signals(conn) == []


def test_unusual_outflow_streak_is_a_bearish_signal(conn):
    _add_farside(conn, "ETH", [-30, 40] * 30 + [-90, -90, -90])
    [sig] = cluster.find_etf_flow_signals(conn)
    assert not sig.bullish and "3 дн. подряд оттока" in sig.details[1]


def test_under_30_days_of_history_uses_the_fixed_thresholds(conn):
    _add_farside(conn, "BTC", [10, -10] * 5 + [450])
    [sig] = cluster.find_etf_flow_signals(conn)
    assert "порог по умолчанию: мало истории" in sig.details


# ----- the bars are per coin: SOL's funds are an order of magnitude smaller than bitcoin's
def test_the_thresholds_are_per_coin_dicts_with_a_default():
    from cluster import crypto as cc
    assert cc.ETF_DAY_FLOW_USD["SOL"] == 50e6 and cc.ETF_STREAK_MIN_USD["SOL"] == 100e6
    assert cc.ETF_DAY_FLOOR_USD["SOL"] == 25e6 and cc.ETF_STREAK_FLOOR_USD["SOL"] == 60e6
    for table, value in ((cc.ETF_DAY_FLOW_USD, 400e6), (cc.ETF_STREAK_MIN_USD, 500e6),
                         (cc.ETF_DAY_FLOOR_USD, 100e6), (cc.ETF_STREAK_FLOOR_USD, 250e6)):
        assert table["BTC"] == table["ETH"] == table["default"] == value


def test_a_60m_sol_day_with_under_30_days_of_history_is_a_signal(conn):
    _add_farside(conn, "SOL", [1, -1] * 5 + [60], fund="BSOL")
    [sig] = cluster.find_etf_flow_signals(conn)
    assert sig.ticker == "CRYPTO:SOL" and sig.bullish and sig.url == "https://farside.co.uk/sol/"
    assert sig.company == "спот-ETF США, фондов: 1"
    assert sig.details[0].startswith(f"за {_days_ago(1)}: +$60 млн")
    assert "порог по умолчанию: мало истории" in sig.details


def test_a_30m_sol_day_with_under_30_days_of_history_is_not(conn):
    _add_farside(conn, "SOL", [1, -1] * 5 + [30], fund="BSOL")
    assert cluster.find_etf_flow_signals(conn) == []


def test_the_fixed_day_bar_of_sol_is_50m_and_bitcoins_stays_400m(conn):
    _add_farside(conn, "SOL", [1, -1] * 5 + [49], fund="BSOL")
    _add_farside(conn, "BTC", [10, -10] * 5 + [399])
    assert cluster.find_etf_flow_signals(conn) == []
    _add_farside(conn, "SOL", [1, -1] * 5 + [50], fund="BSOL")
    _add_farside(conn, "BTC", [10, -10] * 5 + [400])
    assert sorted(s.ticker for s in cluster.find_etf_flow_signals(conn)) == ["CRYPTO:BTC", "CRYPTO:SOL"]


def test_a_sol_outflow_day_is_a_caution(conn):
    _add_farside(conn, "SOL", [1, -1] * 5 + [-60], fund="BSOL")
    [sig] = cluster.find_etf_flow_signals(conn)
    assert sig.ticker == "CRYPTO:SOL" and not sig.bullish


def test_the_fixed_three_day_bar_of_sol_is_100m_and_bitcoins_stays_500m(conn):
    _add_farside(conn, "SOL", [-1, 1] * 5 + [-35, -35, -35], fund="BSOL")        # $105m over three days
    [sig] = cluster.find_etf_flow_signals(conn)
    assert sig.ticker == "CRYPTO:SOL" and not sig.bullish and "3 дн. подряд оттока, всего $105 млн" in sig.details[1]
    conn.execute("DELETE FROM crypto_etf_flows")
    _add_farside(conn, "SOL", [-1, 1] * 5 + [-30, -30, -30], fund="BSOL")        # $90m: under it
    _add_farside(conn, "BTC", [-1, 1] * 5 + [-160, -160, -160])                   # $480m: under bitcoin's
    assert cluster.find_etf_flow_signals(conn) == []


def test_the_relative_day_rule_uses_sol_floors(conn):
    """Top of its history and over SOL's $25m floor -- though far under the $100m floor of bitcoin."""
    _add_farside(conn, "SOL", [5, -4] * 30 + [30], fund="BSOL")
    [sig] = cluster.find_etf_flow_signals(conn)
    assert sig.ticker == "CRYPTO:SOL" and "больше, чем в 100% из 60 дней" in sig.details[0]
    conn.execute("DELETE FROM crypto_etf_flows")
    _add_farside(conn, "SOL", [5, -4] * 30 + [20], fund="BSOL")                   # top of its history, under $25m
    assert cluster.find_etf_flow_signals(conn) == []
    conn.execute("DELETE FROM crypto_etf_flows")
    _add_farside(conn, "BTC", [5, -4] * 30 + [30])                                # the same $30m on bitcoin: nothing
    assert cluster.find_etf_flow_signals(conn) == []


def test_the_relative_streak_rule_uses_sol_floors(conn):
    _add_farside(conn, "SOL", [-3, 3] * 30 + [-20, -20, -20], fund="BSOL")        # $60m: SOL's floor
    [sig] = cluster.find_etf_flow_signals(conn)
    assert sig.ticker == "CRYPTO:SOL" and "3 дн. подряд оттока, всего $60 млн" in sig.details[1]
    conn.execute("DELETE FROM crypto_etf_flows")
    _add_farside(conn, "SOL", [-3, 3] * 30 + [-15, -15, -15], fund="BSOL")        # $45m: under it
    assert cluster.find_etf_flow_signals(conn) == []


def test_a_stale_sol_has_no_issuer_page_to_fall_back_to_and_says_nothing(conn):
    _add_farside(conn, "SOL", [5, -4] * 30 + [900], fund="BSOL", newest_days_ago=10)
    assert cluster.find_etf_flow_signals(conn) == []
    assert cluster.daily_etf_flows(conn)["SOL"][-1][0] == _days_ago(10)


def test_an_explicit_fixed_bar_still_overrides_every_coin(conn):
    _add_farside(conn, "SOL", [1, -1] * 5 + [30], fund="BSOL")
    assert cluster.find_etf_flow_signals(conn) == []
    assert [s.ticker for s in cluster.find_etf_flow_signals(conn, day_flow_usd=20e6)] == ["CRYPTO:SOL"]


def test_stale_farside_falls_back_to_the_issuer_snapshots(conn):
    _add_farside(conn, "BTC", [50, -40] * 30 + [900], newest_days_ago=10)
    _add_etf(conn, "IBIT", 2, 1_000_000_000)
    _add_etf(conn, "IBIT", 1, 1_010_000_000)                  # +$500m on the issuer page
    [sig] = cluster.find_etf_flow_signals(conn)
    assert "IBIT" in sig.company and "только IBIT/ETHA (Farside недоступен)" in sig.details


def test_a_partial_newest_day_waits_for_every_fund(conn):
    for fund in ("IBIT", "GBTC"):
        _add_farside(conn, "BTC", [5, -4] * 30, fund=fund, newest_days_ago=2)
    db.save_etf_flows(conn, [crypto_etf.Flow("BTC", _days_ago(1), "GBTC", -150e6)])   # IBIT still "-"
    assert cluster.find_etf_flow_signals(conn) == []
    assert cluster.daily_etf_flows(conn)["BTC"][-1][0] == _days_ago(2)
    db.save_etf_flows(conn, [crypto_etf.Flow("BTC", _days_ago(1), "IBIT", 700e6)])
    [sig] = cluster.find_etf_flow_signals(conn)
    assert sig.bullish and sig.details[0].startswith(f"за {_days_ago(1)}: +$550 млн")


def test_dossier_flows_are_summed_across_farside_funds(conn):
    _add_farside(conn, "BTC", [10, 20])
    db.save_etf_flows(conn, [crypto_etf.Flow("BTC", _days_ago(1), "FBTC", 5e6)])
    days = cluster.daily_etf_flows(conn)["BTC"]
    assert days[-1][1] == pytest.approx(25e6) and days[-1][2] == ["FBTC", "IBIT"]


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
    """Against a score of 35: a EUR 94m treasury buy (Strive's 1,355 BTC) clears it,
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


def test_a_caution_does_not_corroborate_a_buy(conn):
    db.journal_signal(conn, {"source": "CRYPTO_ETF", "kind": "etf_flow", "ticker": "CRYPTO:BTC",
                             "tier": "caution"})
    db.journal_signal(conn, {"source": "HOUSE", "kind": "cluster", "ticker": "CRYPTO:BTC"})
    _add_treasury(conn, 1_000)
    [sig] = cluster.find_treasury_signals(conn)
    cluster.find_corroboration(conn, [sig])
    assert sig.corroborated_by == ["HOUSE"]


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


def _rising_bars(n=40):
    return [(f"2026-08-{(i % 28) + 1:02d}", 100.0 + i) for i in range(n)]


def test_the_trend_of_a_coin_yahoo_has_no_symbol_for_comes_from_the_multi_source_history(conn, monkeypatch):
    import sources
    asked = []
    monkeypatch.setattr(crypto, "_yahoo_closes", lambda symbol: None)
    monkeypatch.setattr(sources, "price_history",
                        lambda asset, days=800: asked.append((asset.symbol, asset.kind)) or (_rising_bars(), "Bybit"))
    trend = crypto.price_trend(conn, "HYPE")
    assert asked == [("HYPE", "crypto")]
    assert trend["above_ma20"] and trend["ret_7d"] == pytest.approx((139 / 132 - 1) * 100)
    assert crypto.trend_confirms(trend)


def test_a_coin_yahoo_prices_never_asks_the_other_sources(conn, monkeypatch):
    import sources
    monkeypatch.setattr(crypto, "_yahoo_closes", lambda symbol: [100.0] * 28)

    def boom(asset, days=800):
        raise AssertionError("the other sources were asked")
    monkeypatch.setattr(sources, "price_history", boom)
    assert crypto.price_trend(conn, "BTC") is not None


def test_no_history_anywhere_means_no_trend(conn, monkeypatch):
    import sources
    monkeypatch.setattr(crypto, "_yahoo_closes", lambda symbol: None)
    monkeypatch.setattr(sources, "price_history", lambda asset, days=800: (None, None))
    assert crypto.price_trend(conn, "SUI") is None
    monkeypatch.setattr(sources, "price_history", lambda asset, days=800: (_rising_bars(15), "Bybit"))
    assert crypto.price_trend(conn, "SUI") is None            # under 21 closes is no trend


def test_a_failing_history_source_means_no_trend_not_a_crash(conn, monkeypatch):
    import sources

    def down(asset, days=800):
        raise ConnectionError("x")
    monkeypatch.setattr(crypto, "_yahoo_closes", lambda symbol: None)
    monkeypatch.setattr(sources, "price_history", down)
    assert crypto.price_trend(conn, "HYPE") is None


def test_falling_price_below_its_average_confirms_a_caution(conn, monkeypatch):
    _closes(monkeypatch, [100.0] * 20 + [99, 98, 97, 96, 95, 94, 93, 92])
    assert crypto.trend_confirms_down(crypto.price_trend(conn, "BTC"))


def test_rising_price_does_not_confirm_a_caution(conn, monkeypatch):
    _closes(monkeypatch, [100.0] * 20 + [101, 102, 103, 104, 105, 106, 107, 110])
    assert not crypto.trend_confirms_down(crypto.price_trend(conn, "BTC"))
    assert not crypto.trend_confirms_down(None)


def test_cautions_are_journaled_and_marked_even_though_never_sent(conn, monkeypatch):
    monkeypatch.setattr(cluster, "enrich_signals", lambda conn, signals: signals)     # no network
    _add_treasury(conn, 200, side="S")
    [sig] = cluster.find_treasury_signals(conn, today=TODAY)
    bot._journal(conn, [sig], None)         # a bearish coin signal is `caution` whatever the model says
    assert conn.execute("SELECT tier, kind, ticker FROM signal_journal").fetchall() == [
        ("caution", "treasury", "CRYPTO:BTC")]
    assert cluster.find_treasury_signals(conn, today=TODAY) == []
