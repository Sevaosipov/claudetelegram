"""The forex paper league (cfd/league.py, docs/cfd/PAPER_LEAGUE.md): a paper trade's life, the four ideas,
the daily pass, the scoreboard and /league. No network: prices, reports and yields are given."""
from __future__ import annotations

import datetime as dt

import pytest

import telegram_bot as tb
import telegram_notify as tn
from cfd import league
from cfd import research4 as r4
from cfd.data import Bar

UTC = dt.timezone.utc
MON = dt.date(2026, 10, 12)


def weekdays(start: dt.date, n: int) -> list[dt.date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def rows(days, price=1.0, wiggle=0.01, special=None):
    """Raw daily rows, flat at `price`; `special` is {date: (o, h, l, c)}."""
    special = special or {}
    return [(d.isoformat(), *special.get(d, (price, price + wiggle, price - wiggle, price))) for d in days]


def fetch_of(table):
    return lambda symbol, interval: table.get(symbol, [])


HISTORY = weekdays(dt.date(2026, 8, 3), 50)              # ... to Fri 2026-10-09


def eurusd(extra=None, upto=MON, today_open=1.0):
    days = [d for d in HISTORY if d < upto]
    more = weekdays(dt.date(2026, 10, 12), 30)
    days += [d for d in more if d < upto]
    out = rows(days, special=extra)
    return out + [(upto.isoformat(), today_open, today_open, today_open, today_open)]


def signal(**kw):
    base = dict(idea="RATE-MOM", pair="EURUSD", side="long", ref="2026-W42", stop_atr=2.0, exit_after="bars:5")
    base.update(kw)
    return league.Signal(**base)


# ------------------------------------------------------------------ a trade's life
def test_a_signal_becomes_a_trade_filled_at_todays_open_with_its_stop(conn):
    feed = league.Feed(MON, fetch_of({"EURUSD=X": eurusd()}))
    t = league.make(conn, signal(), feed)
    assert (t.status, t.signal_date) == ("pending", "2026-10-09") and t.atr == pytest.approx(0.02)
    assert league.advance(t, feed.bars(t.symbol), feed.forming(t.symbol), MON) == ["open"]
    assert (t.status, t.entry_date, t.entry) == ("open", "2026-10-12", 1.0) and t.stop == pytest.approx(0.96)


def test_one_trade_per_idea_and_pair_and_a_signal_only_once(conn):
    feed = league.Feed(MON, fetch_of({"EURUSD=X": eurusd()}))
    first = league.make(conn, signal(), feed)
    assert league.make(conn, signal(ref="2026-W43"), feed) is None               # still not closed
    assert league.make(conn, signal(idea="CMD-LEAD", ref="x"), feed) is not None  # another idea may
    conn.execute("UPDATE league_trades SET status = 'closed' WHERE id = ?", (first.id,))
    assert league.make(conn, signal(), feed) is None                              # the same ref again
    assert league.make(conn, signal(ref="2026-W43"), feed) is not None


def test_no_trade_without_an_atr(conn):
    feed = league.Feed(MON, fetch_of({"EURUSD=X": eurusd()[-5:]}))
    assert league.make(conn, signal(), feed) is None


def _opened(conn, **kw):
    feed = league.Feed(MON, fetch_of({"EURUSD=X": eurusd()}))
    t = league.make(conn, signal(**kw), feed)
    league.advance(t, feed.bars(t.symbol), feed.forming(t.symbol), MON)
    return t


def test_the_time_exit_is_the_open_of_the_nth_bar_after_the_entry_bar(conn):
    t = _opened(conn)
    day = dt.date(2026, 10, 19)                                   # Mon 12 is the entry bar; the 5th after is Mon 19
    feed = league.Feed(day, fetch_of({"EURUSD=X": eurusd(upto=day, today_open=1.02)}))
    assert league.advance(t, feed.bars(t.symbol), feed.forming(t.symbol), day) == ["close"]
    assert (t.reason, t.exit_date, t.exit_price) == ("time", "2026-10-19", 1.02)
    assert t.result_r == pytest.approx(0.02 / 0.04 - (0.015 + 7 * 0.010) / 100 * 1.0 / 0.04)


def test_the_stop_inside_a_bar_and_a_gap_through_it(conn):
    t = _opened(conn)
    wed = dt.date(2026, 10, 14)
    hit = {dt.date(2026, 10, 13): (1.0, 1.01, 0.95, 0.97)}
    feed = league.Feed(wed, fetch_of({"EURUSD=X": eurusd(extra=hit, upto=wed)}))
    assert league.advance(t, feed.bars(t.symbol), feed.forming(t.symbol), wed) == ["close"]
    assert (t.reason, t.exit_price, t.exit_date) == ("stop", pytest.approx(0.96), "2026-10-13")
    assert t.result_r == pytest.approx(-1 - (0.015 + 1 * 0.010) / 100 * 1.0 / 0.04)

    u = _opened(conn, idea="CMD-LEAD", ref="g")
    tue = dt.date(2026, 10, 13)
    feed = league.Feed(tue, fetch_of({"EURUSD=X": eurusd(upto=tue, today_open=0.94)}))
    league.advance(u, feed.bars(u.symbol), feed.forming(u.symbol), tue)
    assert (u.reason, u.exit_price) == ("stop", 0.94)


def test_a_short_mirrors_and_a_date_exit_waits_for_the_date(conn):
    t = _opened(conn, side="short", exit_after="date:2026-11-01", idea="MONTH-END", ref="2026-10")
    assert t.stop == pytest.approx(1.04)
    day = dt.date(2026, 10, 30)
    feed = league.Feed(day, fetch_of({"EURUSD=X": eurusd(upto=day)}))
    assert league.advance(t, feed.bars(t.symbol), feed.forming(t.symbol), day) == [] and t.status == "open"
    day = dt.date(2026, 11, 2)
    feed = league.Feed(day, fetch_of({"EURUSD=X": eurusd(upto=day, today_open=0.98)}))
    assert league.advance(t, feed.bars(t.symbol), feed.forming(t.symbol), day) == ["close"]
    assert t.reason == "time" and t.result_r > 0


def test_take_profit_levels_are_marks_the_trade_goes_on(conn):
    t = _opened(conn, exit_after="bars:10")
    assert [league.level(t, k) for k in (1, 2)] == [pytest.approx(1.04), pytest.approx(1.08)]
    day = dt.date(2026, 10, 15)
    up = {dt.date(2026, 10, 13): (1.0, 1.05, 0.99, 1.04), dt.date(2026, 10, 14): (1.04, 1.13, 1.03, 1.12)}
    feed = league.Feed(day, fetch_of({"EURUSD=X": eurusd(extra=up, upto=day, today_open=1.12)}))
    assert league.advance(t, feed.bars(t.symbol), feed.forming(t.symbol), day) == ["tp:1", "tp:2", "tp:3"]
    assert (t.status, t.checkpoint) == ("open", 3)
    assert league.advance(t, feed.bars(t.symbol), feed.forming(t.symbol), day) == []          # said once
    text = tn.format_league_event(t, "tp:2", html=False)
    assert text == "EURUSD TP2 1.08000"
    assert tn.format_league_event(t, "open", html=False) == (
        "EURUSD Long · CFD test\nEntry 1.00000\nStop  0.96000\nTP1   1.04000\nTP2   1.08000\nTP3   1.12000\nTP4   1.16000")


def test_in_the_bar_of_the_stop_a_mark_counts_only_by_the_open(conn):
    t = _opened(conn, exit_after="bars:10")
    day = dt.date(2026, 10, 14)
    both = {dt.date(2026, 10, 13): (1.0, 1.05, 0.95, 0.97)}                 # up to TP1 and down through the stop
    feed = league.Feed(day, fetch_of({"EURUSD=X": eurusd(extra=both, upto=day)}))
    assert league.advance(t, feed.bars(t.symbol), feed.forming(t.symbol), day) == ["close"]
    assert (t.reason, t.checkpoint) == ("stop", 0)


def test_the_result_is_the_exit_whatever_marks_were_reached(conn):
    t = _opened(conn)                                                        # bars:5
    day = dt.date(2026, 10, 19)
    up = {dt.date(2026, 10, 13): (1.0, 1.05, 0.99, 1.0)}
    feed = league.Feed(day, fetch_of({"EURUSD=X": eurusd(extra=up, upto=day, today_open=1.0)}))
    assert league.advance(t, feed.bars(t.symbol), feed.forming(t.symbol), day) == ["tp:1", "close"]
    assert t.reason == "time" and t.result_r == pytest.approx(-(0.015 + 7 * 0.010) / 100 * 1.0 / 0.04)


# ---- the second version: a quarter closes at each level, the stop steps up
def test_the_second_version_takes_quarters_and_stops_the_rest_at_the_stepped_stop(conn):
    t = _opened(conn, exit_after="bars:10")                  # entry 1.00, stop 0.96: TP1 1.04, TP2 1.08
    day = dt.date(2026, 10, 16)
    path = {dt.date(2026, 10, 13): (1.01, 1.05, 1.005, 1.04),        # TP1: a quarter, the stop to 1.00
            dt.date(2026, 10, 14): (1.05, 1.09, 1.045, 1.08),        # TP2: a quarter, the stop to 1.04
            dt.date(2026, 10, 15): (1.08, 1.085, 1.03, 1.05)}        # back through 1.04: the half left closes
    feed = league.Feed(day, fetch_of({"EURUSD=X": eurusd(extra=path, upto=day, today_open=1.05)}))
    league.advance(t, feed.bars(t.symbol), feed.forming(t.symbol), day)
    assert t.status == "open" and (t.stage2, t.remaining2, t.exit2_date) == (2, 0.0, "2026-10-15")
    gross = 0.25 * 1 + 0.25 * 2 + 0.5 * 1
    assert t.result2_r == pytest.approx(gross - (0.015 + 3 * 0.010) / 100 * 1.0 / 0.04)


def test_the_second_version_leaves_with_the_time_exit_and_loses_one_r_at_the_first_stop(conn):
    t = _opened(conn)                                         # bars:5
    day = dt.date(2026, 10, 19)
    up = {dt.date(2026, 10, 13): (1.01, 1.05, 1.005, 1.04)}
    flat = {d: (1.04, 1.045, 1.035, 1.04) for d in weekdays(dt.date(2026, 10, 14), 3)}
    feed = league.Feed(day, fetch_of({"EURUSD=X": eurusd(extra={**up, **flat}, upto=day, today_open=1.04)}))
    league.advance(t, feed.bars(t.symbol), feed.forming(t.symbol), day)
    cost = (0.015 + 7 * 0.010) / 100 * 1.0 / 0.04
    assert t.reason == "time" and t.result_r == pytest.approx(1.0 - cost)
    assert (t.stage2, t.exit2_date) == (1, "2026-10-19") and t.result2_r == pytest.approx(0.25 + 0.75 * 1.0 - cost)

    u = _opened(conn, idea="CMD-LEAD", ref="s")
    wed = dt.date(2026, 10, 14)
    feed = league.Feed(wed, fetch_of({"EURUSD=X": eurusd(extra={dt.date(2026, 10, 13): (1.0, 1.01, 0.95, 0.97)}, upto=wed)}))
    league.advance(u, feed.bars(u.symbol), feed.forming(u.symbol), wed)
    assert u.result2_r == pytest.approx(u.result_r) and u.result_r < -1


def test_the_close_message_gives_both_results(conn):
    t = _opened(conn)
    day = dt.date(2026, 10, 19)
    up = {dt.date(2026, 10, 13): (1.0, 1.05, 1.0, 1.0)}
    feed = league.Feed(day, fetch_of({"EURUSD=X": eurusd(extra=up, upto=day, today_open=1.0)}))
    league.advance(t, feed.bars(t.symbol), feed.forming(t.symbol), day)
    # the bar that reached TP1 also traded at the entry: the second version's rest stopped there, a day in
    assert tn.format_league_event(t, "close", html=False) == "EURUSD closed 1.00000 -0.02R (staged +0.24R)"


def test_a_bar_is_read_once(conn):
    t = _opened(conn)
    day = dt.date(2026, 10, 14)
    feed = league.Feed(day, fetch_of({"EURUSD=X": eurusd(upto=day)}))
    league.advance(t, feed.bars(t.symbol), feed.forming(t.symbol), day)
    assert t.last_bar == "2026-10-13"
    assert league.advance(t, feed.bars(t.symbol), feed.forming(t.symbol), day) == []


# ------------------------------------------------------------------ the ideas
def _reports(nets, last=dt.date(2026, 10, 6)):
    n = len(nets)
    return [(last - dt.timedelta(weeks=n - 1 - k), v) for k, v in enumerate(nets)]


def test_cot_with_goes_with_the_speculators_at_an_extreme(monkeypatch):
    monkeypatch.setattr(r4, "COT_WINDOW", 3)
    reports = {"099741": _reports([0.1, 0.2, 0.3]),          # EUR at its high: long EURUSD
               "097741": _reports([0.1, 0.2, 0.3]),          # JPY at its high: long JPY = short USDJPY
               "096742": _reports([0.3, 0.2, 0.1]),          # GBP at its low: short GBPUSD
               "232741": _reports([0.1, 0.3, 0.2])}          # AUD in the middle: nothing
    got = {s.pair: (s.side, s.ref, s.exit_after, s.stop_atr) for s in league.cot_with(reports, MON)}
    assert got == {"EURUSD": ("long", "2026-10-06", "bars:10", 2.0), "USDJPY": ("short", "2026-10-06", "bars:10", 2.0),
                   "GBPUSD": ("short", "2026-10-06", "bars:10", 2.0)}


def test_cot_with_waits_for_the_friday_of_the_report_to_pass(monkeypatch):
    monkeypatch.setattr(r4, "COT_WINDOW", 3)
    reports = {"099741": _reports([0.3, 0.2, 0.1, 0.5])}            # the newest (Oct 6) is the extreme
    assert league.cot_with(reports, dt.date(2026, 10, 9)) != []     # ... but on its Friday the bot reads Sep 29's
    assert league.cot_with(reports, dt.date(2026, 10, 9))[0].ref == "2026-09-29"
    assert league.cot_with(reports, dt.date(2026, 10, 10))[0].ref == "2026-10-06"


def _yields(change):
    days = weekdays(dt.date(2026, 9, 14), 20)
    us = {d: 4.0 for d in days}
    other = {d: 3.0 for d in days}
    other[days[-1]] = 3.0 + change
    return {"US": us, "EA": other, "CA": dict(other), "JP": {d: 2.0 for d in days}}


def test_rate_mom_follows_the_spread_and_is_one_signal_a_week():
    got = {s.pair: (s.side, s.ref, s.exit_after) for s in league.rate_mom(_yields(+0.12), MON)}
    assert got == {"EURUSD": ("long", "2026-W42", "bars:5"), "USDCAD": ("short", "2026-W42", "bars:5")}
    assert {s.pair: s.side for s in league.rate_mom(_yields(-0.12), MON)} == {"EURUSD": "short", "USDCAD": "long"}
    assert league.rate_mom(_yields(+0.05), MON) == []


def test_rate_mom_needs_fresh_yields_on_both_sides():
    y = _yields(+0.12)
    assert league.rate_mom(y, MON + dt.timedelta(days=30)) == []          # stale
    assert league.rate_mom({"EA": y["EA"]}, MON) == []                     # no US


def _commodity(last_move):
    days = weekdays(dt.date(2026, 6, 1), 95)
    days = [d for d in days if d < MON]
    values = [100.0 + (1 if i % 2 else -1) * 0.5 for i in range(len(days))]
    values[-1] = values[-4] * (1 + last_move)
    return list(zip(days, values))


def test_cmd_lead_trades_the_currency_the_way_of_a_sharp_move():
    closes = {"CL=F": _commodity(+0.05), "HG=F": _commodity(-0.05), "BZ=F": _commodity(+0.001)}
    got = {s.pair: (s.side, s.exit_after, s.stop_atr) for s in league.cmd_lead(closes, MON)}
    assert got == {"USDCAD": ("short", "bars:3", 1.5), "AUDUSD": ("short", "bars:3", 1.5)}
    assert league.cmd_lead(closes, MON)[0].ref == closes["CL=F"][-1][0].isoformat()


def test_cmd_lead_ignores_an_old_bar_and_a_short_history():
    assert league.cmd_lead({"CL=F": _commodity(+0.05)}, MON + dt.timedelta(days=10)) == []
    assert league.cmd_lead({"CL=F": _commodity(+0.05)[-20:]}, MON) == []


def _spx(move):
    return [(dt.date(2026, 9, 30), 100.0), (dt.date(2026, 10, 27), 100.0 * (1 + move))]


def test_month_end_sells_the_dollar_after_a_strong_month_with_two_weekdays_left():
    wed = dt.date(2026, 10, 28)                                            # Thu 29 and Fri 30 are left
    assert league.weekdays_left(wed) == 2
    got = {s.pair: (s.side, s.ref, s.exit_after) for s in league.month_end(_spx(+0.03), wed)}
    assert got == {"EURUSD": ("long", "2026-10", "date:2026-11-01"), "GBPUSD": ("long", "2026-10", "date:2026-11-01"),
                   "AUDUSD": ("long", "2026-10", "date:2026-11-01"), "USDJPY": ("short", "2026-10", "date:2026-11-01")}
    assert {s.side for s in league.month_end(_spx(-0.03), wed) if s.pair != "USDJPY"} == {"short"}


@pytest.mark.parametrize("day, move", [(dt.date(2026, 10, 27), 0.03), (dt.date(2026, 10, 30), 0.03),
                                       (dt.date(2026, 10, 28), 0.005), (dt.date(2026, 10, 31), 0.03)])
def test_month_end_on_no_other_day_and_not_after_a_flat_month(day, move):
    assert league.month_end(_spx(move), day) == []


# ------------------------------------------------------------------ the daily pass
def _run(conn, today, sent, table, **kw):
    kw.setdefault("cot", lambda today: {})
    kw.setdefault("yields", lambda today: {})
    return league.run(conn, today=today, fetch=fetch_of(table), send=lambda text: sent.append(text) or True, **kw)


def test_the_pass_opens_follows_and_closes_and_says_each(conn):
    sent = []
    table = {"EURUSD=X": eurusd()}
    assert _run(conn, MON, sent, table, yields=lambda today: _yields(+0.12)) == (1, 0)     # USDCAD has no bars
    assert sent == ["<pre>EURUSD Long · CFD test\nEntry 1.00000\nStop  0.96000\nTP1   1.04000\nTP2   1.08000\n"
                    "TP3   1.12000\nTP4   1.16000</pre>"]
    assert _run(conn, MON, sent, table, yields=lambda today: _yields(+0.12)) == (0, 0)     # the same day again
    day = dt.date(2026, 10, 19)
    assert _run(conn, day, sent, {"EURUSD=X": eurusd(upto=day, today_open=1.02)}) == (0, 1)
    assert sent[-1] == "<pre>EURUSD closed 1.02000 +0.48R (staged +0.48R)</pre>"
    assert league.live_trades(conn) == [] and len(league.closed_trades(conn)) == 1


def test_a_muted_league_sends_no_trade_but_still_keeps_it(conn):
    sent = []
    league.handle_command(conn, "/league off")
    _run(conn, MON, sent, {"EURUSD=X": eurusd()}, yields=lambda today: _yields(+0.12))
    assert sent == [] and len(league.live_trades(conn)) == 1


def test_an_idea_whose_data_fails_does_not_stop_the_others(conn, capsys):
    sent = []

    def boom(today):
        raise RuntimeError("down")
    _run(conn, MON, sent, {"EURUSD=X": eurusd()}, cot=boom, yields=lambda today: _yields(+0.12))
    assert len(league.live_trades(conn)) == 1 and "COT-WITH failed" in capsys.readouterr().err


def test_no_new_trade_after_thirteen_weeks_and_the_final_board_once(conn):
    sent = []
    table = {"EURUSD=X": eurusd()}
    _run(conn, MON, sent, table)                                                   # the league starts
    late = MON + dt.timedelta(weeks=13)
    sent.clear()
    _run(conn, late, sent, {"EURUSD=X": eurusd(upto=late)}, yields=lambda today: _yields(+0.12))
    assert league.live_trades(conn) == [] and any("League final" in s for s in sent)
    again = []
    _run(conn, late + dt.timedelta(days=1), again, {"EURUSD=X": eurusd(upto=late + dt.timedelta(days=1))})
    assert not any("League final" in s for s in again)


def test_the_months_board_goes_out_at_the_first_run_of_the_next_month_once(conn):
    sent = []
    _run(conn, MON, sent, {})
    assert sent == []                                                              # September is before the start
    nov = dt.date(2026, 11, 2)
    _run(conn, nov, sent, {})
    assert len(sent) == 1 and "League 10.2026" in sent[0]
    _run(conn, nov + dt.timedelta(days=1), sent, {})
    assert len(sent) == 1


# ------------------------------------------------------------------ the scoreboard and /league
def _closed(conn, idea, r, exit_date, ref):
    conn.execute("INSERT INTO league_trades (idea, pair, symbol, side, ref, signal_date, atr, stop_atr, exit_after, "
                 "status, entry_date, entry, stop, exit_date, exit_price, result_r, reason, created) "
                 "VALUES (?, 'EURUSD', 'EURUSD=X', 'long', ?, '2026-10-09', 0.02, 2, 'bars:5', 'closed', "
                 "'2026-10-12', 1.0, 0.96, ?, 1.0, ?, 'time', 'x')", (idea, ref, exit_date, r))
    conn.commit()


def test_the_scoreboard_adds_up_each_idea_by_month_and_gives_the_verdict(conn):
    league.start_date(conn, MON)
    for k in range(8):
        _closed(conn, "RATE-MOM", 0.5, f"2026-{10 + k % 3}-20", f"a{k}")
    _closed(conn, "CMD-LEAD", -1.0, "2026-10-20", "b")
    board = league.scoreboard(conn, dt.date(2027, 1, 12))
    rate, cmd = board.scores[1], board.scores[2]
    assert (rate.closed, rate.net, rate.months_up, rate.qualifies) == (8, 4.0, 3, True)
    assert (cmd.closed, cmd.net, cmd.qualifies) == (1, -1.0, False)
    text = tn.format_league_board(board, final=True, html=False)
    assert text.splitlines() == [
        "League final 12.10-11.01",
        "                 R   TP-R  Cl Op",
        "Speculators  +0.00  +0.00   0  0 fail",
        "Rates        +4.00  +4.00   8  0 pass",
        "Commodities  -1.00  -1.00   1  0 fail",
        "Month-end    +0.00  +0.00   0  0 fail",
        "Retest       +0.00  +0.00   0  0 fail",
        "Deviation    +0.00  +0.00   0  0 fail",
        "Total        +3.00  +3.00   9  0"]
    month = tn.format_league_board(board, month=(2026, 10), html=False).splitlines()
    assert month[0] == "League 10.2026 12.10-11.01" and month[3] == "Rates        +1.50  +4.00   8  0"
    assert month[8:] == ["Total        +0.50  +3.00   9  0", "Since start +3.00R"]
    conn.execute("UPDATE league_trades SET result2_r = 0.1 WHERE idea = 'RATE-MOM'")
    conn.commit()
    assert league.scoreboard(conn, dt.date(2027, 1, 12)).scores[1].net2 == pytest.approx(0.8)


def test_seven_trades_or_one_good_month_do_not_qualify():
    assert not league.IdeaScore("X", 7, 5.0, {(2026, 10): 3.0, (2026, 11): 2.0}, 0).qualifies
    assert not league.IdeaScore("X", 9, 5.0, {(2026, 10): 6.0, (2026, 11): -1.0}, 0).qualifies
    assert not league.IdeaScore("X", 9, -0.1, {(2026, 10): 1.0, (2026, 11): 1.0, (2026, 12): -2.1}, 0).qualifies


def test_league_in_telegram_shows_the_board_with_the_open_trades_and_the_euros(conn, monkeypatch):
    from cfd import live
    sent = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    live.set_setting(conn, "balance", 1000)
    _run(conn, MON, [], {"EURUSD=X": eurusd()}, yields=lambda today: _yields(+0.12))
    _closed(conn, "CMD-LEAD", 1.5, "2026-10-12", "c")
    tb._handle_message(conn, "/league")
    text = sent[-1]
    assert text.startswith("<pre>League 12.10-11.01\n") and text.endswith("</pre>")
    assert "\nCommodities  +1.50  +1.50   1  0\n" in text and "\n1R = €10.00\n" in text
    assert text.endswith("\n\nEURUSD Long\n1.00000  SL 0.96000  TP1 1.04000</pre>")
    assert "идея" not in text and "•" not in text
    tb._handle_message(conn, "/league what")
    assert sent[-1] == league.USAGE
    tb._handle_message(conn, "/league off")
    assert league.quiet(conn) and "не присылаю" in sent[-1]
    tb._handle_message(conn, "/league on")
    assert not league.quiet(conn)


def test_the_days_openings_are_one_message_a_block_each_and_the_yen_has_three_decimals(conn):
    sent = []
    jpy = [(d, 150.0, 151.0, 149.0, 150.0) for d, *_ in eurusd()[:-1]] + [(MON.isoformat(), 150.0, 150.0, 150.0, 150.0)]
    table = {"EURUSD=X": eurusd(), "USDJPY=X": jpy}
    y = _yields(+0.12)
    y["JP"] = dict(y["EA"])
    _run(conn, MON, sent, table, yields=lambda today: y)
    assert sent == ["<pre>EURUSD Long · CFD test\nEntry 1.00000\nStop  0.96000\nTP1   1.04000\nTP2   1.08000\nTP3   1.12000\n"
                    "TP4   1.16000\n\nUSDJPY Short · CFD test\nEntry 150.000\nStop  154.000\nTP1   146.000\nTP2   142.000\n"
                    "TP3   138.000\nTP4   134.000</pre>"]


def test_the_board_names_the_next_target_and_a_trade_not_yet_entered(conn):
    feed = league.Feed(MON, fetch_of({"EURUSD=X": eurusd()}))
    waiting = league.make(conn, signal(), feed)
    board = league.scoreboard(conn, MON)
    assert tn.format_league_board(board, html=False).endswith("\n\nEURUSD Long\nEntry at next open")
    league.advance(waiting, feed.bars(waiting.symbol), feed.forming(waiting.symbol), MON)
    waiting.checkpoint = 2
    league._save(conn, waiting)
    assert tn.format_league_board(league.scoreboard(conn, MON), html=False).endswith(
        "\n\nEURUSD Long\n1.00000  SL 0.96000  TP3 1.12000")



# ------------------------------------------------------------------ idea 5: the break, the retest, the confirming bar
def _bar(i, o, h, low, c):
    return Bar(dt.datetime(2026, 1, 1, tzinfo=UTC) + dt.timedelta(days=i), o, h, low, c)


def _range(n=40):
    return [_bar(i, 1.0, 1.01, 0.99, 1.0) for i in range(n)]            # flat between 0.99 and 1.01


def _flat_atr(bars, n):
    return [0.02] * len(bars)


def test_a_break_a_retest_and_a_confirming_bar_make_a_long_with_its_stop_and_target():
    bars = _range() + [_bar(40, 1.0, 1.03, 1.0, 1.025),          # closes above the range's high 1.01: the breakout
                       _bar(41, 1.025, 1.03, 1.008, 1.012),      # comes back to 1.01 -- closes above it, but down
                       _bar(42, 1.012, 1.03, 1.011, 1.028)]      # the first up-close above the level: confirmed
    [s] = league.brk_retest({"EURUSD": bars}, atr_of=_flat_atr)
    assert (s.idea, s.side, s.ref, s.exit_after) == ("BRK-RETEST", "long", "2026-02-10-long", "bars:20")
    assert s.stop_price == 1.0 and s.target_r == 2.0             # the lowest low from the breakout bar on
    assert league.brk_retest({"EURUSD": bars[:-1]}, atr_of=_flat_atr) == []                # not confirmed yet
    later = bars + [_bar(43, 1.028, 1.04, 1.02, 1.035)]
    assert league.brk_retest({"EURUSD": later}, atr_of=_flat_atr) == []                    # said once


def test_a_short_mirrors_it_and_no_retest_or_a_failed_break_is_nothing():
    down = _range() + [_bar(40, 1.0, 1.0, 0.97, 0.975), _bar(41, 0.975, 0.992, 0.97, 0.988),
                       _bar(42, 0.988, 0.989, 0.97, 0.972)]
    [s] = league.brk_retest({"GBPUSD": down}, atr_of=_flat_atr)
    assert (s.side, s.stop_price) == ("short", 1.0)
    no_retest = _range() + [_bar(40, 1.0, 1.03, 1.0, 1.025), _bar(41, 1.025, 1.04, 1.02, 1.035),
                            _bar(42, 1.035, 1.05, 1.041, 1.045)]             # each low stays above the last level
    assert league.brk_retest({"EURUSD": no_retest}, atr_of=_flat_atr) == []
    failed = _range() + [_bar(40, 1.0, 1.03, 1.0, 1.025), _bar(41, 1.025, 1.03, 0.98, 0.985),   # deep back under the level
                         _bar(42, 0.985, 1.03, 0.985, 1.028)]
    assert league.brk_retest({"EURUSD": failed}, atr_of=_flat_atr) == []
    assert league.brk_retest({"EURUSD": _range(30)}, atr_of=_flat_atr) == []                # too little history


def _retest_trade(conn, today_open=1.03, ref="r"):
    feed = league.Feed(MON, fetch_of({"EURUSD=X": eurusd(today_open=today_open)}))
    sig5 = league.Signal("BRK-RETEST", "EURUSD", "long", ref, 0.0, "bars:20", stop_price=1.0, target_r=2.0)
    t = league.make(conn, sig5, feed)
    return t, league.advance(t, feed.bars(t.symbol), feed.forming(t.symbol), MON)


def test_a_trade_with_a_named_stop_leaves_at_its_target(conn):
    t, events = _retest_trade(conn)                              # entry 1.03, stop 1.00: R 0.03, target 1.09
    assert events == ["open"] and t.stop == 1.0 and league.target_of(t) == pytest.approx(1.09)
    day = dt.date(2026, 10, 14)
    up = {MON: (1.03, 1.04, 1.02, 1.03), dt.date(2026, 10, 13): (1.03, 1.10, 1.02, 1.08)}
    feed = league.Feed(day, fetch_of({"EURUSD=X": eurusd(extra=up, upto=day)}))
    assert league.advance(t, feed.bars(t.symbol), feed.forming(t.symbol), day)[-1] == "close"
    assert (t.reason, t.exit_price) == ("target", pytest.approx(1.09))
    assert t.result_r == pytest.approx(2.0 - (0.015 + 1 * 0.010) / 100 * 1.03 / 0.03)
    assert tn.format_league_event(t, "close", html=False).startswith("EURUSD target 1.09000 +1.9")


def test_inside_one_bar_the_stop_comes_before_the_target(conn):
    t, _ = _retest_trade(conn)
    day = dt.date(2026, 10, 14)
    both = {MON: (1.03, 1.04, 1.02, 1.03), dt.date(2026, 10, 13): (1.03, 1.10, 0.99, 1.0)}
    feed = league.Feed(day, fetch_of({"EURUSD=X": eurusd(extra=both, upto=day)}))
    league.advance(t, feed.bars(t.symbol), feed.forming(t.symbol), day)
    assert t.reason == "stop"


def test_an_open_on_the_wrong_side_of_the_stop_or_too_far_from_it_is_no_trade(conn):
    t, events = _retest_trade(conn, today_open=0.99)             # under the named stop
    assert events == [] and (t.status, t.reason, t.result_r) == ("closed", "void", 0.0)
    league._save(conn, t)
    assert league.closed_trades(conn) == [] and league.live_trades(conn) == []
    far, events = _retest_trade(conn, today_open=1.20, ref="r2")           # 10 ATR away
    assert events == [] and far.reason == "void"


def test_a_target_trade_shows_its_levels_up_to_the_target(conn):
    t, _ = _retest_trade(conn)
    assert tn.format_league_opens([t], html=False) == (
        "EURUSD Long · CFD test\nEntry 1.03000\nStop  1.00000\nTP1   1.06000\nTP2   1.09000")


# ------------------------------------------------------------------ idea 6: a false break of the range, back inside
def test_a_pierce_of_the_range_and_a_close_back_inside_make_a_long_to_the_other_side():
    bars = _range() + [_bar(40, 1.0, 1.0, 0.975, 0.985),         # under the range's low 0.99, closes under it
                       _bar(41, 0.985, 0.999, 0.98, 0.995)]      # the first close back above 0.99
    [s] = league.rng_dev({"EURUSD": bars}, atr_of=lambda b, n: [0.005] * len(b))
    assert (s.idea, s.side, s.ref, s.exit_after) == ("RNG-DEV", "long", "2026-02-10-long", "bars:20")
    assert (s.stop_price, s.target_price, s.target_r) == (0.975, 1.01, None)
    assert league.rng_dev({"EURUSD": bars[:-1]}, atr_of=lambda b, n: [0.005] * len(b)) == []    # still outside
    later = bars + [_bar(42, 0.995, 1.0, 0.992, 0.998)]
    assert league.rng_dev({"EURUSD": later}, atr_of=lambda b, n: [0.005] * len(b)) == []        # said once


def test_a_short_mirrors_it_and_a_narrow_range_or_a_late_return_is_nothing():
    up = _range() + [_bar(40, 1.0, 1.02, 1.0, 1.005)]            # over the high 1.01 and back inside in one bar
    [s] = league.rng_dev({"GBPUSD": up}, atr_of=lambda b, n: [0.005] * len(b))
    assert (s.side, s.stop_price, s.target_price) == ("short", 1.02, 0.99)
    assert league.rng_dev({"GBPUSD": up}, atr_of=_flat_atr) == []                # the range is 1 ATR wide
    late = _range() + [_bar(40 + i, 0.985, 0.988, 0.975, 0.985) for i in range(3)] + [_bar(43, 0.985, 1.0, 0.98, 0.995)]
    assert league.rng_dev({"EURUSD": late}, atr_of=lambda b, n: [0.005] * len(b)) == []
    assert league.rng_dev({"EURUSD": _range(30)}, atr_of=_flat_atr) == []


def _deviation_trade(conn, target, ref="d"):
    feed = league.Feed(MON, fetch_of({"EURUSD=X": eurusd(today_open=1.03)}))
    sig6 = league.Signal("RNG-DEV", "EURUSD", "long", ref, 0.0, "bars:20", stop_price=1.0, target_price=target)
    t = league.make(conn, sig6, feed)
    return t, league.advance(t, feed.bars(t.symbol), feed.forming(t.symbol), MON)


def test_a_target_at_a_price_is_a_real_exit_and_is_shown_as_the_last_level(conn):
    t, events = _deviation_trade(conn, 1.081)                    # entry 1.03, stop 1.00: the target is 1.7R away
    assert events == ["open"] and t.target_r == pytest.approx(1.7) and league.target_of(t) == pytest.approx(1.081)
    assert tn.format_league_opens([t], html=False) == (
        "EURUSD Long · CFD test\nEntry 1.03000\nStop  1.00000\nTP1   1.06000\nTP2   1.08100")
    day = dt.date(2026, 10, 14)
    up = {MON: (1.03, 1.04, 1.02, 1.03), dt.date(2026, 10, 13): (1.03, 1.10, 1.02, 1.08)}
    feed = league.Feed(day, fetch_of({"EURUSD=X": eurusd(extra=up, upto=day)}))
    assert league.advance(t, feed.bars(t.symbol), feed.forming(t.symbol), day)[-1] == "close"
    assert (t.reason, t.exit_price) == ("target", pytest.approx(1.081))


def test_a_target_nearer_than_the_stop_is_no_trade(conn):
    t, events = _deviation_trade(conn, 1.05)                     # 0.02 to the target, 0.03 to the stop
    assert events == [] and t.reason == "void" and league.closed_trades(conn) == []

