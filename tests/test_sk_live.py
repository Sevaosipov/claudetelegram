"""The SK sequence on paper (cfd/sk_live.py): the live replay is the tested rule, the table follows the replay,
and a message goes out for what is new. No network: the bars are made here."""
from __future__ import annotations

import datetime as dt
import random

import pytest

import telegram_bot as tb
import telegram_notify as tn
from cfd import indicators as ind
from cfd import research_abc
from cfd import sk_live as sk
from cfd.data import Bar

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 12, 16, 0, 30, tzinfo=UTC)


def walk(n: int, seed: int = 7) -> list[Bar]:
    """Four-hour bars of a random walk around 1.10."""
    rng, price, out = random.Random(seed), 1.10, []
    start = dt.datetime(2025, 1, 6, tzinfo=UTC)
    for i in range(n):
        o = price
        price += rng.gauss(0, 0.002)
        hi, lo = max(o, price) + abs(rng.gauss(0, 0.0008)), min(o, price) - abs(rng.gauss(0, 0.0008))
        out.append(Bar(start + i * sk.H4, o, hi, lo, price))
    return out


def test_the_live_replay_is_the_tested_rule():
    bars = walk(3000)
    atr = ind.atr(bars, 14)
    tested = []
    for sign in (1, -1):
        tested += research_abc.trades(bars, atr, dt.date.min, dt.date.max, sign, cost=0.8e-4, night_pct=0.010, **sk.RULE)
    replayed = sk.replay("EURUSD", bars)
    closed = [v for v in replayed.values() if v["status"] == "closed"]
    assert len(closed) >= 10
    if not any(v["status"] == "open" for v in replayed.values()):
        assert sorted(round(v["result_r"], 9) for v in closed) == sorted(round(r, 9) for _y, r in tested)
    one = closed[0]
    assert (one["entry"] > one["stop"]) == (one["side"] == "long") == (one["target"] > one["entry"])


def test_only_completed_four_hour_bars_are_used():
    hours = [Bar(dt.datetime(2026, 10, 12, h, tzinfo=UTC), 1.0, 1.1, 0.9, 1.0) for h in range(0, 17)]
    bars = sk.to_h4(hours, NOW)                                  # 16:00:30 -- the 16:00 hour is not over
    assert [b.ts.hour for b in bars] == [0, 4, 8, 12] and bars[0].high == 1.1


def _order(status="pending", **kw):
    base = dict(side="long", entry=1.1205, stop=1.118, target=1.124, order_ts="2026-10-12T12:00:00+00:00",
                status=status, fill_ts=None, fill_price=None, exit_ts=None, result_r=None)
    base.update(kw)
    return base


def _pass(conn, monkeypatch, state, now=NOW):
    sent = []
    monkeypatch.setattr(sk, "hours_of", lambda pair, fetch=None: [None])
    monkeypatch.setattr(sk, "to_h4", lambda hours, now: [None] * 400)
    monkeypatch.setattr(sk, "replay", lambda pair, bars: state if pair == "EURUSD" else {})
    sk.run(conn, now=now, send=lambda text: sent.append(text) or True)
    return sent


def test_an_order_is_told_and_then_what_becomes_of_it(conn, monkeypatch):
    old = _order("closed", order_ts="2026-09-01T12:00:00+00:00", exit_ts="2026-09-02T12:00:00+00:00", result_r=1.5)
    sent = _pass(conn, monkeypatch, {"old": old, "a": _order()})
    assert sent == ["<pre>EURUSD Buy limit · CFD · SK\nEntry 1.12050\nStop  1.11800\nTP    1.12400</pre>"]
    assert [t.scored for t in sk.trades(conn)] == [0, 1]         # what ended before the start is not scored
    assert _pass(conn, monkeypatch, {"old": old, "a": _order()}) == []                         # nothing new
    filled = _order("open", fill_ts="2026-10-12T16:00:00+00:00", fill_price=1.1205)
    assert _pass(conn, monkeypatch, {"a": filled}) == ["<pre>EURUSD SK filled 1.12050</pre>"]
    won = dict(filled, status="closed", exit_ts="2026-10-12T16:00:00+00:00", result_r=1.36)
    assert _pass(conn, monkeypatch, {"a": won}) == ["<pre>EURUSD SK TP 1.12400 +1.4R</pre>"]
    b = _order(side="short", entry=1.125, stop=1.128, target=1.121)
    assert _pass(conn, monkeypatch, {"a": won, "b": b})[0].startswith("<pre>EURUSD Sell limit · CFD · SK\n")
    assert _pass(conn, monkeypatch, {"a": won, "b": dict(b, status="cancelled", exit_ts="2026-10-12T16:00:00+00:00")}) \
        == ["<pre>EURUSD SK cancelled</pre>"]
    lost = _order("closed", fill_ts="x", fill_price=1.1205, exit_ts="2026-10-10T00:00:00+00:00", result_r=-1.05)
    assert _pass(conn, monkeypatch, {"c": _order()}) and _pass(conn, monkeypatch, {"c": lost}) == []    # stale: silent
    board = tn.format_sk_board(sk.trades(conn, "WHERE scored = 1"), dt.date(2026, 10, 12), html=False)
    assert board.splitlines()[:4] == ["SK since 12.10", "            R  Cl   W", "EURUSD  +0.31   2   1", "Total   +0.31   2   1"]


def test_the_command_and_the_mute(conn, monkeypatch):
    assert sk.handle_command(conn, "/sk").startswith("SK начнётся")
    _pass(conn, monkeypatch, {"a": _order()})
    assert sk.handle_command(conn, "/sk").endswith("EURUSD Buy · limit\n1.12050  SL 1.11800  TP 1.12400</pre>")
    assert "не присылаю" in sk.handle_command(conn, "/sk off") and sk.quiet(conn)
    assert _pass(conn, monkeypatch, {"a": _order("cancelled", exit_ts="2026-10-12T16:00:00+00:00")}) == []
    assert sk.handle_command(conn, "/sk x").startswith("Команды")
    told = []
    monkeypatch.setattr(tn, "send_text", lambda text, **k: told.append(text) or True)
    assert tb._handle_sk_command(conn, "/sk on") and not tb._handle_sk_command(conn, "/day")
    assert "присылаю каждую" in told[0]
