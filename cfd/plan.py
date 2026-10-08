"""cfd/plan.py: the user's own CFD trades, planned and followed (/cfd plan).

«/cfd plan XAUUSD buy 4461.80 stop 4449.10» is a trade the user has just taken, by their own choice: the
bot does not say which trades to take. It answers with the four take-profit stages (+1R..+4R, R being
the distance from the entry to the stop), the risk in euro and the quantity for the balance and the risk
percent of /cfd, keeps a row in `cfd_plans`, and then follows the price and sends a message at each stage
and at the close. The bot never places an order.

The position is exits.LadderState under E1, the same machine the research ran: a quarter closes at each
of TP1..TP4; the stop moves to the entry after TP1, to TP1 after TP2 and to TP2 after TP3; what is left
closes at the stop. Inside one bar the stop goes before the next target.

The prices are Yahoo's 1-minute bars of the instrument's symbol, completed ones only. Gold, silver and
oil have no spot price there, only the future, which trades at a distance from a broker's spot quote:
for those the plan keeps `offset` = the entry less the future's last price when the plan is made (the
trade being taken at that moment), and every bar is moved by it. A result is in R before any cost, and in
euro as R times the risk: what the broker charged is the broker's to say.
"""
from __future__ import annotations

import datetime as dt
import math
import sys
from collections.abc import Callable
from dataclasses import dataclass, fields, replace

import fx
from cfd import data, exits, live
from cfd import instruments as ins
from cfd.data import Bar

UTC = dt.timezone.utc
INTRADAY = "1m"                     # short bars: what came first inside a bar is then rarely a question
BAR = dt.timedelta(minutes=1)
INTRADAY_PERIOD = "5d"
TRACK_SECONDS = 300                 # the Telegram agent follows the open plans this often
STAGES = 4
MAX_OFFSET_PCT = 3.0                # a future further than this from the entry is not the same price
MAX_ENTRY_R = 1.0                   # a price further than this many R from the entry: not taken just now
FUTURES = frozenset({"GC=F", "SI=F", "CL=F", "BZ=F"})       # no spot price at Yahoo: followed by the future

ALIASES = {"GOLD": "XAUUSD", "XAU": "XAUUSD", "SILVER": "XAGUSD", "XAG": "XAGUSD", "OIL": "WTI",
           "USOIL": "WTI", "UKOIL": "BRENT", "SPX": "US500", "SP500": "US500", "NAS100": "US100",
           "NDX": "US100", "DOW": "US30", "DAX": "DE40", "GER40": "DE40", "FTSE": "UK100"}
SIDES = {"buy": "long", "long": "long", "купил": "long", "покупка": "long",
         "sell": "short", "short": "short", "продал": "short", "продажа": "short"}
_STOP_WORDS = ("stop", "sl", "стоп")

USAGE = {
    "plan": "/cfd plan XAUUSD buy 4461.80 stop 4449.10 — ваша сделка: 4 цели, риск и объём от баланса, "
            "сообщение на каждой цели и на стопе",
    "cancel": "/cfd cancel 3 — перестать следить за сделкой №3",
}


def resolve(name: str) -> ins.Instrument | None:
    """The instrument a user's word names: EURUSD, eur/usd, gold, BTC, BTCUSD."""
    key = name.upper().replace("/", "").replace("-", "").replace("_", "")
    key = ALIASES.get(key, key)
    for candidate in (key, f"{key}USD"):
        try:
            return ins.by_name(candidate)
        except KeyError:
            continue
    return None


# ------------------------------------------------------------------ the table
@dataclass
class Plan:
    """A row of cfd_plans."""
    id: int
    name: str
    symbol: str
    side: str
    entry: float
    stop0: float
    stop: float
    r: float
    stage: int
    remaining: float
    realized_r: float
    offset: float
    last_ts: str
    status: str
    closed_ts: str | None
    exit_price: float | None
    result_r: float | None
    risk_pct: float | None
    risk_eur: float | None
    qty: float | None
    created: str


_COLUMNS = [f.name for f in fields(Plan)]
_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM cfd_plans"


def _plans(conn, where: str = "", params=()) -> list[Plan]:
    return [Plan(*row) for row in conn.execute(f"{_SELECT} {where} ORDER BY id", params)]


def get_plan(conn, plan_id: int) -> Plan | None:
    rows = _plans(conn, "WHERE id = ?", (plan_id,))
    return rows[0] if rows else None


def open_plans(conn) -> list[Plan]:
    return _plans(conn, "WHERE status = 'open'")


def closed_plans(conn) -> list[Plan]:
    return _plans(conn, "WHERE status = 'closed'")


def insert_plan(conn, plan: Plan) -> int:
    names = _COLUMNS[1:]
    cur = conn.execute(f"INSERT INTO cfd_plans ({', '.join(names)}) VALUES ({','.join('?' * len(names))})",
                       tuple(getattr(plan, n) for n in names))
    conn.commit()
    return cur.lastrowid


def _save(conn, plan: Plan) -> None:
    names = ("stop", "stage", "remaining", "realized_r", "last_ts", "status", "closed_ts", "exit_price",
             "result_r")
    conn.execute(f"UPDATE cfd_plans SET {', '.join(f'{n} = ?' for n in names)} WHERE id = ?",
                 (*(getattr(plan, n) for n in names), plan.id))
    conn.commit()


# ------------------------------------------------------------------ the prices
IntradayFetch = Callable[[str], list]


def yahoo_intraday(symbol: str) -> list[tuple]:
    """The last days of 1-minute bars from Yahoo, as raw rows stamped with their UTC start."""
    import yfinance as yf
    frame = yf.Ticker(symbol).history(interval=INTRADAY, period=INTRADAY_PERIOD, auto_adjust=False)
    return data.rows_from_frame(frame, INTRADAY)


def completed_bars(rows, now: dt.datetime) -> list[Bar]:
    """The rows as bars in time order: whole ones only (a missing or inconsistent price drops the row),
    and none that is still forming at `now`."""
    bars = []
    for row in rows:
        ts = data._parse_ts(row[0], INTRADAY)
        prices = [data._num(x) for x in row[1:5]]
        if ts is None or any(p is None or p <= 0 for p in prices):
            continue
        o, h, low, c = prices
        if h < max(o, c) or low > min(o, c) or ts + BAR > now:
            continue
        bars.append(Bar(ts, o, h, low, c))
    bars.sort(key=lambda b: b.ts)
    return bars


def _shift(bar: Bar, offset: float) -> Bar:
    if not offset:
        return bar
    return Bar(bar.ts, bar.open + offset, bar.high + offset, bar.low + offset, bar.close + offset)


# ------------------------------------------------------------------ sizing
def qty_step(inst: ins.Instrument, price: float) -> float:
    """The smallest step of a quantity: 100 units of an FX pair, a hundredth of an ounce or of an index
    contract, a barrel; a coin by its price (live.qty_step)."""
    if inst.gate_class == "FX":
        return 100.0
    if inst.klass == ins.ENERGY:
        return 1.0
    if inst.klass == ins.CRYPTO:
        return live.qty_step(price)
    return 0.01


def size_qty(inst: ins.Instrument, risk_eur: float, r: float, price: float, conn=None) -> float:
    """How much of the instrument risks `risk_eur` over a stop `r` away (in the instrument's quote
    currency), rounded down to its step."""
    r_eur = fx.to_eur(r, inst.quote, conn)
    step = qty_step(inst, price)
    return round(math.floor(risk_eur / r_eur / step + 1e-9) * step, 4)


# ------------------------------------------------------------------ making a plan
@dataclass(frozen=True)
class Draft:
    inst: ins.Instrument
    side: str
    entry: float
    stop: float


def parse(args: list[str]) -> Draft | None:
    """«XAUUSD buy 4461.80 stop 4449.10» (the word «stop» may be left out, or be «sl»). None when it is
    not that: an unknown instrument or side, a price that is not a positive number, a stop on the wrong
    side of the entry."""
    words = [a for a in args if a.lower() not in _STOP_WORDS]
    if len(words) != 4:
        return None
    inst, side = resolve(words[0]), SIDES.get(words[1].lower())
    entry, stop = live._number(words[2]), live._number(words[3])
    if inst is None or side is None or not entry or not stop or entry <= 0 or stop <= 0:
        return None
    if (entry - stop) * (1 if side == "long" else -1) <= 0:
        return None
    return Draft(inst, side, entry, stop)


def _now() -> dt.datetime:
    return dt.datetime.now(UTC)


def make(conn, draft: Draft, *, fetch: IntradayFetch | None = None,
         now: dt.datetime | None = None) -> tuple[Plan, float | None, str | None]:
    """The plan of a draft, sized by the settings of /cfd: (the plan, the quantity for a €1 000 balance
    when no balance is set, why it is not followed). A plan that can be followed is kept in cfd_plans
    and carries its id; one that cannot (no prices, a price far from the entry or already beyond the
    stop) has id 0 and is only the calculation."""
    now = now or _now()
    inst, settings = draft.inst, live.get_settings(conn)
    r = abs(draft.entry - draft.stop)
    risk_pct = risk_eur = qty = per_1000 = None
    if settings.balance_eur:
        risk_pct = settings.risk_pct
        risk_eur = round(settings.balance_eur * risk_pct / 100.0, 2)
        qty = size_qty(inst, risk_eur, r, draft.entry, conn)
    else:
        per_1000 = size_qty(inst, live.REFERENCE_BALANCE * settings.risk_pct / 100.0, r, draft.entry, conn)

    plan = Plan(0, inst.name, inst.symbol, draft.side, draft.entry, draft.stop, draft.stop, r, 0, 1.0, 0.0,
                0.0, "", "open", None, None, None, risk_pct, risk_eur, qty, now.isoformat(timespec="seconds"))
    try:
        bars = completed_bars((fetch or yahoo_intraday)(inst.symbol), now)
    except Exception as e:
        print(f"[CFD] plan {inst.name}: prices failed: {type(e).__name__}: {e}", file=sys.stderr)
        bars = []
    if not bars:
        return plan, per_1000, "цены сейчас недоступны"
    last = bars[-1]
    sign = 1 if draft.side == "long" else -1
    if inst.symbol in FUTURES:
        offset = draft.entry - last.close
        if abs(offset) > MAX_OFFSET_PCT / 100.0 * draft.entry:
            return plan, per_1000, "цена входа далеко от текущей цены"
        plan.offset = offset
    else:
        if sign * (last.close - draft.stop) <= 0:
            return plan, per_1000, "цена уже за стопом"
        if abs(last.close - draft.entry) > MAX_ENTRY_R * r:
            return plan, per_1000, "цена сейчас дальше одного R от входа"
    plan.last_ts = last.ts.isoformat()
    plan.id = insert_plan(conn, plan)
    return plan, per_1000, None


# ------------------------------------------------------------------ following a plan
@dataclass(frozen=True)
class PlanNotice:
    """One event of a plan to tell the user about. `kind` is "tp" (stage `k` taken at `price`; `stop`
    the stop in force afterwards, None when that was the last stage) or "stop" (what was left closed at
    `price`). `plan` is the row after the event; `result_r` the result so far, final when the plan is
    closed."""
    kind: str
    plan: Plan
    k: int
    price: float
    stop: float | None
    result_r: float


Send = Callable[[PlanNotice], bool]


def track_plan(conn, plan: Plan, bars: list[Bar], send: Send) -> int:
    """Feed an open plan the completed `bars` after its `last_ts`, in order. A bar is one unit: its
    events are all sent, then the row is saved; a refused send leaves the row as it was before that
    bar, so the next pass repeats the bar whole. `plan` is updated in place. Returns the number of
    messages sent."""
    if plan.status != "open":
        return 0
    sent = 0
    sign = 1 if plan.side == "long" else -1
    last = dt.datetime.fromisoformat(plan.last_ts)
    for raw in bars:
        if raw.ts <= last:
            continue
        bar = _shift(raw, plan.offset)
        state = exits.LadderState(plan.side, plan.entry, plan.stop0, exits.E1, stage=plan.stage,
                                  remaining=plan.remaining, stop=plan.stop)
        after = replace(plan, last_ts=raw.ts.isoformat())
        notices = []
        for ev in state.step(bar):
            after.realized_r += ev.fraction * sign * (ev.price - plan.entry) / plan.r
            after.stage, after.remaining = state.stage, state.remaining
            if state.closed:
                after.status, after.closed_ts = "closed", raw.ts.isoformat()
                after.exit_price, after.result_r = ev.price, after.realized_r
            else:
                after.stop = state.stop
            notices.append(PlanNotice("tp" if ev.kind == "tp" else "stop", replace(after), ev.stage,
                                      ev.price, ev.stop, after.realized_r))
        for notice in notices:
            if not send(notice):
                return sent
            sent += 1
        _save(conn, after)
        for name in _COLUMNS:
            setattr(plan, name, getattr(after, name))
        last = raw.ts
        if plan.status != "open":
            break
    return sent


def notify(notice: PlanNotice) -> bool:
    import telegram_notify
    return bool(telegram_notify.send_text(telegram_notify.format_cfd_plan_notice(notice)))


def run(conn, *, fetch: IntradayFetch | None = None, now: dt.datetime | None = None,
        send: Send | None = None) -> int:
    """One pass over the open plans (the Telegram agent, every TRACK_SECONDS): the bars of each symbol
    are fetched once; a symbol failing is logged and the others go on. No open plan, no request.
    Returns the number of messages sent."""
    plans = open_plans(conn)
    if not plans:
        return 0
    now, send, fetch = now or _now(), send or notify, fetch or yahoo_intraday
    loaded: dict[str, list[Bar]] = {}
    sent = 0
    for plan in plans:
        try:
            if plan.symbol not in loaded:
                loaded[plan.symbol] = completed_bars(fetch(plan.symbol), now)
            sent += track_plan(conn, plan, loaded[plan.symbol], send)
        except Exception as e:
            print(f"[CFD] plan {plan.id} {plan.name}: tracking failed: {type(e).__name__}: {e}",
                  file=sys.stderr)
    return sent


# ------------------------------------------------------------------ the commands
def handle_plan(conn, args: list[str], *, fetch: IntradayFetch | None = None,
                now: dt.datetime | None = None) -> str:
    """The answer to «/cfd plan ...»."""
    import telegram_notify
    draft = parse(args)
    if draft is None:
        return USAGE["plan"]
    plan, per_1000, why_not = make(conn, draft, fetch=fetch, now=now)
    return telegram_notify.format_cfd_plan(plan, live.get_settings(conn), qty_per_1000=per_1000,
                                           why_not=why_not)


def handle_cancel(conn, args: list[str]) -> str:
    """The answer to «/cfd cancel N»: the plan is no longer followed (its row stays, as cancelled)."""
    plan = get_plan(conn, int(args[0])) if len(args) == 1 and args[0].isdigit() else None
    if plan is None or plan.status != "open":
        return USAGE["cancel"] if plan is None else f"Сделка №{plan.id} уже не отслеживается."
    conn.execute("UPDATE cfd_plans SET status = 'cancelled' WHERE id = ?", (plan.id,))
    conn.commit()
    return f"Сделка №{plan.id} ({plan.name}) больше не отслеживается."
