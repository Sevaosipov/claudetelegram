"""weekly.py: the week's Telegram messages -- one short message per signal (a picked buy, a group exit), then
a short summary of the user's own Trading 212 account (spec 2026-10-04-remove-model-portfolio.md, which
builds on 2026-10-04-signal-message-style.md). Offline -- the picks, the positions, the stored prices and the
account snapshots are rows or dicts written here."""
from __future__ import annotations

import datetime as dt
import json
import re
import time

import pytest

import db
import signals_weekly
import telegram_notify as tn
import weekly

FRI = dt.date(2026, 10, 9)           # a weekly run's day: the week is 03.10 - 09.10
_TAGS = re.compile(r"</?[a-zA-Z][^>]*>")


@pytest.fixture(autouse=True)
def _utc(monkeypatch):
    """The group exits read journal timestamps (stored in UTC) as local dates; the tests that put a row on
    the edge of a week need the local day to be the UTC day."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


# ------------------------------------------------------------------ builders
def _pick(ticker="GME", **kw):
    base = dict(ticker=ticker, source="SEC", company=f"{ticker} Corp", kind="stock", score=70.0, stop_pct=0.10,
                reasons=["2 инсайдера из руководства", "CEO среди покупателей"], t212=True)
    base.update(kw)
    return base


def _exit_row(conn, ticker, company, sellers, *, emitted="2026-10-08 12:00:00", kind="exit"):
    """A journal row; returns its id."""
    db.journal_signal(conn, {"source": "SEC", "kind": kind, "ticker": ticker, "company": company,
                             "members": json.dumps(sellers, ensure_ascii=False)})
    conn.execute("UPDATE signal_journal SET emitted_at = ? WHERE id = (SELECT MAX(id) FROM signal_journal)",
                 (emitted,))
    conn.commit()
    return conn.execute("SELECT MAX(id) FROM signal_journal").fetchone()[0]


def _hold(conn, ticker, entry=100.0, *, last=None, origin="t212", source=None, opened="2026-09-01",
          price_day="2026-10-09", t212_ticker=None, closed=None):
    """An open position of the user's, with the last price the Trading 212 sync stored for it (None: none)."""
    conn.execute("INSERT INTO positions (ticker, source, opened_at, entry_price, origin, quantity, t212_ticker, "
                 "currency, closed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 (ticker, source, opened, entry, origin, 5.0 if origin == "t212" else None,
                  t212_ticker or (f"{ticker}_US_EQ" if origin == "t212" else None),
                  "USD" if origin == "t212" else None, closed))
    if last is not None:
        conn.execute("INSERT OR REPLACE INTO t212_prices (ticker, date, price) VALUES (?,?,?)",
                     (ticker, price_day, last))
    conn.commit()


def _equity(conn, rows, currency="EUR"):
    """The account's daily snapshots as a sync stores them: rows [(ISO date, total value)]."""
    for day, value in rows:
        conn.execute("INSERT OR REPLACE INTO t212_equity (date, total_value, invested_value, invested_cost, "
                     "cash_free, currency) VALUES (?,?,?,?,?,?)", (day, value, None, None, None, currency))
    conn.commit()


def _alerted(conn, ticker, day):
    """A position whose sell alert went out on `day` (positions.mark_alerted's stamp): the open one in `ticker`,
    or a new one when there is none. The alert does not close it."""
    if conn.execute("UPDATE positions SET close_alerted_at = ? WHERE ticker = ? AND closed_at IS NULL",
                    (day, ticker)).rowcount == 0:
        conn.execute("INSERT INTO positions (ticker, opened_at, entry_price, close_alerted_at) VALUES (?,?,?,?)",
                     (ticker, "2026-08-01", 10.0, day))
    conn.commit()


def _bought(conn, ticker, day):
    """A buy signal whose message went out on `day`."""
    signals_weekly.record_signal(conn, signals_weekly.pick_record(_score(ticker)), dt.date.fromisoformat(day))


def _score(ticker):
    import types
    return types.SimpleNamespace(ticker=ticker, source="SEC", company=ticker, kind="stock", total=64.0,
                                 stop_pct=0.1, reasons=["x"], t212=True)


def _summary(conn, today=FRI, **kw):
    return weekly.format_summary(conn, today, html=False, **kw)


def _lines(conn, **kw):
    return _summary(conn, **kw).splitlines()


# ===================================================================== a buy
def test_a_buy_is_a_bare_block_and_nothing_else():
    text = weekly.buy_text(_pick(price=23.1, amount_eur=19.2, chart="График против (по правилам: за) — оферта"))
    assert text == "<pre>GME Buy · Invest\nPrice 23.10\nStop  20.79\nSize  €19\nChart Against</pre>"
    assert "инсайдер" not in text and "балл" not in text and "оферта" not in text      # no reasons, no sentence


@pytest.mark.parametrize("chart, row", [("График за — x", "\nChart For"), ("График нейтрален (по правилам, без Claude) — y", "\nChart Neutral"),
                                        (None, ""), ("что-то другое", "")])
def test_the_chart_verdict_is_one_word_in_the_block(chart, row):
    assert weekly.buy_text(_pick(chart=chart)) == f"<pre>GME Buy · Invest\nStop  -10%{row}</pre>"


def test_the_company_line_is_not_shown_but_the_analyst_still_gets_it():
    pick = _pick(about="Specialty Retail · кап. $10,3 млрд")
    assert weekly.buy_text(pick) == "<pre>GME Buy · Invest\nStop  -10%</pre>"
    assert "Компания: Specialty Retail · кап. $10,3 млрд" in weekly.facts(pick)


def test_a_buy_without_a_price_gives_the_stop_as_a_percent_and_without_a_size_has_no_size_line():
    assert weekly.buy_text(_pick()) == "<pre>GME Buy · Invest\nStop  -10%</pre>"


def test_a_buy_the_user_cannot_make_at_trading_212_says_so_in_the_block():
    assert weekly.buy_text(_pick(t212=False)) == "<pre>GME Buy · Invest\nStop  -10%\nNot on Trading 212</pre>"


@pytest.mark.parametrize("t212", [True, None])
def test_a_buy_with_the_label_known_to_be_there_or_unknown_does_not_mention_trading_212(t212):
    assert "Trading 212" not in weekly.buy_text(_pick(t212=t212))


@pytest.mark.parametrize("pick, text", [
    (dict(stop_pct=None), "<pre>GME Buy · Invest</pre>"),
    (dict(score=None), "<pre>GME Buy · Invest\nStop  -10%</pre>"),
    (dict(reasons=[], score=None, stop_pct=None), "<pre>GME Buy · Invest</pre>")])
def test_a_buy_leaves_out_what_the_pick_does_not_have(pick, text):
    assert weekly.buy_text(_pick(**pick)) == text


def test_a_buy_rounds_the_stop_to_a_whole_percent():
    assert weekly.buy_text(_pick(score=64.4, stop_pct=0.1249, reasons=[])) == "<pre>GME Buy · Invest\nStop  -12%</pre>"


def test_a_coin_buy_is_named_by_its_symbol_and_a_cheap_one_has_more_decimals():
    text = weekly.buy_text(_pick("CRYPTO:BTC", kind="crypto", source="CRYPTO", company="BTC", score=75.0,
                                 stop_pct=0.15, reasons=["выше 100-дн. средней"], t212=None, price=80000.0))
    assert text == "<pre>BTC Buy · Invest\nPrice 80000.00\nStop  68000.00</pre>"
    doge = weekly.buy_text(_pick("CRYPTO:DOGE", price=0.152, stop_pct=0.2, reasons=[], score=None))
    assert doge == "<pre>DOGE Buy · Invest\nPrice 0.1520\nStop  0.1216</pre>"


def test_an_alts_buy_says_high_risk_in_the_block():
    text = weekly.buy_text(_pick("CRYPTO:SOL", kind="crypto", source="CRYPTO", company="SOL", score=75.0,
                                 stop_pct=0.22, reasons=["выше 100-дн. средней", "приток в ETF"], t212=None,
                                 risk=True))
    assert text == "<pre>SOL Buy · Invest\nStop  -22%\nHigh risk</pre>"


def test_bitcoin_and_ether_buys_have_no_risk_tag():
    for coin in ("BTC", "ETH"):
        text = weekly.buy_text(_pick(f"CRYPTO:{coin}", kind="crypto", source="CRYPTO", company=coin, score=75.0,
                                     stop_pct=0.15, reasons=["выше 100-дн. средней"], t212=None))
        assert "risk" not in text.lower()
    assert "risk" not in weekly.buy_text(_pick(risk=False)).lower()


def test_the_risk_tag_comes_after_the_trading_212_note_and_stands_alone_when_there_is_nothing_else():
    assert weekly.buy_text(_pick(t212=False, risk=True)) == (
        "<pre>GME Buy · Invest\nStop  -10%\nNot on Trading 212\nHigh risk</pre>")
    assert weekly.buy_text(_pick(reasons=[], score=None, stop_pct=None, risk=True)) == "<pre>GME Buy · Invest\nHigh risk</pre>"


def test_a_pick_kept_before_the_flag_existed_has_no_tag():
    old = dict(ticker="CRYPTO:SOL", source="CRYPTO", company="SOL", kind="crypto", score=75.0, stop_pct=0.22,
               reasons=["выше 100-дн. средней"], t212=None)
    assert "risk" not in weekly.buy_text(old).lower()


def test_the_weeks_signals_carry_the_tag_of_the_picks_that_have_it(conn):
    picks = [_pick("CRYPTO:SOL", kind="crypto", source="CRYPTO", company="SOL", risk=True), _pick("CRYPTO:BTC", kind="crypto", source="CRYPTO", company="BTC")]
    texts = dict(weekly.week_signals(conn, FRI, picks))
    assert "High risk" in texts["buy:CRYPTO:SOL"] and "risk" not in texts["buy:CRYPTO:BTC"].lower()


def test_a_buy_has_only_the_pre_tag_and_every_dynamic_part_escaped():
    text = weekly.buy_text(_pick("A&B", reasons=["<b>bold</b> & co"], about="R&D <labs>"))
    assert "A&amp;B Buy" in text and "labs" not in text and "bold" not in text
    assert set(_TAGS.findall(text)) == {"<pre>", "</pre>"}


def test_the_facts_for_the_analyst_still_carry_the_reasons_and_the_score():
    text = weekly.facts(_pick(price=23.1))
    assert "Причины сигнала: 2 инсайдера из руководства; CEO среди покупателей" in text and "Балл: 70" in text


# ============================================================= group exits
def test_group_exits_of_the_week_are_one_message_each_naming_the_sellers(conn):
    zzz = _exit_row(conn, "ZZZ", "Exit Corp", ["Ann Lee", "Bo Chen"], emitted="2026-10-08 12:00:00")
    yyy = _exit_row(conn, "YYY", "Early Inc", ["C D"], emitted="2026-10-03 12:00:00")       # the week's first day
    _exit_row(conn, "OLD", "Old Inc", ["E F"], emitted="2026-10-02 12:00:00")               # before the week
    _exit_row(conn, "CLU", "Cluster Inc", ["G H"], emitted="2026-10-08 12:00:00", kind="cluster")
    assert weekly.week_signals(conn, FRI, []) == [
        (f"exit:{zzz}", "🔴 <b>ZZZ!</b>: продают те, кто покупал — Ann Lee, Bo Chen"),
        (f"exit:{yyy}", "🔴 <b>YYY!</b>: продают те, кто покупал — C D")]                    # by journal id


def test_a_ticker_that_exited_twice_in_a_week_is_one_signal_with_its_latest_sellers(conn):
    _exit_row(conn, "ZZZ", "Exit Corp", ["A"], emitted="2026-10-06 12:00:00")
    latest = _exit_row(conn, "ZZZ", "Exit Corp", ["A", "B"], emitted="2026-10-08 12:00:00")
    assert weekly.week_signals(conn, FRI, []) == [(f"exit:{latest}", "🔴 <b>ZZZ!</b>: продают те, кто покупал — A, B")]


def test_a_long_seller_list_is_cut_and_one_with_no_names_has_no_details(conn):
    _exit_row(conn, "ZZZ", "Exit Corp", [f"P{i}" for i in range(9)])
    _exit_row(conn, "YYY", "Nameless", [])
    texts = [t for _k, t in weekly.week_signals(conn, FRI, [])]
    assert texts[0] == "🔴 <b>ZZZ!</b>: продают те, кто покупал — P0, P1, P2, P3, P4 и ещё 4"
    assert texts[1] == "🔴 <b>YYY!</b>: продают те, кто покупал"


def test_the_exits_window_is_the_seven_days_ending_today(conn):
    _exit_row(conn, "EDGE", "Edge", ["A"], emitted="2026-10-03 00:00:00")
    _exit_row(conn, "LATE", "Late", ["A"], emitted="2026-10-09 23:59:59")
    _exit_row(conn, "BEFORE", "Before", ["A"], emitted="2026-10-02 23:59:59")
    _exit_row(conn, "AFTER", "After", ["A"], emitted="2026-10-10 00:00:01")
    assert [t.split("!")[0].removeprefix("🔴 <b>") for _k, t in weekly.week_signals(conn, FRI, [])] == ["EDGE", "LATE"]


def test_an_exit_message_is_escaped_and_one_line(conn):
    _exit_row(conn, "Z&Z", "Evil <script>alert(1)</script> & Co", ["Ann <i>Lee</i>", "B&B"])
    [(_key, text)] = weekly.week_signals(conn, FRI, [])
    assert "Z&amp;Z" in text and "Ann &lt;i&gt;Lee&lt;/i&gt;" in text and "B&amp;B" in text
    assert "\n" not in text and set(_TAGS.findall(text)) == {"<b>", "</b>"}


# ====================================================== the week's signals
def test_the_signals_are_the_picked_buys_in_the_order_given_then_the_group_exits(conn):
    zzz = _exit_row(conn, "ZZZ", "Exit Corp", ["A"])
    picks = [_pick("TOP", score=90.0), _pick("CRYPTO:BTC", kind="crypto", company="BTC", source="CRYPTO"),
             _pick("LOW", score=61.0)]
    signals = weekly.week_signals(conn, FRI, picks)
    assert [k for k, _t in signals] == ["buy:TOP", "buy:CRYPTO:BTC", "buy:LOW", f"exit:{zzz}"]
    assert signals[0][1].startswith("<pre>TOP Buy") and signals[1][1].startswith("<pre>BTC Buy")


def test_the_signal_keys_are_stable_from_one_call_to_the_next(conn):
    _exit_row(conn, "ZZZ", "Exit Corp", ["A"])
    picks = [_pick("AAA"), _pick("BBB")]
    assert weekly.week_signals(conn, FRI, picks) == weekly.week_signals(conn, FRI, picks)
    assert all(re.fullmatch(r"(buy:[A-Z:]+|exit:\d+)", k) for k, _t in weekly.week_signals(conn, FRI, picks))


def test_a_quiet_week_has_no_signals(conn):
    assert weekly.week_signals(conn, FRI, []) == []


def test_there_are_no_sale_messages_any_more(conn):
    _hold(conn, "AAA", last=90.0)
    _alerted(conn, "AAA", "2026-10-08")
    assert weekly.week_signals(conn, FRI, []) == []


# ===================================================== the summary: line 1
def test_the_summary_opens_with_the_week_and_the_account(conn):
    _equity(conn, [("2026-10-02", 12_000.0), ("2026-10-09", 12_345.67)])
    assert _lines(conn)[0] == "📊 Неделя 03.10–09.10: счёт Trading 212 €12 346 (за неделю +2,9%)"
    html = weekly.format_summary(conn, FRI).splitlines()[0]
    assert html == "📊 <b>Неделя 03.10–09.10</b>: счёт Trading 212 €12 346 (за неделю +2,9%)"


def test_the_account_is_valued_on_or_before_today_and_a_week_before(conn):
    """Today is FRI 09.10: the last snapshot on or before it is of 07.10, and the week is measured from
    the last one on or before 02.10, which is of 30.09."""
    _equity(conn, [("2026-09-30", 10_000.0), ("2026-10-07", 9_000.0), ("2026-10-10", 99_999.0)])
    assert _lines(conn)[0] == "📊 Неделя 03.10–09.10: счёт Trading 212 €9 000 (за неделю −10,0%)"


@pytest.mark.parametrize("rows", [[("2026-10-08", 12_000.0)],                           # nothing that early
                                  [("2026-10-01", 0.0), ("2026-10-08", 12_000.0)]])     # nothing to compare with
def test_the_week_change_is_left_out_when_it_is_not_known(conn, rows):
    _equity(conn, rows)
    assert _lines(conn)[0] == "📊 Неделя 03.10–09.10: счёт Trading 212 €12 000"


def test_the_week_change_needs_a_snapshot_from_about_a_week_ago(conn):
    """The only earlier snapshot is twelve days before 02.10: the change since then is not «за неделю»."""
    _equity(conn, [("2026-09-20", 10_000.0), ("2026-10-09", 12_000.0)])
    assert _lines(conn)[0].endswith("счёт Trading 212 €12 000")
    _equity(conn, [("2026-09-29", 10_000.0)])                       # three days before 02.10: near enough
    assert _lines(conn)[0].endswith("счёт Trading 212 €12 000 (за неделю +20,0%)")


def test_a_snapshot_older_than_three_days_is_not_the_account_today(conn):
    """The sync has not got through: a value of last week is not «счёт» today."""
    _equity(conn, [("2026-10-05", 12_000.0)])                       # FRI is 09.10: four days old
    assert _lines(conn)[0] == "📊 Неделя 03.10–09.10"
    _equity(conn, [("2026-10-06", 12_500.0)])                       # three days old: still the account
    assert _lines(conn)[0] == "📊 Неделя 03.10–09.10: счёт Trading 212 €12 500"


def test_without_account_data_the_line_is_just_the_week(conn):
    assert _lines(conn)[0] == "📊 Неделя 03.10–09.10"
    assert weekly.format_summary(conn, FRI).splitlines()[0] == "📊 <b>Неделя 03.10–09.10</b>"
    _equity(conn, [("2026-10-10", 12_000.0)])                       # only a day after today
    assert _lines(conn)[0] == "📊 Неделя 03.10–09.10"
    conn.execute("INSERT INTO t212_equity (date, total_value) VALUES ('2026-10-08', NULL)")
    assert _lines(conn)[0] == "📊 Неделя 03.10–09.10"


def test_the_account_is_named_in_its_own_currency(conn):
    _equity(conn, [("2026-10-02", 10_000.0), ("2026-10-09", 12_000.0)], currency="GBP")
    assert _lines(conn)[0] == "📊 Неделя 03.10–09.10: счёт Trading 212 £12 000 (за неделю +20,0%)"


def test_a_loss_on_the_account_reads_with_a_real_minus(conn):
    _equity(conn, [("2026-10-02", 12_000.0), ("2026-10-09", 11_000.0)])
    assert _lines(conn)[0].endswith("€11 000 (за неделю −8,3%)")


def test_the_week_is_the_seven_days_ending_on_the_day_given(conn):
    assert weekly.format_summary(conn, dt.date(2026, 10, 11), html=False).splitlines()[0] == "📊 Неделя 05.10–11.10"
    assert weekly.format_summary(conn, dt.date(2026, 10, 2), html=False).splitlines()[0] == "📊 Неделя 26.09–02.10"


# ===================================================== the summary: line 2
def _held(conn, results, **kw):
    """Trading 212 holdings with the given results since purchase (entry 100): {ticker: result}."""
    for ticker, result in results.items():
        _hold(conn, ticker, 100.0, last=100.0 * (1 + result), **kw)


def test_the_positions_line_shows_the_three_best_and_the_three_worst(conn):
    _held(conn, {"SMCI": 0.31, "BBD": 0.124, "GME": 0.081, "MID": 0.01, "DEF": -0.042, "ABC": -0.09, "XYZ": -0.225})
    assert _lines(conn)[1] == ("Позиций 7: лучшие — SMCI +31,0%, BBD +12,4%, GME +8,1%; "
                               "худшие — XYZ −22,5%, ABC −9,0%, DEF −4,2%")


def test_with_exactly_six_priced_positions_the_best_and_the_worst_do_not_overlap(conn):
    _held(conn, {"A": 0.30, "B": 0.20, "C": 0.10, "D": -0.10, "E": -0.20, "F": -0.30})
    assert _lines(conn)[1] == "Позиций 6: лучшие — A +30,0%, B +20,0%, C +10,0%; худшие — F −30,0%, E −20,0%, D −10,0%"


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5])
def test_with_fewer_than_six_priced_positions_they_are_listed_once_best_first(conn, n):
    results = {f"T{i}": 0.10 - i * 0.05 for i in range(n)}
    _held(conn, results)
    expected = ", ".join(f"T{i} {tn.signed_pct(0.10 - i * 0.05)}" for i in range(n))
    assert _lines(conn)[1] == f"Позиций {n}: {expected}"
    assert "лучшие" not in _lines(conn)[1] and "худшие" not in _lines(conn)[1]


def test_the_count_is_every_open_position_but_only_priced_ones_are_ranked(conn):
    _held(conn, {"A": 0.10, "B": -0.05})
    _hold(conn, "NOPRICE", 100.0)                                       # a holding the sync stored no price for
    _hold(conn, "MANUAL", 100.0, origin="manual")                      # a /bought position: no stored price
    assert _lines(conn)[1] == "Позиций 4: A +10,0%, B −5,0%"


def test_with_positions_but_no_prices_the_line_says_so(conn):
    _hold(conn, "NOPRICE", 100.0)
    assert _lines(conn)[1] == "Позиций 1: цен пока нет"


def test_with_no_positions_the_line_says_so(conn):
    assert _lines(conn)[1] == "Позиций нет."
    _hold(conn, "GONE", 100.0, last=110.0, closed="2026-10-01")          # sold: not a position
    assert _lines(conn)[1] == "Позиций нет."


def test_the_result_is_the_last_stored_price_against_the_entry_price(conn):
    _hold(conn, "AAA", 40.0, last=44.0)
    assert _lines(conn)[1] == "Позиций 1: AAA +10,0%"


def test_the_last_stored_price_is_the_one_that_counts(conn):
    _hold(conn, "AAA", 100.0, last=90.0, price_day="2026-10-06")
    conn.execute("INSERT INTO t212_prices (ticker, date, price) VALUES ('AAA', '2026-10-08', 120.0)")
    conn.commit()
    assert _lines(conn)[1] == "Позиций 1: AAA +20,0%"


def test_a_stored_price_older_than_three_days_is_not_a_price(conn):
    _hold(conn, "STALE", 100.0, last=150.0, price_day="2026-10-05")        # four days old
    _hold(conn, "FRESH", 100.0, last=110.0, price_day="2026-10-06")        # three: fine
    assert _lines(conn)[1] == "Позиций 2: FRESH +10,0%"


def test_a_manual_position_is_not_priced_from_the_prices_a_holding_left_under_the_same_ticker(conn):
    conn.execute("INSERT INTO t212_prices (ticker, date, price) VALUES ('GME', '2026-10-08', 999.0)")
    _hold(conn, "GME", 100.0, origin="manual")
    assert _lines(conn)[1] == "Позиций 1: цен пока нет"


def test_a_holding_keyed_by_its_isin_is_named_the_way_its_owner_knows_it(conn):
    _hold(conn, "DE0007164600", 100.0, last=110.0, source="T212", t212_ticker="SAPd_EQ")
    line = _lines(conn)[1]
    assert line == "Позиций 1: SAP +10,0%" and "DE0007164600" not in line


def test_positions_with_equal_results_are_ordered_by_name(conn):
    _held(conn, {"BBB": 0.05, "AAA": 0.05, "CCC": 0.05})
    assert _lines(conn)[1] == "Позиций 3: AAA +5,0%, BBB +5,0%, CCC +5,0%"


def test_a_position_with_no_entry_price_is_not_ranked(conn):
    _hold(conn, "ZERO", 0.0, last=10.0)
    assert _lines(conn)[1] == "Позиций 1: цен пока нет"


def test_the_positions_line_makes_no_network_call(conn, monkeypatch):
    import positions
    monkeypatch.setattr(positions, "last_close", lambda *a, **k: 1 / 0)
    monkeypatch.setattr(positions, "_yahoo_close", lambda *a, **k: 1 / 0)
    monkeypatch.setattr(positions, "daily_closes", lambda *a, **k: 1 / 0)
    _held(conn, {"AAA": 0.1})
    assert _lines(conn)[1] == "Позиций 1: AAA +10,0%"


# ===================================================== the summary: line 3
def test_a_quiet_week_says_so(conn):
    assert _lines(conn)[2] == "Сигналов за неделю не было."


def test_the_summary_counts_the_weeks_buy_signals_sell_alerts_and_group_exits(conn):
    _bought(conn, "AAA", "2026-10-09")
    _bought(conn, "BBB", "2026-10-06")
    _alerted(conn, "CCC", "2026-10-07")
    _exit_row(conn, "ZZZ", "Exit Corp", ["A", "B"])
    assert _lines(conn)[2] == "Сигналов за неделю: покупок 2, на продажу 1, групповых выходов 1"


def test_zero_parts_of_a_busy_week_are_shown_as_zeros(conn):
    _alerted(conn, "CCC", "2026-10-07")
    assert _lines(conn)[2] == "Сигналов за неделю: покупок 0, на продажу 1, групповых выходов 0"


@pytest.mark.parametrize("setup", ["buy", "alert", "exit"])
def test_any_one_kind_of_signal_makes_the_week_not_quiet(conn, setup):
    {"buy": lambda: _bought(conn, "AAA", "2026-10-08"), "alert": lambda: _alerted(conn, "CCC", "2026-10-08"),
     "exit": lambda: _exit_row(conn, "ZZZ", "Exit Corp", ["A"])}[setup]()
    assert "не было" not in _lines(conn)[2]


def test_signals_outside_the_week_are_not_counted(conn):
    _bought(conn, "OLD", "2026-10-02")                                  # a day before the week
    _bought(conn, "FUTURE", "2026-10-10")                               # a day after today
    _alerted(conn, "OLD", "2026-10-02")
    _alerted(conn, "FUTURE", "2026-10-10")
    _exit_row(conn, "OLD", "Old", ["A"], emitted="2026-10-02 12:00:00")
    assert _lines(conn)[2] == "Сигналов за неделю не было."
    _bought(conn, "EDGE", "2026-10-03")                                 # the week's first day
    assert _lines(conn)[2].startswith("Сигналов за неделю: покупок 1,")


def test_a_ticker_that_exited_twice_is_one_group_exit(conn):
    _exit_row(conn, "ZZZ", "Exit Corp", ["A"], emitted="2026-10-06 12:00:00")
    _exit_row(conn, "ZZZ", "Exit Corp", ["A", "B"], emitted="2026-10-08 12:00:00")
    assert _lines(conn)[2].endswith("групповых выходов 1")


def test_a_cluster_row_is_not_a_group_exit(conn):
    _exit_row(conn, "CLU", "Cluster Inc", ["G H"], kind="cluster")
    assert _lines(conn)[2] == "Сигналов за неделю не было."


# ================================================ the summary: the warning
def test_a_failed_scoring_adds_the_warning_as_the_last_line(conn):
    assert _lines(conn, scoring_failed=True)[-1] == "⚠️ Оценка сигналов на этой неделе не отработала — покупок не было."
    assert weekly.SCORING_FAILED_WARNING in weekly.format_summary(conn, FRI, scoring_failed=True)
    assert "⚠️" not in _summary(conn) and "⚠️" not in _summary(conn, scoring_failed=False)


def test_the_summary_is_always_three_lines_and_a_fourth_with_the_warning(conn):
    assert len(_lines(conn)) == 3 and len(_lines(conn, scoring_failed=True)) == 4


def test_the_summary_is_one_short_message_without_the_old_sections(conn):
    _held(conn, {"AAA": 0.1})
    _bought(conn, "AAA", "2026-10-09")
    text = weekly.format_summary(conn, FRI)
    assert "\n\n" not in text
    for gone in ("Модель", "модел", "смесь", "в портфеле", "В портфеле", "с начала", "Покупки", "Наблюдение", "👀"):
        assert gone not in text


# ===================================================== the summary: format
def test_the_summary_html_has_one_bold_title_and_escapes_the_names(conn):
    _held(conn, {"H&H<i>": 0.05})
    _equity(conn, [("2026-10-09", 12_000.0)])
    text = weekly.format_summary(conn, FRI, scoring_failed=True)
    assert "H&amp;H&lt;i&gt; +5,0%" in text and set(_TAGS.findall(text)) == {"<b>", "</b>"}
    assert not _TAGS.search(_summary(conn).replace("H&H<i>", ""))          # plain: no tags at all


def test_the_summary_is_html_by_default(conn):
    assert "<b>" in weekly.format_summary(conn, FRI)
    assert "<b>" not in weekly.format_summary(conn, FRI, html=False)


def test_the_whole_summary_for_a_busy_week(conn):
    _equity(conn, [("2026-10-02", 12_000.0), ("2026-10-09", 12_345.67)])
    _held(conn, {"SMCI": 0.31, "BBD": 0.124, "GME": 0.081, "MID": 0.01, "DEF": -0.042, "ABC": -0.09, "XYZ": -0.225})
    _bought(conn, "GME", "2026-10-09")
    _bought(conn, "NEW", "2026-10-09")
    _alerted(conn, "XYZ", "2026-10-07")
    _exit_row(conn, "ZZZ", "Exit Corp", ["A", "B"])
    assert _summary(conn) == "\n".join([
        "📊 Неделя 03.10–09.10: счёт Trading 212 €12 346 (за неделю +2,9%)",
        "Позиций 7: лучшие — SMCI +31,0%, BBD +12,4%, GME +8,1%; худшие — XYZ −22,5%, ABC −9,0%, DEF −4,2%",
        "Сигналов за неделю: покупок 2, на продажу 1, групповых выходов 1"])


def test_the_summary_reads_and_writes_nothing_but_the_database_it_is_given(conn):
    before = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("positions", "buy_signals", "kv_cache", "t212_prices", "t212_equity", "signal_journal")]
    _summary(conn, scoring_failed=True)
    after = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
             for t in ("positions", "buy_signals", "kv_cache", "t212_prices", "t212_equity", "signal_journal")]
    assert before == after
