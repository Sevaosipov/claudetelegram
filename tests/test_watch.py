"""watch.py: a level an analysis said to wait for, kept and checked each day; a fresh analysis when the
price closes beyond it."""
from __future__ import annotations

import datetime as dt

import pytest

import analyst
import bot
import db
import telegram_bot as tb
import telegram_notify as tn
import watch

TODAY = dt.date(2026, 10, 12)
ANSWER = ("<pre>🟡 XRP · Watch\nPrice  1.41\nScore  45\nChart  Neutral\nStop   -15%\nWait   close above 1.43</pre>\n"
          "Цена ниже 50-дн. средней.\n🎯 Итог: ждать закрытия выше 1.43.")


# ------------------------------------------------------------------ reading the row
@pytest.mark.parametrize("text, found", [
    (ANSWER, ("above", 1.43)),
    ("<pre>NVDA\nWait   close below 222.08</pre>", ("below", 222.08)),
    ("Wait close above 82 500,5", ("above", 82500.5)),
    ("<pre>NVDA · Buy\nPrice  229.28</pre>\n🎯 Итог: ждать закрытия выше 1.43.", None),      # words, not the row
    ("Wait   close over 1.43", None), ("Wait   close above", None), ("Wait   close above 0", None), ("", None)])
def test_the_wait_row_is_read_and_nothing_else_is(text, found):
    assert watch.parse(text) == found


# ------------------------------------------------------------------ keeping a level
def test_a_delivered_analysis_keeps_its_level_and_the_next_one_replaces_it(conn):
    kept = watch.record(conn, "CRYPTO:XRP", ANSWER, TODAY)
    assert (kept.ticker, kept.direction, kept.level, kept.created) == ("CRYPTO:XRP", "above", 1.43, "2026-10-12")
    watch.record(conn, "CRYPTO:XRP", ANSWER.replace("1.43", "1.50"), TODAY)
    assert [(lv.ticker, lv.level) for lv in watch.active(conn)] == [("CRYPTO:XRP", 1.5)]


def test_an_analysis_with_no_level_ends_the_watch(conn):
    watch.record(conn, "CRYPTO:XRP", ANSWER, TODAY)
    assert watch.record(conn, "CRYPTO:XRP", "<pre>🟢 XRP · Buy\nPrice  1.50</pre>", TODAY) is None
    assert watch.active(conn) == []


def test_no_more_than_the_limit_of_assets_is_watched(conn, monkeypatch, capsys):
    monkeypatch.setattr(watch, "MAX_ACTIVE", 2)
    for t in ("AAA", "BBB", "CCC"):
        watch.record(conn, t, ANSWER, TODAY)
    assert [lv.ticker for lv in watch.active(conn)] == ["AAA", "BBB"] and "CCC: not watched" in capsys.readouterr().err
    assert watch.record(conn, "AAA", ANSWER.replace("1.43", "9"), TODAY).level == 9      # one already watched may change


# ------------------------------------------------------------------ the daily pass
def _level(direction="above", level=1.43, created="2026-10-09"):
    return watch.Level(1, "CRYPTO:XRP", direction, level, created)


@pytest.mark.parametrize("level, bar, hit", [
    (_level(), ("2026-10-11", 1.45), True),
    (_level(), ("2026-10-11", 1.43), False),                    # at the level is not beyond it
    (_level(), ("2026-10-09", 1.60), False),                    # the bar the analysis already saw
    (_level("below", 1.28), ("2026-10-11", 1.27), True),
    (_level("below", 1.28), ("2026-10-11", 1.30), False),
    (_level(), None, False)])
def test_a_level_is_reached_by_a_later_close_beyond_it(level, bar, hit):
    assert watch.reached(level, bar) is hit


def _closes(table):
    return lambda ticker, source: table.get(ticker, [])


def test_a_reached_level_is_said_marked_and_a_fresh_analysis_queued_and_run(conn):
    watch.record(conn, "CRYPTO:XRP", ANSWER, dt.date(2026, 10, 9))
    watch.record(conn, "$NVDA", "Wait   close below 222.08", dt.date(2026, 10, 9))
    sent, analysed = [], []
    closes = _closes({"CRYPTO:XRP": [("2026-10-10", 1.40), ("2026-10-11", 1.45), ("2026-10-12", 1.60)],
                      "NVDA": [("2026-10-11", 229.0)]})
    fired = watch.run(conn, today=TODAY, closes_fn=closes, send=lambda t: sent.append(t) or True,
                      analyse=lambda c: analysed.append(db.pending_analysis(c)))
    assert fired == 1 and sent == ["<pre>🔔 XRP closed above 1.43 (1.45)</pre>"]          # today's bar is not complete
    assert [t for _id, t in analysed[0]] == ["CRYPTO:XRP"]
    assert [lv.ticker for lv in watch.active(conn)] == ["$NVDA"]                           # XRP is done, NVDA waits
    assert conn.execute("SELECT status, fired_at, fired_close FROM watch_levels WHERE ticker = 'CRYPTO:XRP'"
                        ).fetchone() == ("reached", "2026-10-12", 1.45)
    assert watch.run(conn, today=TODAY, closes_fn=closes, send=lambda t: True, analyse=lambda c: 1 / 0) == 0


def test_a_note_that_did_not_go_out_leaves_the_level_for_the_next_run(conn):
    watch.record(conn, "CRYPTO:XRP", ANSWER, dt.date(2026, 10, 9))
    closes = _closes({"CRYPTO:XRP": [("2026-10-11", 1.45)]})
    assert watch.run(conn, today=TODAY, closes_fn=closes, send=lambda t: False, analyse=lambda c: None) == 0
    assert len(watch.active(conn)) == 1 and db.pending_analysis(conn) == []


def test_an_old_level_expires_and_a_failing_asset_does_not_stop_the_rest(conn, capsys):
    watch.record(conn, "OLD", ANSWER, TODAY - dt.timedelta(days=61))
    watch.record(conn, "BAD", ANSWER, dt.date(2026, 10, 9))
    watch.record(conn, "CRYPTO:XRP", ANSWER, dt.date(2026, 10, 9))

    def closes(ticker, source):
        if ticker == "BAD":
            raise RuntimeError("no prices")
        return [("2026-10-11", 1.45)]
    assert watch.run(conn, today=TODAY, closes_fn=closes, send=lambda t: True, analyse=lambda c: None) == 1
    assert [lv.ticker for lv in watch.active(conn)] == ["BAD"] and "BAD: RuntimeError" in capsys.readouterr().err
    assert conn.execute("SELECT status FROM watch_levels WHERE ticker = 'OLD'").fetchone() == ("expired",)


def test_an_oslo_listing_is_priced_on_its_own_exchange(conn):
    asked = []
    watch.last_close(conn, "EQNR.OL", TODAY, lambda ticker, source: asked.append((ticker, source)) or [])
    assert asked == [("EQNR", "NORWAY")]


# ------------------------------------------------------------------ the analyst keeps the level
class _Proc:
    def __init__(self, stdout):
        self.returncode, self.stdout, self.stderr = 0, stdout, ""


def test_a_ticker_analysis_that_went_out_keeps_its_level_and_a_question_does_not(conn, monkeypatch):
    monkeypatch.setattr(analyst, "context", lambda conn, ticker: "МОДЕЛЬ: балл 45")
    db.enqueue_analysis(conn, "CRYPTO:XRP")
    db.enqueue_question(conn, "что с XRP?")
    assert analyst.process_queue(conn, run=lambda *a, **k: _Proc(ANSWER), send=lambda text: True) == 0
    assert [(lv.ticker, lv.direction, lv.level) for lv in watch.active(conn)] == [("CRYPTO:XRP", "above", 1.43)]


def test_an_analysis_that_did_not_go_out_keeps_nothing(conn, monkeypatch):
    monkeypatch.setattr(analyst, "context", lambda conn, ticker: "МОДЕЛЬ: балл 45")
    db.enqueue_analysis(conn, "CRYPTO:XRP")
    analyst.process_queue(conn, run=lambda *a, **k: _Proc(ANSWER), send=lambda text: False)
    assert watch.active(conn) == []


def test_the_method_tells_claude_to_write_the_row_the_bot_reads():
    method = (analyst.BASE_DIR / "analyst_method.txt").read_text(encoding="utf-8")
    assert "Wait   close above 1.43</pre>" in method and "- Wait: «close above 1.43» or «close below 222.08»" in method
    assert watch.parse("Wait   close above 1.43") and "The bot reads this" in " ".join(method.split())


# ------------------------------------------------------------------ /watch and the daily run
def test_watch_lists_the_levels_and_off_stops_one(conn, monkeypatch):
    sent = []
    monkeypatch.setattr("telegram_notify.send_text", lambda msg: sent.append(msg) or True)
    tb._handle_message(conn, "/watch")
    assert sent[-1] == "<pre>Watching: nothing</pre>"
    watch.record(conn, "CRYPTO:XRP", ANSWER, TODAY)
    watch.record(conn, "$NVDA", "Wait   close below 222.08", TODAY)
    tb._handle_message(conn, "/watch")
    assert sent[-1] == "<pre>Watching\nXRP   close above 1.43\nNVDA  close below 222.08</pre>"
    tb._handle_message(conn, "/watch off xrp")
    assert sent[-1] == "XRP: больше не жду." and [lv.ticker for lv in watch.active(conn)] == ["$NVDA"]
    tb._handle_message(conn, "/watch off xrp")
    assert sent[-1] == "XRP: уровня и не было."
    tb._handle_message(conn, "/watch what")
    assert sent[-1] == watch.USAGE


def test_the_reached_note_names_the_level_and_the_close():
    assert tn.format_watch_reached(_level("below", 222.08), 221.5, html=False) == "🔔 XRP closed below 222.08 (221.5)"
    assert tn.format_watch_reached(watch.Level(1, "CRYPTO:DOGE", "above", 0.152, "x"), 0.1534, html=False) == (
        "🔔 DOGE closed above 0.152 (0.1534)")
