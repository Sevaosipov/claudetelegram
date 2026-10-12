"""cfd/sk_live.py: the SK sequence on paper (docs/cfd/SK_LIVE.md) -- the rule of cfd/research_abc.py run on the
live four-hour bars of EURUSD and gold, each limit order told to Telegram with its stop and its target, and
what becomes of it. The bot places no order.

A pass replays the tested state machine over the bars from a fixed date, so the live rule is the tested one
line for line, and brings the table to what the replay says; a message goes out for what is new."""
from __future__ import annotations

import datetime as dt
import sys
from collections.abc import Callable
from dataclasses import dataclass, fields

import db
from cfd import data, mt5_bridge, research_abc
from cfd import indicators as ind
from cfd.data import Bar

UTC = dt.timezone.utc
MARKETS = {"EURUSD": ("EURUSD=X", 0.8e-4, 0.010), "XAUUSD": ("GC=F", 0.30, 0.020)}   # symbol, round trip, night %
REPLAY_FROM = dt.datetime(2025, 1, 1, tzinfo=UTC)      # fixed, so every pass replays the same history
H4 = dt.timedelta(hours=4)
FRESH = dt.timedelta(hours=12)                         # older news is recorded, not told
START_KEY, QUIET_KEY = "sk_live_start", "sk_live_quiet"
_FOREVER = 100 * 365 * 24 * 3600
RULE = dict(m=0.0, e=0.618, stop_at=1.0, thirds=False, look=300, break_k=3, c_target="A")

Send = Callable[[str], bool]


@dataclass
class Trade:
    """A row of sk_trades."""
    id: int
    pair: str
    ref: str
    side: str
    entry: float
    stop: float
    target: float
    order_ts: str
    status: str
    fill_ts: str | None
    fill_price: float | None
    exit_ts: str | None
    result_r: float | None
    scored: int


_COLUMNS = [f.name for f in fields(Trade)]


def trades(conn, where: str = "", params=()) -> list[Trade]:
    return [Trade(*row) for row in conn.execute(f"SELECT {', '.join(_COLUMNS)} FROM sk_trades {where} ORDER BY id",
                                                params)]


def to_h4(hours: list[Bar], now: dt.datetime) -> list[Bar]:
    """Completed four-hour bars (UTC 00, 04, ...), as the tests built them."""
    return [b for b in research_abc.to_h4([h for h in hours if h.ts + dt.timedelta(hours=1) <= now])
            if b.ts + H4 <= now]


def replay(pair: str, bars: list[Bar]) -> dict[str, dict]:
    """ref -> what the rule has done with that order, from the first bar on."""
    _symbol, cost, night = MARKETS[pair]
    atr = ind.atr(bars, 14)
    out: dict[str, dict] = {}
    for side, sign in (("long", 1), ("short", -1)):
        log: list = []
        research_abc.trades(bars, atr, dt.date.min, dt.date.max, sign, cost=cost, night_pct=night, log=log, **RULE)
        cur = None
        for ev in log:
            when = (bars[ev[1]].ts + H4).isoformat()
            if ev[0] == "order":
                _k, _i, zero_i, top_i, entry, stop, target = ev
                ref = f"{bars[zero_i].ts:%y%m%d%H}-{bars[top_i].ts:%y%m%d%H}-{side}"
                cur = out[ref] = dict(side=side, entry=sign * entry, stop=sign * stop, target=sign * target,
                                      order_ts=when, status="pending", fill_ts=None, fill_price=None,
                                      exit_ts=None, result_r=None)
            elif cur is None:
                continue
            elif ev[0] in ("cancel", "void"):
                cur.update(status="cancelled", exit_ts=when)
            elif ev[0] == "fill":
                cur.update(status="open", fill_ts=when, fill_price=sign * ev[2])
            elif ev[0] == "exit":
                cur.update(status="closed", exit_ts=when, result_r=ev[2])
    return out


def hours_of(pair: str, fetch: data.Fetch | None = None) -> list[Bar]:
    """The hourly bars of a market: the terminal's history and its recent file, else Yahoo's."""
    symbol = MARKETS[pair][0]
    if fetch is None:
        merged = {b.ts: b for b in mt5_bridge.history(pair, "H1")}
        merged.update({b.ts: b for b in data.clean_rows(mt5_bridge.rows(symbol, "1h") or [], "1h").bars})
        if merged:
            return [merged[ts] for ts in sorted(merged) if ts >= REPLAY_FROM]
        fetch = data.yahoo_fetch
    return [b for b in data.clean_rows(fetch(symbol, "1h"), "1h").bars if b.ts >= REPLAY_FROM]


def quiet(conn) -> bool:
    return bool(db.get_cached_value(conn, QUIET_KEY, _FOREVER))


def _send(text: str) -> bool:
    import telegram_notify
    return bool(telegram_notify.send_text(text))


def run(conn, *, now: dt.datetime | None = None, fetch: data.Fetch | None = None, send: Send | None = None) -> int:
    """One pass: bring sk_trades to what the replay says and tell what is new. Returns the messages sent."""
    import telegram_notify
    now = now or dt.datetime.now(UTC)
    send = send or _send
    first_run = db.get_cached_value(conn, START_KEY, _FOREVER) is None
    loud, sent = not quiet(conn), 0
    for pair in MARKETS:
        try:
            bars = to_h4(hours_of(pair, fetch), now)
            if len(bars) < RULE["look"] + 20:
                continue
            for ref, new in replay(pair, bars).items():
                old = trades(conn, "WHERE pair = ? AND ref = ?", (pair, ref))
                live = new["status"] in ("pending", "open")
                if not old:
                    scored = int(not first_run or live)
                    conn.execute(
                        "INSERT INTO sk_trades (pair, ref, side, entry, stop, target, order_ts, status, fill_ts, "
                        "fill_price, exit_ts, result_r, scored, created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (pair, ref, new["side"], new["entry"], new["stop"], new["target"], new["order_ts"],
                         new["status"], new["fill_ts"], new["fill_price"], new["exit_ts"], new["result_r"], scored,
                         now.isoformat(timespec="seconds")))
                    before = None
                else:
                    before = old[0].status
                    if before == new["status"]:
                        continue
                    conn.execute("UPDATE sk_trades SET status = ?, fill_ts = ?, fill_price = ?, exit_ts = ?, "
                                 "result_r = ? WHERE id = ?", (new["status"], new["fill_ts"], new["fill_price"],
                                                               new["exit_ts"], new["result_r"], old[0].id))
                conn.commit()
                t = trades(conn, "WHERE pair = ? AND ref = ?", (pair, ref))[0]
                if not loud or not t.scored:
                    continue
                stamp = dt.datetime.fromisoformat(t.exit_ts or t.fill_ts or t.order_ts)
                if before is None and live:                     # a new order (or, at the start, one in force)
                    sent += bool(send(telegram_notify.format_sk_order(t)))
                    if t.status == "open":
                        sent += bool(send(telegram_notify.format_sk_event(t)))
                elif before is not None and now - stamp <= FRESH:
                    sent += bool(send(telegram_notify.format_sk_event(t)))
        except Exception as e:
            print(f"[sk] {pair}: {type(e).__name__}: {e}", file=sys.stderr)
    if first_run:
        db.save_cached_value(conn, START_KEY, float(now.date().toordinal()))
    return sent


def handle_command(conn, text: str) -> str:
    import telegram_notify
    args = [a.lower() for a in text.split()[1:]]
    if args in (["off"], ["on"]):
        db.save_cached_value(conn, QUIET_KEY, 1.0 if args == ["off"] else 0.0)
        return "SK-сделки: " + ("не присылаю." if args == ["off"] else "присылаю каждую.")
    if args:
        return "Команды: /sk, /sk off, /sk on."
    stamp = db.get_cached_value(conn, START_KEY, _FOREVER)
    if stamp is None:
        return "SK начнётся с первого прохода: в течение часа."
    return telegram_notify.format_sk_board(trades(conn, "WHERE scored = 1"), dt.date.fromordinal(int(stamp)))
