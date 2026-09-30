"""bot.py's daily flow on the model portfolio (spec 2026-09-30, section 5): the new signals
are collected, the model runs, the journal takes the model's decisions, and one message goes
out only when there is something to say. The network and the model itself are stubbed."""
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


# ------------------------------------------------------------------ _send_day
def test_send_day_sends_nothing_when_there_is_nothing_to_say(conn, monkeypatch):
    sent = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    assert bot._send_day(conn, _report(), [], []) is False
    assert bot._send_day(conn, None, [], []) is False
    assert sent == []


def test_send_day_marks_close_alerts_only_after_a_successful_send(conn, monkeypatch):
    positions.open_position(conn, "ZZZ", 10.0, today=TODAY - dt.timedelta(days=400))
    closes = positions.check_exits(conn, price_fn=lambda t, s=None: None)
    assert closes

    monkeypatch.setattr("telegram_notify.send_text", lambda msg: False)
    assert bot._send_day(conn, _report(), closes, []) is False
    assert positions.check_exits(conn, price_fn=lambda t, s=None: None)        # still pending

    sent = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    assert bot._send_day(conn, _report(), closes, []) is True
    assert "Модельный портфель" in sent[0] and "ZZZ" in sent[0]
    assert positions.check_exits(conn, price_fn=lambda t, s=None: None) == []


def test_send_day_says_a_failed_send_on_stderr(conn, monkeypatch, capsys):
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: False)
    assert bot._send_day(conn, _report(), [], [_exit_sig()]) is False
    assert "[telegram] send failed" in capsys.readouterr().err


def test_send_day_sends_when_there_are_only_group_exits(conn, monkeypatch):
    sent = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    assert bot._send_day(conn, None, [], [_exit_sig()]) is True
    assert "BBB" in sent[0]


def test_send_day_does_not_touch_the_alert_state_of_exits(conn, monkeypatch):
    """The alert state and the journal belong to _journal now, not to the send."""
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: True)
    assert bot._send_day(conn, None, [], [_exit_sig()]) is True
    assert db.get_alert_state(conn, "SEC_EXIT", "BBB") is None


# ------------------------------------------------------------------ _run_model
def _args(*flags):
    return bot.build_parser().parse_args(["--once", *flags])


def _boom(*a, **kw):
    raise AssertionError("model.run must not be called")


@pytest.mark.parametrize("flag", ["--sec-only", "--house-only", "--bafin-only", "--norway-only",
                                  "--sweden-only", "--crypto-only", "--min-score=35",
                                  "--min-liquidity=1000000"])
def test_run_model_skips_a_filtered_run(conn, monkeypatch, capsys, flag):
    monkeypatch.setattr(model, "run", _boom)
    monkeypatch.setattr("telegram_notify.send_text", _boom)
    assert bot._run_model(conn, _args(flag)) is None
    assert "filtered run" in capsys.readouterr().out


def test_run_model_returns_the_models_report(conn, monkeypatch):
    report = _report({"AAA": "buy"})
    monkeypatch.setattr(model, "run", lambda c: report)
    assert bot._run_model(conn, _args()) is report


def test_run_model_reports_a_crash_and_carries_on(conn, monkeypatch, capsys):
    def crash(c):
        raise RuntimeError("no prices")
    sent = []
    monkeypatch.setattr(model, "run", crash)
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    assert bot._run_model(conn, _args()) is None
    assert "[MODEL] pass failed: RuntimeError: no prices" in capsys.readouterr().err
    assert sent == ["⚠️ disclosure-bot: модельный портфель упал в этом прогоне. "
                    "Логи: data/launchd.err.log"]


def test_run_model_does_not_message_about_a_crash_with_no_telegram(conn, monkeypatch):
    def crash(c):
        raise RuntimeError("no prices")
    monkeypatch.setattr(model, "run", crash)
    monkeypatch.setattr("telegram_notify.send_text", _boom)
    assert bot._run_model(conn, _args("--no-telegram")) is None


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
class _Stop(Exception):
    pass


@pytest.fixture
def main_run(monkeypatch, tmp_path):
    """bot.main() with every pass stubbed, recording the order things happen in."""
    calls = []
    closes = ["close-alert"]
    report = _report({"AAA": "buy"}, buys=[object()], sells=[object()])
    buy_sig, exit_sig = _cluster_sig(), _exit_sig()

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
        return [buy_sig], [exit_sig]

    def run_model(conn, args):
        calls.append("model")
        return report

    def journal(conn, signals, rep):
        calls.append(("journal", signals, rep))

    def check_exits(conn):
        calls.append("check_exits")
        return closes

    def send_day(conn, rep, cl, ex):
        calls.append(("send", rep, cl, ex))
        return True

    def monthly(conn, today):
        calls.append("monthly")
        return False

    monkeypatch.setattr(bot, "collect_new_signals", collect)
    monkeypatch.setattr(bot, "_run_model", run_model)
    monkeypatch.setattr(bot, "_journal", journal)
    monkeypatch.setattr(bot.positions, "check_exits", check_exits)
    monkeypatch.setattr(bot, "_send_day", send_day)
    monkeypatch.setattr(paper_report, "maybe_send_monthly_report", monthly)
    monkeypatch.setattr(bot.telegram_notify, "send_text", lambda msg: True)
    monkeypatch.setattr(bot.telegram_notify, "format_close_alert", lambda a, html=False: str(a))

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["bot.py", "--once", *argv])
        monkeypatch.setattr(sys, "stdout", sys.stdout)     # main() wraps the streams: restore them after
        monkeypatch.setattr(sys, "stderr", sys.stderr)
        bot.main()
        return calls

    run.report, run.buy_sig, run.exit_sig, run.closes = report, buy_sig, exit_sig, closes
    return run


def test_main_runs_collect_model_journal_closes_send_monthly_in_that_order(main_run, capsys):
    calls = main_run()
    assert calls[0] == "collect" and calls[1] == "model"
    assert calls[2] == ("journal", [main_run.buy_sig, main_run.exit_sig], main_run.report)
    assert calls[3] == "check_exits"
    assert calls[4] == ("send", main_run.report, main_run.closes, [main_run.exit_sig])
    assert calls[5] == "monthly"
    assert len(calls) == 6


def test_main_prints_the_poll_finished_line(main_run, capsys):
    main_run("--no-telegram")
    out = capsys.readouterr().out
    assert "poll finished, 0 new purchase(s), 2 new signal(s), 1 model buy(s), " \
           "1 model sale(s), 1 close alert(s)" in out


def test_main_with_no_telegram_sends_nothing_and_skips_the_monthly_report(main_run):
    calls = main_run("--no-telegram")
    assert [c if isinstance(c, str) else c[0] for c in calls] == ["collect", "model", "journal", "check_exits"]


def test_main_on_a_filtered_run_journals_but_skips_the_monthly_report(main_run):
    calls = main_run("--sec-only")
    kinds = [c if isinstance(c, str) else c[0] for c in calls]
    assert "journal" in kinds and "monthly" not in kinds
    assert "send" in kinds


def test_main_records_the_successful_run(main_run, tmp_path):
    main_run("--no-telegram")
    conn = db.connect(tmp_path / "data" / "d.db")
    assert db.get_cached_value(conn, "last_successful_run", float("inf")) is not None


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
def test_a_failing_monthly_report_does_not_escape_main(main_run, monkeypatch, tmp_path, capsys):
    def boom(conn, today):
        raise RuntimeError("report")
    monkeypatch.setattr(paper_report, "maybe_send_monthly_report", boom)
    main_run()                                                    # no exception
    assert "[PAPER_REPORT] pass failed: RuntimeError: report" in capsys.readouterr().err
    conn = db.connect(tmp_path / "data" / "d.db")
    assert db.get_cached_value(conn, "last_successful_run", float("inf")) is not None
