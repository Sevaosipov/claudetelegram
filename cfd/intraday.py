"""cfd/intraday.py: the day league (docs/cfd/DAY_LEAGUE.md) -- three intraday ideas run forward on paper on
15-minute bars for four weeks, between 09:00 and 18:00 Europe/Riga, each trade told to Telegram as it is
made and as it ends. The bot places no order.

A pass (`run`) follows the trades that are not closed and makes the signals of the last completed bar. The
ideas are pure functions of the day's bars; the network is `fetch(symbol, "15m") -> raw rows`."""
from __future__ import annotations

import datetime as dt
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, fields
from zoneinfo import ZoneInfo

import db
from cfd import data, league
from cfd import indicators as ind
from cfd import instruments as ins
from cfd.data import Bar

UTC = dt.timezone.utc
TZ = ZoneInfo("Europe/Riga")                 # the user's clock
LONDON = ZoneInfo("Europe/London")
M15 = "15m"
BAR = dt.timedelta(minutes=15)
ENTRY_FROM, ENTRY_UNTIL, FLAT_AT = dt.time(9), dt.time(17), dt.time(18)
MARKETS = ("EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "BTCUSD", "ETHUSD")
OPEN_RANGE_MARKETS = MARKETS[:4]
IDEAS = {"OPEN-RANGE": "Open range", "BRK-15": "Retest", "ASIA-SWEEP": "Asia sweep"}
MAX_R_ATR, MAX_COST_R, MIN_TARGET_R = 4.0, 0.25, 1.0
TARGET_R = 2.0
ASIA_HOURS, SWEEP_WINDOW = 6, 3
WEEKS, MIN_TRADES = 4, 20
STALE = dt.timedelta(minutes=30)             # a last bar older than this: the market is shut or the feed late
START_KEY, QUIET_KEY, FINAL_KEY, DAY_KEY = "day_league_start", "day_league_quiet", "day_league_final", "day_league_{day}"
_FOREVER = 100 * 365 * 24 * 3600

Send = Callable[[str], bool]


# ================================================================ the table
@dataclass
class Trade:
    """A row of day_trades."""
    id: int
    idea: str
    pair: str
    symbol: str
    side: str
    ref: str
    signal_ts: str               # UTC start of the signal bar
    atr: float
    stop_price: float
    target_r: float | None       # the target that many R from the entry ...
    target_price: float | None   # ... or at this price
    status: str                  # pending | open | closed
    entry_ts: str | None = None
    entry: float | None = None
    target: float | None = None
    last_ts: str | None = None   # UTC start of the last completed bar processed
    exit_ts: str | None = None
    exit_price: float | None = None
    result_r: float | None = None
    reason: str | None = None    # stop | target | time | void


_COLUMNS = [f.name for f in fields(Trade)]


def trades(conn, where: str = "", params=()) -> list[Trade]:
    return [Trade(*row) for row in conn.execute(f"SELECT {', '.join(_COLUMNS)} FROM day_trades {where} ORDER BY id",
                                                params)]


def live_trades(conn) -> list[Trade]:
    return trades(conn, "WHERE status != 'closed'")


def closed_trades(conn) -> list[Trade]:
    return trades(conn, "WHERE status = 'closed' AND reason != 'void'")


def _save(conn, t: Trade) -> None:
    names = [n for n in _COLUMNS if n != "id"]
    conn.execute(f"UPDATE day_trades SET {', '.join(f'{n} = ?' for n in names)} WHERE id = ?",
                 (*(getattr(t, n) for n in names), t.id))
    conn.commit()


@dataclass(frozen=True)
class Signal:
    idea: str
    pair: str
    side: str
    ref: str
    stop_price: float
    target_r: float | None = None
    target_price: float | None = None


def make(conn, sig: Signal, bar: Bar, atr: float) -> Trade | None:
    """The paper trade of a signal made on `bar`, kept as `pending`; None when it was made before."""
    if conn.execute("SELECT 1 FROM day_trades WHERE idea = ? AND pair = ? AND ref = ?",
                    (sig.idea, sig.pair, sig.ref)).fetchone():
        return None
    cur = conn.execute(
        "INSERT INTO day_trades (idea, pair, symbol, side, ref, signal_ts, atr, stop_price, target_r, target_price, "
        "status, created) VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?)",
        (sig.idea, sig.pair, ins.by_name(sig.pair).symbol, sig.side, sig.ref, bar.ts.isoformat(), atr,
         sig.stop_price, sig.target_r, sig.target_price, dt.datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    return trades(conn, "WHERE id = ?", (cur.lastrowid,))[0]


# ================================================================ a trade's life
def local(ts: dt.datetime) -> dt.datetime:
    return ts.astimezone(TZ)


def _at(day: dt.date, clock: dt.time, tz=TZ) -> dt.datetime:
    return dt.datetime.combine(day, clock, tzinfo=tz).astimezone(UTC)


def cost_r(t: Trade) -> float:
    """The round trip in R."""
    return ins.by_symbol(t.symbol).costs.round_trip_pct / 100.0 * t.entry / abs(t.entry - t.stop_price)


def _close(t: Trade, ts: dt.datetime, price: float, reason: str) -> None:
    sign = 1 if t.side == "long" else -1
    t.status, t.exit_ts, t.exit_price, t.reason = "closed", ts.isoformat(), price, reason
    t.result_r = sign * (price - t.entry) / abs(t.entry - t.stop_price) - cost_r(t)


def _void(t: Trade, ts: dt.datetime, price: float) -> None:
    t.status, t.exit_ts, t.exit_price, t.reason, t.result_r = "closed", ts.isoformat(), price, "void", 0.0


def _enter(t: Trade, ts: dt.datetime, open_: float, flat: dt.datetime) -> bool:
    """Fill a pending trade at a bar's open, or void it (DAY_LEAGUE.md, «No trade»)."""
    sign = 1 if t.side == "long" else -1
    t.entry_ts, t.entry = ts.isoformat(), open_
    distance = sign * (open_ - t.stop_price)
    if ts >= flat or distance <= 0 or distance > MAX_R_ATR * t.atr or cost_r(t) > MAX_COST_R:
        _void(t, ts, open_)
        return False
    if t.target_price is not None:
        if sign * (t.target_price - open_) < MIN_TARGET_R * distance:
            _void(t, ts, open_)
            return False
        t.target = t.target_price
    else:
        t.target = open_ + sign * t.target_r * distance
    t.status = "open"
    return True


def advance(t: Trade, bars: Sequence[Bar], forming: Bar | None, now: dt.datetime) -> list[str]:
    """Move a trade through the bars it has not seen: fill a pending one at the open of the bar after its
    signal bar, then, bar by bar, the 18:00 exit at the open, a gap through the stop or the target at the
    open, the stop inside the bar, the target inside the bar. Of the forming bar only the open is known:
    it can fill, and it can end the day. Returns the events in order: "open", "close"."""
    events = []
    sign = 1 if t.side == "long" else -1
    signal_ts = dt.datetime.fromisoformat(t.signal_ts)
    flat = _at(local(signal_ts).date(), FLAT_AT)
    seen = dt.datetime.fromisoformat(t.last_ts or t.signal_ts)
    for bar, done in [(b, True) for b in bars if b.ts > seen] + ([(forming, False)] if forming else []):
        if bar.ts <= signal_ts or t.status == "closed":
            continue
        if t.status == "pending":
            if _enter(t, bar.ts, bar.open, flat):
                events.append("open")
        elif bar.ts > dt.datetime.fromisoformat(t.entry_ts):
            if bar.ts >= flat:
                _close(t, bar.ts, bar.open, "time")
            elif sign * (bar.open - t.stop_price) <= 0:
                _close(t, bar.ts, bar.open, "stop")
            elif sign * (bar.open - t.target) >= 0:
                _close(t, bar.ts, bar.open, "target")
        if done and t.status == "open":
            if sign * ((bar.low if sign > 0 else bar.high) - t.stop_price) <= 0:
                _close(t, bar.ts, t.stop_price, "stop")
            elif sign * ((bar.high if sign > 0 else bar.low) - t.target) >= 0:
                _close(t, bar.ts, t.target, "target")
        if done:
            t.last_ts = bar.ts.isoformat()
    if t.status == "pending" and now >= flat:
        _void(t, now, t.stop_price)
    elif t.status == "open" and now >= flat + STALE and bars:      # the feed stopped before 18:00: the last close
        _close(t, flat, bars[-1].close, "time")
    if t.status == "closed" and t.reason != "void":
        events.append("close")
    return events


# ================================================================ the ideas
def _of_day(bars: Sequence[Bar], start: dt.datetime, end: dt.datetime) -> list[Bar]:
    return [b for b in bars if start <= b.ts < end]


def london_break(bars: Sequence[Bar]) -> tuple[str, float, float] | None:
    """(side, high, low) of the range of London's first hour when the last bar is the first to close
    outside it; else None."""
    last = bars[-1]
    day = last.ts.astimezone(LONDON).date()
    start = _at(day, dt.time(8), LONDON)
    first = _of_day(bars, start, start + 4 * BAR)
    later = [b for b in bars if b.ts >= start + 4 * BAR]
    if len(first) < 4 or not later:
        return None
    high, low = max(b.high for b in first), min(b.low for b in first)
    if any(b.close > high or b.close < low for b in later[:-1]) or low <= last.close <= high:
        return None
    return ("long" if last.close > high else "short"), high, low


def open_range(pair: str, bars: Sequence[Bar]) -> list[Signal]:
    """Idea 1: the last bar is the first to close outside the range of London's first hour."""
    found = london_break(bars)
    if not found:
        return []
    side, high, low = found
    return [Signal("OPEN-RANGE", pair, side, local(bars[-1].ts).date().isoformat(), (high + low) / 2,
                   target_r=TARGET_R)]


def brk_15(pair: str, bars: Sequence[Bar]) -> list[Signal]:
    """Idea 2: the league's break, retest and confirming bar, on these bars."""
    day = local(bars[-1].ts).date().isoformat()
    return [Signal("BRK-15", pair, s.side, day, s.stop_price, target_r=TARGET_R)
            for s in league.brk_retest({pair: list(bars)})[:1]]


def asia_sweep(pair: str, bars: Sequence[Bar]) -> list[Signal]:
    """Idea 3: a false break of the range of the six hours before 09:00, back inside within three bars."""
    last = bars[-1]
    start = _at(local(last.ts).date(), ENTRY_FROM)
    asia = _of_day(bars, start - dt.timedelta(hours=ASIA_HOURS), start)
    day = [b for b in bars if b.ts >= start]
    if len(asia) < ASIA_HOURS * 2 or not day:               # half of its bars at least
        return []
    out = []
    for side, sign in (("long", 1), ("short", -1)):
        floor = min(sign * (b.low if sign > 0 else b.high) for b in asia)
        roof = max(sign * (b.high if sign > 0 else b.low) for b in asia)
        lo = (lambda b: sign * (b.low if sign > 0 else b.high))
        cl = (lambda b: sign * b.close)
        c = len(day) - 1
        for b in range(max(c - SWEEP_WINDOW + 1, 0), c + 1):               # the piercing bar, oldest first
            if lo(day[b]) < floor and cl(day[c]) > floor and all(cl(x) <= floor for x in day[b:c]):
                out.append(Signal("ASIA-SWEEP", pair, side, f"{local(last.ts).date().isoformat()}-{side}",
                                  sign * min(lo(x) for x in day[b:]), target_price=sign * roof))
                break
    return out


def signals(pair: str, bars: Sequence[Bar]) -> list[Signal]:
    out = []
    for name, idea in (("OPEN-RANGE", open_range), ("BRK-15", brk_15), ("ASIA-SWEEP", asia_sweep)):
        if name == "OPEN-RANGE" and pair not in OPEN_RANGE_MARKETS:
            continue
        try:
            out += idea(pair, bars)
        except Exception as e:
            print(f"[day] {name} {pair} failed: {type(e).__name__}: {e}", file=sys.stderr)
    return out


# ================================================================ the pass
def start_date(conn, today: dt.date) -> dt.date:
    stamp = db.get_cached_value(conn, START_KEY, _FOREVER)
    if stamp is None:
        db.save_cached_value(conn, START_KEY, float(today.toordinal()))
        return today
    return dt.date.fromordinal(int(stamp))


def end_date(conn, today: dt.date) -> dt.date:
    return start_date(conn, today) + dt.timedelta(weeks=WEEKS)


def quiet(conn) -> bool:
    return bool(db.get_cached_value(conn, QUIET_KEY, _FOREVER))


def _send(text: str) -> bool:
    import telegram_notify
    return bool(telegram_notify.send_text(text))


def run(conn, *, now: dt.datetime | None = None, fetch: data.Fetch | None = None,
        send: Send | None = None) -> tuple[int, int]:
    """One pass, after a 15-minute bar: on a weekday from 09:00 (Europe/Riga) until the day's trades are all
    closed. Returns (trades opened, trades closed)."""
    import telegram_notify
    now = now or dt.datetime.now(UTC)
    send = send or _send
    here = local(now)
    live = live_trades(conn)
    if not live and (here.weekday() >= 5 or not ENTRY_FROM <= here.time() < FLAT_AT):
        _boards(conn, here, send)
        return 0, 0
    if here.date() >= end_date(conn, here.date()) and not live:
        _boards(conn, here, send)
        return 0, 0
    entering = here.weekday() < 5 and here.date() < end_date(conn, here.date())
    loud = not quiet(conn)
    opened = closed = 0
    new: list[Trade] = []
    for pair in MARKETS:
        try:
            symbol = ins.by_name(pair).symbol
            mine = [t for t in live if t.symbol == symbol]
            cleaned = data.clean_rows((fetch or data.yahoo_fetch)(symbol, M15), M15,
                                      max_wick=data.MAX_WICK_CRYPTO if symbol.endswith("-USD") else None).bars
            bars = [b for b in cleaned if b.ts + BAR <= now]
            forming = next((b for b in cleaned if b.ts <= now < b.ts + BAR), None)
            if not bars:
                continue
            last = bars[-1]
            fresh = now - (last.ts + BAR) < STALE
            if entering and fresh and ENTRY_FROM <= local(last.ts + BAR).time() <= ENTRY_UNTIL \
                    and local(last.ts).date() == here.date():
                atr = ind.atr(bars, 14)[-1]
                for sig in signals(pair, bars) if atr else []:
                    t = make(conn, sig, last, atr)
                    if t is not None:
                        mine.append(t)
            for t in mine:
                events = advance(t, bars, forming, now)
                _save(conn, t)
                opened += "open" in events
                closed += "close" in events
                if "open" in events:
                    new.append(t)
                if "close" in events and loud:
                    send(telegram_notify.format_day_event(t))
        except Exception as e:
            print(f"[day] {pair}: {type(e).__name__}: {e}", file=sys.stderr)
    if new and loud:
        send(telegram_notify.format_day_opens([t for t in new if t.status == "open"] or new))
    _boards(conn, here, send)
    return opened, closed


def _boards(conn, here: dt.datetime, send: Send) -> None:
    """The scoreboard once a day, after 18:00 on a day that had trades and none is left open; the final
    one once, when the four weeks are over."""
    if live_trades(conn) or db.get_cached_value(conn, START_KEY, _FOREVER) is None:
        return
    import telegram_notify
    today = here.date()
    over = today >= end_date(conn, today)
    if over:
        if not db.get_cached_value(conn, FINAL_KEY, _FOREVER) and (closed_trades(conn) or today > end_date(conn, today)):
            if send(telegram_notify.format_day_board(scoreboard(conn, today), final=True)):
                db.save_cached_value(conn, FINAL_KEY, 1.0)
        return
    key = DAY_KEY.format(day=today.isoformat())
    had = any(local(dt.datetime.fromisoformat(t.exit_ts)).date() == today for t in closed_trades(conn))
    if had and here.time() >= FLAT_AT and not db.get_cached_value(conn, key, _FOREVER):
        if send(telegram_notify.format_day_board(scoreboard(conn, today), day=today)):
            db.save_cached_value(conn, key, 1.0)


# ================================================================ the scoreboard
@dataclass(frozen=True)
class IdeaScore:
    idea: str
    closed: int
    open: int
    net: float
    wins: int
    today: float

    @property
    def qualifies(self) -> bool:
        return self.closed >= MIN_TRADES and self.net > 0


@dataclass(frozen=True)
class Board:
    start: dt.date
    end: dt.date
    scores: list[IdeaScore]
    open: list[Trade]


def scoreboard(conn, today: dt.date) -> Board:
    done, live = closed_trades(conn), [t for t in live_trades(conn) if t.status == "open"]
    scores = []
    for idea in IDEAS:
        mine = [t for t in done if t.idea == idea]
        scores.append(IdeaScore(idea, len(mine), sum(1 for t in live if t.idea == idea),
                                sum(t.result_r for t in mine), sum(1 for t in mine if t.result_r > 0),
                                sum(t.result_r for t in mine
                                    if local(dt.datetime.fromisoformat(t.exit_ts)).date() == today)))
    return Board(start_date(conn, today), end_date(conn, today), scores, live)


def handle_command(conn, text: str, *, today: dt.date | None = None) -> str:
    import telegram_notify
    args = [a.lower() for a in text.split()[1:]]
    if args in (["off"], ["on"]):
        db.save_cached_value(conn, QUIET_KEY, 1.0 if args == ["off"] else 0.0)
        return "Дневные бумажные сделки: " + ("не присылаю, только итог дня." if args == ["off"] else "присылаю каждую.")
    if args:
        return "Команды: /day, /day off, /day on."
    today = today or dt.datetime.now(TZ).date()
    if db.get_cached_value(conn, START_KEY, _FOREVER) is None:
        return "Дневная лига начнётся с первой сделки: будни, 09:00–18:00."
    return telegram_notify.format_day_board(scoreboard(conn, today))
