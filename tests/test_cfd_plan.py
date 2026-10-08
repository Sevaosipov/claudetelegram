"""/cfd plan (cfd/plan.py): the user's own CFD trade -- its four stages, risk and quantity, the row that
follows it through 1-minute bars, the messages, the commands, the pass in the Telegram agent's loop."""
from __future__ import annotations

import datetime as dt

import pytest

import fx
import telegram_bot as tb
import telegram_notify as tn
from cfd import instruments as ins
from cfd import live, plan

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def row(minute, o, h, low, c):
    return ((T0 + dt.timedelta(minutes=minute)).isoformat(), o, h, low, c)


def feed(*rows):
    return lambda symbol: list(rows)


@pytest.fixture(autouse=True)
def rates(monkeypatch):
    monkeypatch.setattr(fx, "per_eur", lambda currency, conn=None: {"USD": 1.25, "JPY": 160.0}.get(currency, 1.0))


@pytest.fixture
def sent():
    out = []
    return out, lambda notice: out.append(notice) or True


def gold(conn, **kw):
    """A followed long of gold at 4000 with the stop at 3990 (R 10), the future at 4000 too."""
    args = "XAUUSD buy 4000 stop 3990".split()
    live.set_setting(conn, "balance", kw.pop("balance", 1000))
    made, _, why = plan.make(conn, plan.parse(args), fetch=feed(row(-10, 4000, 4001, 3999, 4000)), now=T0)
    assert why is None
    return made


# ------------------------------------------------------------------ names and parsing
@pytest.mark.parametrize("word, name", [("XAUUSD", "XAUUSD"), ("gold", "XAUUSD"), ("eur/usd", "EURUSD"),
                                        ("btc", "BTCUSD"), ("SOLUSD", "SOLUSD"), ("dax", "DE40")])
def test_a_word_names_its_instrument(word, name):
    assert plan.resolve(word).name == name


def test_an_unknown_word_names_nothing():
    assert plan.resolve("NOPE") is None


@pytest.mark.parametrize("text", ["XAUUSD buy 4461.80 stop 4449.10", "gold long 4461,80 4449,10",
                                  "xauusd купил 4461.80 sl 4449.10"])
def test_a_plan_is_parsed_with_or_without_the_stop_word(text):
    d = plan.parse(text.split())
    assert (d.inst.name, d.side, d.entry, d.stop) == ("XAUUSD", "long", 4461.80, 4449.10)


@pytest.mark.parametrize("text", ["", "XAUUSD buy 4461.80", "NOPE buy 10 stop 9", "XAUUSD hold 10 stop 9",
                                  "XAUUSD buy 10 stop 11", "XAUUSD sell 10 stop 9", "XAUUSD buy x stop 9",
                                  "XAUUSD buy 10 stop 10", "XAUUSD buy -10 stop -11"])
def test_what_is_not_a_plan_is_refused(text):
    assert plan.parse(text.split()) is None


# ------------------------------------------------------------------ sizing
def test_the_quantity_risks_the_euro_amount_over_the_stop_in_the_quote_currency():
    # €10 over a $12.50 stop = €10 a unit -> 1 ounce; over 25 pips of EURUSD (= €0.002) -> 5000 units
    assert plan.size_qty(ins.by_name("XAUUSD"), 10, 12.5, 4000) == 1.0
    assert plan.size_qty(ins.by_name("EURUSD"), 10, 0.0025, 1.12) == 5000
    assert plan.size_qty(ins.by_name("EURUSD"), 10, 0.0021, 1.12) == 5900      # 5952 down to the step
    assert plan.size_qty(ins.by_name("USDJPY"), 10, 0.40, 150) == 4000         # ¥0.40 = €0.0025
    assert plan.size_qty(ins.by_name("EURUSD"), 0.1, 0.0025, 1.12) == 0        # under the step of 100


# ------------------------------------------------------------------ making a plan
def test_a_plan_is_sized_by_the_balance_and_kept(conn):
    p = gold(conn)
    assert (p.id, p.risk_pct, p.risk_eur, p.qty, p.r, p.offset) == (1, 1.0, 10.0, 1.25, 10, 0)
    assert plan.open_plans(conn)[0].last_ts == (T0 - dt.timedelta(minutes=10)).isoformat()


def test_with_no_balance_the_quantity_is_for_a_thousand(conn):
    made, per_1000, why = plan.make(conn, plan.parse("XAUUSD buy 4000 stop 3990".split()),
                                    fetch=feed(row(-10, 4000, 4001, 3999, 4000)), now=T0)
    assert (made.risk_eur, made.qty, per_1000, why) == (None, None, 1.25, None)


def test_a_future_is_followed_with_the_distance_to_the_entry(conn):
    made, _, why = plan.make(conn, plan.parse("XAUUSD buy 4000 stop 3990".split()),
                             fetch=feed(row(-10, 3975, 3981, 3974, 3980)), now=T0)
    assert why is None and made.offset == 20


@pytest.mark.parametrize("text, last, why", [
    ("XAUUSD buy 4000 stop 3990", 3000, "цена входа далеко от текущей цены"),
    ("EURUSD buy 1.1200 stop 1.1180", 1.1170, "цена уже за стопом"),
    ("EURUSD sell 1.1200 stop 1.1220", 1.1230, "цена уже за стопом"),
    ("EURUSD buy 1.1200 stop 1.1180", 1.1250, "цена сейчас дальше одного R от входа"),
])
def test_a_plan_that_cannot_be_followed_is_only_the_calculation(conn, text, last, why):
    made, _, reason = plan.make(conn, plan.parse(text.split()), fetch=feed(row(-10, last, last, last, last)), now=T0)
    assert (made.id, reason) == (0, why) and plan.open_plans(conn) == []


def test_no_prices_no_following(conn):
    def broken(symbol):
        raise RuntimeError("offline")
    for fetch in (broken, feed()):
        made, _, reason = plan.make(conn, plan.parse("EURUSD buy 1.12 stop 1.118".split()), fetch=fetch, now=T0)
        assert (made.id, reason) == (0, "цены сейчас недоступны")


# ------------------------------------------------------------------ the bars
def test_only_whole_completed_bars_count():
    rows = [row(5, 2, 3, 1, 2), row(0, 1, 2, 1, 2), row(10, 1, None, 1, 1), row(15, 1, 0.5, 1, 1),
            row(20, 1, 2, 1, 2)]
    now = T0 + dt.timedelta(minutes=20, seconds=30)     # the bar of minute 20 is still forming
    assert [b.ts for b in plan.completed_bars(rows, now)] == [T0, T0 + dt.timedelta(minutes=5)]


# ------------------------------------------------------------------ following
def test_the_four_stages_are_taken_in_turn(conn, sent):
    out, send = sent
    p = gold(conn)
    bars = plan.completed_bars([row(0, 4001, 4012, 4001, 4011), row(5, 4011, 4025, 4010.5, 4024),
                                row(10, 4024, 4029, 4021, 4028), row(15, 4028, 4041, 4027, 4040)], T0 + dt.timedelta(hours=1))
    assert plan.track_plan(conn, p, bars, send) == 4
    assert [(n.kind, n.k, n.price, n.stop) for n in out] == [
        ("tp", 1, 4010, 4000), ("tp", 2, 4020, 4010), ("tp", 3, 4030, 4020), ("tp", 4, 4040, None)]
    assert [round(n.result_r, 2) for n in out] == [0.25, 0.75, 1.5, 2.5]
    stored = plan.get_plan(conn, p.id)
    assert (stored.status, stored.stage, stored.remaining, stored.result_r, stored.exit_price) == (
        "closed", 4, 0.0, 2.5, 4040)
    assert plan.open_plans(conn) == []


def test_the_initial_stop_loses_one_r(conn, sent):
    out, send = sent
    p = gold(conn)
    plan.track_plan(conn, p, plan.completed_bars([row(0, 4000, 4005, 3989, 3992)], T0 + dt.timedelta(hours=1)), send)
    assert [(n.kind, n.k, n.price, n.result_r) for n in out] == [("stop", 0, 3990, -1.0)]
    assert p.status == "closed"


def test_after_tp1_the_rest_stops_at_the_entry(conn, sent):
    out, send = sent
    p = gold(conn)
    bars = plan.completed_bars([row(0, 4001, 4011, 4001, 4008), row(5, 4008, 4009, 3998, 3999)],
                               T0 + dt.timedelta(hours=1))
    plan.track_plan(conn, p, bars, send)
    assert [(n.kind, n.k, n.price) for n in out] == [("tp", 1, 4010), ("stop", 1, 4000)]
    assert out[-1].result_r == 0.25


def test_a_short_mirrors_the_long(conn, sent):
    out, send = sent
    live.set_setting(conn, "balance", 1000)
    p, _, why = plan.make(conn, plan.parse("EURUSD sell 1.1200 stop 1.1220".split()),
                          fetch=feed(row(-10, 1.12, 1.1201, 1.1199, 1.12)), now=T0)
    bars = plan.completed_bars([row(0, 1.1199, 1.1199, 1.1179, 1.1185)], T0 + dt.timedelta(hours=1))
    plan.track_plan(conn, p, bars, send)
    assert why is None and [(n.kind, n.k) for n in out] == [("tp", 1)]
    assert out[0].price == pytest.approx(1.1180) and out[0].stop == pytest.approx(1.12)


def test_a_future_is_read_with_its_offset(conn, sent):
    out, send = sent
    live.set_setting(conn, "balance", 1000)
    p, _, _ = plan.make(conn, plan.parse("XAUUSD buy 4000 stop 3990".split()),
                        fetch=feed(row(-10, 3980, 3981, 3979, 3980)), now=T0)
    plan.track_plan(conn, p, plan.completed_bars([row(0, 3981, 3991, 3981, 3990)], T0 + dt.timedelta(hours=1)), send)
    assert [(n.kind, n.k, n.price) for n in out] == [("tp", 1, 4010)]


def test_bars_already_read_are_not_read_again(conn, sent):
    out, send = sent
    p = gold(conn)
    bars = plan.completed_bars([row(-10, 4000, 4050, 3950, 4000), row(0, 4001, 4011, 4001, 4008)],
                               T0 + dt.timedelta(hours=1))
    assert plan.track_plan(conn, p, bars, send) == 1 and plan.track_plan(conn, p, bars, send) == 0
    assert len(out) == 1


def test_a_refused_message_leaves_the_bar_to_be_read_again(conn, sent):
    out, send = sent
    p = gold(conn)
    bars = plan.completed_bars([row(0, 4011, 4021, 4011, 4020)], T0 + dt.timedelta(hours=1))
    calls = []
    assert plan.track_plan(conn, p, bars, lambda n: calls.append(n) or len(calls) < 2) == 1
    stored = plan.get_plan(conn, p.id)
    assert (stored.stage, stored.stop, stored.last_ts) == (0, 3990, (T0 - dt.timedelta(minutes=10)).isoformat())
    assert plan.track_plan(conn, plan.get_plan(conn, p.id), bars, send) == 2
    assert plan.get_plan(conn, p.id).stage == 2


def test_the_pass_fetches_each_symbol_once_and_nothing_with_no_plan(conn, sent):
    out, send = sent
    asked = []

    def fetch(symbol):
        asked.append(symbol)
        return [row(0, 4001, 4011, 4001, 4008)]

    assert plan.run(conn, fetch=fetch, send=send, now=T0 + dt.timedelta(hours=1)) == 0 and asked == []
    gold(conn)
    gold(conn)
    assert plan.run(conn, fetch=fetch, send=send, now=T0 + dt.timedelta(hours=1)) == 2 and asked == ["GC=F"]


def test_one_symbol_failing_does_not_stop_the_pass(conn, sent, capsys):
    out, send = sent
    gold(conn)
    plan.make(conn, plan.parse("EURUSD buy 1.12 stop 1.118".split()),
              fetch=feed(row(-10, 1.12, 1.12, 1.12, 1.12)), now=T0)

    def fetch(symbol):
        if symbol == "GC=F":
            raise RuntimeError("offline")
        return [row(0, 1.1201, 1.1221, 1.1201, 1.122)]

    assert plan.run(conn, fetch=fetch, send=send, now=T0 + dt.timedelta(hours=1)) == 1
    assert "tracking failed" in capsys.readouterr().err


# ------------------------------------------------------------------ the messages
def test_the_answer_to_a_plan(conn):
    p = gold(conn)
    text = tn.format_cfd_plan(p, live.get_settings(conn), html=False)
    assert text == ("🟢 XAUUSD!: покупка по 4 000,00 — стоп 3 990,00; TP1 4 010,00 · TP2 4 020,00 · "
                    "TP3 4 030,00 · TP4 4 040,00; риск 1% = €10,00, объём 1,25 унц.\n"
                    "На каждой цели закрывается четверть; стоп после TP1 — на вход, после TP2 — на TP1, "
                    "после TP3 — на TP2.\n"
                    "№1 — слежу за ценой, напишу на каждой цели и на стопе. /cfd cancel 1 — перестать.")


def test_the_answer_names_the_future_and_why_a_plan_is_not_followed(conn):
    made, per_1000, _ = plan.make(conn, plan.parse("XAUUSD sell 4000 stop 4010".split()),
                                  fetch=feed(row(-10, 3980, 3981, 3979, 3980)), now=T0)
    text = tn.format_cfd_plan(made, live.get_settings(conn), qty_per_1000=per_1000, html=False)
    assert text.startswith("🔴 XAUUSD!: продажа по 4 000,00 — стоп 4 010,00; TP1 3 990,00")
    assert "риск 1%, объём на €1 000 баланса: 1,25 унц. (/cfd balance — задать свой)" in text
    assert text.endswith("Цены — по фьючерсу GC=F, поправка к вашей цене +20,00.")
    made.id, made.offset = 0, 0.0
    assert tn.format_cfd_plan(made, live.get_settings(conn), why_not="цена уже за стопом",
                              html=False).endswith("\nНе отслеживаю: цена уже за стопом.")


def test_an_fx_price_has_five_decimals_and_a_coin_is_counted_in_coins(conn):
    live.set_setting(conn, "balance", 1000)
    eur, _, _ = plan.make(conn, plan.parse("EURUSD buy 1.12 stop 1.118".split()), fetch=feed(), now=T0)
    sol, _, _ = plan.make(conn, plan.parse("SOL buy 200 stop 190".split()), fetch=feed(), now=T0)
    assert "покупка по 1,12000 — стоп 1,11800; TP1 1,12200" in tn.format_cfd_plan(eur, live.get_settings(conn), html=False)
    assert "объём 6 200 ед." in tn.format_cfd_plan(eur, live.get_settings(conn), html=False)
    assert "объём 1,25 SOL" in tn.format_cfd_plan(sol, live.get_settings(conn), html=False)


def test_the_messages_of_the_stages_and_of_the_stops(conn, sent):
    out, send = sent
    p = gold(conn)
    bars = plan.completed_bars([row(0, 4001, 4012, 4001, 4011), row(5, 4011, 4025, 4010.5, 4024),
                                row(10, 4024, 4029, 4021, 4028), row(15, 4028, 4041, 4027, 4040)], T0 + dt.timedelta(hours=1))
    plan.track_plan(conn, p, bars, send)
    texts = [tn.format_cfd_plan_notice(n, html=False) for n in out]
    assert texts[0] == "🟢 XAUUSD!: взят TP1 4 010,00 — закройте четверть, стоп на 4 000,00, закрыто на +€2,50 (+0,25R)"
    assert texts[3] == "🟢 XAUUSD!: взят TP4 4 040,00 — сделка закрыта, итог +€25,00 (+2,50R)"

    out.clear()
    q = gold(conn)
    plan.track_plan(conn, q, plan.completed_bars([row(0, 4000, 4001, 3989, 3990)], T0 + dt.timedelta(hours=1)), send)
    assert tn.format_cfd_plan_notice(out[0], html=False) == (
        "🔴 XAUUSD!: сработал исходный стоп — закрыто по 3 990,00, итог −€10,00 (−1,00R)")

    out.clear()
    q = gold(conn)
    bars = plan.completed_bars([row(0, 4001, 4021, 4001, 4020), row(5, 4020, 4021, 4009, 4010)],
                               T0 + dt.timedelta(hours=1))
    plan.track_plan(conn, q, bars, send)
    assert tn.format_cfd_plan_notice(out[-1], html=False) == (
        "🟢 XAUUSD!: сработал стоп на TP1 — закрыто по 4 010,00, итог +€12,50 (+1,25R)")


def test_a_result_with_no_balance_is_in_r(conn, sent):
    out, send = sent
    p, _, _ = plan.make(conn, plan.parse("XAUUSD buy 4000 stop 3990".split()),
                        fetch=feed(row(-10, 4000, 4001, 3999, 4000)), now=T0)
    plan.track_plan(conn, p, plan.completed_bars([row(0, 4000, 4001, 3989, 3990)], T0 + dt.timedelta(hours=1)), send)
    assert tn.format_cfd_plan_notice(out[0], html=False).endswith("итог −1,00R")


# ------------------------------------------------------------------ the commands
def test_the_plan_command_answers_and_keeps_the_trade(conn, monkeypatch):
    monkeypatch.setattr(plan, "yahoo_intraday", feed(row(-10, 4000, 4001, 3999, 4000)))
    monkeypatch.setattr(plan, "_now", lambda: T0)
    text = live.handle_command(conn, "/cfd plan gold buy 4000 stop 3990")
    assert text.startswith("🟢 <b>XAUUSD!</b>: покупка по 4 000,00") and len(plan.open_plans(conn)) == 1


def test_a_bad_plan_gets_the_usage_line(conn):
    assert live.handle_command(conn, "/cfd plan gold buy") == plan.USAGE["plan"]
    assert plan.USAGE["plan"] in live.usage_text() and plan.USAGE["cancel"] in live.usage_text()


def test_cancel_stops_following(conn, sent):
    out, send = sent
    p = gold(conn)
    assert live.handle_command(conn, f"/cfd cancel {p.id}") == "Сделка №1 (XAUUSD) больше не отслеживается."
    assert plan.open_plans(conn) == [] and plan.get_plan(conn, p.id).status == "cancelled"
    assert live.handle_command(conn, f"/cfd cancel {p.id}") == "Сделка №1 уже не отслеживается."
    assert live.handle_command(conn, "/cfd cancel 99") == plan.USAGE["cancel"]
    assert live.handle_command(conn, "/cfd cancel x") == plan.USAGE["cancel"]


def test_the_view_lists_the_trades_only_when_there_are_some(conn, sent):
    out, send = sent
    assert "Мои сделки" not in live.status_text(conn, html=False)
    p = gold(conn)
    q = gold(conn)
    plan.track_plan(conn, q, plan.completed_bars([row(0, 4000, 4001, 3989, 3990)], T0 + dt.timedelta(hours=1)), send)
    assert live.status_text(conn, html=False).endswith(
        "\n\nМои сделки (/cfd plan)\n🟢 №1 XAUUSD — покупка по 4 000,00, стоп 3 990,00, целей пока нет\n"
        "Закрытые: 1, всего −1,00R (−€10,00)")


# ------------------------------------------------------------------ the Telegram agent's loop
def test_the_loop_follows_the_plans_every_five_minutes(conn, monkeypatch):
    passes = []
    monkeypatch.setattr(tb.cfd_plan, "run", lambda c: passes.append(1))
    monkeypatch.setattr(tb, "_sync_t212", lambda c: tb.T212_SYNC_SECONDS)
    monkeypatch.setattr(tb, "_poll_once", lambda *a: None)
    it = iter([0, 0, 0, 100, 301])                        # two reads at the start, then one a round
    tb._serve(conn, "t", "1", None, clock=lambda: next(it), rounds=3)
    assert len(passes) == 2                               # at 0 and at 301, not at 100


def test_a_failing_pass_does_not_stop_the_loop(conn, monkeypatch, capsys):
    def boom(c):
        raise RuntimeError("x")
    monkeypatch.setattr(tb.cfd_plan, "run", boom)
    tb._track_cfd_plans(conn)
    assert "CFD plans failed: RuntimeError" in capsys.readouterr().err
