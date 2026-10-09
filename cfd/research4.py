"""cfd/research4.py: round 4 of the CFD research (docs/cfd/PREREGISTRATION_R4.md) -- two forex ideas on data
the earlier rounds did not use, judged by the user's monthly gate.

  H8  COT-EXT  against the speculators when their net futures position (CFTC, weekly) is at a three-year
               extreme; daily bars from Yahoo.
  H9  LDN-BO   the break of the Asian range in London's morning, flat by the evening; hourly bars from
               Dukascopy.

The pure parts (the index, the two simulators, the statistics, the gate) take plain rows and bars; the two
loaders at the end are the only network code, each with a file cache under data/cfd_cache. `python -m
cfd.research4` runs both and writes docs/cfd/BACKTEST_REPORT_R4.md and the `round4` block of enabled.json.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import lzma
import math
import struct
import sys
import time
import zipfile
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from cfd import data
from cfd import indicators as ind
from cfd.data import Bar

UTC = dt.timezone.utc
LONDON = ZoneInfo("Europe/London")
OOS_START = dt.date(2017, 1, 1)
IS_START = dt.date(2006, 1, 1)
OUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "cfd"
CACHE_DIR = data.CACHE_DIR
MIN_TRADES = 100


# ================================================================ trades and statistics
@dataclass(frozen=True)
class Trade:
    pair: str
    side: str            # long | short
    entry_date: dt.date
    exit_date: dt.date
    r: float             # the result in R after costs
    how: str             # stop | report | time | evening | both


@dataclass(frozen=True)
class Stats:
    n: int
    win_rate: float
    mean: float
    t: float
    pf: float
    net: float
    months: int              # calendar months that closed a trade
    months_up: int
    worst_month: float
    losing_run: int          # the longest run of losing months in a row

    @property
    def months_up_share(self) -> float:
        return self.months_up / self.months if self.months else 0.0


def month_results(trades: Sequence[Trade]) -> dict[tuple[int, int], float]:
    """The sum of the results of the trades closed in each calendar month."""
    out: dict[tuple[int, int], float] = defaultdict(float)
    for t in trades:
        out[(t.exit_date.year, t.exit_date.month)] += t.r
    return dict(sorted(out.items()))


def stats(trades: Sequence[Trade]) -> Stats:
    rs = [t.r for t in trades]
    n = len(rs)
    if not n:
        return Stats(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0, 0.0, 0)
    mean = sum(rs) / n
    var = sum((r - mean) ** 2 for r in rs) / (n - 1) if n > 1 else 0.0
    t = mean / math.sqrt(var / n) if var > 0 else 0.0
    gain, loss = sum(r for r in rs if r > 0), -sum(r for r in rs if r < 0)
    months = list(month_results(trades).values())
    run = longest = 0
    for m in months:
        run = run + 1 if m < 0 else 0
        longest = max(longest, run)
    return Stats(n, sum(1 for r in rs if r > 0) / n, mean, t, gain / loss if loss > 0 else math.inf,
                 sum(rs), len(months), sum(1 for m in months if m > 0), min(months), longest)


def split(trades: Sequence[Trade]) -> tuple[list[Trade], list[Trade]]:
    """(in-sample, out-of-sample) by the entry date; a trade entered before 2006 is in neither."""
    ins_ = [t for t in trades if IS_START <= t.entry_date < OOS_START]
    oos = [t for t in trades if t.entry_date >= OOS_START]
    return ins_, oos


@dataclass(frozen=True)
class Gate:
    name: str
    checks: tuple[tuple[str, str, bool], ...]       # (what, the value, passed)

    @property
    def passed(self) -> bool:
        return all(ok for _w, _v, ok in self.checks)


def gate(name: str, is_stats: Stats, oos: Stats) -> Gate:
    """The user's gate (PREREGISTRATION_R4.md): profitable out of sample and in sample, more than half of
    the out-of-sample months in profit, at least MIN_TRADES trades out of sample."""
    return Gate(name, (
        (f"вне выборки не меньше {MIN_TRADES} сделок", str(oos.n), oos.n >= MIN_TRADES),
        ("вне выборки итог после издержек > 0", f"{oos.net:+.1f}R", oos.net > 0),
        ("в выборке итог после издержек > 0", f"{is_stats.net:+.1f}R", is_stats.net > 0),
        ("вне выборки прибыльных месяцев больше половины",
         f"{oos.months_up} из {oos.months}", oos.months > 0 and oos.months_up * 2 > oos.months),
    ))


# ================================================================ H8: COT-EXT
COT_WINDOW = 156
COT_HIGH, COT_LOW, COT_MID = 95.0, 5.0, 50.0
COT_STOP_ATR = 3.0
COT_MAX_BARS = 130
COT_ROUND_TRIP_PCT, COT_FINANCING_PCT = 0.015, 0.010
# CME contract code -> (currency, Yahoo pair, the pair moves with the currency)
COT_MARKETS = {"099741": ("EUR", "EURUSD=X", True), "096742": ("GBP", "GBPUSD=X", True),
               "232741": ("AUD", "AUDUSD=X", True), "112741": ("NZD", "NZDUSD=X", True),
               "097741": ("JPY", "USDJPY=X", False), "090741": ("CAD", "USDCAD=X", False),
               "092741": ("CHF", "USDCHF=X", False)}


def cot_index(nets: Sequence[float], window: int = COT_WINDOW) -> list[float | None]:
    """Where each value stands in the range of the last `window` values including its own, 0..100; None
    before `window` values or when the range is flat."""
    out: list[float | None] = []
    for i in range(len(nets)):
        if i + 1 < window:
            out.append(None)
            continue
        chunk = nets[i + 1 - window:i + 1]
        low, high = min(chunk), max(chunk)
        out.append(100.0 * (nets[i] - low) / (high - low) if high > low else None)
    return out


def usable_bar(report_date: dt.date, dates: Sequence[dt.date]) -> int | None:
    """The index of the bar a report of `report_date` (a Tuesday) is usable on: the second daily bar after
    the Friday of its week. None when the bars end before it."""
    friday = report_date + dt.timedelta(days=(4 - report_date.weekday()) % 7)
    after = [i for i, d in enumerate(dates) if d > friday]
    return after[1] if len(after) > 1 else None


def backtest_cot(pair: str, with_currency: bool, bars: Sequence[Bar],
                 reports: Sequence[tuple[dt.date, float]]) -> list[Trade]:
    """H8 on one pair. `reports` are (report date, net ÷ open interest), oldest first."""
    dates = [b.ts.date() for b in bars]
    atr = ind.atr(bars, 14)
    index = cot_index([net for _d, net in reports], COT_WINDOW)
    at_bar: dict[int, float] = {}                    # the bar a report is usable on -> its index
    position = 0
    for (day, _net), value in zip(reports, index):
        if value is None:
            continue
        while position < len(dates) and dates[position] <= day:
            position += 1
        k = usable_bar(day, dates[position:])
        if k is not None:
            at_bar[position + k] = value             # a later report on the same bar replaces an earlier one
    trades: list[Trade] = []
    open_trade = None                                # (side, entry index, entry, stop, currency side)
    for i in range(len(bars)):
        bar = bars[i]
        if open_trade is not None:
            side, start, entry, stop, long_currency = open_trade
            sign = 1 if side == "long" else -1
            exit_price = how = None
            back = at_bar.get(i - 1)
            if i > start and back is not None and (back >= COT_MID if long_currency else back <= COT_MID):
                exit_price, how = bar.open, "report"
            elif i - start >= COT_MAX_BARS:
                exit_price, how = bar.open, "time"
            if sign * (bar.open - stop) <= 0:        # gapped through the stop: the open, whatever else
                exit_price, how = bar.open, "stop"
            elif exit_price is None and sign * ((bar.low if sign > 0 else bar.high) - stop) <= 0:
                exit_price, how = stop, "stop"
            if exit_price is not None:
                distance = abs(entry - stop)
                nights = (dates[i] - dates[start]).days
                cost = (COT_ROUND_TRIP_PCT + nights * COT_FINANCING_PCT) / 100.0 * entry / distance
                trades.append(Trade(pair, side, dates[start], dates[i],
                                    sign * (exit_price - entry) / distance - cost, how))
                open_trade = None
        value = at_bar.get(i)
        if open_trade is None and value is not None and i + 1 < len(bars) and atr[i]:
            if value >= COT_HIGH or value <= COT_LOW:
                long_currency = value <= COT_LOW
                side = "long" if long_currency == with_currency else "short"
                entry = bars[i + 1].open
                stop = entry - COT_STOP_ATR * atr[i] if side == "long" else entry + COT_STOP_ATR * atr[i]
                open_trade = (side, i + 1, entry, stop, long_currency)
    return trades


# ================================================================ H9: LDN-BO
LDN_PAIRS = {"EURUSD": 0.015, "GBPUSD": 0.015, "USDJPY": 0.015, "AUDUSD": 0.015,
             "EURGBP": 0.030, "EURJPY": 0.030, "GBPJPY": 0.030}          # round trip, % of the entry
RANGE_HOURS, ENTRY_HOURS, FLAT_HOUR = range(0, 8), range(8, 12), 20
MIN_RANGE_BARS = 6
MAX_RANGE_ATR = 0.6


def london_days(bars: Sequence[Bar]) -> dict[dt.date, list[tuple[int, Bar]]]:
    """The hourly bars by London calendar day, each with its London hour, in time order."""
    days: dict[dt.date, list[tuple[int, Bar]]] = defaultdict(list)
    for b in bars:
        local = b.ts.astimezone(LONDON)
        days[local.date()].append((local.hour, b))
    return dict(sorted(days.items()))


def daily_from_hourly(days: dict[dt.date, list[tuple[int, Bar]]]) -> list[Bar]:
    return [Bar(dt.datetime(d.year, d.month, d.day, tzinfo=UTC), rows[0][1].open,
                max(b.high for _h, b in rows), min(b.low for _h, b in rows), rows[-1][1].close)
            for d, rows in days.items()]


def london_trade(pair: str, day: dt.date, rows: Sequence[tuple[int, Bar]], atr_before: float | None,
                 cost_pct: float) -> Trade | None:
    """The trade of one London day, or None: no weekday, no range, a wide range, no break."""
    if day.weekday() > 4 or not atr_before:
        return None
    night = [b for h, b in rows if h in RANGE_HOURS]
    if len(night) < MIN_RANGE_BARS:
        return None
    high, low = max(b.high for b in night), min(b.low for b in night)
    if high <= low or high - low > MAX_RANGE_ATR * atr_before:
        return None
    later = [(h, b) for h, b in rows if h >= ENTRY_HOURS.start]
    for n, (hour, bar) in enumerate(later):
        if hour not in ENTRY_HOURS:
            break
        up, down = bar.high > high, bar.low < low
        if not up and not down:
            continue
        if up and down:                              # both sides in one bar: the worst is assumed
            entry = high
            return Trade(pair, "long", day, day, -1.0 - cost_pct / 100.0 * entry / (high - low), "both")
        side, sign = ("long", 1) if up else ("short", -1)
        level, stop = (high, low) if up else (low, high)
        entry = bar.open if sign * (bar.open - level) > 0 else level
        distance = abs(entry - stop)
        cost = cost_pct / 100.0 * entry / distance
        if sign * ((bar.low if up else bar.high) - stop) <= 0:       # the entry bar reaches the stop
            return Trade(pair, side, day, day, -1.0 - cost, "stop")
        exit_price, how = later[-1][1].close, "evening"
        for hour2, bar2 in later[n + 1:]:
            if hour2 >= FLAT_HOUR:
                exit_price = bar2.open
                break
            if sign * (bar2.open - stop) <= 0:
                exit_price, how = bar2.open, "stop"
                break
            if sign * ((bar2.low if up else bar2.high) - stop) <= 0:
                exit_price, how = stop, "stop"
                break
        return Trade(pair, side, day, day, sign * (exit_price - entry) / distance - cost, how)
    return None


def backtest_london(pair: str, bars: Sequence[Bar], cost_pct: float) -> list[Trade]:
    """H9 on one pair's hourly bars."""
    days = london_days(bars)
    daily = daily_from_hourly(days)
    atr = ind.atr(daily, 14)
    trades = []
    for i, (day, rows) in enumerate(days.items()):
        trade = london_trade(pair, day, rows, atr[i - 1] if i else None, cost_pct)
        if trade is not None:
            trades.append(trade)
    return trades


# ================================================================ the report
def _cells(st: Stats) -> list[str]:
    if not st.n:
        return ["0"] + ["—"] * 9
    pf = "∞" if math.isinf(st.pf) else f"{st.pf:.2f}"
    return [str(st.n), f"{st.win_rate * 100:.0f} %", f"{st.mean:+.3f}", f"{st.t:+.2f}", pf, f"{st.net:+.1f}",
            f"{st.months_up} из {st.months} ({st.months_up_share * 100:.0f} %)", f"{st.worst_month:+.1f}",
            str(st.losing_run)]


_HEAD = ("| Период | Сделок | Прибыльных | Средний R | t | Профит-фактор | Итог, R | Прибыльных месяцев | "
         "Худший месяц, R | Убыточных месяцев подряд |")
_RULE = "|---|---|---|---|---|---|---|---|---|---|"


def _section(title: str, trades: Sequence[Trade], g: Gate) -> list[str]:
    ins_, oos = split(trades)
    lines = [f"## {title}", "", _HEAD, _RULE,
             "| В выборке 2006–2016 | " + " | ".join(_cells(stats(ins_))) + " |",
             "| Вне выборки с 2017 | " + " | ".join(_cells(stats(oos))) + " |", "",
             "| Условие | Значение | Выполнено |", "|---|---|---|"]
    lines += [f"| {what} | {value} | {'да' if ok else 'нет'} |" for what, value, ok in g.checks]
    lines += ["", f"**Итог: {'проходит' if g.passed else 'не проходит'}.**", ""]
    st = stats(oos)
    if g.passed:
        verdict = ("отличим от случайного" if st.t >= 2.0 else
                   "НЕ отличим от случайного: такой результат часто получается и без всякого преимущества")
        lines += [f"t вне выборки {st.t:+.2f} — результат {verdict}.", ""]
    lines += ["Для сведения, вне выборки по парам:", "", "| Пара | Сделок | Средний R | Итог, R |", "|---|---|---|---|"]
    for pair in sorted({t.pair for t in oos}):
        s = stats([t for t in oos if t.pair == pair])
        lines.append(f"| {pair} | {s.n} | {s.mean:+.3f} | {s.net:+.1f} |")
    lines += ["", "Для сведения, по годам (все сделки):", "", "| Год | Сделок | Итог, R |", "|---|---|---|"]
    for year in sorted({t.exit_date.year for t in trades if t.entry_date >= IS_START}):
        s = stats([t for t in trades if t.exit_date.year == year and t.entry_date >= IS_START])
        lines.append(f"| {year} | {s.n} | {s.net:+.1f} |")
    return lines + [""]


def render_report(cot: Sequence[Trade], london: Sequence[Trade], gates: Sequence[Gate], now: dt.datetime,
                  notes: Sequence[str] = ()) -> str:
    lines = ["# CFD research, round 4 (forex): the results", "",
             f"Run {now.isoformat(timespec='seconds')}. The rules and the gate are in PREREGISTRATION_R4.md, "
             "written before this run. Results are in R after costs; a month's result is the sum of the trades "
             "closed in it.", ""]
    lines += _section("H8. COT-EXT: против спекулянтов на трёхлетнем экстремуме позиций", cot, gates[0])
    lines += _section("H9. LDN-BO: пробой азиатского диапазона утром в Лондоне", london, gates[1])
    if notes:
        lines += ["## Notes", ""] + [f"- {n}" for n in notes] + [""]
    return "\n".join(lines)


def round4_block(gates: Sequence[Gate], now: dt.datetime) -> dict:
    return {"enabled": [g.name for g in gates if g.passed],
            "exit": {"COT-EXT": "report/stop/time", "LDN-BO": "evening/stop"},
            "run_at": now.isoformat(timespec="seconds")}


def write_outputs(report: str, block: dict, out_dir: Path | str = OUT_DIR) -> None:
    out_dir = Path(out_dir)
    (out_dir / "BACKTEST_REPORT_R4.md").write_text(report, encoding="utf-8")
    path = out_dir / "enabled.json"
    enabled = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    enabled["round4"] = block
    path.write_text(json.dumps(enabled, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ================================================================ the loaders (network, cached)
_AGENT = {"User-Agent": "Mozilla/5.0 (disclosure-bot research)"}


def _get(url: str, tries: int = 5) -> bytes | None:
    """The body of a URL, or None for a 404; a server error is retried with a growing pause."""
    import requests
    for attempt in range(tries):
        try:
            resp = requests.get(url, headers=_AGENT, timeout=60)
        except requests.RequestException:
            resp = None
        if resp is not None and resp.status_code == 200:
            return resp.content
        if resp is not None and resp.status_code == 404:
            return None
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"no answer from {url}")


def _cached(path: Path, url: str, *, final: bool) -> bytes | None:
    """`url` through a file cache. `final` is whether the file can no longer change (a past year, a past
    month): only then is an answer kept."""
    if path.exists():
        return path.read_bytes() or None
    body = _get(url)
    if final:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body or b"")
    return body


def load_cot(first_year: int = 2002, today: dt.date | None = None,
             cache_dir: Path = CACHE_DIR) -> dict[str, list[tuple[dt.date, float]]]:
    """contract code -> [(report date, net non-commercial ÷ open interest)], oldest first, from the CFTC's
    yearly archives of the legacy futures-only report."""
    today = today or dt.date.today()
    out: dict[str, dict[dt.date, float]] = defaultdict(dict)
    for year in range(first_year, today.year + 1):
        body = _cached(cache_dir / "cot" / f"deacot{year}.zip",
                       f"https://www.cftc.gov/files/dea/history/deacot{year}.zip", final=year < today.year)
        if not body:
            continue
        archive = zipfile.ZipFile(io.BytesIO(body))
        with archive.open(archive.namelist()[0]) as raw:
            rows = csv.reader(io.TextIOWrapper(raw, encoding="latin-1"))
            next(rows)
            for row in rows:
                code = row[3].strip()
                if code not in COT_MARKETS:
                    continue
                try:
                    day = dt.date.fromisoformat(row[2].strip())
                    oi, long_, short = float(row[7]), float(row[8]), float(row[9])
                except ValueError:
                    continue
                if oi > 0:
                    out[code][day] = (long_ - short) / oi
    return {code: sorted(by_day.items()) for code, by_day in out.items()}


def load_duka_hourly(pair: str, first_year: int = 2005, today: dt.date | None = None,
                     cache_dir: Path = CACHE_DIR, pause: float = 0.05) -> list[Bar]:
    """Dukascopy's hourly bid candles of `pair` as bars in UTC, the hours with no volume left out."""
    today = today or dt.date.today()
    scale = 1e3 if pair.endswith("JPY") else 1e5
    bars: list[Bar] = []
    for year in range(first_year, today.year + 1):
        for month in range(1, 13):
            if (year, month) > (today.year, today.month):
                break
            path = cache_dir / "duka" / f"{pair}_{year}_{month:02d}.bi5"
            fresh = not path.exists()
            body = _cached(path, f"https://datafeed.dukascopy.com/datafeed/{pair}/{year}/{month - 1:02d}/"
                                 "BID_candles_hour_1.bi5", final=(year, month) < (today.year, today.month))
            if fresh:
                time.sleep(pause)
            if not body:
                continue
            raw = lzma.decompress(body)
            start = dt.datetime(year, month, 1, tzinfo=UTC)
            for k in range(len(raw) // 24):
                secs, o, c, low, high, volume = struct.unpack(">5if", raw[k * 24:(k + 1) * 24])
                if volume > 0 and low > 0:
                    bars.append(Bar(start + dt.timedelta(seconds=secs), o / scale, high / scale, low / scale,
                                    c / scale))
    return bars


# ================================================================ the run
def run(*, log: Callable[[str], None] = print, today: dt.date | None = None) -> tuple[list[Gate], str]:
    now = dt.datetime.now(UTC)
    notes: list[str] = []
    log("H8: CFTC reports")
    reports = load_cot(today=today)
    daily = data.cached_fetch(data.yahoo_fetch)
    cot: list[Trade] = []
    for code, (currency, symbol, with_currency) in COT_MARKETS.items():
        bars = data.daily_bars(symbol, fetch=daily, today=today)
        rows = reports.get(code, [])
        trades = backtest_cot(symbol.removesuffix("=X"), with_currency, bars, rows)
        notes.append(f"H8 {currency}: {len(rows)} weekly reports from {rows[0][0] if rows else '—'}, "
                     f"{len(bars)} daily bars, {len(trades)} trades.")
        cot += trades
    london: list[Trade] = []
    for pair, cost in LDN_PAIRS.items():
        log(f"H9: {pair} hourly bars")
        bars = load_duka_hourly(pair, today=today)
        trades = backtest_london(pair, bars, cost)
        notes.append(f"H9 {pair}: {len(bars)} hourly bars from {bars[0].ts.date() if bars else '—'}, "
                     f"{len(trades)} trades.")
        london += trades
    gates = [gate("COT-EXT", *map(stats, split(cot))), gate("LDN-BO", *map(stats, split(london)))]
    report = render_report(cot, london, gates, now, notes)
    write_outputs(report, round4_block(gates, now))
    return gates, report


def main() -> int:
    gates, _report = run(log=lambda m: print(f"[round4] {m}", file=sys.stderr))
    for g in gates:
        print(f"{g.name}: {'проходит' if g.passed else 'не проходит'}")
        for what, value, ok in g.checks:
            print(f"  {'да ' if ok else 'нет'}  {what}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
