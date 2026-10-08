"""cfd/live.py, the scanner and the sizing (spec 2026-10-08-cfd-live-crypto-breakout.md, "Daily pass" 2 and
"Sizing"): a signal is read at the close of the last completed daily bar only; the reference entry is the
open of the forming bar (the signal close when it is missing); the skip rules are the backtest's; one open
signal per coin and none twice for a coin and a date; the quantity is the risk over R in euro, rounded down."""
from __future__ import annotations

import datetime as dt

import pytest

from cfd import live
from cfd import setups
from cfd.data import Bar

UTC = dt.timezone.utc
DAY = dt.timedelta(days=1)
TODAY = dt.date(2026, 10, 8)                 # the forming bar's day; the signal bar is 2026-10-07
K = 400.0


def calm(n, end=TODAY - DAY, level=100.0):
    """n bars ending on `end`: close at `level`, range 99-101, so every true range is 2."""
    first = dt.datetime.combine(end, dt.time(), tzinfo=UTC) - (n - 1) * DAY
    return [Bar(first + i * DAY, level, level + 1, level - 1, level) for i in range(n)]


def with_last(bars, o, h, low, c):
    last = bars[-1]
    return bars[:-1] + [Bar(last.ts, o, h, low, c)]


def breakout(n=330, end=TODAY - DAY):
    """Calm, then a bar that closes at 110: above the 55-bar high (101) and the 200-day average."""
    return with_last(calm(n, end=end), 100.0, 111.0, 99.0, 110.0)


def flip(bars):
    return [Bar(b.ts, K - b.open, K - b.low, K - b.high, K - b.close) for b in bars]


def rows(bars, forming=None):
    out = [(b.ts.date().isoformat(), b.open, b.high, b.low, b.close) for b in bars]
    if forming is not None:
        o = forming
        out.append((TODAY.isoformat(), o, o + 0.3, o - 0.3, o + 0.1))         # the bar still forming
    return out


def fetcher(by_symbol, default=None, fail=()):
    """fetch(symbol, interval) -> rows: `by_symbol` has the rows of the coins that matter, the others
    get `default` (calm, no signal)."""
    calls = []

    def fetch(symbol, interval):
        calls.append((symbol, interval))
        if symbol in fail:
            raise OSError("yahoo is down")
        return by_symbol.get(symbol, default if default is not None else rows(calm(330), forming=100.0))
    fetch.calls = calls
    return fetch


class Outbox:
    def __init__(self, ok=True):
        self.notices, self.ok = [], ok

    def __call__(self, notice):
        if self.ok:
            self.notices.append(notice)
        return self.ok


def scan(conn, fetch, out=None, settings=None):
    out = out if out is not None else Outbox()
    n = live.scan_all(conn, out, fetch=fetch, today=TODAY, settings=settings or live.get_settings(conn))
    return n, out


# ------------------------------------------------------------------ detection
def test_a_breakout_on_the_last_completed_bar_makes_a_long_signal_entered_at_the_forming_open(conn):
    n, out = scan(conn, fetcher({"SOL-USD": rows(breakout(), forming=110.5)}))
    assert n == 1
    (sig,) = live.open_signals(conn)
    assert (sig.coin, sig.symbol, sig.side, sig.signal_date) == ("SOL", "SOL-USD", "long", "2026-10-07")
    bars = breakout()
    expected = setups.bo_d(bars)[-1]
    assert expected.index == len(bars) - 1
    assert sig.entry == 110.5 and sig.stop0 == pytest.approx(expected.stop) and sig.stop == sig.stop0
    assert sig.r == pytest.approx(110.5 - expected.stop)
    assert (sig.checkpoint, sig.status, sig.last_bar) == (0, "open", "2026-10-07")
    (notice,) = out.notices
    assert notice.kind == "entry" and notice.signal.coin == "SOL"


def test_a_short_is_the_mirror(conn):
    scan(conn, fetcher({"ETH-USD": rows(flip(breakout()), forming=K - 110.5)}))
    (sig,) = live.open_signals(conn)
    assert (sig.coin, sig.side, sig.entry) == ("ETH", "short", K - 110.5)
    assert sig.stop0 > sig.entry and sig.r == pytest.approx(sig.stop0 - sig.entry)


def test_without_the_forming_bar_the_entry_is_the_signal_close(conn):
    scan(conn, fetcher({"BTC-USD": rows(breakout())}))
    (sig,) = live.open_signals(conn)
    assert sig.entry == 110.0


def test_every_one_of_the_thirteen_coins_is_scanned_on_daily_bars(conn):
    fetch = fetcher({})
    scan(conn, fetch)
    assert [s for s, i in fetch.calls] == ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "BNB-USD", "DOGE-USD",
                                          "AVAX-USD", "LTC-USD", "LINK-USD", "TRX-USD", "ENA-USD",
                                          "HYPE32196-USD", "SUI20947-USD"]
    assert {i for s, i in fetch.calls} == {"1d"}
    assert live.open_signals(conn) == []


def test_a_breakout_earlier_than_the_last_completed_bar_is_not_a_signal(conn):
    late = breakout(329, end=TODAY - 2 * DAY)                            # the breakout bar is two days old
    after = Bar(late[-1].ts + DAY, 110.0, 111.0, 109.0, 110.0)           # yesterday: 110 again, not above 111
    series = late + [after]
    assert setups.bo_d(series)[-1].index == len(series) - 2              # it was a signal bar, a day ago
    scan(conn, fetcher({"SOL-USD": rows(series, forming=110.0)}))
    assert live.open_signals(conn) == []


def test_a_series_that_ends_before_yesterday_is_stale_and_gives_no_signal(conn):
    stale = breakout()
    stale = [Bar(b.ts - DAY, b.open, b.high, b.low, b.close) for b in stale]        # ends the day before yesterday
    scan(conn, fetcher({"SOL-USD": rows(stale)}))
    assert live.open_signals(conn) == []


def test_the_forming_bar_is_never_the_signal_bar(conn):
    """A forming bar that looks like a breakout is not read: only completed bars are."""
    bars = calm(330)
    fetch = fetcher({"SOL-USD": rows(bars) + [(TODAY.isoformat(), 100.0, 130.0, 99.0, 125.0)]})
    scan(conn, fetch)
    assert live.open_signals(conn) == []


def test_the_coins_wick_threshold_is_sixty_percent(conn):
    """A completed bar whose low is 40 % under its close is kept (the 15 % rule would drop it), so the
    breakout bar with such a wick is read."""
    bars = with_last(calm(330), 100.0, 111.0, 66.0, 110.0)               # low 40 % under the close
    scan(conn, fetcher({"SOL-USD": rows(bars, forming=110.5)}))
    assert len(live.open_signals(conn)) == 1


# ------------------------------------------------------------------ the skip rules
def test_skipped_when_r_is_a_quarter_atr_or_less(conn):
    bars = breakout()
    stop = setups.bo_d(bars)[-1].stop
    scan(conn, fetcher({"SOL-USD": rows(bars, forming=stop + 0.1)}))          # the open is at the stop level
    assert live.open_signals(conn) == []


def test_skipped_when_r_is_more_than_six_atr(conn):
    bars = breakout()
    sig = setups.bo_d(bars)[-1]
    forming = sig.stop + 6.01 * sig.atr
    scan(conn, fetcher({"SOL-USD": rows(bars, forming=forming)}))
    assert live.open_signals(conn) == []
    scan(conn, fetcher({"SOL-USD": rows(bars, forming=sig.stop + 5.99 * sig.atr)}))
    assert len(live.open_signals(conn)) == 1


# ------------------------------------------------------------------ one per coin
def test_one_open_signal_per_coin(conn):
    live.insert_signal(conn, coin="SOL", symbol="SOL-USD", side="long", signal_date="2026-10-01", entry=100.0,
                       stop0=90.0, r=10.0, last_bar="2026-10-06")
    n, out = scan(conn, fetcher({"SOL-USD": rows(breakout(), forming=110.5),
                                 "XRP-USD": rows(breakout(), forming=110.5)}))
    assert [s.coin for s in live.open_signals(conn)] == ["SOL", "XRP"] and n == 1
    assert out.notices[0].signal.coin == "XRP"


def test_no_duplicate_on_a_second_run_the_same_day(conn):
    fetch = fetcher({"SOL-USD": rows(breakout(), forming=110.5)})
    scan(conn, fetch)
    n, out = scan(conn, fetch)
    assert n == 0 and out.notices == [] and len(live.all_signals(conn)) == 1


def test_no_duplicate_when_that_signal_has_already_closed(conn):
    fetch = fetcher({"SOL-USD": rows(breakout(), forming=110.5)})
    scan(conn, fetch)
    (sig,) = live.open_signals(conn)
    live.update_signal(conn, sig.id, status="closed", closed_date="2026-10-07", exit_price=100.0, result_r=-1.0)
    n, _ = scan(conn, fetch)
    assert n == 0 and len(live.all_signals(conn)) == 1


def test_a_coin_that_fails_is_logged_and_the_others_go_on(conn, capsys):
    fetch = fetcher({"XRP-USD": rows(breakout(), forming=110.5)}, fail={"BTC-USD", "SOL-USD"})
    n, out = scan(conn, fetch)
    assert n == 1 and [s.coin for s in live.open_signals(conn)] == ["XRP"]
    err = capsys.readouterr().err
    assert "BTC" in err and "SOL" in err and "OSError" in err


def test_a_failed_send_stores_nothing_and_the_next_run_makes_the_signal(conn):
    fetch = fetcher({"SOL-USD": rows(breakout(), forming=110.5)})
    n, out = scan(conn, fetch, Outbox(ok=False))
    assert n == 0 and live.all_signals(conn) == []
    n, out = scan(conn, fetch)
    assert n == 1 and len(live.open_signals(conn)) == 1


# ------------------------------------------------------------------ sizing
def test_the_sizing_is_stored_with_a_balance(conn):
    live.set_setting(conn, "balance", 500.0)
    scan(conn, fetcher({"SOL-USD": rows(breakout(), forming=110.5)}))
    (sig,) = live.open_signals(conn)
    assert sig.risk_pct == 1.0 and sig.risk_eur == 5.0
    assert sig.qty == live.size_qty(5.0, sig.r, 110.5, conn)
    assert sig.qty > 0


def test_nothing_is_stored_without_a_balance_but_the_notice_has_the_quantity_for_a_thousand(conn):
    n, out = scan(conn, fetcher({"SOL-USD": rows(breakout(), forming=110.5)}))
    (sig,) = live.open_signals(conn)
    assert (sig.risk_pct, sig.risk_eur, sig.qty) == (None, None, None)
    (notice,) = out.notices
    assert notice.qty_per_1000 == live.size_qty(10.0, sig.r, 110.5, conn)
    assert notice.signal.risk_eur is None


def test_with_a_balance_the_notice_has_no_per_thousand_figure(conn):
    live.set_setting(conn, "balance", 1000.0)
    _, out = scan(conn, fetcher({"SOL-USD": rows(breakout(), forming=110.5)}))
    assert out.notices[0].qty_per_1000 is None


def test_the_quantity_is_the_risk_over_r_in_euro(conn):
    # a rate of 1.16 dollars per euro: R = $11.60 is €10.00
    assert live.size_qty(5.0, 11.6, 121.5, conn) == 0.5


@pytest.mark.parametrize("risk_eur, r_usd, price, expected", [
    (5.0, 13.3, 121.5, 0.436),          # above 100: down to 0.0001 (0.43609 -- not up to 0.4361)
    (5.0, 11.6, 100.0, 0.5),            # exactly 100 is not above it: hundredths
    (5.579, 1.16, 50.0, 5.57),          # from 1 to 100: down to 0.01
    (5.0, 0.0116, 0.2, 500.0),          # under 1: whole coins
    (5.99, 0.0116, 0.2, 599.0),
    (0.0001, 1160.0, 60000.0, 0.0),     # smaller than a step: nothing
])
def test_the_quantity_rounds_down(conn, risk_eur, r_usd, price, expected):
    assert live.size_qty(risk_eur, r_usd, price, conn) == expected


def test_the_rounding_step_by_price():
    assert live.qty_step(60000.0) == 0.0001 and live.qty_step(100.01) == 0.0001
    assert live.qty_step(100.0) == 0.01 and live.qty_step(1.0) == 0.01
    assert live.qty_step(0.999) == 1.0


# ------------------------------------------------------------------ the open-risk limit
def test_over_the_limit_the_signal_is_still_made_and_the_notice_says_so(conn):
    live.set_setting(conn, "balance", 500.0)
    live.set_setting(conn, "risk", 2.0)
    live.set_setting(conn, "maxrisk", 3.0)
    live.insert_signal(conn, coin="BTC", symbol="BTC-USD", side="long", signal_date="2026-10-01", entry=100.0,
                       stop0=90.0, r=10.0, last_bar="2026-10-06", risk_pct=2.0, risk_eur=10.0, qty=1.0)
    _, out = scan(conn, fetcher({"SOL-USD": rows(breakout(), forming=110.5)}))
    assert len(live.open_signals(conn)) == 2
    assert out.notices[0].over_limit == (4.0, 3.0)


def test_at_the_limit_there_is_no_note(conn):
    live.set_setting(conn, "balance", 500.0)
    live.set_setting(conn, "risk", 1.0)
    live.set_setting(conn, "maxrisk", 3.0)
    for coin, d in (("BTC", "2026-10-01"), ("ETH", "2026-10-02")):
        live.insert_signal(conn, coin=coin, symbol=f"{coin}-USD", side="long", signal_date=d, entry=100.0,
                           stop0=90.0, r=10.0, last_bar="2026-10-06", risk_pct=1.0, risk_eur=5.0, qty=1.0)
    _, out = scan(conn, fetcher({"SOL-USD": rows(breakout(), forming=110.5)}))
    assert out.notices[0].over_limit is None                          # 3 % is not above 3 %


def test_open_risk_counts_a_signal_without_a_balance_at_the_current_risk_percent(conn):
    live.set_setting(conn, "risk", 1.5)
    live.insert_signal(conn, coin="BTC", symbol="BTC-USD", side="long", signal_date="2026-10-01", entry=100.0,
                       stop0=90.0, r=10.0, last_bar="2026-10-06")
    live.insert_signal(conn, coin="ETH", symbol="ETH-USD", side="long", signal_date="2026-10-01", entry=100.0,
                       stop0=90.0, r=10.0, last_bar="2026-10-06", risk_pct=0.5, risk_eur=2.5, qty=1.0)
    assert live.open_risk_pct(conn, live.get_settings(conn)) == 2.0


def test_the_open_risk_note_works_without_a_balance_too(conn):
    live.set_setting(conn, "maxrisk", 1.0)
    _, out = scan(conn, fetcher({"SOL-USD": rows(breakout(), forming=110.5)}))
    assert out.notices[0].over_limit is None                          # 1 % alone is not above 1 %
    _, out = scan(conn, fetcher({"XRP-USD": rows(breakout(), forming=110.5)}))
    assert out.notices[0].over_limit == (2.0, 1.0)
