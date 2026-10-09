"""cfd/league.py: the forex paper league (docs/cfd/PAPER_LEAGUE.md) -- four ideas run forward on paper for
13 weeks. The bot places no orders: a paper trade is a message and a row in `league_trades`.

In order: the table; the prices (Yahoo daily bars: the completed ones and today's open); a trade's life
(make it from a signal, fill it, follow it to its stop or its time exit); the four ideas, each a pure
function from data to signals; the data behind them (CFTC reports, 2-year yields); the daily pass; the
scoreboard and the commands.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import statistics
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, fields

import db
from cfd import data
from cfd import indicators as ind
from cfd import instruments as ins
from cfd import research4 as r4
from cfd.data import Bar

WEEKS = 13
START_KEY, QUIET_KEY, BOARD_KEY, FINAL_KEY = "league_start", "league_quiet", "league_board_{month}", "league_final"
_FOREVER = 100 * 365 * 24 * 3600
FINANCING_PCT = 0.010            # of the entry per night, every pair
IDEAS = {"COT-WITH": "За спекулянтами", "RATE-MOM": "Ставки", "CMD-LEAD": "Сырьё", "MONTH-END": "Конец месяца"}
MIN_TRADES = 8                   # the verdict: at least this many closed trades


# ================================================================ the table
@dataclass
class Trade:
    """A row of league_trades."""
    id: int
    idea: str
    pair: str
    symbol: str
    side: str
    ref: str
    signal_date: str
    atr: float
    stop_atr: float
    exit_after: str              # "bars:N" -- the open of the Nth bar after the entry bar; "date:YYYY-MM-DD"
    status: str                  # pending | open | closed
    entry_date: str | None
    entry: float | None
    stop: float | None
    last_bar: str | None
    exit_date: str | None
    exit_price: float | None
    result_r: float | None
    reason: str | None           # stop | time
    created: str


_COLUMNS = [f.name for f in fields(Trade)]
_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM league_trades"


def trades(conn, where: str = "", params=()) -> list[Trade]:
    return [Trade(*row) for row in conn.execute(f"{_SELECT} {where} ORDER BY id", params)]


def live_trades(conn) -> list[Trade]:
    return trades(conn, "WHERE status != 'closed'")


def closed_trades(conn) -> list[Trade]:
    return trades(conn, "WHERE status = 'closed'")


def _save(conn, t: Trade) -> None:
    names = [n for n in _COLUMNS if n != "id"]
    conn.execute(f"UPDATE league_trades SET {', '.join(f'{n} = ?' for n in names)} WHERE id = ?",
                 (*(getattr(t, n) for n in names), t.id))
    conn.commit()


# ================================================================ the prices
class Feed:
    """Yahoo's daily bars for one pass, fetched once per symbol: `bars` the completed ones (dated before
    `today`), `forming` today's open or None."""

    def __init__(self, today: dt.date, fetch: data.Fetch | None = None):
        self.today, self._fetch, self._loaded = today, fetch, {}

    def _load(self, symbol: str) -> tuple[list[Bar], float | None]:
        if symbol not in self._loaded:
            rows = (self._fetch or data.yahoo_fetch)(symbol, data.DAILY)
            cleaned = data.clean_rows(rows, data.DAILY).bars
            done = [b for b in cleaned if b.ts.date() < self.today]
            forming = next((b.open for b in cleaned if b.ts.date() == self.today), None)
            self._loaded[symbol] = (done, forming)
        return self._loaded[symbol]

    def bars(self, symbol: str) -> list[Bar]:
        return self._load(symbol)[0]

    def forming(self, symbol: str) -> float | None:
        return self._load(symbol)[1]


# ================================================================ a trade's life
@dataclass(frozen=True)
class Signal:
    idea: str
    pair: str                    # an instrument's name: EURUSD
    side: str                    # long | short, of the pair
    ref: str
    stop_atr: float
    exit_after: str


def make(conn, sig: Signal, feed: Feed) -> Trade | None:
    """The paper trade of a signal, kept as `pending`; None when the idea has a trade in that pair that is
    not closed, when this signal was made before, or when the pair has no ATR."""
    inst = ins.by_name(sig.pair)
    if conn.execute("SELECT 1 FROM league_trades WHERE idea = ? AND pair = ? AND (status != 'closed' OR ref = ?)",
                    (sig.idea, sig.pair, sig.ref)).fetchone():
        return None
    bars = feed.bars(inst.symbol)
    atr = ind.atr(bars, 14)[-1] if bars else None
    if not atr:
        return None
    cur = conn.execute(
        "INSERT INTO league_trades (idea, pair, symbol, side, ref, signal_date, atr, stop_atr, exit_after, "
        "status, created) VALUES (?,?,?,?,?,?,?,?,?,'pending',?)",
        (sig.idea, sig.pair, inst.symbol, sig.side, sig.ref, bars[-1].ts.date().isoformat(), atr, sig.stop_atr,
         sig.exit_after, dt.datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    return trades(conn, "WHERE id = ?", (cur.lastrowid,))[0]


def _due(t: Trade, day: dt.date, bars_after_entry: int) -> bool:
    kind, _, value = t.exit_after.partition(":")
    if kind == "bars":
        return bars_after_entry >= int(value)
    return day >= dt.date.fromisoformat(value)


def _close(t: Trade, day: dt.date, price: float, reason: str) -> None:
    sign = 1 if t.side == "long" else -1
    distance = abs(t.entry - t.stop)
    nights = (day - dt.date.fromisoformat(t.entry_date)).days
    cost = (ins.by_symbol(t.symbol).costs.round_trip_pct + nights * FINANCING_PCT) / 100.0 * t.entry / distance
    t.status, t.exit_date, t.exit_price, t.reason = "closed", day.isoformat(), price, reason
    t.result_r = sign * (price - t.entry) / distance - cost


def advance(t: Trade, bars: Sequence[Bar], forming: float | None, today: dt.date) -> list[str]:
    """Move a trade through the bars it has not seen: fill a pending one at the open of the first bar after
    its signal bar, then, bar by bar, the time exit at the open, the stop gapped through at the open, the
    stop inside the bar. Today's bar is known by its open alone: it can fill, and it can close at the open.
    `t` is changed in place; returns the events, "open" and/or "close"."""
    events = []
    days = [(b.ts.date(), b.open, b) for b in bars] + ([(today, forming, None)] if forming else [])
    sign = 1 if t.side == "long" else -1
    for day, open_, bar in days:
        if day.isoformat() <= (t.last_bar or t.signal_date):
            continue
        if t.status == "pending":
            t.status, t.entry_date, t.entry = "open", day.isoformat(), open_
            t.stop = open_ - sign * t.stop_atr * t.atr
            events.append("open")
        else:
            after = sum(1 for d, _o, _b in days if dt.date.fromisoformat(t.entry_date) < d <= day)
            if sign * (open_ - t.stop) <= 0:
                _close(t, day, open_, "stop")
            elif _due(t, day, after):
                _close(t, day, open_, "time")
        if t.status == "open" and bar is not None and sign * ((bar.low if sign > 0 else bar.high) - t.stop) <= 0:
            _close(t, day, t.stop, "stop")
        if bar is not None:
            t.last_bar = day.isoformat()
        if t.status == "closed":
            events.append("close")
            break
    return events


# ================================================================ the ideas
COT_HIGH, COT_LOW = 90.0, 10.0


def cot_with(reports: dict[str, list[tuple[dt.date, float]]], today: dt.date) -> list[Signal]:
    """Idea 1: with the speculators at an extreme of their net position (the newest report whose Friday is
    past)."""
    out = []
    for code, (_currency, symbol, with_currency) in r4.COT_MARKETS.items():
        rows = [(d, n) for d, n in reports.get(code, [])
                if d + dt.timedelta(days=(4 - d.weekday()) % 7) < today]
        if not rows:
            continue
        index = r4.cot_index([n for _d, n in rows], r4.COT_WINDOW)[-1]
        if index is None or COT_LOW < index < COT_HIGH:
            continue
        long_currency = index >= COT_HIGH
        side = "long" if long_currency == with_currency else "short"
        out.append(Signal("COT-WITH", symbol.removesuffix("=X"), side, rows[-1][0].isoformat(), 2.0, "bars:10"))
    return out


RATE_PAIRS = {"EA": ("EURUSD", True), "CA": ("USDCAD", False), "JP": ("USDJPY", False)}
RATE_LAG, RATE_MOVE = 10, 0.10


def rate_mom(yields: dict[str, dict[dt.date, float]], today: dt.date) -> list[Signal]:
    """Idea 2: towards the currency whose 2-year yield gained on the US one over the last 10 observations.
    `yields` is region ("US", "EA", "CA", "JP") -> {date: percent}."""
    us = yields.get("US") or {}
    year, week, _ = today.isocalendar()
    out = []
    for region, (pair, with_currency) in RATE_PAIRS.items():
        other = yields.get(region) or {}
        days = sorted(d for d in other if d in us and d < today)
        if len(days) <= RATE_LAG or (today - days[-1]).days > 7:
            continue
        spread = [other[d] - us[d] for d in days]
        change = spread[-1] - spread[-1 - RATE_LAG]
        if abs(change) < RATE_MOVE:
            continue
        side = "long" if (change > 0) == with_currency else "short"
        out.append(Signal("RATE-MOM", pair, side, f"{year}-W{week:02d}", 2.0, "bars:5"))
    return out


CMD_PAIRS = {"CL=F": ("USDCAD", False), "HG=F": ("AUDUSD", True), "BZ=F": ("EURNOK", False)}
CMD_BARS, CMD_HISTORY, CMD_Z, CMD_MAX_AGE = 3, 60, 1.5, 3


def cmd_lead(closes: dict[str, list[tuple[dt.date, float]]], today: dt.date) -> list[Signal]:
    """Idea 3: a commodity currency after a sharp 3-bar move of its commodity. `closes` is commodity symbol
    -> [(date, close)] of its completed bars, oldest first."""
    out = []
    for symbol, (pair, with_commodity) in CMD_PAIRS.items():
        rows = closes.get(symbol) or []
        if len(rows) < CMD_BARS + CMD_HISTORY + 1 or (today - rows[-1][0]).days > CMD_MAX_AGE:
            continue
        values = [c for _d, c in rows]
        moves = [values[i] / values[i - CMD_BARS] - 1 for i in range(CMD_BARS, len(values))]
        sd = statistics.pstdev(moves[-1 - CMD_HISTORY:-1])
        if not sd or abs(moves[-1] / sd) < CMD_Z:
            continue
        side = "long" if (moves[-1] > 0) == with_commodity else "short"
        out.append(Signal("CMD-LEAD", pair, side, rows[-1][0].isoformat(), 1.5, "bars:3"))
    return out


MONTH_PAIRS = {"EURUSD": True, "GBPUSD": True, "AUDUSD": True, "USDJPY": False}    # short USD = long the pair
MONTH_MOVE = 0.01


def weekdays_left(today: dt.date) -> int:
    """The weekdays of the month after `today`."""
    day, n = today + dt.timedelta(days=1), 0
    while day.month == today.month:
        n += day.weekday() < 5
        day += dt.timedelta(days=1)
    return n


def month_end(spx: list[tuple[dt.date, float]], today: dt.date) -> list[Signal]:
    """Idea 4: against the dollar after a strong month of the S&P 500, with it after a weak one; on a
    weekday with two weekdays (or one) left in the month."""
    if today.weekday() > 4 or weekdays_left(today) not in (1, 2):
        return []
    before = [c for d, c in spx if (d.year, d.month) < (today.year, today.month)]
    this = [c for d, c in spx if (d.year, d.month) == (today.year, today.month) and d < today]
    if not before or not this:
        return []
    move = this[-1] / before[-1] - 1
    if abs(move) < MONTH_MOVE:
        return []
    short_usd = move > 0
    first = (today.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
    return [Signal("MONTH-END", pair, "long" if short_usd == usd_is_quote else "short",
                   f"{today.year}-{today.month:02d}", 2.0, f"date:{first.isoformat()}")
            for pair, usd_is_quote in MONTH_PAIRS.items()]


# ================================================================ the data behind the ideas
def _get(url: str) -> bytes | None:
    """One try, no waiting: the daily pass must not hang on a source that is down. A failure raises, the
    idea makes no signal that day, and the next run asks again."""
    import requests
    resp = requests.get(url, headers=r4._AGENT, timeout=30)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.content


def _text(url: str) -> str:
    return (_get(url) or b"").decode("utf-8", errors="replace")


def load_cot(today: dt.date) -> dict[str, list[tuple[dt.date, float]]]:
    return r4.load_cot(today=today, get=_get)


def _us_yields(today: dt.date) -> dict[dt.date, float]:
    out = {}
    for year in {today.year, (today - dt.timedelta(days=45)).year}:
        text = _text("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
                     f"daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
                     f"&field_tdr_date_value={year}&page&_format=csv")
        for row in csv.DictReader(io.StringIO(text)):
            try:
                out[dt.datetime.strptime(row["Date"], "%m/%d/%Y").date()] = float(row["2 Yr"])
            except (KeyError, ValueError, TypeError):
                continue
    return out


def _ea_yields(today: dt.date) -> dict[dt.date, float]:
    start = (today - dt.timedelta(days=60)).isoformat()
    text = _text("https://data-api.ecb.europa.eu/service/data/YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y"
                 f"?startPeriod={start}&format=csvdata")
    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        try:
            out[dt.date.fromisoformat(row["TIME_PERIOD"])] = float(row["OBS_VALUE"])
        except (KeyError, ValueError, TypeError):
            continue
    return out


def _ca_yields(today: dt.date) -> dict[dt.date, float]:
    import json
    series = "BD.CDN.2YR.DQ.YLD"
    body = json.loads(_text(f"https://www.bankofcanada.ca/valet/observations/{series}/json?recent=40") or "{}")
    out = {}
    for obs in body.get("observations", []):
        try:
            out[dt.date.fromisoformat(obs["d"])] = float(obs[series]["v"])
        except (KeyError, ValueError, TypeError):
            continue
    return out


def _jp_yields(today: dt.date) -> dict[dt.date, float]:
    base = "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/"
    out = {}
    for url in (base + "historical/jgbcme_all.csv", base + "jgbcme.csv"):
        for line in _text(url).splitlines()[-80:]:
            cells = line.split(",")
            try:
                year, month, day = (int(x) for x in cells[0].split("/"))
                out[dt.date(year, month, day)] = float(cells[2])
            except (ValueError, IndexError):
                continue
    return out


YIELD_SOURCES = {"US": _us_yields, "EA": _ea_yields, "CA": _ca_yields, "JP": _jp_yields}


def load_yields(today: dt.date) -> dict[str, dict[dt.date, float]]:
    """The 2-year yields by region; a source that fails is logged and left out (its pair makes no signal)."""
    out = {}
    for region, load in YIELD_SOURCES.items():
        try:
            out[region] = load(today)
        except Exception as e:
            print(f"[league] {region} yields failed: {type(e).__name__}: {e}", file=sys.stderr)
    return out


# ================================================================ the daily pass
Send = Callable[[str], bool]


def _send(text: str) -> bool:
    import telegram_notify
    return bool(telegram_notify.send_text(text))


def start_date(conn, today: dt.date) -> dt.date:
    """The day of the league's first run (kept once)."""
    stored = db.get_cached_value(conn, START_KEY, _FOREVER)
    if stored is None:
        db.save_cached_value(conn, START_KEY, float(today.toordinal()))
        return today
    return dt.date.fromordinal(int(stored))


def end_date(conn, today: dt.date) -> dt.date:
    return start_date(conn, today) + dt.timedelta(weeks=WEEKS)


def quiet(conn) -> bool:
    return bool(db.get_cached_value(conn, QUIET_KEY, _FOREVER))


def signals_of_the_day(today: dt.date, feed: Feed, *, cot=None, yields=None) -> list[Signal]:
    """The signals of the four ideas for `today`; an idea whose data fails is logged and makes none."""
    def closes(symbol):
        return [(b.ts.date(), b.close) for b in feed.bars(symbol)]

    ideas = (
        ("COT-WITH", lambda: cot_with((cot or load_cot)(today), today)),
        ("RATE-MOM", lambda: rate_mom((yields or load_yields)(today), today)),
        ("CMD-LEAD", lambda: cmd_lead({s: closes(s) for s in CMD_PAIRS}, today)),
        ("MONTH-END", lambda: month_end(closes("^GSPC"), today)),
    )
    out = []
    for name, run_ in ideas:
        try:
            out += run_()
        except Exception as e:
            print(f"[league] {name} failed: {type(e).__name__}: {e}", file=sys.stderr)
    return out


def run(conn, *, today: dt.date | None = None, fetch: data.Fetch | None = None, send: Send | None = None,
        cot=None, yields=None) -> tuple[int, int]:
    """The daily pass, after the CFD pass on a full run: follow the trades that are not closed, make the
    day's new ones (until the league's 13 weeks are over), send the month's scoreboard at the first run of
    a month and the final one once. Returns (trades opened, trades closed)."""
    import telegram_notify
    today = today or dt.date.today()
    send = send or _send
    feed = Feed(today, fetch)
    over = today >= end_date(conn, today)
    loud = not quiet(conn)
    opened = closed = 0

    def follow(t: Trade) -> None:
        nonlocal opened, closed
        events = advance(t, feed.bars(t.symbol), feed.forming(t.symbol), today)
        _save(conn, t)
        for event in events:
            opened += event == "open"
            closed += event == "close"
            if loud:
                send(telegram_notify.format_league_event(t, event))

    for t in live_trades(conn):
        try:
            follow(t)
        except Exception as e:
            print(f"[league] trade {t.id} {t.pair}: {type(e).__name__}: {e}", file=sys.stderr)
    if not over:
        for sig in signals_of_the_day(today, feed, cot=cot, yields=yields):
            try:
                t = make(conn, sig, feed)
                if t is not None:
                    follow(t)
            except Exception as e:
                print(f"[league] {sig.idea} {sig.pair}: {type(e).__name__}: {e}", file=sys.stderr)
    _boards(conn, today, send, over)
    return opened, closed


def _boards(conn, today: dt.date, send: Send, over: bool) -> None:
    import telegram_notify
    start = start_date(conn, today)
    last = today.replace(day=1) - dt.timedelta(days=1)             # the month just ended
    key = BOARD_KEY.format(month=f"{last.year}-{last.month:02d}")
    if (last.year, last.month) >= (start.year, start.month) and not db.get_cached_value(conn, key, _FOREVER):
        if send(telegram_notify.format_league_board(scoreboard(conn, today), month=(last.year, last.month))):
            db.save_cached_value(conn, key, 1.0)
    if over and not live_trades(conn) and not db.get_cached_value(conn, FINAL_KEY, _FOREVER):
        if send(telegram_notify.format_league_board(scoreboard(conn, today), final=True)):
            db.save_cached_value(conn, FINAL_KEY, 1.0)


# ================================================================ the scoreboard and the commands
@dataclass(frozen=True)
class IdeaScore:
    idea: str
    closed: int
    net: float
    months: dict            # (year, month) -> the sum of the results of the trades closed in it
    open: int

    @property
    def months_up(self) -> int:
        return sum(1 for v in self.months.values() if v > 0)

    @property
    def qualifies(self) -> bool:
        """The verdict of PAPER_LEAGUE.md: in profit, at least two months in profit, at least 8 trades."""
        return self.net > 0 and self.months_up >= 2 and self.closed >= MIN_TRADES


@dataclass(frozen=True)
class Board:
    start: dt.date
    end: dt.date
    today: dt.date
    scores: tuple[IdeaScore, ...]
    live: tuple[Trade, ...]
    balance_risk_eur: float | None      # the euros of one R at the user's /cfd balance and risk, or None


def scoreboard(conn, today: dt.date) -> Board:
    from cfd import live
    done, running = closed_trades(conn), live_trades(conn)
    scores = []
    for idea in IDEAS:
        mine = [t for t in done if t.idea == idea]
        months: dict = {}
        for t in mine:
            day = dt.date.fromisoformat(t.exit_date)
            months[(day.year, day.month)] = months.get((day.year, day.month), 0.0) + t.result_r
        scores.append(IdeaScore(idea, len(mine), sum(t.result_r for t in mine), dict(sorted(months.items())),
                                sum(1 for t in running if t.idea == idea)))
    s = live.get_settings(conn)
    risk = round(s.balance_eur * s.risk_pct / 100.0, 2) if s.balance_eur else None
    return Board(start_date(conn, today), end_date(conn, today), today, tuple(scores), tuple(running), risk)


USAGE = ("/league — бумажная лига форекс-идей: счёт по каждой идее и открытые сделки\n"
         "/league off — не присылать каждую бумажную сделку (итоги месяца приходят всё равно)\n"
         "/league on — снова присылать")


def handle_command(conn, text: str, *, today: dt.date | None = None) -> str:
    import telegram_notify
    args = [a.lower() for a in text.split()[1:]]
    if args in (["off"], ["on"]):
        db.save_cached_value(conn, QUIET_KEY, 1.0 if args == ["off"] else 0.0)
        return "Бумажные сделки лиги: " + ("не присылаю, только итоги месяца." if args == ["off"]
                                           else "присылаю каждую.")
    if args:
        return USAGE
    return telegram_notify.format_league_board(scoreboard(conn, today or dt.date.today()))
