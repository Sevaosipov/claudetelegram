"""signal_record.py: how the bot's own buy signals did, against the market."""
from __future__ import annotations

import datetime as dt

import pytest

import signal_record as sr
import signals_weekly
import telegram_bot as tb
import telegram_notify as tn


def series(start: str, values: list[float]) -> list[tuple[str, float]]:
    day = dt.date.fromisoformat(start)
    return [((day + dt.timedelta(days=i)).isoformat(), v) for i, v in enumerate(values)]


def test_a_signal_is_measured_from_the_first_close_to_each_horizon_it_reached():
    closes = series("2026-09-01", [100.0] * 7 + [110.0] * 21 + [121.0] * 10)        # +10 % a week on, +21 % four
    bench = series("2026-09-01", [50.0] * 7 + [51.0] * 31)
    out = sr.outcome("AAA", "2026-09-01", closes, bench)
    assert out.returns == {"1w": pytest.approx(0.10), "4w": pytest.approx(0.21)}        # 12 weeks: not yet
    assert out.excess == {"1w": pytest.approx(0.08), "4w": pytest.approx(0.19)}
    assert out.now == pytest.approx(0.21)


def test_a_signal_sent_on_a_day_with_no_bar_starts_at_the_next_and_no_history_is_no_outcome():
    closes = series("2026-09-03", [100.0, 105.0])
    assert sr.outcome("AAA", "2026-09-01", closes, []).now == pytest.approx(0.05)
    assert sr.outcome("AAA", "2026-09-01", closes, []).returns == {}
    assert sr.outcome("AAA", "2026-09-10", closes, []) is None and sr.outcome("AAA", "2026-09-01", [], []) is None


def _signal(conn, ticker, sent, source="SEC"):
    signals_weekly.record_signal(conn, {"ticker": ticker, "source": source, "kind": "stock", "score": 70.0},
                                 dt.date.fromisoformat(sent))


def test_stocks_are_set_against_the_sp500_and_coins_against_bitcoin(conn):
    _signal(conn, "AAA", "2026-09-01")
    _signal(conn, "CRYPTO:SOL", "2026-09-01", "CRYPTO")
    _signal(conn, "GONE", "2026-09-01")
    table = {"AAA": series("2026-09-01", [100.0] * 7 + [110.0] * 5), "SPY": series("2026-09-01", [10.0] * 12),
             "CRYPTO:SOL": series("2026-09-01", [100.0] * 7 + [90.0] * 5),
             "CRYPTO:BTC": series("2026-09-01", [100.0] * 7 + [95.0] * 5)}
    asked = []

    def closes(ticker, source):
        asked.append(ticker)
        return table.get(ticker, [])
    got = {o.ticker: o for o in sr.outcomes(conn, closes_fn=closes)}
    assert set(got) == {"AAA", "CRYPTO:SOL"}                                          # GONE has no prices
    assert got["AAA"].excess["1w"] == pytest.approx(0.10) and got["CRYPTO:SOL"].excess["1w"] == pytest.approx(-0.05)
    assert asked.count("SPY") == 1 and asked.count("CRYPTO:BTC") == 1                 # each series fetched once


def test_the_record_is_a_bare_table():
    outs = [sr.Outcome("AAA", "2026-09-01", {"1w": 0.10, "4w": 0.20}, {"1w": 0.08, "4w": 0.15}, 0.22),
            sr.Outcome("CRYPTO:ENA", "2026-09-08", {"1w": -0.04}, {"1w": -0.02}, -0.031)]
    assert tn.format_signal_record(outs, html=False).splitlines() == [
        "Signals 2 since 01.09",
        "            1w     4w    12w",
        "Avg      +3.0% +20.0%      -",
        "Win        50%   100%      -",
        "vs mkt   +3.0% +15.0%      -",
        "N            2      1      0",
        "Best  AAA +22.0%",
        "Worst ENA -3.1%"]
    assert tn.format_signal_record([], html=False) == "Signals: none yet"


def test_the_record_goes_out_once_a_month_and_only_with_signals(conn):
    sent = []
    send = lambda t: sent.append(t) or True                                           # noqa: E731
    closes = lambda t, s: series("2026-09-01", [100.0] * 40)                          # noqa: E731
    day = dt.date(2026, 10, 1)
    assert sr.monthly(conn, today=day, send=send, closes_fn=closes) is False          # nothing to show
    _signal(conn, "AAA", "2026-09-01")
    assert sr.monthly(conn, today=day, send=lambda t: False, closes_fn=closes) is False
    assert sr.monthly(conn, today=day, send=send, closes_fn=closes) is True
    assert sr.monthly(conn, today=day + dt.timedelta(days=5), send=send, closes_fn=closes) is False
    assert sr.monthly(conn, today=dt.date(2026, 11, 1), send=send, closes_fn=closes) is True
    assert len(sent) == 2 and sent[0].startswith("<pre>Signals 1 since 01.09")


def test_signals_in_telegram(conn, monkeypatch):
    sent = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    monkeypatch.setattr(tb.signal_record, "report", lambda c: "<pre>Signals: none yet</pre>")
    tb._handle_message(conn, "/signals")
    assert sent == ["<pre>Signals: none yet</pre>"]


def test_the_weekly_budget_in_the_signals_against_the_same_euros_in_the_index():
    outs = [sr.Outcome("AAA", "2026-09-04", {}, {}, 0.10, 0.02), sr.Outcome("BBB", "2026-09-04", {}, {}, -0.20, 0.02),
            sr.Outcome("CCC", "2026-09-11", {}, {}, 0.05, -0.01),
            sr.Outcome("DDD", "2026-09-18", {}, {}, 0.50, None)]              # no index figure: the week is left out
    put, signals, index = sr.against_the_index(outs, 30.0)
    assert put == 60.0
    assert signals == pytest.approx(15 * 1.10 + 15 * 0.80 + 30 * 1.05)
    assert index == pytest.approx(30 * 1.02 + 30 * 0.99)
    assert sr.against_the_index(outs, 0.0) is None and sr.against_the_index([], 30.0) is None
    text = tn.format_signal_record(outs, html=False, money=(put, signals, index))
    assert text.splitlines()[-3:] == ["Put in   €60", "Signals  €60", "S&P 500  €60"]


def test_every_outcome_carries_the_index_over_its_own_days(conn):
    _signal(conn, "AAA", "2026-09-01")
    table = {"AAA": series("2026-09-01", [100.0, 110.0]), "SPY": series("2026-09-01", [50.0, 51.0])}
    [o] = sr.outcomes(conn, closes_fn=lambda t, s: table.get(t, []))
    assert o.index_now == pytest.approx(0.02) and o.now == pytest.approx(0.10)

