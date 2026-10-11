"""The day league (cfd/intraday.py, docs/cfd/DAY_LEAGUE.md): the three ideas, a trade's life on 15-minute
bars, the pass and its hours, the scoreboard and /day. No network: the bars are given."""
from __future__ import annotations

import datetime as dt

import pytest

import telegram_bot as tb
import telegram_notify as tn
from cfd import intraday as day
from cfd.data import Bar

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 12, 0, 0, tzinfo=UTC)          # Monday 03:00 in Riga; 09:00 there is bar 24
FLAT = (1.0, 1.001, 0.999, 1.0)


def ts(i: int) -> dt.datetime:
    return T0 + i * day.BAR


def rows(n: int, special=None, start: int = 0):
    """Raw 15-minute rows, bars `start`..n-1, flat between 0.999 and 1.001; `special` is {i: (o, h, l, c)}."""
    special = special or {}
    return [(ts(i).isoformat(), *special.get(i, FLAT)) for i in range(start, n)]


def bars(n: int, special=None, start: int = 0) -> list[Bar]:
    return [Bar(dt.datetime.fromisoformat(r[0]), *r[1:]) for r in rows(n, special, start)]


def fetch_of(table):
    return lambda symbol, interval: table.get(symbol, [])


def after(i: int) -> dt.datetime:
    """Twenty seconds into bar i: bar i-1 is the last completed one, bar i is forming."""
    return ts(i) + dt.timedelta(seconds=20)


BREAK = {33: (1.0, 1.002, 1.0, 1.0015)}                   # London's hour is bars 28..31; bar 33 closes above it


# ------------------------------------------------------------------ the ideas
def test_the_first_close_outside_londons_first_hour_is_an_open_range_signal():
    [s] = day.open_range("EURUSD", bars(34, BREAK))
    assert (s.idea, s.side, s.ref, s.stop_price, s.target_r) == ("OPEN-RANGE", "long", "2026-10-12", 1.0, 2.0)
    assert day.open_range("EURUSD", bars(33)) == []                                   # still inside
    assert day.open_range("EURUSD", bars(35, {**BREAK, 34: (1.0015, 1.003, 1.001, 1.002)})) == []    # said once
    [down] = day.open_range("EURUSD", bars(34, {33: (1.0, 1.0, 0.998, 0.9985)}))
    assert down.side == "short"
    assert day.open_range("EURUSD", bars(31)) == []                                   # the hour is not over


def test_a_false_break_of_the_asia_range_is_a_sweep_to_its_other_side():
    sweep = {24: (1.0, 1.0, 0.9985, 0.9988), 25: (0.9988, 0.9997, 0.9986, 0.9995)}
    [s] = day.asia_sweep("EURUSD", bars(26, sweep))
    assert (s.idea, s.side, s.ref) == ("ASIA-SWEEP", "long", "2026-10-12-long")
    assert (s.stop_price, s.target_price) == (0.9985, 1.001)
    assert day.asia_sweep("EURUSD", bars(25, sweep)) == []                            # still outside
    assert day.asia_sweep("EURUSD", bars(27, sweep)) == []                            # said once
    [up] = day.asia_sweep("EURUSD", bars(25, {24: (1.0, 1.002, 1.0, 1.0005)}))
    assert (up.side, up.stop_price, up.target_price) == ("short", 1.002, 0.999)
    assert day.asia_sweep("EURUSD", bars(26, sweep, start=20)) == []                  # too little of the range


def test_the_retest_idea_is_the_leagues_on_these_bars():
    pattern = {40: (1.0, 1.003, 1.0, 1.0025), 41: (1.0025, 1.003, 1.0008, 1.0012), 42: (1.0012, 1.003, 1.0011, 1.0028)}
    [s] = day.brk_15("GBPUSD", bars(43, pattern))
    assert (s.idea, s.side, s.ref, s.stop_price, s.target_r) == ("BRK-15", "long", "2026-10-12", 1.0, 2.0)
    assert day.brk_15("GBPUSD", bars(42, pattern)) == []


# ------------------------------------------------------------------ the pass
def _pass(conn, n, special, sent, symbol="EURUSD=X"):
    return day.run(conn, now=after(n - 1), fetch=fetch_of({symbol: rows(n, special)}),
                   send=lambda text: sent.append(text) or True)


QUIET_34 = {**BREAK, 34: (1.0015, 1.0025, 1.0012, 1.0018)}


def test_a_signal_is_filled_at_the_next_open_and_leaves_at_its_target(conn):
    sent = []
    assert _pass(conn, 35, {**BREAK, 34: (1.0015, 1.0015, 1.0015, 1.0015)}, sent) == (1, 0)
    [t] = day.trades(conn)
    assert (t.status, t.entry, t.stop_price, t.target) == ("open", 1.0015, 1.0, pytest.approx(1.0045))
    assert sent == ["<pre>EURUSD Long · CFD day\nEntry 1.00150\nStop  1.00000\nTP    1.00450</pre>"]
    assert _pass(conn, 35, {**BREAK, 34: (1.0015, 1.0015, 1.0015, 1.0015)}, sent) == (0, 0)     # the same bar again
    assert _pass(conn, 36, {**BREAK, 34: (1.0015, 1.005, 1.0012, 1.004)}, sent) == (0, 1)
    [t] = day.trades(conn)
    assert (t.reason, t.exit_price) == ("target", pytest.approx(1.0045))
    assert t.result_r == pytest.approx(2.0 - 0.015 / 100 * 1.0015 / 0.0015)
    assert sent[-1] == "<pre>EURUSD target 1.00450 +1.9R</pre>"
    assert _pass(conn, 37, {**QUIET_34}, sent) == (0, 0)
    assert len(day.trades(conn, "WHERE idea = 'OPEN-RANGE'")) == 1                        # one a day


def test_inside_one_bar_the_stop_comes_first_and_a_gap_leaves_at_the_open(conn):
    sent = []
    _pass(conn, 35, QUIET_34, sent)
    _pass(conn, 36, {**BREAK, 34: (1.0015, 1.005, 0.9995, 1.004)}, sent)
    [t] = day.trades(conn)
    assert (t.reason, t.exit_price) == ("stop", 1.0) and sent[-1] == "<pre>EURUSD stop 1.00000 -1.1R</pre>"


def test_every_trade_is_out_at_six_in_the_evening(conn):
    sent = []
    _pass(conn, 35, QUIET_34, sent)
    calm = {**QUIET_34, **{i: (1.002, 1.0025, 1.0015, 1.002) for i in range(35, 61)}}
    assert _pass(conn, 60, calm, sent) == (0, 0)                               # 17:45: still open
    assert _pass(conn, 61, calm, sent) == (0, 1)                               # bar 60 opens at 18:00 in Riga
    [t] = day.trades(conn)
    assert (t.reason, t.exit_price) == ("time", 1.002) and sent[-2] == "<pre>EURUSD closed 1.00200 +0.2R</pre>"
    assert sent[-1].startswith("<pre>Day league 12.10 12.10-08.11\n")           # the day's board, once
    assert _pass(conn, 62, calm, sent) == (0, 0) and len(sent) == 3


def test_no_signal_after_five_and_nothing_on_a_weekend_or_from_old_bars(conn):
    late = {57: (1.0, 1.002, 1.0, 1.0015)}                                     # closes at 17:30 in Riga
    late_break = {**{i: FLAT for i in range(32, 57)}, **late}
    assert _pass(conn, 59, late_break, []) == (0, 0) and day.trades(conn) == []
    saturday = after(34) + dt.timedelta(days=5)
    assert day.run(conn, now=saturday, fetch=fetch_of({"EURUSD=X": rows(35, BREAK)}), send=lambda t: True) == (0, 0)
    stale = after(34) + dt.timedelta(hours=1)                                  # the feed is an hour behind
    assert day.run(conn, now=stale, fetch=fetch_of({"EURUSD=X": rows(34, BREAK)}), send=lambda t: True) == (0, 0)
    assert day.trades(conn) == []


def _made(conn, pair, stop, entry, **kw):
    sig = day.Signal("BRK-15", pair, "long", "r", stop, **({"target_r": 2.0} if not kw else kw))
    t = day.make(conn, sig, Bar(ts(30), entry, entry, entry, entry), kw.pop("atr", 0.002) if False else 0.002 * entry)
    return t, day.advance(t, [], Bar(ts(31), entry, entry, entry, entry), after(31))


def test_a_trade_that_costs_too_much_or_has_no_room_is_not_made(conn):
    costly, events = _made(conn, "BTCUSD", 79900.0, 80000.0)                   # 0.25 % of 80000 is 2R of a 100 stop
    assert events == [] and costly.reason == "void" and day.closed_trades(conn) == []
    wrong, events = _made(conn, "EURUSD", 1.002, 1.0)                          # the open is under a long's stop
    assert events == [] and wrong.reason == "void"
    far, events = _made(conn, "GBPUSD", 0.98, 1.0)                             # 10 ATR away
    assert events == [] and far.reason == "void"
    near, events = _made(conn, "USDJPY", 149.8, 150.0, target_price=150.1)     # the target nearer than the stop
    assert events == [] and near.reason == "void"
    fine, events = _made(conn, "XAUUSD", 3990.0, 4000.0, target_price=4015.0)
    assert events == ["open"] and fine.target == 4015.0
    assert tn.format_day_opens([fine], html=False) == "XAUUSD Long · CFD day\nEntry 4000.00\nStop  3990.00\nTP    4015.00"


# ------------------------------------------------------------------ the scoreboard and /day
def test_the_board_and_the_command(conn):
    assert day.handle_command(conn, "/day").startswith("Дневная лига начнётся")
    sent = []
    _pass(conn, 35, QUIET_34, sent)
    _pass(conn, 36, {**BREAK, 34: (1.0015, 1.005, 1.0012, 1.004)}, sent)
    board = day.scoreboard(conn, dt.date(2026, 10, 12))
    assert tn.format_day_board(board, html=False).splitlines() == [
        "Day league 12.10-08.11",
        "               R  Cl   W Op",
        "Open range +1.90   1   1  0",
        "Retest     +0.00   0   0  0",
        "Asia sweep +0.00   0   0  0",
        "Total      +1.90   1   1  0"]
    final = tn.format_day_board(board, final=True, html=False).splitlines()
    assert final[0] == "Day league final 12.10-08.11" and final[2].endswith(" fail")        # one trade is not twenty
    assert "не присылаю" in day.handle_command(conn, "/day off") and day.quiet(conn)
    assert "присылаю каждую" in day.handle_command(conn, "/day on") and not day.quiet(conn)
    assert day.handle_command(conn, "/day x").startswith("Команды")
    told = []
    import telegram_notify
    orig, telegram_notify.send_text = telegram_notify.send_text, lambda text, **k: told.append(text) or True
    try:
        assert tb._handle_day_command(conn, "/day") and not tb._handle_day_command(conn, "/league")
    finally:
        telegram_notify.send_text = orig
    assert told and told[0].startswith("<pre>Day league 12.10-08.11")
