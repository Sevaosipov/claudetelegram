"""cfd/live.py, the tracker (spec 2026-10-08-cfd-live-crypto-breakout.md, "Daily pass" 1): the live exit is
the backtest's. A stored signal is fed the completed bars after `last_bar`, one at a time, through the same
LadderState as exits.simulate(..., E0): the exit price and the result in R must be the backtest's, on crafted
series (long and short, a gap through the stop, a stop and a checkpoint in one bar) and on many random ones.
The checkpoints are marks, not exits. A failed send leaves the row where it was."""
from __future__ import annotations

import datetime as dt
import random

import pytest

from cfd import exits, live
from cfd import indicators as ind
from cfd import instruments as ins
from cfd.data import Bar
from cfd.setups import Signal

UTC = dt.timezone.utc
DAY = dt.timedelta(days=1)
D0 = dt.datetime(2025, 1, 1, tzinfo=UTC)
K = 400.0
SIG = 29                                    # the index of the signal bar in the series below


def date_of(i):
    return (D0 + i * DAY).date().isoformat()


def flat(n):
    """n bars at 100 with a range of 2 and no gaps: every true range is 2."""
    return [Bar(D0 + i * DAY, 100.0, 101.0, 99.0, 100.0) for i in range(n)]


def series(tail_rows, n=SIG + 1):
    """30 flat bars (the signal bar is the last of them) and then (o, h, l, c) rows."""
    bars = flat(n)
    for j, (o, h, low, c) in enumerate(tail_rows):
        bars.append(Bar(D0 + (n + j) * DAY, float(o), float(h), float(low), float(c)))
    return bars


def flip(bars):
    return [Bar(b.ts, K - b.open, K - b.low, K - b.high, K - b.close) for b in bars]


def backtest(bars, side, sig_index=SIG, coin="SOL-USD"):
    """exits.simulate of the BO-D signal at sig_index: (Signal, Trade)."""
    atr = ind.atr(bars, 14)
    close, a = bars[sig_index].close, atr[sig_index]
    stop = close - 2.5 * a if side == "long" else close + 2.5 * a
    signal = Signal(sig_index, side, stop, a)
    return signal, exits.simulate(bars, signal, exits.E0, costs=ins.by_symbol(coin).costs, atr=atr)


def store(conn, bars, signal, trade, coin="SOL"):
    return live.insert_signal(conn, coin=coin, symbol=f"{coin}-USD", side=signal.side,
                              signal_date=date_of(signal.index), entry=trade.entry_price,
                              stop0=signal.stop, r=abs(trade.entry_price - signal.stop),
                              last_bar=date_of(signal.index))


class Outbox:
    """send(): records every notice; `fail_on` is a set of 0-based positions that are refused."""

    def __init__(self, fail_on=()):
        self.notices, self.attempts, self.fail_on = [], 0, set(fail_on)

    def __call__(self, notice):
        self.attempts += 1
        if self.attempts - 1 in self.fail_on:
            return False
        self.notices.append(notice)
        return True

    def kinds(self):
        return [(n.kind, n.k) if n.kind == "checkpoint" else n.kind for n in self.notices]


def replay(conn, bars, side, **kw):
    signal, trade = backtest(bars, side, **kw)
    sid = store(conn, bars, signal, trade)
    out = Outbox()
    live.track_signal(conn, live.get_signal(conn, sid), bars, out)
    return live.get_signal(conn, sid), trade, out


# ------------------------------------------------------------------ the live exit is the backtest's
LONG_RUN = [(100, 104, 99, 103), (103, 106, 102, 105), (105, 111, 104, 110), (110, 112, 100, 101),
            (101, 102, 99, 100)]
GAP_THROUGH = [(100, 104, 99, 103), (103, 107, 102, 106), (106, 106.5, 105, 106), (90, 91, 88, 89),
               (89, 90, 88, 89)]
STOP_AND_CHECKPOINT = [(100, 104, 99, 103), (103, 108, 94, 96), (96, 97, 95, 96)]   # TP1 and the stop in a bar


@pytest.mark.parametrize("rows", [LONG_RUN, GAP_THROUGH, STOP_AND_CHECKPOINT],
                         ids=["trail", "gap", "stop_and_checkpoint"])
@pytest.mark.parametrize("side", ["long", "short"])
def test_replayed_bar_by_bar_the_exit_and_r_are_the_backtests(conn, rows, side):
    bars = series(rows)
    bars = bars if side == "long" else flip(bars)
    sig, trade, out = replay(conn, bars, side)
    assert trade.closed
    assert sig.status == "closed"
    assert sig.exit_price == pytest.approx(trade.parts[-1].price)
    assert sig.result_r == pytest.approx(trade.result_r)
    assert sig.closed_date == date_of(trade.exit_index)
    assert out.kinds()[-1] == "close"
    assert out.notices[-1].price == pytest.approx(trade.parts[-1].price)
    assert out.notices[-1].result_r == pytest.approx(trade.result_r)


def test_a_gap_through_the_stop_closes_at_the_open(conn):
    sig, trade, out = replay(conn, series(GAP_THROUGH), "long")
    assert trade.parts[-1].gap and sig.exit_price == 90.0


def test_a_stop_and_a_checkpoint_in_one_bar_the_stop_goes_first(conn):
    """The bar reaches TP1 (108 >= 105) and trades down through the stop (94 <= 95): it is a stop, and TP1
    is not reported (the backtest's bar order)."""
    sig, trade, out = replay(conn, series(STOP_AND_CHECKPOINT), "long")
    assert out.kinds() == ["close"] and sig.checkpoint == 0
    assert sig.exit_price == pytest.approx(trade.parts[-1].price)


def test_unless_the_bar_opened_beyond_the_checkpoint(conn):
    """A bar that opens above TP1 and then falls through the stop: TP1 was reached at the open."""
    bars = series([(100, 104, 99, 103), (108, 109, 90, 92), (92, 93, 90, 91)])
    sig, trade, out = replay(conn, bars, "long")
    assert out.kinds() == [("checkpoint", 1), "close"]
    assert sig.checkpoint == 1 and sig.status == "closed"
    assert sig.exit_price == pytest.approx(trade.parts[-1].price)


@pytest.mark.parametrize("seed", range(150))
def test_random_series_agree_with_the_backtest_on_every_closed_trade(conn, seed):
    rng = random.Random(seed)
    side = rng.choice(["long", "short"])
    bars, price = [], 100.0
    for i in range(80):
        o = price * (1 + rng.gauss(0, 0.01))
        c = o * (1 + rng.gauss(0.002 if side == "long" else -0.002, 0.03))
        h, low = max(o, c) * (1 + abs(rng.gauss(0, 0.01))), min(o, c) * (1 - abs(rng.gauss(0, 0.01)))
        bars.append(Bar(D0 + i * DAY, o, h, low, c))
        price = c
    signal, trade = backtest(bars, side, sig_index=40)
    if not trade.closed:
        return          # skipped by the R rules or still open: nothing to compare (the guard below counts them)
    sid = store(conn, bars, signal, trade)
    live.track_signal(conn, live.get_signal(conn, sid), bars, Outbox())
    sig = live.get_signal(conn, sid)
    assert sig.status == "closed"
    assert sig.exit_price == pytest.approx(trade.parts[-1].price)
    assert sig.result_r == pytest.approx(trade.result_r)


def test_enough_random_series_actually_close():
    """Guards the test above against passing by skipping: most of them end in a closed trade."""
    closed = 0
    for seed in range(150):
        rng = random.Random(seed)
        side = rng.choice(["long", "short"])
        bars, price = [], 100.0
        for i in range(80):
            o = price * (1 + rng.gauss(0, 0.01))
            c = o * (1 + rng.gauss(0.002 if side == "long" else -0.002, 0.03))
            h, low = max(o, c) * (1 + abs(rng.gauss(0, 0.01))), min(o, c) * (1 - abs(rng.gauss(0, 0.01)))
            bars.append(Bar(D0 + i * DAY, o, h, low, c))
            price = c
        closed += backtest(bars, side, sig_index=40)[1].closed
    assert closed >= 60


def test_the_cost_in_r_is_the_coins_own(conn):
    """0.40 % round trip and 0.06 % a night for an alt, 0.25 % for bitcoin: the backtest's costs."""
    bars = series(LONG_RUN)
    for coin, symbol in (("SOL", "SOL-USD"), ("BTC", "BTC-USD")):
        signal, trade = backtest(bars, "long", coin=symbol)
        sid = store(conn, bars, signal, trade, coin=coin)
        live.track_signal(conn, live.get_signal(conn, sid), bars, Outbox())
        assert live.get_signal(conn, sid).result_r == pytest.approx(trade.result_r)
    sol, btc = live.closed_signals(conn)
    assert sol.result_r < btc.result_r


# ------------------------------------------------------------------ the checkpoints
CLIMB = [(100, 104, 99, 103),            # below TP1 (105); stop 98.x
         (103, 106, 102, 105),           # TP1
         (105, 111, 104, 110),           # TP2
         (110, 112, 109, 111)]           # nothing new


def test_each_checkpoint_is_reported_once_with_the_stop_after_the_bar(conn):
    bars = series(CLIMB)
    signal, trade = backtest(bars, "long")
    sid = store(conn, bars, signal, trade)
    out = Outbox()
    live.track_signal(conn, live.get_signal(conn, sid), bars, out)
    assert out.kinds() == [("checkpoint", 1), ("checkpoint", 2)]
    sig = live.get_signal(conn, sid)
    assert (sig.checkpoint, sig.status, sig.last_bar) == (2, "open", date_of(SIG + 4))
    r = sig.r
    assert r == pytest.approx(5.0)                       # entry 100, stop 95
    one, two = out.notices
    assert one.level == pytest.approx(105.0) and two.level == pytest.approx(110.0)
    atr = ind.atr(bars, 14)
    stop = signal.stop
    for i in range(SIG + 1, SIG + 3):
        stop = max(stop, bars[i].high - 3 * atr[i])
    assert one.stop == pytest.approx(max(signal.stop, bars[SIG + 2].high - 3 * atr[SIG + 2],
                                         bars[SIG + 1].high - 3 * atr[SIG + 1]))
    assert two.stop == pytest.approx(max(stop, bars[SIG + 3].high - 3 * atr[SIG + 3]))
    assert sig.stop == pytest.approx(max(two.stop, bars[SIG + 4].high - 3 * atr[SIG + 4]))


def test_the_checkpoints_never_close_the_position(conn):
    bars = series([(100, 105.5, 99, 105), (105, 121, 104, 120), (120, 130, 119, 129)])   # TP1..TP4 and beyond
    signal, trade = backtest(bars, "long")
    sid = store(conn, bars, signal, trade)
    out = Outbox()
    live.track_signal(conn, live.get_signal(conn, sid), bars, out)
    assert out.kinds() == [("checkpoint", k) for k in (1, 2, 3, 4)]
    sig = live.get_signal(conn, sid)
    assert sig.status == "open" and sig.checkpoint == 4 and sig.exit_price is None


def test_one_bar_can_report_several_checkpoints_in_order(conn):
    bars = series([(100, 116, 99, 115)])
    signal, trade = backtest(bars, "long")
    sid = store(conn, bars, signal, trade)
    out = Outbox()
    live.track_signal(conn, live.get_signal(conn, sid), bars, out)
    assert [n.k for n in out.notices] == [1, 2, 3]
    assert len({n.stop for n in out.notices}) == 1       # one bar: one stop after it


def test_a_second_run_with_no_new_bar_sends_nothing(conn):
    bars = series(CLIMB)
    signal, trade = backtest(bars, "long")
    sid = store(conn, bars, signal, trade)
    live.track_signal(conn, live.get_signal(conn, sid), bars, Outbox())
    again = Outbox()
    live.track_signal(conn, live.get_signal(conn, sid), bars, again)
    assert again.attempts == 0


def test_new_bars_are_picked_up_where_the_last_run_stopped(conn):
    bars = series(CLIMB + [(111, 112, 105, 106), (106, 107, 80, 82)])
    signal, trade = backtest(bars, "long")
    sid = store(conn, bars, signal, trade)
    first = Outbox()
    live.track_signal(conn, live.get_signal(conn, sid), bars[:SIG + 5], first)       # the data of one day
    assert first.kinds() == [("checkpoint", 1), ("checkpoint", 2)]
    second = Outbox()
    live.track_signal(conn, live.get_signal(conn, sid), bars, second)
    assert second.kinds() == ["close"]
    sig = live.get_signal(conn, sid)
    assert sig.status == "closed" and sig.exit_price == pytest.approx(trade.parts[-1].price)
    assert sig.result_r == pytest.approx(trade.result_r)


def test_a_closed_signal_is_left_alone(conn):
    bars = series(LONG_RUN)
    sig, trade, out = replay(conn, bars, "long")
    again = Outbox()
    live.track_signal(conn, sig, bars, again)
    assert again.attempts == 0


def test_the_close_says_whether_the_stop_had_trailed(conn):
    trailed = replay(conn, series(LONG_RUN), "long")[2].notices[-1]
    conn.execute("DELETE FROM cfd_signals")
    untouched = replay(conn, series([(100, 101, 90, 91)]), "long")[2].notices[-1]
    assert trailed.trailed is True and untouched.trailed is False


# ------------------------------------------------------------------ a failed send changes nothing
def test_a_failed_checkpoint_send_leaves_the_row_and_the_next_run_retries_without_a_duplicate(conn):
    bars = series([(100, 106, 99, 105), (105, 111, 104, 110)])        # TP1 on the first bar, TP2 on the second
    signal, trade = backtest(bars, "long")
    sid = store(conn, bars, signal, trade)
    before = live.get_signal(conn, sid)
    refused = Outbox(fail_on={0})
    live.track_signal(conn, live.get_signal(conn, sid), bars, refused)
    assert live.get_signal(conn, sid) == before and refused.notices == []
    retry = Outbox()
    live.track_signal(conn, live.get_signal(conn, sid), bars, retry)
    assert retry.kinds() == [("checkpoint", 1), ("checkpoint", 2)]


def test_a_failure_between_two_messages_of_one_bar_retries_only_the_second(conn):
    bars = series([(100, 116, 99, 115)])                  # TP1, TP2, TP3 in one bar
    signal, trade = backtest(bars, "long")
    sid = store(conn, bars, signal, trade)
    first = Outbox(fail_on={1})
    live.track_signal(conn, live.get_signal(conn, sid), bars, first)
    assert [n.k for n in first.notices] == [1]
    sig = live.get_signal(conn, sid)
    assert sig.checkpoint == 1 and sig.last_bar == date_of(SIG)        # the bar is not done
    retry = Outbox()
    live.track_signal(conn, sig, bars, retry)
    assert [n.k for n in retry.notices] == [2, 3]
    assert live.get_signal(conn, sid).last_bar == date_of(SIG + 1)


def test_a_failed_close_send_leaves_the_signal_open_and_it_closes_on_the_retry(conn):
    bars = series(LONG_RUN)
    signal, trade = backtest(bars, "long")
    sid = store(conn, bars, signal, trade)
    out = Outbox(fail_on={2})                             # TP1, TP2 go; the close is refused
    live.track_signal(conn, live.get_signal(conn, sid), bars, out)
    sig = live.get_signal(conn, sid)
    assert sig.status == "open" and sig.checkpoint == 2 and sig.exit_price is None
    retry = Outbox()
    live.track_signal(conn, sig, bars, retry)
    assert retry.kinds() == ["close"]
    assert live.get_signal(conn, sid).result_r == pytest.approx(trade.result_r)


def test_a_send_that_raises_is_not_swallowed_by_the_tracker(conn):
    bars = series(CLIMB)
    signal, trade = backtest(bars, "long")
    sid = store(conn, bars, signal, trade)

    def boom(notice):
        raise RuntimeError("telegram is down")
    with pytest.raises(RuntimeError):
        live.track_signal(conn, live.get_signal(conn, sid), bars, boom)
    assert live.get_signal(conn, sid).checkpoint == 0


def test_track_all_goes_on_after_one_coin_fails(conn):
    bars_ok = series(LONG_RUN)
    signal, trade = backtest(bars_ok, "long")
    store(conn, bars_ok, signal, trade, coin="SOL")
    store(conn, bars_ok, signal, trade, coin="XRP")

    def fetch(symbol, interval):
        if symbol == "SOL-USD":
            raise OSError("yahoo is down")
        return [(b.ts.date().isoformat(), b.open, b.high, b.low, b.close) for b in bars_ok]
    out = Outbox()
    today = (D0 + 40 * DAY).date()
    live.track_all(conn, out, fetch=fetch, today=today)
    assert {n.signal.coin for n in out.notices} == {"XRP"}
    assert [(s.coin, s.status) for s in live.all_signals(conn)] == [("SOL", "open"), ("XRP", "closed")]
