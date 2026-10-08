"""cfd/live.py, the daily pass (spec 2026-10-08-cfd-live-crypto-breakout.md, "Daily pass"): track first, then
scan unless paused; the messages go out through telegram_notify.send_text, and a row is kept only after its
message did. Two days on one coin, the data stubbed."""
from __future__ import annotations

import datetime as dt

import pytest

import telegram_notify
from cfd import live
from cfd.data import Bar

UTC = dt.timezone.utc
DAY = dt.timedelta(days=1)
D1 = dt.date(2026, 10, 8)                       # day 1: the signal bar is 2026-10-07, the forming bar this day


def calm(n, end):
    first = dt.datetime.combine(end, dt.time(), tzinfo=UTC) - (n - 1) * DAY
    return [Bar(first + i * DAY, 100.0, 101.0, 99.0, 100.0) for i in range(n)]


def sol_series(extra=()):
    """Calm bars, the breakout bar on 2026-10-07 (close 110), then the `extra` rows as the next days' bars."""
    bars = calm(330, D1 - DAY)
    bars[-1] = Bar(bars[-1].ts, 100.0, 111.0, 99.0, 110.0)
    for j, (o, h, low, c) in enumerate(extra):
        bars.append(Bar(bars[-1].ts + DAY, o, h, low, c))
    return bars


def rows(bars, forming_day, forming_open):
    out = [(b.ts.date().isoformat(), b.open, b.high, b.low, b.close) for b in bars]
    out.append((forming_day.isoformat(), forming_open, forming_open + 0.5, forming_open - 0.5, forming_open))
    return out


def fetch_for(bars, forming_day, forming_open):
    calm_rows = rows(calm(330, forming_day - DAY), forming_day, 100.0)

    def fetch(symbol, interval):
        return rows(bars, forming_day, forming_open) if symbol == "SOL-USD" else calm_rows
    return fetch


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: out.append(msg) or True)
    return out


def test_the_first_run_makes_the_signal_and_sends_the_entry_message(conn, sent):
    live.set_setting(conn, "balance", 500.0)
    result = live.run(conn, fetch=fetch_for(sol_series(), D1, 110.5), today=D1)
    assert (result.tracked, result.made) == (0, 1)
    (sig,) = live.open_signals(conn)
    assert sent == [telegram_notify.format_cfd_notice(live.Notice("entry", sig, risk_pct=1.0))]
    assert sent[0].startswith("🟢 <b>SOLUSD!</b>: покупка по 110,50 — стоп ") and sent[0].endswith("; эксперимент")


def test_the_next_day_tracks_the_checkpoint_and_the_same_run_scans_too(conn, sent):
    live.run(conn, fetch=fetch_for(sol_series(), D1, 110.5), today=D1)
    (sig,) = live.open_signals(conn)
    tp1 = sig.entry + sig.r
    day2 = sol_series([(110.5, tp1 + 0.5, 109.0, tp1 - 1.0)])                   # the bar of D1, now complete
    sent.clear()
    result = live.run(conn, fetch=fetch_for(day2, D1 + DAY, tp1 - 1.0), today=D1 + DAY)
    assert (result.tracked, result.made) == (1, 0)
    assert len(sent) == 1 and sent[0].startswith("🟢 <b>SOLUSD!</b>: достигнут TP1 ")
    assert live.get_signal(conn, sig.id).checkpoint == 1
    sent.clear()
    live.run(conn, fetch=fetch_for(day2, D1 + DAY, tp1 - 1.0), today=D1 + DAY)     # the same day again
    assert sent == []


def test_a_closed_signal_is_told_and_its_coin_is_free_again(conn, sent):
    live.run(conn, fetch=fetch_for(sol_series(), D1, 110.5), today=D1)
    (sig,) = live.open_signals(conn)
    crash = sol_series([(110.5, 111.0, sig.stop0 - 5, sig.stop0 - 4)])
    sent.clear()
    live.run(conn, fetch=fetch_for(crash, D1 + DAY, 100.0), today=D1 + DAY)
    assert len(sent) == 1 and "сработал стоп — закрыто по " in sent[0]
    assert live.open_signals(conn) == [] and live.closed_signals(conn)[0].exit_price == pytest.approx(sig.stop0)


def test_paused_makes_no_new_signals_but_tracking_goes_on(conn, sent):
    live.run(conn, fetch=fetch_for(sol_series(), D1, 110.5), today=D1)
    (sig,) = live.open_signals(conn)
    live.set_setting(conn, "paused", True)
    day2 = sol_series([(110.5, sig.entry + sig.r + 0.5, 109.0, sig.entry)])
    sent.clear()
    xrp_breakout = sol_series()                                                  # another coin breaks out on D1
    base = fetch_for(day2, D1 + DAY, 110.0)

    def fetch(symbol, interval):
        if symbol == "XRP-USD":
            return rows(xrp_breakout[:-1] + [Bar(xrp_breakout[-1].ts + DAY, 110.0, 111.0, 109.0, 110.0)],
                        D1 + DAY, 110.5)
        return base(symbol, interval)
    result = live.run(conn, fetch=fetch, today=D1 + DAY)
    assert result.tracked == 1 and result.made == 0 and result.paused is True
    assert [s.coin for s in live.open_signals(conn)] == ["SOL"] and len(sent) == 1


def test_the_new_signals_come_back_with_on(conn, sent):
    live.set_setting(conn, "paused", True)
    live.run(conn, fetch=fetch_for(sol_series(), D1, 110.5), today=D1)
    assert live.all_signals(conn) == [] and sent == []
    live.set_setting(conn, "paused", False)
    live.run(conn, fetch=fetch_for(sol_series(), D1, 110.5), today=D1)
    assert len(live.open_signals(conn)) == 1


def test_a_refused_message_keeps_nothing_and_the_rerun_sends_it(conn, monkeypatch):
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: False)
    live.run(conn, fetch=fetch_for(sol_series(), D1, 110.5), today=D1)
    assert live.all_signals(conn) == []
    out = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: out.append(msg) or True)
    live.run(conn, fetch=fetch_for(sol_series(), D1, 110.5), today=D1)
    assert len(out) == 1 and len(live.open_signals(conn)) == 1


def test_a_send_that_raises_costs_that_coin_only(conn, monkeypatch, capsys):
    def send_text(msg):
        if "SOLUSD" in msg:
            raise RuntimeError("telegram")
        return True
    monkeypatch.setattr("telegram_notify.send_text", send_text)
    live.run(conn, fetch=fetch_for(sol_series(), D1, 110.5), today=D1)
    assert live.all_signals(conn) == [] and "RuntimeError" in capsys.readouterr().err
