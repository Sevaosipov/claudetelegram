"""signals_weekly.py: which buy scores the weekly run signals (spec 2026-10-04-remove-model-portfolio.md):
only a BUY, the best first, at most five, never a name the user already holds and never one signalled in
the last 30 days. Offline -- the scores are hand-built, the positions and the signal history are rows."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pytest

import db
import model
import model_score
import positions
import signals_weekly

TODAY = dt.date(2026, 10, 9)          # a Friday: the weekly run's day


def _stock(ticker="AAA", total=64.0, decision=model_score.BUY, source="SEC", **kw):
    base = dict(ticker=ticker, source=source, company=f"{ticker} Corp", signal=None, insiders=52.0,
                triggers=5.0, momentum=7.0, news=0.0, total=total, decision=decision,
                reasons=["3 инсайдера из руководства", "CEO среди покупателей", "первая покупка", "позиция +30%"],
                block=None, untradeable=None, t212=True, stop_pct=0.10, last_close=10.0)
    base.update(kw)
    return model_score.StockScore(**base)


def _coin(coin="BTC", total=60.0, decision=model_score.BUY, **kw):
    base = dict(coin=coin, ticker=f"CRYPTO:{coin}", trend=45.0, flows=15.0, news=0.0, total=total,
                trend_up=True, trend_down=False, caution=None, block=None, decision=decision,
                reasons=["выше 100-дн. средней"], stop_pct=0.20, last_close=100.0)
    base.update(kw)
    return model_score.CoinScore(**base)


def _hold(conn, ticker, source=None, *, origin="manual", t212_ticker=None):
    """An open position of the user's, as /bought or the Trading 212 sync records it."""
    conn.execute("INSERT INTO positions (ticker, source, opened_at, entry_price, origin, quantity, t212_ticker, "
                 "currency) VALUES (?,?,?,?,?,?,?,?)",
                 (ticker, source, "2026-09-01", 10.0, origin, 5.0 if origin == "t212" else None, t212_ticker,
                  "USD" if origin == "t212" else None))
    conn.commit()


def _signalled(conn, ticker, days_ago):
    conn.execute("INSERT INTO buy_signals (ticker, source, company, kind, score, stop_pct, reasons, t212, sent_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?)",
                 (ticker, "SEC", "X", "stock", 64.0, 0.10, "[]", 1, (TODAY - dt.timedelta(days=days_ago)).isoformat()))
    conn.commit()


def _picked(conn, scored):
    return [s.ticker for s in signals_weekly.pick_buys(conn, TODAY, scored)]


# ------------------------------------------------------------------ the table
def test_the_buy_signals_table_has_the_columns_the_spec_names(conn):
    columns = [(r[1], r[2]) for r in conn.execute("PRAGMA table_info(buy_signals)")]
    assert columns == [("id", "INTEGER"), ("ticker", "TEXT"), ("source", "TEXT"), ("company", "TEXT"),
                       ("kind", "TEXT"), ("score", "REAL"), ("stop_pct", "REAL"), ("reasons", "TEXT"),
                       ("t212", "INTEGER"), ("sent_at", "TEXT")]
    pk = [r[1] for r in conn.execute("PRAGMA table_info(buy_signals)") if r[5]]
    assert pk == ["id"]


def test_the_table_is_created_in_a_database_made_before_it(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE kv_cache (key TEXT PRIMARY KEY, value REAL, computed_at TEXT)")
    old.commit()
    old.close()
    conn = db.connect(path)
    conn.execute("INSERT INTO buy_signals (ticker, sent_at) VALUES ('AAA', '2026-10-09')")
    conn.commit()
    conn.close()
    assert db.connect(path).execute("SELECT ticker FROM buy_signals").fetchall() == [("AAA",)]    # kept on reconnect


def test_the_archived_paper_tables_stay_in_the_schema(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"paper_books", "paper_orders", "paper_positions", "paper_equity"} <= names


# ----------------------------------------------------------------- the picks
def test_only_a_buy_is_picked(conn):
    scored = [_stock("WAT", 50.0, model_score.WATCH), _stock("BLK", 30.0, model_score.BLOCK, block="fraud"),
              _stock("SKP", 20.0, model_score.SKIP), _stock("BUY", 64.0), _coin("ETH", 30.0, model_score.WATCH),
              _coin("BTC", 60.0)]
    assert _picked(conn, scored) == ["BUY", "CRYPTO:BTC"]            # 64 before 60


def test_the_highest_total_comes_first_whatever_order_the_scores_come_in(conn):
    scored = [_stock("LOW", 61.0), _coin("BTC", 75.0), _stock("TOP", 90.0), _stock("MID", 64.0)]
    assert _picked(conn, scored) == ["TOP", "CRYPTO:BTC", "MID", "LOW"]


def test_equal_totals_keep_the_order_they_came_in(conn):
    assert _picked(conn, [_stock("B", 64.0), _stock("A", 64.0), _stock("C", 64.0)]) == ["B", "A", "C"]


def test_at_most_five_are_picked_and_they_are_the_best_five(conn):
    scored = [_stock(f"T{i}", 60.0 + i) for i in range(8)]
    assert signals_weekly.WEEKLY_BUY_LIMIT == 5
    assert _picked(conn, scored) == ["T7", "T6", "T5", "T4", "T3"]


def test_a_name_that_is_skipped_does_not_use_up_a_place(conn):
    _hold(conn, "T7")
    scored = [_stock(f"T{i}", 60.0 + i) for i in range(8)]
    assert _picked(conn, scored) == ["T6", "T5", "T4", "T3", "T2"]


# ------------------------------------------------- at most two coins a week
def _coins(*names, top=80.0):
    """BUY coin scores, `top` for the first and one point less for each next."""
    return [_coin(c, top - i) for i, c in enumerate(names)]


def test_the_week_takes_two_coins_at_most():
    assert signals_weekly.WEEKLY_COIN_LIMIT == 2 and signals_weekly.WEEKLY_BUY_LIMIT == 5


def test_five_coin_buys_and_three_stock_buys_make_the_two_highest_coins_and_three_stocks(conn):
    scored = _coins("BTC", "ETH", "SOL", "XRP", "LINK") + [_stock(f"S{i}", 70.0 - 3 * i) for i in range(3)]
    assert _picked(conn, scored) == ["CRYPTO:BTC", "CRYPTO:ETH", "S0", "S1", "S2"]


def test_the_two_coins_are_the_two_highest_scoring_whatever_order_they_come_in(conn):
    scored = [_coin("SUI", 61.0), _coin("HYPE", 90.0), _coin("LINK", 75.0), _coin("TRX", 88.0)]
    assert _picked(conn, scored) == ["CRYPTO:HYPE", "CRYPTO:TRX"]


def test_coins_and_stocks_are_ranked_together_but_the_coins_stop_at_two(conn):
    scored = [_stock("S0", 95.0), _coin("SOL", 90.0), _stock("S1", 85.0), _coin("XRP", 80.0), _coin("LINK", 78.0),
              _stock("S2", 70.0), _stock("S3", 65.0), _stock("S4", 61.0)]
    assert _picked(conn, scored) == ["S0", "CRYPTO:SOL", "S1", "CRYPTO:XRP", "S2"]


def test_a_third_coin_does_not_take_a_stocks_place_when_there_are_too_few_stocks(conn):
    scored = _coins("BTC", "ETH", "SOL", "XRP", "LINK", "SUI") + [_stock("S0", 62.0), _stock("S1", 61.0)]
    assert _picked(conn, scored) == ["CRYPTO:BTC", "CRYPTO:ETH", "S0", "S1"]        # four, not five


def test_a_coin_that_is_skipped_does_not_use_up_one_of_the_two(conn):
    _hold(conn, "CRYPTO:BTC", "CRYPTO")
    _signalled(conn, "CRYPTO:ETH", 10)
    assert _picked(conn, _coins("BTC", "ETH", "SOL", "XRP", "LINK")) == ["CRYPTO:SOL", "CRYPTO:XRP"]


def test_a_coin_that_is_not_a_buy_does_not_use_up_one_of_the_two(conn):
    scored = [_coin("BTC", 99.0, model_score.WATCH), _coin("ETH", 98.0, model_score.BLOCK, block="x")] + _coins("SOL", "XRP", "LINK")
    assert _picked(conn, scored) == ["CRYPTO:SOL", "CRYPTO:XRP"]


def test_the_coin_limit_leaves_the_stock_rules_alone(conn):
    scored = [_stock(f"T{i}", 60.0 + i) for i in range(8)] + [_coin("BTC", 60.5)]
    assert _picked(conn, scored) == ["T7", "T6", "T5", "T4", "T3"]                # the five best; bitcoin is below them


def test_the_picks_are_the_score_objects_themselves(conn):
    scored = [_stock("AAA", 64.0)]
    assert signals_weekly.pick_buys(conn, TODAY, scored) == scored
    assert signals_weekly.pick_buys(conn, TODAY, scored)[0] is scored[0]


def test_no_scores_no_picks(conn):
    assert signals_weekly.pick_buys(conn, TODAY, []) == []


# ------------------------------------------------------------- what is held
def test_a_manual_position_is_not_signalled_again(conn):
    _hold(conn, "AAA")
    assert _picked(conn, [_stock("AAA"), _stock("BBB", 63.0)]) == ["BBB"]


def test_a_trading_212_holding_is_not_signalled_again(conn):
    _hold(conn, "GME", origin="t212", t212_ticker="GME_US_EQ")
    assert _picked(conn, [_stock("GME"), _stock("BBB", 63.0)]) == ["BBB"]


def test_a_closed_position_does_not_count_as_held(conn):
    _hold(conn, "AAA")
    conn.execute("UPDATE positions SET closed_at = '2026-10-01', close_reason = 'manual'")
    conn.commit()
    assert _picked(conn, [_stock("AAA")]) == ["AAA"]


def test_a_coin_is_held_by_its_crypto_ticker(conn):
    _hold(conn, "CRYPTO:BTC", "CRYPTO")
    assert _picked(conn, [_coin("BTC", 75.0), _coin("ETH", 70.0)]) == ["CRYPTO:ETH"]


def test_a_stock_and_a_coin_of_the_same_letters_are_two_assets(conn):
    _hold(conn, "BTC")                                          # the stock BTC (the Grayscale ETF)
    assert _picked(conn, [_coin("BTC", 75.0)]) == ["CRYPTO:BTC"]
    conn.execute("DELETE FROM positions")
    _hold(conn, "CRYPTO:BTC", "CRYPTO")
    assert _picked(conn, [_stock("BTC", 70.0)]) == ["BTC"]


def test_an_oslo_listing_is_held_on_oslo_not_as_the_us_stock_of_the_same_name(conn):
    _hold(conn, "NRC", "NORWAY")
    scored = [_stock("NRC", 70.0, source="NORWAY"), _stock("NRC", 65.0, source="SEC")]
    assert [(s.ticker, s.source) for s in signals_weekly.pick_buys(conn, TODAY, scored[:1])] == []
    assert [(s.ticker, s.source) for s in signals_weekly.pick_buys(conn, TODAY, scored[1:])] == [("NRC", "SEC")]


def test_a_us_holding_does_not_hide_the_oslo_stock_of_the_same_name(conn):
    _hold(conn, "NRC", "SEC")
    assert _picked(conn, [_stock("NRC", 70.0, source="NORWAY")]) == ["NRC"]


def test_a_stockholm_listing_is_held_with_its_venue_too(conn):
    _hold(conn, "VOLV-B", "SWEDEN")
    assert _picked(conn, [_stock("VOLV-B", 70.0, source="SEC")]) == ["VOLV-B"]
    assert _picked(conn, [_stock("VOLV-B", 70.0, source="SWEDEN")]) == []


def test_a_holding_keyed_by_the_isin_of_an_oslo_listing_holds_that_listing(conn):
    """Trading 212 keys Equinor by the ISIN of its Frankfurt line; Oslo's insider signals name it EQNR."""
    conn.execute("INSERT INTO oslo_isins (ticker, isin, fetched_at) VALUES ('EQNR', 'NO0010096985', "
                 "'2026-10-01T00:00:00')")
    _hold(conn, "NO0010096985", "T212", origin="t212", t212_ticker="EQNRd_EQ")
    assert _picked(conn, [_stock("EQNR", 70.0, source="NORWAY"), _stock("BBB", 63.0)]) == ["BBB"]
    assert _picked(conn, [_stock("EQNR", 70.0, source="SEC")]) == ["EQNR"]       # the US EQNR is another company


def test_a_holding_keyed_by_an_isin_the_cache_does_not_know_holds_only_that_isin(conn):
    _hold(conn, "DE0007164600", "T212", origin="t212", t212_ticker="SAPd_EQ")
    assert _picked(conn, [_stock("DE0007164600", 70.0, source="BAFIN"), _stock("SAP", 65.0)]) == ["SAP"]


def test_tickers_are_compared_in_any_case(conn):
    _hold(conn, "AAA")
    assert _picked(conn, [_stock("aaa", 70.0)]) == []


# ------------------------------------------------------ the 30-day re-signal rule
@pytest.mark.parametrize("days_ago, picked", [(0, False), (7, False), (29, False), (30, False), (31, True),
                                              (90, True)])
def test_a_ticker_signalled_in_the_last_30_days_is_not_signalled_again(conn, days_ago, picked):
    _signalled(conn, "AAA", days_ago)
    assert signals_weekly.RESIGNAL_DAYS == 30
    assert _picked(conn, [_stock("AAA")]) == (["AAA"] if picked else [])


def test_the_resignal_rule_is_per_ticker(conn):
    _signalled(conn, "AAA", 10)
    assert _picked(conn, [_stock("AAA", 70.0), _stock("BBB", 64.0)]) == ["BBB"]


def test_the_latest_signal_of_a_ticker_decides(conn):
    _signalled(conn, "AAA", 60)
    _signalled(conn, "AAA", 5)
    assert _picked(conn, [_stock("AAA")]) == []


def test_a_coin_signalled_this_month_is_not_signalled_again(conn):
    _signalled(conn, "CRYPTO:BTC", 14)
    assert _picked(conn, [_coin("BTC", 75.0)]) == []


def test_the_rule_counts_days_from_the_day_given(conn):
    _signalled(conn, "AAA", 0)                                  # sent on TODAY
    assert signals_weekly.pick_buys(conn, TODAY + dt.timedelta(days=31), [_stock("AAA")]) != []
    assert signals_weekly.pick_buys(conn, TODAY + dt.timedelta(days=30), [_stock("AAA")]) == []


def test_a_signal_dated_in_the_future_still_blocks(conn):
    """A clock set back: the row is not older than 30 days."""
    _signalled(conn, "AAA", -3)
    assert _picked(conn, [_stock("AAA")]) == []


# ------------------------------------------------------------- what is stored
def test_a_pick_is_stored_as_what_its_message_needs(conn):
    record = signals_weekly.pick_record(_stock("GME", 70.0, t212=False, stop_pct=0.12, company="GameStop"))
    assert record == {"ticker": "GME", "source": "SEC", "company": "GameStop", "kind": "stock", "score": 70.0,
                      "stop_pct": 0.12, "t212": False,
                      "reasons": ["3 инсайдера из руководства", "CEO среди покупателей", "первая покупка"]}
    assert json.loads(json.dumps(record)) == record


def test_a_coin_pick_is_stored_with_its_symbol_as_the_company_and_crypto_as_the_source(conn):
    record = signals_weekly.pick_record(_coin("BTC", 75.0))
    assert (record["ticker"], record["source"], record["company"], record["kind"], record["t212"]) == (
        "CRYPTO:BTC", "CRYPTO", "BTC", "crypto", None)


# ------------------------------------------------- the high-risk flag of an alt
@pytest.mark.parametrize("coin", model.COINS[2:])
def test_an_alts_pick_carries_the_risk_flag(coin):
    record = signals_weekly.pick_record(_coin(coin, 75.0))
    assert record["risk"] is True and record["company"] == coin and record["kind"] == "crypto"
    assert json.loads(json.dumps(record)) == record


@pytest.mark.parametrize("pick", [_coin("BTC", 75.0), _coin("ETH", 75.0), _stock("GME", 70.0)])
def test_bitcoin_ether_and_stock_picks_carry_no_risk_flag(pick):
    assert "risk" not in signals_weekly.pick_record(pick)


def test_the_risk_flag_survives_the_days_kept_scores_and_a_json_round_trip(conn):
    live = [_coin("SOL", 75.0), _coin("BTC", 70.0), _stock("AAA", 64.0)]
    model.keep_scores(conn, TODAY, live)
    kept = {s.ticker: s for s in model.cached_scores(conn, TODAY)}
    for s in live:
        assert signals_weekly.pick_record(kept[s.ticker]) == signals_weekly.pick_record(s)
    assert signals_weekly.pick_record(kept["CRYPTO:SOL"])["risk"] is True
    assert "risk" not in signals_weekly.pick_record(kept["CRYPTO:BTC"])


def test_the_risk_flag_is_not_a_column_of_buy_signals(conn):
    record = signals_weekly.pick_record(_coin("SOL", 75.0))
    signals_weekly.record_signal(conn, record, TODAY)
    assert "risk" not in [r[1] for r in conn.execute("PRAGMA table_info(buy_signals)")]
    assert conn.execute("SELECT ticker, kind FROM buy_signals").fetchall() == [("CRYPTO:SOL", "crypto")]


def test_a_score_read_back_from_the_days_kept_scores_is_stored_the_same_way(conn):
    live = _stock("AAA", 64.0, t212=False)
    model.keep_scores(conn, TODAY, [live, _coin("BTC", 75.0)])
    kept = {s.ticker: s for s in model.cached_scores(conn, TODAY)}
    assert signals_weekly.pick_record(kept["AAA"]) == signals_weekly.pick_record(live)
    assert signals_weekly.pick_record(kept["CRYPTO:BTC"]) == signals_weekly.pick_record(_coin("BTC", 75.0))


def test_recording_a_signal_writes_the_row_the_spec_describes(conn):
    record = signals_weekly.pick_record(_stock("GME", 70.0, t212=False, stop_pct=0.12))
    signals_weekly.record_signal(conn, record, TODAY)
    [row] = conn.execute("SELECT id, ticker, source, company, kind, score, stop_pct, reasons, t212, sent_at "
                         "FROM buy_signals").fetchall()
    assert row == (1, "GME", "SEC", "GME Corp", "stock", 70.0, 0.12,
                   '["3 инсайдера из руководства", "CEO среди покупателей", "первая покупка"]', 0, "2026-10-09")


@pytest.mark.parametrize("t212, stored", [(True, 1), (False, 0), (None, None)])
def test_the_trading_212_flag_is_stored_as_an_integer_or_null(conn, t212, stored):
    signals_weekly.record_signal(conn, signals_weekly.pick_record(_stock(t212=t212)), TODAY)
    assert conn.execute("SELECT t212 FROM buy_signals").fetchone() == (stored,)


def test_a_recorded_signal_blocks_the_ticker_for_30_days(conn):
    signals_weekly.record_signal(conn, signals_weekly.pick_record(_stock("AAA")), TODAY)
    assert signals_weekly.pick_buys(conn, TODAY + dt.timedelta(days=29), [_stock("AAA")]) == []
    assert signals_weekly.pick_buys(conn, TODAY + dt.timedelta(days=31), [_stock("AAA")]) != []


def test_recording_can_leave_the_commit_to_the_caller(conn):
    signals_weekly.record_signal(conn, signals_weekly.pick_record(_stock("AAA")), TODAY, commit=False)
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM buy_signals").fetchone() == (0,)
    signals_weekly.record_signal(conn, signals_weekly.pick_record(_stock("AAA")), TODAY, commit=False)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM buy_signals").fetchone() == (1,)


def test_picking_reads_and_writes_nothing(conn):
    _hold(conn, "AAA")
    _signalled(conn, "BBB", 3)
    before = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("positions", "buy_signals", "kv_cache")]
    signals_weekly.pick_buys(conn, TODAY, [_stock("AAA"), _stock("BBB"), _stock("CCC")])
    after = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("positions", "buy_signals", "kv_cache")]
    assert before == after


def test_a_us_holding_trading_212_keys_by_isin_is_held_under_its_market_symbol(conn):
    """A US instrument Yahoo could not price is keyed by its ISIN (source T212): the SEC signal for
    its market symbol must still see it as held -- by the instrument list's symbol, or the code's own."""
    conn.execute("INSERT INTO t212_instruments (ticker, isin, type, short_name, currency) VALUES (?,?,?,?,?)",
                 ("FB_US_EQ", "US30303M1027", "STOCK", "META", "USD"))
    _hold(conn, "US30303M1027", positions.T212_SOURCE, origin="t212", t212_ticker="FB_US_EQ")
    _hold(conn, "US36467W1099", positions.T212_SOURCE, origin="t212", t212_ticker="GME_US_EQ")
    held = signals_weekly.held_names(conn)
    assert positions._asset_key("META", None) in held and positions._asset_key("GME", None) in held
    assert positions._asset_key("FB", None) not in held          # the old code is not the symbol
