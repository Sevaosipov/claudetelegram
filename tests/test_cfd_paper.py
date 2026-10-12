"""cfd/paper.py: every paper test on one board, and an idea retired at -10R."""
from __future__ import annotations

import datetime as dt

from cfd import league, paper

TODAY = dt.date(2026, 10, 19)


def _closed(conn, idea, r, ref):
    conn.execute(
        "INSERT INTO league_trades (idea, pair, symbol, side, ref, signal_date, atr, stop_atr, exit_after, status, "
        "entry_date, entry, stop, exit_date, exit_price, result_r, reason, created) "
        "VALUES (?, 'EURUSD', 'EURUSD=X', 'long', ?, '2026-10-12', 0.01, 2.0, 'bars:5', 'closed', '2026-10-13', 1.0, "
        "0.98, '2026-10-16', 0.98, ?, 'stop', 'x')", (idea, ref, r))
    conn.commit()


def test_the_board_lists_every_test_and_an_idea_is_retired_once(conn):
    league.start_date(conn, dt.date(2026, 10, 9))
    for k in range(9):
        _closed(conn, "CMD-LEAD", -1.2, f"a{k}")
    _closed(conn, "RATE-MOM", 0.8, "b")
    text = paper.board(conn, TODAY, html=False).splitlines()
    assert text[0] == "Paper tests 19.10" and text[1].split() == ["R", "Cl", "Op"]
    assert "       Rates        +0.80   1  0" in text and "       Commodities -10.80   9  0" in text
    assert any(line.startswith("SK     EURUSD") for line in text) and text[-1].startswith("Crypto Breakout")
    told = []
    assert paper.retire(conn, TODAY, lambda t: told.append(t) or True) == ["League:Commodities"]
    assert told == ["<pre>League · Commodities stopped at -10.8R after 9 trades</pre>"]
    assert paper.retire(conn, TODAY, lambda t: told.append(t) or True) == [] and len(told) == 1
    assert paper.stopped(conn, "League:Commodities") and not paper.stopped(conn, "League:Rates")
    assert "       Commodities -10.80   9  0 stopped" in paper.board(conn, TODAY, html=False).splitlines()


def test_the_board_goes_out_once_a_week(conn):
    league.start_date(conn, dt.date(2026, 10, 9))
    sent = []
    assert paper.weekly(conn, TODAY, lambda t: sent.append(t) or True) and len(sent) == 1
    assert not paper.weekly(conn, TODAY + dt.timedelta(days=2), lambda t: sent.append(t) or True)
    assert paper.weekly(conn, TODAY + dt.timedelta(days=7), lambda t: sent.append(t) or True) and len(sent) == 2
    assert not paper.weekly(conn, TODAY + dt.timedelta(days=14), lambda t: False)       # a failed send is tried again
