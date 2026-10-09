"""bot.py's daily flow (spec 2026-09-30 section 5, as the weekly-message spec 2026-10-01 and the removal of the
virtual portfolio, 2026-10-04, change it): the new signals are collected and scored, the journal takes the
decisions, the close alerts on the user's positions (/bought and the Trading 212 account) go out as they
fire, and once a week -- the first full run from Friday to Sunday whose scoring got through -- the week's buy
signals are picked, and they, the group exits and a short summary go out. The network and the scoring itself
are stubbed."""
from __future__ import annotations

import dataclasses
import datetime as dt
import sys
import types

import pytest

import bot
import cluster
import db
import model
import model_score
import positions
import signals_weekly
import strategy
import t212_account
import telegram_notify
import weekly
from conftest import add_sec_purchase, add_sec_sale

REAL_T212_SYNC = t212_account.sync        # the main_run fixture stubs it
REAL_PICK_BUYS, REAL_WEEK_SIGNALS, REAL_FORMAT_SUMMARY = (       # ... and these three
    signals_weekly.pick_buys, weekly.week_signals, weekly.format_summary)

TODAY = dt.date.today()
RECENT = (TODAY - dt.timedelta(days=1)).isoformat()
MON, TUE, WED, THU, FRI, SAT, SUN = (dt.date(2026, 10, d) for d in range(5, 12))      # ISO week 2026-W41
NEXT_FRI = dt.date(2026, 10, 16)


def _cluster_sig(ticker="AAA"):
    return cluster.ClusterSignal(source="SEC", ticker=ticker, company="C", buyer_count=2,
                                 total_value=1e6, members=[], window_start=RECENT,
                                 window_end=RECENT, member_names=["A", "B"])


def _coin_sig(bullish, kind="etf_flow", source="CRYPTO_ETF", ticker="CRYPTO:BTC"):
    return cluster.CryptoSignal(source, kind, ticker, "IBIT", bullish, None, 5e8, RECENT, RECENT,
                                [], None, ["k"])


def _exit_sig(ticker="BBB"):
    return cluster.ExitSignal(source="SEC", ticker=ticker, company="C", total_buyers=2,
                              seller_count=2, lines=["x"], seller_names=["A", "B"])


def _report(decisions=None, scored=()):
    return model.ScoreReport(scored=list(scored), decisions=decisions or {})


def _score(ticker="AAA", total=70.0, **kw):
    """A StockScore that says BUY."""
    base = dict(ticker=ticker, source="SEC", company=f"{ticker} Corp", signal=None, insiders=52.0,
                triggers=5.0, momentum=7.0, news=0.0, total=total, decision=model_score.BUY,
                reasons=["2 инсайдера из руководства", "CEO среди покупателей"], block=None, untradeable=None,
                t212=True, stop_pct=0.10, last_close=10.0)
    base.update(kw)
    return model_score.StockScore(**base)


def _pick(ticker="AAA", **kw):
    """A pick as the week keeps it (signals_weekly.pick_record)."""
    base = dict(ticker=ticker, source="SEC", company=f"{ticker} Corp", kind="stock", score=70.0, stop_pct=0.10,
                reasons=["2 инсайдера из руководства", "CEO среди покупателей"], t212=True)
    base.update(kw)
    return base


@pytest.fixture(autouse=True)
def _positions_offline(monkeypatch):
    """/bought positions look at price history and headlines: none, here."""
    monkeypatch.setattr(positions, "daily_closes", lambda ticker, source=None: [])
    monkeypatch.setattr(model, "default_news", lambda ticker, source: [])


@pytest.fixture
def recorded(monkeypatch):
    """_journal with the network out (enrich_signals is the identity) and the commit recorded."""
    seen = {"enriched": [], "committed": []}

    def enrich(conn, signals):
        seen["enriched"].append(list(signals))
        return signals
    monkeypatch.setattr(cluster, "enrich_signals", enrich)
    monkeypatch.setattr(bot, "_commit_signals", lambda conn, signals: seen["committed"].extend(signals))
    return seen


# ------------------------------------------------------------------ _journal
def test_journal_sets_the_tier_from_the_models_decision(conn, recorded):
    aaa, bbb, ccc = _cluster_sig("AAA"), _cluster_sig("BBB"), _cluster_sig("CCC")
    bot._journal(conn, [aaa, bbb, ccc], _report({"AAA": "buy", "BBB": "block"}))
    assert (aaa.tier, bbb.tier, ccc.tier) == ("buy", "block", None)    # CCC: not scored
    assert recorded["committed"] == [aaa, bbb, ccc]


def test_journal_marks_a_bearish_coin_signal_caution_whatever_the_model_says(conn, recorded):
    bearish = _coin_sig(False)
    bot._journal(conn, [bearish], _report({"CRYPTO:BTC": "buy"}))
    assert bearish.tier == strategy.CAUTION == "caution"


def test_journal_takes_a_bullish_coin_signals_tier_from_the_decision(conn, recorded):
    bullish = _coin_sig(True)
    bot._journal(conn, [bullish], _report({"CRYPTO:BTC": "watch"}))
    assert bullish.tier == "watch"


def test_journal_leaves_the_tier_empty_without_a_report(conn, recorded):
    sig = _cluster_sig()
    bot._journal(conn, [sig], None)
    assert sig.tier is None
    assert recorded["committed"] == [sig]


def test_journal_enriches_the_buy_side_but_not_the_exits(conn, recorded):
    buy, exit_sig = _cluster_sig(), _exit_sig()
    bot._journal(conn, [buy, exit_sig], _report({"AAA": "buy"}))
    assert recorded["enriched"] == [[buy]]
    assert getattr(exit_sig, "tier", None) is None
    assert recorded["committed"] == [buy, exit_sig]


def test_journal_with_only_exits_does_not_enrich_at_all(conn, recorded):
    exit_sig = _exit_sig()
    bot._journal(conn, [exit_sig], None)
    assert recorded["enriched"] == []
    assert recorded["committed"] == [exit_sig]


def test_journal_logs_each_signal_after_it_is_enriched(conn, monkeypatch, capsys):
    """The log line carries what enrichment found (the market cap), so it is printed
    after enrich_signals, not when the finders return."""
    def enrich(conn, signals):
        for s in signals:
            s.market_cap_eur = 5e9
        return signals
    monkeypatch.setattr(cluster, "enrich_signals", enrich)
    monkeypatch.setattr(bot, "_commit_signals", lambda conn, signals: None)
    sig, exit_sig = _cluster_sig(), _exit_sig()
    assert "млрд" not in telegram_notify.format_any_signal(sig)        # not yet enriched
    bot._journal(conn, [sig, exit_sig], _report({"AAA": "buy"}))
    out = capsys.readouterr().out
    assert telegram_notify.format_any_signal(sig) in out and "компания €5.0 млрд" in out
    assert telegram_notify.format_any_signal(exit_sig) in out


def test_journal_writes_the_decision_and_the_alert_state_for_real(conn, monkeypatch):
    monkeypatch.setattr(cluster, "enrich_signals", lambda conn, signals: signals)
    sig = _cluster_sig()
    bot._journal(conn, [sig, _coin_sig(False, "treasury", "CRYPTO_TREASURY")], _report({"AAA": "watch"}))
    rows = dict(conn.execute("SELECT ticker, tier FROM signal_journal").fetchall())
    assert rows == {"AAA": "watch", "CRYPTO:BTC": "caution"}
    assert db.get_alert_state(conn, "SEC", "AAA") is not None


# ---------------------------------------------------------------- _send_closes
def _closes(conn, tickers=("ZZZ",)):
    """The close alerts of positions opened more than a year ago (the year rule fires), in the
    order of `tickers`."""
    for i, ticker in enumerate(tickers):
        positions.open_position(conn, ticker, 10.0, today=TODAY - dt.timedelta(days=400 - i))
    closes = positions.check_exits(conn, price_fn=lambda t, s=None: None)
    assert [a.position.ticker for a in closes] == list(tickers)
    return closes


def _pending(conn):
    return [a.position.ticker for a in positions.check_exits(conn, price_fn=lambda t, s=None: None)]


def test_send_closes_sends_nothing_when_there_are_none(conn, monkeypatch):
    sent = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    assert bot._send_closes(conn, []) is False
    assert sent == []


def test_send_closes_marks_a_close_alert_only_after_its_own_successful_send(conn, monkeypatch):
    closes = _closes(conn)

    monkeypatch.setattr("telegram_notify.send_text", lambda msg: False)
    assert bot._send_closes(conn, closes) is False
    assert _pending(conn) == ["ZZZ"]                                           # still pending

    sent = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    assert bot._send_closes(conn, closes) is True
    assert _pending(conn) == []


def test_send_closes_sends_each_alert_as_its_own_message_in_html_with_no_header(conn, monkeypatch):
    closes = _closes(conn, ("ZZZ", "YYY"))
    sent = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    assert bot._send_closes(conn, closes) is True
    assert sent == [telegram_notify.format_close_alert(a) for a in closes] and len(sent) == 2
    assert all(t.startswith("🔴 <b>") for t in sent) and "ZZZ!" in sent[0] and "YYY!" in sent[1]
    assert not any("Ваши позиции" in t or "🚪" in t for t in sent)
    assert _pending(conn) == []


def test_send_closes_marks_each_alert_right_after_its_own_send(conn, monkeypatch):
    closes = _closes(conn, ("ZZZ", "YYY"))
    seen = []

    def send(msg):
        seen.append(_pending(conn))              # what is still unmarked when this message goes out
        return True
    monkeypatch.setattr("telegram_notify.send_text", send)
    bot._send_closes(conn, closes)
    assert seen == [["ZZZ", "YYY"], ["YYY"]]


def test_send_closes_when_the_second_fails_marks_only_the_first(conn, monkeypatch, capsys):
    closes = _closes(conn, ("ZZZ", "YYY"))
    answers = iter([True, False])
    sent = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or next(answers))
    assert bot._send_closes(conn, closes) is False
    assert len(sent) == 2 and _pending(conn) == ["YYY"]
    assert "[telegram] send failed -- leaving 1 close alert(s) for the next run" in capsys.readouterr().err
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    assert bot._send_closes(conn, [a for a in closes if a.position.ticker in _pending(conn)]) is True
    assert _pending(conn) == []


def test_send_closes_stops_at_the_first_failure_and_leaves_the_rest(conn, monkeypatch, capsys):
    closes = _closes(conn, ("ZZZ", "YYY", "XXX"))
    sent = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or False)
    assert bot._send_closes(conn, closes) is False
    assert len(sent) == 1 and _pending(conn) == ["ZZZ", "YYY", "XXX"]          # nothing marked, the others not tried
    assert "leaving 3 close alert(s) for the next run" in capsys.readouterr().err


def test_send_closes_says_a_failed_send_on_stderr(conn, monkeypatch, capsys):
    closes = _closes(conn)
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: False)
    assert bot._send_closes(conn, closes) is False
    assert "[telegram] send failed" in capsys.readouterr().err


# ---------------------------------------------------------------- _send_weekly
SENT_KEY = "weekly_sent_2026-W41"
PICKS_KEY = "weekly_buys_2026-W41"


def _sent_keys(conn, key=SENT_KEY):
    return db.get_cached_json(conn, key)


def _keep_picks(conn, picks, day=FRI):
    """The week's picks as _pick_week keeps them in kv."""
    db.save_cached_json(conn, bot.PICKS_KEY.format(week=bot._week_id(day)), picks)


def _signalled(conn):
    return [r[0] for r in conn.execute("SELECT ticker FROM buy_signals ORDER BY id")]


@pytest.fixture
def week(conn, monkeypatch):
    """_send_weekly with the week's signals and the summary stubbed. `week.sent` is what went out,
    `week.refuse` the messages Telegram refuses, `week.keys_when_sent` the signals marked sent and
    `week.rows_when_sent` the buy_signals tickers written at the moment each message went out."""
    run = types.SimpleNamespace(sent=[], refuse=set(), keys_when_sent=[], rows_when_sent=[], asked=[],
                                signals=[("buy:AAA", "BUY1"), ("buy:BBB", "BUY2"), ("exit:3", "EXIT3")])

    def send(msg):
        run.sent.append(msg)
        run.keys_when_sent.append(_sent_keys(conn))
        run.rows_when_sent.append(_signalled(conn))
        return msg not in run.refuse

    def week_signals(c, today, picks):
        run.asked.append(("signals", today, [p["ticker"] for p in picks]))
        return list(run.signals)

    def week_summary(c, today, *, html=True, scoring_failed=False):
        run.asked.append(("summary", today, scoring_failed))
        return "SUMMARY" + (" (warning)" if scoring_failed else "")

    monkeypatch.setattr("telegram_notify.send_text", send)
    monkeypatch.setattr(weekly, "week_signals", week_signals)
    monkeypatch.setattr(weekly, "format_summary", week_summary)
    _keep_picks(conn, [_pick("AAA"), _pick("BBB")])
    return run


def test_send_weekly_sends_each_signal_as_its_own_message_in_order_and_the_summary_last(conn, week):
    assert bot._send_weekly(conn, FRI) is True
    assert week.sent == ["BUY1", "BUY2", "EXIT3", "SUMMARY"]
    assert bot._weekly_due(conn, FRI) is False and bot._weekly_due(conn, SAT) is False
    assert _sent_keys(conn) == ["buy:AAA", "buy:BBB", "exit:3"]


def test_send_weekly_asks_for_the_signals_with_the_picks_and_for_the_summary_with_the_day(conn, week):
    bot._send_weekly(conn, FRI)
    assert week.asked == [("signals", FRI, ["AAA", "BBB"]), ("summary", FRI, False)]


def test_a_quiet_week_sends_only_the_summary(conn, week):
    week.signals = []
    assert bot._send_weekly(conn, FRI) is True
    assert week.sent == ["SUMMARY"] and bot._weekly_due(conn, FRI) is False


def test_a_signal_is_marked_sent_only_after_its_message_went_out(conn, week):
    bot._send_weekly(conn, FRI)
    assert week.keys_when_sent == [None, ["buy:AAA"], ["buy:AAA", "buy:BBB"], ["buy:AAA", "buy:BBB", "exit:3"]]


def test_a_buy_signals_row_is_written_only_after_its_message_went_out(conn, week):
    bot._send_weekly(conn, FRI)
    assert week.rows_when_sent == [[], ["AAA"], ["AAA", "BBB"], ["AAA", "BBB"]]      # an exit leaves no row
    rows = conn.execute("SELECT ticker, source, company, kind, score, stop_pct, reasons, t212, sent_at "
                        "FROM buy_signals ORDER BY id").fetchall()
    assert rows[0] == ("AAA", "SEC", "AAA Corp", "stock", 70.0, 0.10,
                       '["2 инсайдера из руководства", "CEO среди покупателей"]', 1, "2026-10-09")


def test_a_row_and_its_sent_key_are_written_together(conn, week, monkeypatch):
    """The row and the key are one commit: a crash between them leaves neither."""
    real = db.save_cached_json

    def crash_on_the_first_key(c, key, obj):
        if key.startswith("weekly_sent_"):
            raise KeyboardInterrupt
        return real(c, key, obj)
    monkeypatch.setattr(db, "save_cached_json", crash_on_the_first_key)
    with pytest.raises(KeyboardInterrupt):
        bot._send_weekly(conn, FRI)
    conn.rollback()                                    # what closing the connection does
    assert _signalled(conn) == [] and _sent_keys(conn) is None


def test_a_signal_that_fails_stops_the_sequence_and_leaves_the_week_open(conn, week, capsys):
    week.refuse = {"BUY2"}
    assert bot._send_weekly(conn, FRI) is False
    assert week.sent == ["BUY1", "BUY2"]                                 # EXIT3 and the summary are not tried
    assert _sent_keys(conn) == ["buy:AAA"] and _signalled(conn) == ["AAA"]      # no row for the buy that failed
    assert bot._weekly_due(conn, FRI) is True and bot._weekly_due(conn, SAT) is True
    assert "weekly message not sent" in capsys.readouterr().err


def test_the_rerun_after_a_failed_buy_sends_the_same_picks_without_picking_again(conn, week, monkeypatch):
    monkeypatch.setattr(signals_weekly, "pick_buys", lambda *a, **k: pytest.fail("the week is not picked twice"))
    week.refuse = {"BUY2"}
    bot._send_weekly(conn, FRI)
    week.refuse.clear()
    week.sent.clear()
    assert bot._send_weekly(conn, SAT) is True
    assert week.sent == ["BUY2", "EXIT3", "SUMMARY"]                     # not BUY1 again
    assert week.asked[-2:] == [("signals", SAT, ["AAA", "BBB"]), ("summary", SAT, False)]   # the same picks
    assert _sent_keys(conn) == ["buy:AAA", "buy:BBB", "exit:3"] and bot._weekly_due(conn, SAT) is False
    assert _signalled(conn) == ["AAA", "BBB"]                            # one row each, the second one Saturday's
    assert conn.execute("SELECT sent_at FROM buy_signals ORDER BY id").fetchall() == [("2026-10-09",), ("2026-10-10",)]


def test_a_summary_that_fails_leaves_every_signal_marked_and_is_all_the_rerun_sends(conn, week, capsys):
    week.refuse = {"SUMMARY"}
    assert bot._send_weekly(conn, FRI) is False
    assert week.sent == ["BUY1", "BUY2", "EXIT3", "SUMMARY"] and _sent_keys(conn) == ["buy:AAA", "buy:BBB", "exit:3"]
    assert bot._weekly_due(conn, FRI) is True and "weekly message not sent" in capsys.readouterr().err
    week.refuse.clear()
    week.sent.clear()
    assert bot._send_weekly(conn, SAT) is True
    assert week.sent == ["SUMMARY"] and _signalled(conn) == ["AAA", "BBB"]


def test_a_second_run_in_the_same_week_sends_nothing(conn, week):
    assert bot._send_weekly(conn, FRI) is True
    week.sent.clear()
    assert bot._send_weekly(conn, SAT) is False and week.sent == []
    assert bot._send_weekly(conn, FRI) is False and week.sent == []
    assert _signalled(conn) == ["AAA", "BBB"]


def test_a_new_week_starts_with_nothing_sent_and_its_own_picks(conn, week):
    bot._send_weekly(conn, FRI)
    week.sent.clear()
    _keep_picks(conn, [_pick("CCC")], NEXT_FRI)
    week.signals = [("buy:CCC", "BUY3")]
    assert bot._send_weekly(conn, NEXT_FRI) is True
    assert week.sent == ["BUY3", "SUMMARY"]
    assert _sent_keys(conn, "weekly_sent_2026-W42") == ["buy:CCC"] and _signalled(conn) == ["AAA", "BBB", "CCC"]


def test_the_sent_signals_are_a_json_list_in_kv_named_by_the_iso_week(conn, week):
    bot._send_weekly(conn, SAT)
    assert bot.SENT_KEY.format(week=bot._week_id(SAT)) == "weekly_sent_2026-W41"
    assert conn.execute("SELECT value FROM kv_cache WHERE key = ?", (SENT_KEY,)).fetchone() == (
        '["buy:AAA", "buy:BBB", "exit:3"]',)


def test_an_unreadable_sent_list_counts_as_nothing_sent(conn, week):
    db.save_cached_json(conn, SENT_KEY, {"not": "a list"})
    assert bot._send_weekly(conn, FRI) is True
    assert week.sent == ["BUY1", "BUY2", "EXIT3", "SUMMARY"]


def test_the_picks_are_a_json_list_in_kv_named_by_the_iso_week(conn):
    assert bot.PICKS_KEY.format(week=bot._week_id(FRI)) == PICKS_KEY == "weekly_buys_2026-W41"
    _keep_picks(conn, [_pick("AAA")])
    assert bot._week_picks(conn, FRI) == [_pick("AAA")] and bot._week_picks(conn, NEXT_FRI) == []


@pytest.mark.parametrize("kept", [{"not": "a list"}, "text", 5, [1, "x", None]])
def test_unreadable_picks_count_as_none(conn, kept):
    db.save_cached_json(conn, PICKS_KEY, kept)
    assert bot._week_picks(conn, FRI) == []


def test_with_the_scoring_failed_no_buy_is_sent_and_the_summary_gets_the_warning(conn, week):
    assert bot._send_weekly(conn, SUN, scoring_failed=True) is True
    assert week.asked == [("signals", SUN, []), ("summary", SUN, True)]
    assert week.sent[-1] == "SUMMARY (warning)" and _signalled(conn) == []


def test_picks_left_in_kv_by_a_pass_that_never_finished_are_not_sent_on_sunday(conn, week):
    """The week's key was never set, so those picks are not the week's: the warning says there were none."""
    bot._send_weekly(conn, SUN, scoring_failed=True)
    assert week.asked[0] == ("signals", SUN, [])


def test_send_weekly_does_not_touch_the_buys_key(conn, week):
    bot._send_weekly(conn, FRI)
    assert bot._weekly_due(conn, FRI, bot.BUYS_KEY) is True


def test_a_real_week_goes_out_as_one_message_a_signal_then_the_summary(conn, monkeypatch):
    coin = _pick("CRYPTO:BTC", kind="crypto", source="CRYPTO", company="BTC", score=75.0, stop_pct=0.15,
                 reasons=["выше 100-дн. средней"], t212=None)
    _keep_picks(conn, [_pick("AAA", t212=False), coin])
    db.journal_signal(conn, {"source": "SEC", "kind": "exit", "ticker": "ZZZ", "company": "Exit Corp",
                             "members": '["Ann Lee", "Bo Chen"]'})
    conn.execute("UPDATE signal_journal SET emitted_at = ?", (f"{FRI - dt.timedelta(days=1)} 12:00:00",))
    conn.commit()
    sent = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    assert bot._send_weekly(conn, FRI) is True
    assert sent[:3] == ["🟢 <b>AAA!</b>: покупка — 2 инсайдера из руководства; CEO среди покупателей; "
                        "балл 70, стоп −10%, нет на Trading 212",
                        "🟢 <b>BTC!</b>: покупка — выше 100-дн. средней; балл 75, стоп −15%",
                        "🔴 <b>ZZZ!</b>: продают те, кто покупал — Ann Lee, Bo Chen"]
    assert len(sent) == 4 and sent[3].startswith("📊 <b>Неделя 03.10–09.10</b>")
    assert "Сигналов за неделю: покупок 2, на продажу 0, групповых выходов 1" in sent[3]
    assert conn.execute("SELECT ticker, kind, score, t212, sent_at FROM buy_signals ORDER BY id").fetchall() == [
        ("AAA", "stock", 70.0, 0, "2026-10-09"), ("CRYPTO:BTC", "crypto", 75.0, None, "2026-10-09")]


def test_a_quiet_real_week_is_one_summary_and_no_row(conn, monkeypatch):
    _keep_picks(conn, [])
    sent = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    assert bot._send_weekly(conn, FRI) is True
    assert len(sent) == 1 and sent[0].splitlines()[2] == "Сигналов за неделю не было."
    assert _signalled(conn) == []


# ------------------------------------------------------------------ _pick_week
def test_the_week_is_picked_from_the_days_scores_and_the_picks_are_kept_as_records(conn, monkeypatch):
    seen = []
    chosen = [_score("AAA", 70.0), _score("BBB", 64.0, t212=False)]
    monkeypatch.setattr(signals_weekly, "pick_buys", lambda c, today, scored: seen.append((today, scored)) or chosen)
    report = _report({"AAA": "buy"}, scored=chosen + [_score("ZZZ", 50.0, decision="watch")])
    assert bot._pick_week(conn, FRI, report) is True
    assert seen == [(FRI, report.scored)]
    assert db.get_cached_json(conn, PICKS_KEY) == [signals_weekly.pick_record(s) for s in chosen]
    assert db.get_cached_json(conn, PICKS_KEY)[1]["t212"] is False
    assert bot._weekly_due(conn, FRI, bot.BUYS_KEY) is False          # the week's buys are done


def test_the_week_is_picked_with_the_real_rules(conn):
    conn.execute("INSERT INTO positions (ticker, opened_at, entry_price) VALUES ('HELD', '2026-09-01', 10.0)")
    conn.commit()
    scored = [_score("LOW", 61.0), _score("HELD", 90.0), _score("TOP", 80.0), _score("WAT", 50.0, decision="watch")]
    bot._pick_week(conn, FRI, _report(scored=scored))
    assert [p["ticker"] for p in db.get_cached_json(conn, PICKS_KEY)] == ["TOP", "LOW"]


def _coin_score(coin, total):
    return model_score.CoinScore(
        coin=coin, ticker=f"CRYPTO:{coin}", trend=45.0, flows=15.0, news=0.0, total=total, trend_up=True,
        trend_down=False, caution=None, block=None, decision=model_score.BUY,
        reasons=["выше 100-дн. средней", "покупают крупные игроки"], stop_pct=0.22, last_close=100.0)


def test_the_week_keeps_bitcoin_and_ether_as_the_two_coins_before_higher_scoring_alts(conn):
    scored = [_coin_score(c, 90.0 - i) for i, c in enumerate(["SOL", "BTC", "XRP", "LINK", "ETH"])] + [
        _score("S0", 66.0), _score("S1", 64.0), _score("S2", 62.0)]
    bot._pick_week(conn, FRI, _report(scored=scored))
    kept = db.get_cached_json(conn, PICKS_KEY)
    assert [p["ticker"] for p in kept] == ["S0", "S1", "S2", "CRYPTO:BTC", "CRYPTO:ETH"]     # stocks first, then the coins
    assert [p.get("risk") for p in kept] == [None] * 5                           # no alt among them: no tag


def test_the_week_keeps_two_coins_at_most_and_marks_the_alts_high_risk(conn):
    eth = _coin_score("ETH", 86.0)
    eth.decision = model_score.WATCH                                              # ether is not a buy: its place goes to an alt
    scored = [_coin_score(c, 90.0 - i) for i, c in enumerate(["SOL", "BTC", "XRP", "LINK"])] + [eth] + [
        _score("S0", 66.0), _score("S1", 64.0), _score("S2", 62.0)]
    bot._pick_week(conn, FRI, _report(scored=scored))
    kept = db.get_cached_json(conn, PICKS_KEY)
    assert [p["ticker"] for p in kept] == ["S0", "S1", "S2", "CRYPTO:BTC", "CRYPTO:SOL"]
    assert [p.get("risk") for p in kept] == [None, None, None, None, True]
    # ... and what a retry sends is read back from the kept records
    texts = dict(weekly.week_signals(conn, FRI, bot._week_picks(conn, FRI)))
    assert texts["buy:CRYPTO:SOL"] == ("🟢 <b>SOL!</b>: покупка — выше 100-дн. средней; покупают крупные игроки; "
                                       "балл 90, стоп −22%, высокий риск")
    assert texts["buy:CRYPTO:BTC"].endswith("балл 89, стоп −22%")


def test_a_week_with_nothing_to_signal_is_still_picked_once(conn):
    assert bot._pick_week(conn, FRI, _report(scored=[_score("WAT", 50.0, decision="watch")])) is True
    assert db.get_cached_json(conn, PICKS_KEY) == [] and bot._weekly_due(conn, FRI, bot.BUYS_KEY) is False


def test_picks_are_kept_before_the_week_is_marked(conn, monkeypatch):
    """A crash between the two leaves the week open: the next run picks again (and sends nothing before)."""
    monkeypatch.setattr(bot, "_mark_week", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk")))
    with pytest.raises(RuntimeError):
        bot._pick_week(conn, FRI, _report(scored=[_score("AAA")]))
    assert db.get_cached_json(conn, PICKS_KEY) != [] and bot._weekly_due(conn, FRI, bot.BUYS_KEY) is True


# ------------------------------------------------------------------ _score_signals
def _args(*flags):
    return bot.build_parser().parse_args(["--once", *flags])


def _boom(*a, **kw):
    raise AssertionError("model.score_day must not be called")


@pytest.mark.parametrize("flag", ["--sec-only", "--house-only", "--bafin-only", "--norway-only",
                                  "--sweden-only", "--crypto-only", "--min-score=35",
                                  "--min-liquidity=1000000"])
def test_score_signals_skips_a_filtered_run(conn, monkeypatch, capsys, flag):
    """A filtered run sees only part of the day's signals: it does not score, so it never picks."""
    monkeypatch.setattr(model, "score_day", _boom)
    monkeypatch.setattr("telegram_notify.send_text", _boom)
    assert bot._score_signals(conn, _args(flag)) is None
    assert "filtered run" in capsys.readouterr().out


def test_score_signals_returns_the_models_report(conn, monkeypatch):
    report = _report({"AAA": "buy"})
    monkeypatch.setattr(model, "score_day", lambda c: report)
    assert bot._score_signals(conn, _args()) is report


def test_score_signals_scores_every_full_run_and_has_no_buy_flag(conn, monkeypatch):
    seen = []
    monkeypatch.setattr(model, "score_day", lambda c: seen.append(1) or _report())
    bot._score_signals(conn, _args())
    bot._score_signals(conn, _args())
    assert seen == [1, 1]
    with pytest.raises(TypeError):
        bot._score_signals(conn, _args(), buy=True)


def test_score_signals_reports_a_crash_and_carries_on(conn, monkeypatch, capsys):
    def crash(c):
        raise RuntimeError("no prices")
    sent = []
    monkeypatch.setattr(model, "score_day", crash)
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    assert bot._score_signals(conn, _args()) is None
    assert "[SCORING] pass failed: RuntimeError: no prices" in capsys.readouterr().err
    assert sent == ["⚠️ disclosure-bot: оценка сигналов упала в этом прогоне. Логи: data/launchd.err.log"]


def test_score_signals_does_not_message_about_a_crash_with_no_telegram(conn, monkeypatch):
    def crash(c):
        raise RuntimeError("no prices")
    monkeypatch.setattr(model, "score_day", crash)
    monkeypatch.setattr("telegram_notify.send_text", _boom)
    assert bot._score_signals(conn, _args("--no-telegram")) is None


# ------------------------------------------------------------------ the weekly run
def test_the_weeks_keys_are_named_by_iso_week():
    assert bot._week_id(FRI) == "2026-W41"
    assert bot.BUYS_KEY.format(week=bot._week_id(FRI)) == "model_buys_2026-W41"
    assert bot.MESSAGE_KEY.format(week=bot._week_id(FRI)) == "weekly_message_2026-W41"


@pytest.mark.parametrize("day", [MON, TUE, WED, THU])
@pytest.mark.parametrize("key", [bot.MESSAGE_KEY, bot.BUYS_KEY])
def test_monday_to_thursday_is_never_a_weekly_run(conn, day, key):
    assert bot._weekly_due(conn, day, key) is False


@pytest.mark.parametrize("key", [bot.MESSAGE_KEY, bot.BUYS_KEY])
def test_friday_with_no_key_is_a_weekly_run(conn, key):
    assert bot._weekly_due(conn, FRI, key) is True


def test_the_message_key_is_the_default(conn):
    bot._mark_week(conn, FRI, bot.MESSAGE_KEY)
    assert bot._weekly_due(conn, FRI) is False


def test_saturday_after_fridays_key_is_not_a_weekly_run(conn):
    bot._mark_week(conn, FRI, bot.MESSAGE_KEY)
    assert bot._weekly_due(conn, SAT) is False and bot._weekly_due(conn, SUN) is False


@pytest.mark.parametrize("day", [SAT, SUN])
def test_a_missed_friday_is_made_up_on_the_weekend(conn, day):
    assert bot._weekly_due(conn, day) is True


def test_the_next_iso_week_is_due_again(conn):
    bot._mark_week(conn, FRI, bot.MESSAGE_KEY)
    assert bot._weekly_due(conn, NEXT_FRI) is True


def test_the_two_keys_are_independent(conn):
    bot._mark_week(conn, FRI, bot.BUYS_KEY)
    assert bot._weekly_due(conn, FRI, bot.BUYS_KEY) is False
    assert bot._weekly_due(conn, FRI, bot.MESSAGE_KEY) is True
    bot._mark_week(conn, SAT, bot.MESSAGE_KEY)
    assert bot._weekly_due(conn, SAT, bot.MESSAGE_KEY) is False


def test_the_week_is_the_iso_week_across_new_year(conn):
    """2027-01-01 (a Friday) is in ISO week 2026-W53: Saturday the 2nd is the same week, the
    Friday after the 4th, Monday of 2027-W01, starts a new one."""
    bot._mark_week(conn, dt.date(2027, 1, 1), bot.MESSAGE_KEY)
    assert bot._weekly_due(conn, dt.date(2027, 1, 2)) is False
    assert bot._weekly_due(conn, dt.date(2027, 1, 8)) is True
    assert db.get_cached_value(conn, "weekly_message_2026-W53", float("inf")) is not None


def test_filtered_run_is_true_for_any_filter_flag_and_false_for_a_plain_run():
    assert bot._filtered_run(types.SimpleNamespace()) is False
    assert bot._filtered_run(types.SimpleNamespace(min_score=0, min_liquidity=0)) is False
    for flag in bot._FILTER_FLAGS:
        assert bot._filtered_run(types.SimpleNamespace(**{flag: 1})) is True
    assert set(bot._FILTER_FLAGS) == {"sec_only", "house_only", "bafin_only", "norway_only",
                                      "sweden_only", "crypto_only", "min_score", "min_liquidity"}


# ------------------------------------------------------------ collect_new_signals
def test_collect_new_signals_returns_every_new_buy_side_signal_unfiltered(conn, monkeypatch):
    """No recency window, no Trading 212 filter, no size floor: whatever the finders see."""
    old = (TODAY - dt.timedelta(days=8)).isoformat()
    for name in ("Director One", "Director Two", "Director Three"):
        add_sec_purchase(conn, "AAA", name, 200_000, old, filed_date=old)
    buys, exits = bot.collect_new_signals(conn, _args())
    assert [s.ticker for s in buys] == ["AAA"] and exits == []


def test_collect_new_signals_includes_exit_signals(conn):
    bought = (TODAY - dt.timedelta(days=60)).isoformat()
    sold = (TODAY - dt.timedelta(days=10)).isoformat()
    for i in range(2):
        add_sec_purchase(conn, "BBB", f"Buyer {i}", 200_000, bought)
    add_sec_sale(conn, "BBB", "Buyer 0", 200_000, sold)
    add_sec_sale(conn, "BBB", "Buyer 1", 200_000, sold)
    _buys, exits = bot.collect_new_signals(conn, _args())
    assert [e.ticker for e in exits] == ["BBB"]


def test_collect_new_signals_respects_no_exit_signals(conn):
    bought = (TODAY - dt.timedelta(days=60)).isoformat()
    sold = (TODAY - dt.timedelta(days=10)).isoformat()
    for i in range(2):
        add_sec_purchase(conn, "BBB", f"Buyer {i}", 200_000, bought)
    add_sec_sale(conn, "BBB", "Buyer 0", 200_000, sold)
    add_sec_sale(conn, "BBB", "Buyer 1", 200_000, sold)
    _buys, exits = bot.collect_new_signals(conn, _args("--no-exit-signals"))
    assert exits == []


def test_collect_new_signals_prints_nothing_itself(conn, capsys):
    for name in ("Director One", "Director Two"):
        add_sec_purchase(conn, "AAA", name, 200_000, RECENT, filed_date=RECENT)
    buys, _exits = bot.collect_new_signals(conn, _args())
    assert len(buys) == 1 and capsys.readouterr().out == ""


def test_collect_new_signals_skips_what_was_already_committed(conn, monkeypatch):
    for name in ("Director One", "Director Two"):
        add_sec_purchase(conn, "AAA", name, 200_000, RECENT, filed_date=RECENT)
    monkeypatch.setattr(cluster, "enrich_signals", lambda c, s: s)
    buys, exits = bot.collect_new_signals(conn, _args())
    bot._journal(conn, buys + exits, None)
    assert bot.collect_new_signals(conn, _args()) == ([], [])


# ------------------------------------------------------------------ main() order
@pytest.fixture
def main_run(monkeypatch, tmp_path):
    """bot.main() with every pass stubbed, recording the order things happen in. The day is `run.today` (a
    Friday: a weekly run). The scoring gives `run.report` -- nothing with `run.scoring_crashes`, an incomplete
    one with `run.complete` False; `signals_weekly.pick_buys` picks `run.picks`; the week's signals
    (`run.signals`) and then the summary go out through `send_text`, which says yes unless `run.send_ok` is
    False or the text is in `run.refuse`. The database is real (tmp_path), so the weekly keys carry from one
    run to the next."""
    calls = []
    closes = ["close-alert"]
    report = _report({"AAA": "buy"}, scored=[_score("AAA")])
    buy_sig, exit_sig, caution_sig = _cluster_sig(), _exit_sig(), _coin_sig(False)

    monkeypatch.setattr(bot, "DB_PATH", tmp_path / "data" / "d.db")
    monkeypatch.setattr(bot, "LOG_PATH", tmp_path / "data" / "bot.log")
    for name in ("run_sec_pass", "run_stake_pass", "run_144_pass", "run_house_pass",
                 "run_bafin_pass", "run_norway_pass", "run_sweden_pass", "run_senate_pass",
                 "run_crypto_treasury_pass", "run_crypto_etf_pass", "run_farside_pass",
                 "run_onchain_pass"):
        monkeypatch.setattr(bot, name, lambda *a, **kw: 0)
    monkeypatch.setattr(bot.cik_map, "CikMap", lambda: None)
    monkeypatch.setattr(bot.sec_edgar, "new_session", lambda: None)

    def collect(conn, args):
        calls.append("collect")
        return [buy_sig] + ([caution_sig] if run.cautions else []), [exit_sig]

    def score_signals(conn, args):
        calls.append("score")
        if run.scoring_crashes:
            return None
        return report if run.complete else dataclasses.replace(report, complete=False)

    def pick_buys(conn, today, scored):
        calls.append(("pick", today))
        if run.pick_fails:
            raise RuntimeError("pick")
        return list(run.picks)

    def journal(conn, signals, rep):
        calls.append(("journal", signals, rep))

    def check_exits(conn):
        calls.append("check_exits")
        return closes

    def send_closes(conn, cl):
        calls.append(("closes", cl))
        return True

    def week_signals(conn, today, picks):
        calls.append(("signals", today, [p["ticker"] for p in picks]))
        if run.week_fails:
            raise RuntimeError("format")
        return list(run.signals)

    def format_summary(conn, today, *, html=True, scoring_failed=False):
        calls.append(("summary", today))
        run.week_kwargs.append({"scoring_failed": scoring_failed})
        return "SUMMARY"

    def send_text(msg):
        calls.append(("send", msg))
        return run.send_ok and msg not in run.refuse

    monkeypatch.setattr(bot, "_today", lambda: run.today)
    monkeypatch.setattr(bot, "collect_new_signals", collect)
    monkeypatch.setattr(bot, "_score_signals", score_signals)
    monkeypatch.setattr(signals_weekly, "pick_buys", pick_buys)
    monkeypatch.setattr(bot, "_journal", journal)
    monkeypatch.setattr(bot.positions, "check_exits", check_exits)
    monkeypatch.setattr(bot, "_send_closes", send_closes)
    monkeypatch.setattr(weekly, "week_signals", week_signals)
    monkeypatch.setattr(weekly, "format_summary", format_summary)
    monkeypatch.setattr(bot.telegram_notify, "send_text", send_text)
    monkeypatch.setattr(bot.telegram_notify, "format_close_alert", lambda a, html=False: str(a))

    def t212_sync(conn, *, silent=False):
        # kept apart from `calls` (the order the day's own steps happen in): where in that order it
        # ran -- how many steps were before it -- and whether it was told to stay silent
        run.syncs.append(len(calls))
        run.sync_silent.append(silent)
        if run.sync_crashes:
            raise RuntimeError("t212 is down")
        return t212_account.SyncResult()
    monkeypatch.setattr(bot.t212_account, "sync", t212_sync)

    def cfd_pass(conn):
        # kept apart from `calls` too: the steps that were done before it, and whether it was run at all
        run.cfd.append(list(calls))
        if run.cfd_crashes:
            raise RuntimeError("yahoo is down")
        return types.SimpleNamespace(tracked=1, made=2, paused=False)
    monkeypatch.setattr(bot.cfd_live, "run", cfd_pass)

    def league_pass(conn):
        run.league.append(list(calls))
        if run.league_crashes:
            raise RuntimeError("cftc is down")
        return (1, 0)
    monkeypatch.setattr(bot.cfd_league, "run", league_pass)

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["bot.py", "--once", *argv])
        monkeypatch.setattr(sys, "stdout", sys.stdout)     # main() wraps the streams: restore them after
        monkeypatch.setattr(sys, "stderr", sys.stderr)
        bot.main()
        return calls

    run.report, run.buy_sig, run.exit_sig, run.closes = report, buy_sig, exit_sig, closes
    run.caution_sig, run.cautions = caution_sig, False
    run.today, run.send_ok, run.scoring_crashes, run.week_fails = FRI, True, False, False
    run.pick_fails = False
    run.picks = []                                                # what pick_buys picks (scores)
    run.week_kwargs = []                                          # what the summary was asked for, per call
    run.signals, run.refuse = [], set()                           # the week's signals; texts Telegram refuses
    run.complete = True                                           # False: a scoring pass that did not get through
    run.syncs, run.sync_silent, run.sync_crashes = [], [], False  # the Trading 212 sync (stubbed)
    run.cfd, run.cfd_crashes = [], False                          # the CFD pass (stubbed): `calls` as it was
    run.league, run.league_crashes = [], False                    # the paper league's pass (stubbed), the same
    run.db = lambda: db.connect(tmp_path / "data" / "d.db")
    return run


def _kinds(calls):
    return [c if isinstance(c, str) else c[0] for c in calls]


def _week_keys(main_run, like):
    return main_run.db().execute("SELECT COUNT(*) FROM kv_cache WHERE key LIKE ?", (like,)).fetchone()[0]


def test_main_on_a_weekly_run_goes_collect_score_pick_journal_closes_signals_summary(main_run):
    calls = main_run()
    assert calls == ["collect", "score", ("pick", FRI),
                     ("journal", [main_run.buy_sig, main_run.exit_sig], main_run.report),
                     "check_exits", ("closes", main_run.closes),
                     ("signals", FRI, []),               # weekly.week_signals(conn, today, picks)
                     ("summary", FRI),                   # weekly.format_summary(conn, today)
                     ("send", "SUMMARY")]


def test_main_sends_the_weeks_signals_one_by_one_then_the_summary(main_run):
    main_run.picks = [_score("AAA")]
    main_run.signals = [("buy:AAA", "BUY1"), ("exit:3", "EXIT3")]
    calls = main_run()
    assert calls[6:] == [("signals", FRI, ["AAA"]), ("send", "BUY1"), ("send", "EXIT3"),
                         ("summary", FRI), ("send", "SUMMARY")]


def test_a_signal_that_fails_mid_week_holds_back_the_summary_and_the_rerun_resumes(main_run):
    main_run.picks = [_score("AAA"), _score("BBB")]
    main_run.signals = [("buy:AAA", "BUY1"), ("buy:BBB", "BUY2"), ("exit:3", "EXIT3")]
    main_run.refuse = {"BUY2"}
    calls = main_run()
    assert [c for c in calls if c[0] == "send"] == [("send", "BUY1"), ("send", "BUY2")]
    assert "summary" not in _kinds(calls)
    conn = main_run.db()
    assert db.get_cached_json(conn, "weekly_sent_2026-W41") == ["buy:AAA"] and _signalled(conn) == ["AAA"]
    assert _week_keys(main_run, "weekly_message_%") == 0
    main_run.refuse, main_run.today = set(), SAT
    calls.clear()
    main_run()
    assert [c for c in calls if c[0] == "send"] == [("send", "BUY2"), ("send", "EXIT3"), ("send", "SUMMARY")]
    assert ("pick", SAT) not in calls and ("signals", SAT, ["AAA", "BBB"]) in calls    # Friday's picks, not new ones
    assert _week_keys(main_run, "weekly_message_%") == 1 and _signalled(main_run.db()) == ["AAA", "BBB"]
    calls.clear()
    main_run.today = SUN
    main_run()
    assert "send" not in _kinds(calls)                                # sent: nothing more this week


def test_main_journals_todays_cautions_before_the_scoring_and_the_rest_after(main_run):
    """A bearish coin signal found today is in the journal when the day is scored and the exits are checked:
    positions._crypto_caution reads it back."""
    main_run.cautions = True
    calls = main_run()
    assert calls[:5] == ["collect", ("journal", [main_run.caution_sig], None), "score", ("pick", FRI),
                         ("journal", [main_run.buy_sig, main_run.exit_sig], main_run.report)]
    assert calls[5] == "check_exits"


@pytest.mark.parametrize("day", [MON, TUE, WED, THU])
def test_main_on_a_weekday_does_not_pick_and_sends_only_the_close_alerts(main_run, day):
    main_run.today = day
    calls = main_run()
    assert calls == ["collect", "score",
                     ("journal", [main_run.buy_sig, main_run.exit_sig], main_run.report),
                     "check_exits", ("closes", main_run.closes)]
    assert main_run.db().execute("SELECT COUNT(*) FROM kv_cache WHERE key LIKE 'model_buys_%' "
                                 "OR key LIKE 'weekly_message_%' OR key LIKE 'weekly_buys_%'").fetchone() == (0,)


def test_main_picks_on_the_weeks_first_weekly_run_only(main_run):
    main_run()
    calls = main_run()                                            # the second run on Friday
    assert [c for c in calls if c[0] == "pick"] == [("pick", FRI)]
    main_run.today = SAT
    main_run()
    assert [c for c in calls if c[0] == "pick"] == [("pick", FRI)]
    main_run.today = NEXT_FRI
    main_run()
    assert [c for c in calls if c[0] == "pick"] == [("pick", FRI), ("pick", NEXT_FRI)]      # a new ISO week


def test_a_real_week_through_main_picks_sends_records_and_does_not_repeat_the_ticker_next_week(
        main_run, monkeypatch):
    """Nothing of the weekly flow is stubbed but the network: the scoring's BUY is picked, sent as a message,
    recorded in buy_signals, followed by the summary -- and a week later the same ticker is not signalled again."""
    monkeypatch.setattr(signals_weekly, "pick_buys", REAL_PICK_BUYS)
    monkeypatch.setattr(weekly, "week_signals", REAL_WEEK_SIGNALS)
    monkeypatch.setattr(weekly, "format_summary", REAL_FORMAT_SUMMARY)
    calls = main_run()
    sent = [c[1] for c in calls if c[0] == "send"]
    assert sent == ["🟢 <b>AAA!</b>: покупка — 2 инсайдера из руководства; CEO среди покупателей; балл 70, стоп −10%",
                    "📊 <b>Неделя 03.10–09.10</b>\nПозиций нет.\n"
                    "Сигналов за неделю: покупок 1, на продажу 0, групповых выходов 0"]
    conn = main_run.db()
    assert conn.execute("SELECT ticker, score, sent_at FROM buy_signals").fetchall() == [("AAA", 70.0, "2026-10-09")]
    calls.clear()
    main_run.today = NEXT_FRI
    main_run()
    sent = [c[1] for c in calls if c[0] == "send"]
    assert sent == ["📊 <b>Неделя 10.10–16.10</b>\nПозиций нет.\nСигналов за неделю не было."]      # AAA: signalled 7 days ago
    assert main_run.db().execute("SELECT COUNT(*) FROM buy_signals").fetchone() == (1,)


def test_main_picks_from_the_days_scores_and_keeps_the_picks_for_the_week(main_run):
    main_run.picks = [_score("AAA", 70.0)]
    main_run()
    conn = main_run.db()
    assert db.get_cached_json(conn, "weekly_buys_2026-W41") == [signals_weekly.pick_record(_score("AAA", 70.0))]


def test_main_sets_the_weeks_keys_after_a_clean_weekly_run(main_run):
    main_run()
    conn = main_run.db()
    assert db.get_cached_value(conn, "model_buys_2026-W41", float("inf")) is not None
    assert db.get_cached_value(conn, "weekly_message_2026-W41", float("inf")) is not None
    assert db.get_cached_json(conn, "weekly_buys_2026-W41") == []


def test_the_weekly_message_goes_once_a_week(main_run):
    main_run()
    main_run()                                                    # Friday again
    main_run.today = SUN
    calls = main_run()
    assert [c for c in calls if c[0] == "send"] == [("send", "SUMMARY")]


def test_a_failed_friday_send_is_retried_on_the_next_run_of_the_window(main_run):
    main_run.send_ok = False
    calls = main_run()
    assert ("send", "SUMMARY") in calls
    assert main_run.db().execute("SELECT COUNT(*) FROM kv_cache WHERE key LIKE 'weekly_message_%'"
                                 ).fetchone() == (0,)                 # the key waits for a success
    main_run.send_ok = True
    main_run.today = SAT
    calls.clear()
    main_run()
    assert ("send", "SUMMARY") in calls and ("pick", SAT) not in calls     # the picks were Friday's
    calls.clear()
    main_run.today = SUN
    main_run()
    assert "send" not in _kinds(calls)                                # sent: done for the week


def test_an_incomplete_pass_does_not_pick_the_week_or_send_the_message(main_run):
    """A scoring pass that did not get through gives no scores to pick from: the week stays open, the next
    run of the window picks, and the message waits with it."""
    main_run.complete = False
    calls = main_run()
    assert "pick" not in _kinds(calls) and "send" not in _kinds(calls)
    assert _week_keys(main_run, "model_buys_%") == 0 and _week_keys(main_run, "weekly_message_%") == 0
    assert _week_keys(main_run, "weekly_buys_%") == 0
    main_run.complete = True
    main_run.today = SAT
    calls.clear()
    main_run()
    assert ("pick", SAT) in calls and ("send", "SUMMARY") in calls
    assert _week_keys(main_run, "model_buys_%") == 1


def test_a_complete_pass_sets_the_buys_key(main_run):
    main_run()
    assert _week_keys(main_run, "model_buys_%") == 1


def test_sunday_with_a_pass_that_stayed_incomplete_sends_the_message_with_the_warning(main_run):
    main_run.complete = False
    for day in (FRI, SAT, SUN):
        main_run.today = day
        main_run()
    assert main_run.week_kwargs == [{"scoring_failed": True}]
    assert _week_keys(main_run, "model_buys_%") == 0 and _week_keys(main_run, "weekly_message_%") == 1


def test_a_missed_friday_is_made_up_on_the_weekend_picks_and_message(main_run):
    main_run.today = SAT
    calls = main_run()
    assert ("pick", SAT) in calls and ("send", "SUMMARY") in calls


def test_a_crashed_scoring_pass_does_not_use_up_the_weeks_picks(main_run):
    main_run.scoring_crashes = True
    main_run()
    assert _week_keys(main_run, "model_buys_%") == 0
    main_run.scoring_crashes = False
    main_run.today = SAT
    calls = main_run()
    assert [c for c in calls if c[0] == "pick"] == [("pick", SAT)]
    assert _week_keys(main_run, "model_buys_%") == 1


def test_a_friday_with_a_crashed_scoring_sends_no_weekly_message_and_keeps_the_week_open(main_run):
    """The message waits for the week's scoring pass: with no scores it would show no buys, and the pass
    that picks next (Saturday) would not be in it."""
    main_run.scoring_crashes = True
    calls = main_run()
    kinds = _kinds(calls)
    assert "signals" not in kinds and "summary" not in kinds and "send" not in kinds and "pick" not in kinds
    assert "closes" in kinds                                      # the close alerts do not wait
    assert _week_keys(main_run, "model_buys_%") == 0 and _week_keys(main_run, "weekly_message_%") == 0
    assert main_run.week_kwargs == []


def test_saturday_with_a_good_pass_picks_and_sends_the_message_after_a_crashed_friday(main_run):
    main_run.scoring_crashes = True
    main_run()
    main_run.scoring_crashes = False
    main_run.today = SAT
    calls = main_run()
    assert ("pick", SAT) in calls and ("send", "SUMMARY") in calls
    assert main_run.week_kwargs == [{"scoring_failed": False}]       # no warning: the scoring did run
    assert _week_keys(main_run, "model_buys_%") == 1 and _week_keys(main_run, "weekly_message_%") == 1


def test_sunday_with_the_scoring_still_crashing_sends_the_message_with_the_warning(main_run):
    main_run.scoring_crashes = True
    for day in (FRI, SAT):
        main_run.today = day
        assert "send" not in _kinds(main_run())                   # nothing yet: no scoring this week
    main_run.today = SUN
    calls = main_run()
    assert ("signals", SUN, []) in calls and ("summary", SUN) in calls and ("send", "SUMMARY") in calls
    assert main_run.week_kwargs == [{"scoring_failed": True}]
    assert _week_keys(main_run, "model_buys_%") == 0 and _week_keys(main_run, "weekly_message_%") == 1
    calls.clear()
    main_run()                                                    # Sunday again: done for the week
    assert "send" not in _kinds(calls)


def test_sunday_with_a_good_pass_sends_the_message_without_the_warning(main_run):
    main_run.scoring_crashes = True
    for day in (FRI, SAT):
        main_run.today = day
        main_run()
    main_run.scoring_crashes = False
    main_run.today = SUN
    calls = main_run()
    assert ("pick", SUN) in calls and ("send", "SUMMARY") in calls
    assert main_run.week_kwargs == [{"scoring_failed": False}]


def test_a_sunday_message_that_fails_to_send_is_not_marked(main_run):
    main_run.scoring_crashes = True
    main_run.send_ok = False
    main_run.today = SUN
    main_run()
    assert main_run.week_kwargs == [{"scoring_failed": True}] and _week_keys(main_run, "weekly_message_%") == 0


def test_a_run_before_sunday_with_no_pass_yet_says_so_in_the_log(main_run, capsys):
    main_run.scoring_crashes = True
    main_run()
    assert "weekly message waits for the week's scoring pass" in capsys.readouterr().out


def test_a_filtered_run_never_picks_and_sends_no_weekly_message(main_run):
    calls = main_run("--sec-only")
    kinds = _kinds(calls)
    assert "journal" in kinds and "closes" in kinds              # the rest of the day goes on
    assert "pick" not in kinds and "signals" not in kinds and "summary" not in kinds and "send" not in kinds
    assert main_run.db().execute("SELECT COUNT(*) FROM kv_cache WHERE key LIKE 'model_buys_%' "
                                 "OR key LIKE 'weekly_message_%' OR key LIKE 'weekly_buys_%'").fetchone() == (0,)
    calls.clear()
    main_run()                                                    # the full run later that Friday
    assert ("pick", FRI) in calls and ("send", "SUMMARY") in calls


def test_a_run_with_no_telegram_picks_the_week_but_does_not_send_its_message(main_run):
    calls = main_run("--no-telegram")
    assert _kinds(calls) == ["collect", "score", "pick", "journal", "check_exits"]
    assert _week_keys(main_run, "model_buys_%") == 1 and _week_keys(main_run, "weekly_buys_%") == 1
    calls.clear()
    main_run()                                                    # later, with Telegram
    assert ("pick", FRI) not in calls and ("send", "SUMMARY") in calls


def test_a_failing_pick_does_not_escape_main_and_the_week_stays_open(main_run, capsys):
    main_run.pick_fails = True
    calls = main_run()                                            # no exception
    assert "[PICKS] pass failed: RuntimeError: pick" in capsys.readouterr().err
    assert "closes" in _kinds(calls) and "send" not in _kinds(calls)      # the message waits for the picks
    assert db.get_cached_value(main_run.db(), "last_successful_run", float("inf")) is not None
    assert _week_keys(main_run, "model_buys_%") == 0 and _week_keys(main_run, "weekly_message_%") == 0
    main_run.pick_fails = False
    main_run.today = SAT
    calls.clear()
    main_run()
    assert ("pick", SAT) in calls and ("send", "SUMMARY") in calls


def test_main_prints_the_poll_finished_line(main_run, capsys):
    main_run("--no-telegram")
    out = capsys.readouterr().out
    assert "poll finished, 0 new purchase(s), 2 new signal(s), 1 scored, 1 close alert(s)" in out


def test_main_with_no_telegram_sends_nothing(main_run):
    calls = main_run("--no-telegram")
    assert _kinds(calls) == ["collect", "score", "pick", "journal", "check_exits"]


def test_main_on_a_filtered_run_journals_and_checks_the_exits(main_run):
    kinds = _kinds(main_run("--sec-only"))
    assert "journal" in kinds and "closes" in kinds


def test_main_records_the_successful_run(main_run):
    main_run("--no-telegram")
    assert db.get_cached_value(main_run.db(), "last_successful_run", float("inf")) is not None


def test_a_failing_weekly_message_does_not_escape_main_and_is_retried(main_run, capsys):
    main_run.week_fails = True
    main_run()                                                    # no exception
    assert "[WEEKLY] pass failed: RuntimeError: format" in capsys.readouterr().err
    conn = main_run.db()
    assert db.get_cached_value(conn, "last_successful_run", float("inf")) is not None
    assert db.get_cached_value(conn, "weekly_message_2026-W41", float("inf")) is None
    main_run.week_fails = False
    main_run.today = SAT
    assert ("send", "SUMMARY") in main_run()


def test_the_caution_is_in_the_journal_when_the_day_is_scored(conn, monkeypatch):
    monkeypatch.setattr(cluster, "enrich_signals", lambda conn, signals: signals)
    seen = []

    def score_signals(conn_, args):
        seen.append(conn_.execute("SELECT ticker, tier FROM signal_journal").fetchall())
        return _report({"AAA": "buy"})

    monkeypatch.setattr(bot, "_score_signals", score_signals)
    rest = bot._journal_cautions(conn, [_cluster_sig(), _coin_sig(False), _coin_sig(True)])
    report = bot._score_signals(conn, None)
    bot._journal(conn, rest, report)
    assert seen == [[("CRYPTO:BTC", "caution")]]
    assert [(s.ticker, getattr(s, "bullish", None)) for s in rest] == [("AAA", None), ("CRYPTO:BTC", True)]
    assert conn.execute("SELECT ticker, tier FROM signal_journal ORDER BY id").fetchall() == [
        ("CRYPTO:BTC", "caution"), ("AAA", "buy"), ("CRYPTO:BTC", None)]


def test_no_caution_journals_nothing_before_the_scoring(conn, recorded):
    sig = _cluster_sig()
    assert bot._journal_cautions(conn, [sig]) == [sig]
    assert recorded["committed"] == [] and recorded["enriched"] == []


# ---------------------------------------------------- /bought reads the journal back
def _norway_sig(ticker="EQNR"):
    return cluster.ClusterSignal(source="NORWAY", ticker=ticker, company="Equinor", buyer_count=2,
                                 total_value=1e6, members=[], window_start=RECENT,
                                 window_end=RECENT, member_names=["Ola Nordmann", "Kari Hansen"])


@pytest.mark.parametrize("decision", ["buy", "watch", "block", "skip", None])
def test_bought_finds_the_source_and_insiders_of_a_journaled_signal_whatever_its_decision(
        conn, monkeypatch, decision):
    """/bought EQNR must price the Oslo listing and watch for the insiders' sales -- with
    the journal holding the model's decision in `tier` and no `strong` row ever written."""
    monkeypatch.setattr(cluster, "enrich_signals", lambda conn, signals: signals)
    bot._journal(conn, [_norway_sig()], _report({"EQNR": decision} if decision else {}))
    assert positions.position_source(conn, "EQNR") == "NORWAY"
    pos = positions.open_position(conn, "EQNR", 270.0)
    assert pos.source == "NORWAY" and pos.insiders == ["Ola Nordmann", "Kari Hansen"]
    assert pos.signal_id is not None


def test_bought_ignores_a_later_exit_or_caution_row_for_the_same_ticker(conn, monkeypatch):
    monkeypatch.setattr(cluster, "enrich_signals", lambda conn, signals: signals)
    bot._journal(conn, [_norway_sig()], _report({"EQNR": "buy"}))
    bot._journal(conn, [cluster.ExitSignal(source="SEC", ticker="EQNR", company="Equinor",
                                           total_buyers=2, seller_count=2, lines=[],
                                           seller_names=["A", "B"])], None)
    db.journal_signal(conn, {"source": "SEC", "kind": "cluster", "ticker": "EQNR",
                             "tier": "caution", "members": '["Somebody Else"]'})
    assert positions.position_source(conn, "EQNR") == "NORWAY"
    assert positions.open_position(conn, "EQNR", 270.0).insiders == ["Ola Nordmann", "Kari Hansen"]


def test_bought_takes_the_latest_buy_side_row(conn, monkeypatch):
    monkeypatch.setattr(cluster, "enrich_signals", lambda conn, signals: signals)
    first = _norway_sig()
    bot._journal(conn, [first], _report({"EQNR": "watch"}))
    second = _norway_sig()
    second.member_names = ["Late Buyer"]
    bot._journal(conn, [second], _report({"EQNR": "buy"}))
    assert positions.open_position(conn, "EQNR", 270.0).insiders == ["Late Buyer"]


# ------------------------------------------------------------------ the Trading 212 sync in the daily run
def test_main_syncs_trading_212_before_the_pick_and_the_exits(main_run):
    """A holding bought or sold in the account today is opened or closed before the week's buy
    signals are picked (a name just bought is held, not signalled) and before the exits are checked."""
    calls = main_run()
    assert main_run.syncs == [2]                                    # collect, score, then the sync
    assert calls.index("check_exits") > 2
    assert main_run.sync_silent == [False]                          # its messages go to Telegram


@pytest.mark.parametrize("day", [MON, SAT])
def test_main_syncs_trading_212_on_every_full_run(main_run, day):
    main_run.today = day
    main_run()
    main_run()
    assert len(main_run.syncs) == 2


@pytest.mark.parametrize("flag", ["--sec-only", "--house-only", "--bafin-only", "--norway-only",
                                  "--sweden-only", "--crypto-only", "--min-score=35",
                                  "--min-liquidity=1000000"])
def test_a_filtered_run_does_not_sync_trading_212(main_run, flag):
    kinds = _kinds(main_run(flag))
    assert main_run.syncs == [] and "check_exits" in kinds          # the exits are still checked


def test_with_no_telegram_the_sync_runs_silently(main_run):
    main_run("--no-telegram")
    assert main_run.sync_silent == [True] and len(main_run.syncs) == 1


_HOLDING = t212_account.T212Position("GME_US_EQ", None, "US36467W1099", "USD", 10.0, 23.10, 24.05,
                                     None, None, None, 8.3, "EUR")
_SUMMARY = t212_account.T212Summary("EUR", 1000.0, 100.0, 900.0, 800.0, 100.0, 0.0)
_FIRST_MESSAGE = ("send", "📥 Слежу за вашими позициями в Trading 212 (1): GME\n"
                          "Для уже купленных бумаг правила выхода считаются с сегодняшнего дня.")


def _real_sync(monkeypatch):
    monkeypatch.setattr(bot.t212_account, "sync", REAL_T212_SYNC)
    monkeypatch.setattr(bot.t212_account, "fetch_account", lambda session=None: ([_HOLDING], _SUMMARY))
    # Yahoo knows GME at about Trading 212's price (one close: too few to size a stop)
    monkeypatch.setattr(positions, "daily_closes", lambda ticker, source=None: [("2026-08-03", 24.0)])


def test_a_no_telegram_run_opens_the_holding_and_leaves_the_first_message_for_a_run_with_telegram(
        main_run, monkeypatch, capsys):
    _real_sync(monkeypatch)
    calls = main_run("--no-telegram")
    assert not [c for c in calls if c[0] == "send"]
    conn = main_run.db()
    assert [p.ticker for p in positions.open_positions(conn)] == ["GME"]
    assert db.get_cached_value(conn, t212_account.SYNCED_ONCE_KEY, 10**9) is None
    assert "[T212] opened 1, updated 0, closed 0" in capsys.readouterr().out      # the log says what changed
    calls.clear()
    assert _FIRST_MESSAGE in main_run()                             # the next run, with Telegram
    assert db.get_cached_value(main_run.db(), t212_account.SYNCED_ONCE_KEY, 10**9) is not None


def test_a_sync_that_changed_nothing_adds_no_line_to_the_log(main_run, monkeypatch, capsys):
    _real_sync(monkeypatch)
    main_run()
    capsys.readouterr()
    main_run()
    assert "[T212] opened" not in capsys.readouterr().out


def test_a_crashing_sync_is_reported_like_a_failed_source_and_the_run_goes_on(main_run, capsys):
    main_run.sync_crashes = True
    calls = main_run()
    assert "check_exits" in calls and ("send", "SUMMARY") in calls
    assert "[T212] pass failed: RuntimeError" in capsys.readouterr().err
    assert db.get_cached_value(main_run.db(), "last_successful_run", float("inf")) is not None


def test_the_real_sync_without_a_key_leaves_the_daily_run_as_it_was(main_run, monkeypatch):
    """main calls the sync the way it takes, and with no key it is a no-op: the day's steps are
    exactly what they were."""
    monkeypatch.setattr(bot.t212_account, "sync", REAL_T212_SYNC)
    calls = main_run()
    assert _kinds(calls) == ["collect", "score", "pick", "journal", "check_exits", "closes", "signals",
                             "summary", "send"]
    conn = main_run.db()
    for table in ("positions", "t212_prices", "t212_equity"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)


def test_a_holding_the_daily_sync_finds_is_opened_and_announced_before_the_exits(main_run, monkeypatch):
    _real_sync(monkeypatch)
    calls = main_run()
    assert calls.index("score") < calls.index(_FIRST_MESSAGE) \
        < calls.index(("journal", [main_run.buy_sig, main_run.exit_sig], main_run.report)) \
        < calls.index("check_exits")
    [pos] = positions.open_positions(main_run.db())
    assert (pos.ticker, pos.origin, pos.quantity) == ("GME", "t212", 10.0)


# ------------------------------------------------------------------ the experimental CFD pass
def test_the_cfd_pass_runs_once_after_the_positions_step_of_a_full_run(main_run):
    main_run()
    assert len(main_run.cfd) == 1
    assert "check_exits" in main_run.cfd[0] and ("closes", main_run.closes) in main_run.cfd[0]


@pytest.mark.parametrize("day", [MON, WED, FRI, SAT, SUN])
def test_the_cfd_pass_runs_on_every_full_run_whatever_the_day(main_run, day):
    main_run.today = day
    main_run()
    assert len(main_run.cfd) == 1


@pytest.mark.parametrize("flag", ["--sec-only", "--house-only", "--bafin-only", "--norway-only", "--sweden-only"])
def test_a_filtered_run_does_not_run_the_cfd_pass(main_run, flag):
    main_run(flag)
    assert main_run.cfd == []


def test_a_run_with_no_telegram_skips_the_cfd_pass_and_says_so(main_run, capsys):
    main_run("--no-telegram")
    assert main_run.cfd == [] and "[CFD] skipped: --no-telegram" in capsys.readouterr().out


def test_a_crashing_cfd_pass_is_reported_and_the_run_goes_on(main_run, capsys):
    main_run.cfd_crashes = True
    main_run()
    err = capsys.readouterr()
    assert "[CFD] pass failed: RuntimeError: yahoo is down" in err.err
    assert main_run.db().execute("SELECT COUNT(*) FROM kv_cache WHERE key = 'last_successful_run'").fetchone() == (1,)
    assert "poll finished" in err.out


def test_the_cfd_pass_logs_what_it_did(main_run, capsys):
    main_run()
    assert "[CFD] 1 update message(s), 2 new signal(s)" in capsys.readouterr().out


def test_the_league_pass_runs_after_the_cfd_pass_on_a_full_run_only(main_run, capsys):
    main_run()
    assert len(main_run.league) == 1 and "[LEAGUE] opened 1, closed 0" in capsys.readouterr().out
    main_run("--no-telegram")
    main_run("--sec-only")
    assert len(main_run.league) == 1


def test_a_crashing_league_pass_is_reported_and_the_run_goes_on(main_run, capsys):
    main_run.league_crashes = True
    main_run()
    out = capsys.readouterr()
    assert len(main_run.league) == 1 and "LEAGUE" in out.out + out.err

