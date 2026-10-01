"""bot.py's daily flow on the model portfolio (spec 2026-09-30, section 5, as the weekly-message
spec 2026-10-01 changes it): the new signals are collected, the model runs, the journal takes the
model's decisions, the close alerts on the /bought positions go out as they fire, and once a week
(the first full run from Friday to Sunday) the model buys and the weekly message goes out. The
network and the model itself are stubbed."""
from __future__ import annotations

import datetime as dt
import sys
import types

import pytest

import bot
import cluster
import db
import model
import paper_report
import positions
import strategy
import telegram_notify
from conftest import add_sec_purchase, add_sec_sale

TODAY = dt.date.today()
RECENT = (TODAY - dt.timedelta(days=1)).isoformat()


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


def _report(decisions=None, buys=(), sells=()):
    return model.DayReport(buys=list(buys), sells=list(sells), scored=[], value=100_000.0,
                           bench=100_000.0, decisions=decisions or {})


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
def _closes(conn):
    positions.open_position(conn, "ZZZ", 10.0, today=TODAY - dt.timedelta(days=400))
    closes = positions.check_exits(conn, price_fn=lambda t, s=None: None)
    assert closes
    return closes


def test_send_closes_sends_nothing_when_there_are_none(conn, monkeypatch):
    sent = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    assert bot._send_closes(conn, []) is False
    assert sent == []


def test_send_closes_marks_close_alerts_only_after_a_successful_send(conn, monkeypatch):
    closes = _closes(conn)

    monkeypatch.setattr("telegram_notify.send_text", lambda msg: False)
    assert bot._send_closes(conn, closes) is False
    assert positions.check_exits(conn, price_fn=lambda t, s=None: None)        # still pending

    sent = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    assert bot._send_closes(conn, closes) is True
    assert positions.check_exits(conn, price_fn=lambda t, s=None: None) == []


def test_send_closes_message_is_the_header_and_each_alert_in_html(conn, monkeypatch):
    closes = _closes(conn)
    sent = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    bot._send_closes(conn, closes)
    [text] = sent
    assert text == "\n".join(["<b>🚪 Ваши позиции</b>"]
                             + [telegram_notify.format_close_alert(a) for a in closes])
    assert "ZZZ" in text and "Модельный портфель" not in text


def test_send_closes_says_a_failed_send_on_stderr(conn, monkeypatch, capsys):
    closes = _closes(conn)
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: False)
    assert bot._send_closes(conn, closes) is False
    assert "[telegram] send failed" in capsys.readouterr().err


# ---------------------------------------------------------------- _send_weekly
def test_send_weekly_sends_the_weeks_message_and_marks_the_week(conn, monkeypatch):
    model.create_books(conn, FRI - dt.timedelta(days=21))
    sent = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    assert bot._send_weekly(conn, FRI, None) is True
    assert sent == [paper_report.format_week(conn, FRI, None)] and "неделя 03.10–09.10" in sent[0]
    assert bot._weekly_due(conn, FRI) is False and bot._weekly_due(conn, SAT) is False


def test_send_weekly_leaves_the_week_open_when_the_send_fails(conn, monkeypatch, capsys):
    model.create_books(conn, FRI - dt.timedelta(days=21))
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: False)
    assert bot._send_weekly(conn, FRI, None) is False
    assert "weekly message not sent" in capsys.readouterr().err
    assert bot._weekly_due(conn, FRI) is True and bot._weekly_due(conn, SAT) is True


def test_send_weekly_does_not_touch_the_buys_key(conn, monkeypatch):
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: True)
    bot._send_weekly(conn, FRI, None)
    assert bot._weekly_due(conn, FRI, bot.BUYS_KEY) is True


# ------------------------------------------------------------------ _run_model
def _args(*flags):
    return bot.build_parser().parse_args(["--once", *flags])


def _boom(*a, **kw):
    raise AssertionError("model.run must not be called")


@pytest.mark.parametrize("flag", ["--sec-only", "--house-only", "--bafin-only", "--norway-only",
                                  "--sweden-only", "--crypto-only", "--min-score=35",
                                  "--min-liquidity=1000000"])
@pytest.mark.parametrize("buy", [True, False])
def test_run_model_skips_a_filtered_run_buy_or_not(conn, monkeypatch, capsys, flag, buy):
    """A filtered run never runs the model, so it never buys, whatever it is told."""
    monkeypatch.setattr(model, "run", _boom)
    monkeypatch.setattr("telegram_notify.send_text", _boom)
    assert bot._run_model(conn, _args(flag), buy=buy) is None
    assert "filtered run" in capsys.readouterr().out


def test_run_model_returns_the_models_report(conn, monkeypatch):
    report = _report({"AAA": "buy"})
    monkeypatch.setattr(model, "run", lambda c, buy=True: report)
    assert bot._run_model(conn, _args(), buy=True) is report


def test_run_model_hands_the_buy_flag_to_the_model(conn, monkeypatch):
    seen = []
    monkeypatch.setattr(model, "run", lambda c, buy=True: seen.append(buy) or _report())
    bot._run_model(conn, _args(), buy=True)
    bot._run_model(conn, _args(), buy=False)
    assert seen == [True, False]


def test_run_model_must_be_told_whether_to_buy(conn):
    with pytest.raises(TypeError):
        bot._run_model(conn, _args())


def test_run_model_reports_a_crash_and_carries_on(conn, monkeypatch, capsys):
    def crash(c, buy=True):
        raise RuntimeError("no prices")
    sent = []
    monkeypatch.setattr(model, "run", crash)
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    assert bot._run_model(conn, _args(), buy=False) is None
    assert "[MODEL] pass failed: RuntimeError: no prices" in capsys.readouterr().err
    assert sent == ["⚠️ disclosure-bot: модельный портфель упал в этом прогоне. "
                    "Логи: data/launchd.err.log"]


def test_run_model_does_not_message_about_a_crash_with_no_telegram(conn, monkeypatch):
    def crash(c, buy=True):
        raise RuntimeError("no prices")
    monkeypatch.setattr(model, "run", crash)
    monkeypatch.setattr("telegram_notify.send_text", _boom)
    assert bot._run_model(conn, _args("--no-telegram"), buy=True) is None


# ------------------------------------------------------------------ the weekly run
MON, TUE, WED, THU, FRI, SAT, SUN = (dt.date(2026, 10, d) for d in range(5, 12))      # ISO week 2026-W41
NEXT_FRI = dt.date(2026, 10, 16)


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
    """bot.main() with every pass stubbed, recording the order things happen in. The day is
    `run.today` (a Friday: a weekly run) and the weekly message goes out through `send_text`,
    which says yes unless `run.send_ok` is False; `run.model_crashes` makes the model return
    nothing. The database is real (tmp_path), so the weekly keys carry from one run to the next."""
    calls = []
    closes = ["close-alert"]
    report = _report({"AAA": "buy"}, buys=[object()], sells=[object()])
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

    def run_model(conn, args, *, buy):
        calls.append(("model", buy))
        return None if run.model_crashes else report

    def journal(conn, signals, rep):
        calls.append(("journal", signals, rep))

    def check_exits(conn):
        calls.append("check_exits")
        return closes

    def send_closes(conn, cl):
        calls.append(("closes", cl))
        return True

    def format_week(conn, today, rep, *, html=True, model_failed=False):
        calls.append(("week", today, rep))
        run.week_kwargs.append({"model_failed": model_failed})
        if run.week_fails:
            raise RuntimeError("format")
        return "WEEK"

    def send_text(msg):
        calls.append(("send", msg))
        return run.send_ok

    def monthly(conn, today):
        calls.append(("monthly", today))
        return False

    monkeypatch.setattr(bot, "_today", lambda: run.today)
    monkeypatch.setattr(bot, "collect_new_signals", collect)
    monkeypatch.setattr(bot, "_run_model", run_model)
    monkeypatch.setattr(bot, "_journal", journal)
    monkeypatch.setattr(bot.positions, "check_exits", check_exits)
    monkeypatch.setattr(bot, "_send_closes", send_closes)
    monkeypatch.setattr(paper_report, "format_week", format_week)
    monkeypatch.setattr(paper_report, "maybe_send_monthly_report", monthly)
    monkeypatch.setattr(bot.telegram_notify, "send_text", send_text)
    monkeypatch.setattr(bot.telegram_notify, "format_close_alert", lambda a, html=False: str(a))

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["bot.py", "--once", *argv])
        monkeypatch.setattr(sys, "stdout", sys.stdout)     # main() wraps the streams: restore them after
        monkeypatch.setattr(sys, "stderr", sys.stderr)
        bot.main()
        return calls

    run.report, run.buy_sig, run.exit_sig, run.closes = report, buy_sig, exit_sig, closes
    run.caution_sig, run.cautions = caution_sig, False
    run.today, run.send_ok, run.model_crashes, run.week_fails = FRI, True, False, False
    run.week_kwargs = []                                          # what format_week was asked for, per call
    run.db = lambda: db.connect(tmp_path / "data" / "d.db")
    return run


def _kinds(calls):
    return [c if isinstance(c, str) else c[0] for c in calls]


def test_main_on_a_weekly_run_goes_collect_model_journal_closes_message_monthly(main_run, capsys):
    calls = main_run()
    assert calls[0] == "collect" and calls[1] == ("model", True)
    assert calls[2] == ("journal", [main_run.buy_sig, main_run.exit_sig], main_run.report)
    assert calls[3] == "check_exits"
    assert calls[4] == ("closes", main_run.closes)
    assert calls[5] == ("week", FRI, main_run.report)             # format_week(conn, today, report)
    assert calls[6] == ("send", "WEEK")
    assert calls[7] == ("monthly", FRI)
    assert len(calls) == 8


def test_main_journals_todays_cautions_before_the_model_and_the_rest_after(main_run):
    """A bearish coin signal found today can close a MODEL-C coin today: the model's exit
    reads the caution from the journal."""
    main_run.cautions = True
    calls = main_run()
    assert calls[:4] == ["collect", ("journal", [main_run.caution_sig], None), ("model", True),
                         ("journal", [main_run.buy_sig, main_run.exit_sig], main_run.report)]
    assert calls[4] == "check_exits"


@pytest.mark.parametrize("day", [MON, TUE, WED, THU])
def test_main_on_a_weekday_does_not_buy_and_sends_only_the_close_alerts(main_run, day):
    main_run.today = day
    calls = main_run()
    assert calls == ["collect", ("model", False),
                     ("journal", [main_run.buy_sig, main_run.exit_sig], main_run.report),
                     "check_exits", ("closes", main_run.closes)]
    assert main_run.db().execute("SELECT COUNT(*) FROM kv_cache WHERE key LIKE 'model_buys_%' "
                                 "OR key LIKE 'weekly_message_%'").fetchone() == (0,)


def test_main_buys_on_the_weeks_first_weekly_run_only(main_run):
    main_run()
    calls = main_run()                                            # the second run on Friday
    assert [c for c in calls if c[0] == "model"] == [("model", True), ("model", False)]
    main_run.today = SAT
    main_run()
    assert [c for c in calls if c[0] == "model"][-1] == ("model", False)
    main_run.today = NEXT_FRI
    main_run()
    assert [c for c in calls if c[0] == "model"][-1] == ("model", True)    # a new ISO week


def test_main_sets_the_weeks_two_keys_after_a_clean_weekly_run(main_run):
    main_run()
    conn = main_run.db()
    assert db.get_cached_value(conn, "model_buys_2026-W41", float("inf")) is not None
    assert db.get_cached_value(conn, "weekly_message_2026-W41", float("inf")) is not None


def test_the_weekly_message_goes_once_a_week_and_the_monthly_report_with_it(main_run):
    main_run()
    main_run()                                                    # Friday again
    main_run.today = SUN
    calls = main_run()
    assert [c for c in calls if c[0] == "send"] == [("send", "WEEK")]
    assert [c for c in calls if c[0] == "monthly"] == [("monthly", FRI)]


def test_a_failed_friday_send_is_retried_on_the_next_run_of_the_window(main_run):
    main_run.send_ok = False
    calls = main_run()
    assert ("send", "WEEK") in calls
    assert main_run.db().execute("SELECT COUNT(*) FROM kv_cache WHERE key LIKE 'weekly_message_%'"
                                 ).fetchone() == (0,)                 # the key waits for a success
    main_run.send_ok = True
    main_run.today = SAT
    calls.clear()
    main_run()
    assert ("send", "WEEK") in calls and ("model", False) in calls    # the buys were Friday's
    calls.clear()
    main_run.today = SUN
    main_run()
    assert "send" not in _kinds(calls)                                # sent: done for the week


def test_the_monthly_report_is_still_tried_when_the_weekly_send_fails(main_run):
    main_run.send_ok = False
    assert ("monthly", FRI) in main_run()


def test_a_missed_friday_is_made_up_on_the_weekend_buys_and_message(main_run):
    main_run.today = SAT
    calls = main_run()
    assert ("model", True) in calls and ("send", "WEEK") in calls and ("monthly", SAT) in calls


def _week_keys(main_run, like):
    return main_run.db().execute("SELECT COUNT(*) FROM kv_cache WHERE key LIKE ?", (like,)).fetchone()[0]


def test_a_crashed_model_pass_does_not_use_up_the_weeks_buys(main_run):
    main_run.model_crashes = True
    main_run()
    assert _week_keys(main_run, "model_buys_%") == 0
    main_run.model_crashes = False
    main_run.today = SAT
    calls = main_run()
    assert [c for c in calls if c[0] == "model"] == [("model", True), ("model", True)]
    assert _week_keys(main_run, "model_buys_%") == 1


def test_a_friday_with_a_crashed_model_sends_no_weekly_message_and_keeps_the_week_open(main_run):
    """The message waits for the week's model pass (spec amendment): it would show no buys, and
    the pass that buys next (Saturday) would not be in it."""
    main_run.model_crashes = True
    calls = main_run()
    kinds = _kinds(calls)
    assert "week" not in kinds and "send" not in kinds and "monthly" not in kinds
    assert "closes" in kinds                                      # the close alerts do not wait
    assert _week_keys(main_run, "model_buys_%") == 0 and _week_keys(main_run, "weekly_message_%") == 0
    assert main_run.week_kwargs == []


def test_saturday_with_a_good_pass_buys_and_sends_the_message_after_a_crashed_friday(main_run):
    main_run.model_crashes = True
    main_run()
    main_run.model_crashes = False
    main_run.today = SAT
    calls = main_run()
    assert ("model", True) in calls and ("send", "WEEK") in calls and ("monthly", SAT) in calls
    assert main_run.week_kwargs == [{"model_failed": False}]       # no warning: the model did run
    assert _week_keys(main_run, "model_buys_%") == 1 and _week_keys(main_run, "weekly_message_%") == 1


def test_sunday_with_the_model_still_crashing_sends_the_message_with_the_warning(main_run):
    main_run.model_crashes = True
    for day in (FRI, SAT):
        main_run.today = day
        assert "send" not in _kinds(main_run())                   # nothing yet: no model pass this week
    main_run.today = SUN
    calls = main_run()
    assert ("week", SUN, None) in calls and ("send", "WEEK") in calls
    assert main_run.week_kwargs == [{"model_failed": True}]
    assert _week_keys(main_run, "model_buys_%") == 0 and _week_keys(main_run, "weekly_message_%") == 1
    assert ("monthly", SUN) in calls                               # the message went: its monthly follows
    calls.clear()
    main_run()                                                    # Sunday again: done for the week
    assert "send" not in _kinds(calls)


def test_sunday_with_a_good_pass_sends_the_message_without_the_warning(main_run):
    main_run.model_crashes = True
    for day in (FRI, SAT):
        main_run.today = day
        main_run()
    main_run.model_crashes = False
    main_run.today = SUN
    calls = main_run()
    assert ("model", True) in calls and ("send", "WEEK") in calls
    assert main_run.week_kwargs == [{"model_failed": False}]


def test_a_sunday_message_that_fails_to_send_is_not_marked(main_run):
    main_run.model_crashes = True
    main_run.send_ok = False
    main_run.today = SUN
    main_run()
    assert main_run.week_kwargs == [{"model_failed": True}] and _week_keys(main_run, "weekly_message_%") == 0


def test_a_run_before_sunday_with_no_pass_yet_says_so_in_the_log(main_run, capsys):
    main_run.model_crashes = True
    main_run()
    assert "weekly message waits" in capsys.readouterr().out


def test_a_filtered_run_never_buys_and_sends_no_weekly_message(main_run):
    calls = main_run("--sec-only")
    kinds = _kinds(calls)
    assert ("model", False) in calls
    assert "journal" in kinds and "closes" in kinds              # the rest of the day goes on
    assert "week" not in kinds and "send" not in kinds and "monthly" not in kinds
    assert main_run.db().execute("SELECT COUNT(*) FROM kv_cache WHERE key LIKE 'model_buys_%' "
                                 "OR key LIKE 'weekly_message_%'").fetchone() == (0,)
    calls.clear()
    main_run()                                                    # the full run later that Friday
    assert ("model", True) in calls and ("send", "WEEK") in calls


def test_a_run_with_no_telegram_uses_the_weeks_buys_but_not_its_message(main_run):
    calls = main_run("--no-telegram")
    assert _kinds(calls) == ["collect", "model", "journal", "check_exits"]
    assert ("model", True) in calls
    calls.clear()
    main_run()                                                    # later, with Telegram
    assert ("model", False) in calls and ("send", "WEEK") in calls


def test_the_monthly_report_comes_only_with_a_weekly_message(main_run):
    for day in (MON, TUE, WED, THU):
        main_run.today = day
        assert "monthly" not in _kinds(main_run())
    main_run.today = FRI
    assert "monthly" in _kinds(main_run())


def test_main_prints_the_poll_finished_line(main_run, capsys):
    main_run("--no-telegram")
    out = capsys.readouterr().out
    assert "poll finished, 0 new purchase(s), 2 new signal(s), 1 model buy(s), " \
           "1 model sale(s), 1 close alert(s)" in out


def test_main_with_no_telegram_sends_nothing_and_skips_the_monthly_report(main_run):
    calls = main_run("--no-telegram")
    assert _kinds(calls) == ["collect", "model", "journal", "check_exits"]


def test_main_on_a_filtered_run_journals_but_skips_the_monthly_report(main_run):
    kinds = _kinds(main_run("--sec-only"))
    assert "journal" in kinds and "monthly" not in kinds
    assert "closes" in kinds


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
    assert ("send", "WEEK") in main_run()


def test_the_caution_is_in_the_journal_when_the_model_runs(conn, monkeypatch):
    monkeypatch.setattr(cluster, "enrich_signals", lambda conn, signals: signals)
    seen = []

    def run_model(conn_, args, *, buy):
        seen.append(conn_.execute("SELECT ticker, tier FROM signal_journal").fetchall())
        return _report({"AAA": "buy"})

    monkeypatch.setattr(bot, "_run_model", run_model)
    rest = bot._journal_cautions(conn, [_cluster_sig(), _coin_sig(False), _coin_sig(True)])
    report = bot._run_model(conn, None, buy=True)
    bot._journal(conn, rest, report)
    assert seen == [[("CRYPTO:BTC", "caution")]]
    assert [(s.ticker, getattr(s, "bullish", None)) for s in rest] == [("AAA", None), ("CRYPTO:BTC", True)]
    assert conn.execute("SELECT ticker, tier FROM signal_journal ORDER BY id").fetchall() == [
        ("CRYPTO:BTC", "caution"), ("AAA", "buy"), ("CRYPTO:BTC", None)]


def test_no_caution_journals_nothing_before_the_model(conn, recorded):
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


# ------------------------------------------------------- the monthly report is contained
def test_a_failing_monthly_report_does_not_escape_main(main_run, monkeypatch, capsys):
    def boom(conn, today):
        raise RuntimeError("report")
    monkeypatch.setattr(paper_report, "maybe_send_monthly_report", boom)
    main_run()                                                    # no exception
    assert "[PAPER_REPORT] pass failed: RuntimeError: report" in capsys.readouterr().err
    assert db.get_cached_value(main_run.db(), "last_successful_run", float("inf")) is not None
