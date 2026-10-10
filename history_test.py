"""history_test.py: the stock score's insider-cluster path on 20 years of SEC data
(docs/history/PREREGISTRATION.md -- read that first: what is tested, what cannot be, the known flaw).

Three steps, each kept on disk under data/history_cache so that a run can be cut short and gone on with:

    python history_test.py purchases      the SEC's quarterly data sets -> one row per buyer and filing
    python history_test.py prices         Yahoo adjusted closes for the tickers of the candidate clusters
    python history_test.py report         the signals, both measurements, docs/history/REPORT.md

The pure parts (the clusters, the points, the trades, the statistics) take plain rows and closes and are
tested in tests/test_history_test.py; the two loaders are the only network code.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import math
import statistics
import sys
import time
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import model_score
from cluster.common import MIN_CLUSTER_VALUE, SEC_MIN_BUYERS, SEC_SOLO_THRESHOLD, SEC_WINDOW_DAYS
from cluster.roles import INSIDER_ROLES, sec_role

BASE = Path(__file__).resolve().parent
CACHE = BASE / "data" / "history_cache"
OUT = BASE / "docs" / "history" / "REPORT.md"
FIRST_YEAR, LAST_YEAR = 2006, 2025
MIN_FILING_VALUE = 50_000           # the bot's --min-value
RESIGNAL_DAYS = 30
HORIZONS = (20, 60, 126, 252)       # trading days
COST = 0.003                        # a round trip
DEAD_MONEY_BDAYS, DEAD_MONEY_MIN_RETURN, MAX_DAYS = 60, 0.05, 365
CANDIDATE_POINTS = model_score.STOCK_BUY - model_score.MOMENTUM_CAP     # fewer insiders points can never buy
SPLIT_YEAR = 2016
AGENT = {"User-Agent": "disclosure-bot research contact@example.com"}
_MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT",
                                       "NOV", "DEC"), 1)}


# ================================================================ purchases
@dataclass(frozen=True)
class Purchase:
    symbol: str
    filed: str              # ISO date
    owner: str              # the reporting owner's CIK
    role: str               # cluster.roles.sec_role
    value: float            # dollars
    increase_pct: float | None


def _date(text: str) -> str | None:
    """«31-JAN-2018» -> «2018-01-31»."""
    try:
        day, mon, year = text.strip().split("-")
        return dt.date(int(year), _MONTHS[mon.upper()], int(day)).isoformat()
    except (ValueError, KeyError):
        return None


def _float(text: str) -> float | None:
    try:
        x = float(text)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def purchases_of(submissions, owners, transactions) -> list[Purchase]:
    """One Purchase per (filing, first reporting owner) from the three tables of a quarter (iterables of
    dict rows): Form 4 filings with a ticker, their code-P acquisitions with a price, added up; a filing
    under MIN_FILING_VALUE is dropped. The increase is the shares bought over what was held before them."""
    filings = {}
    for row in submissions:
        symbol = (row.get("ISSUERTRADINGSYMBOL") or "").strip().upper()
        filed = _date(row.get("FILING_DATE") or "")
        if row.get("DOCUMENT_TYPE") == "4" and filed and symbol.isalnum() and len(symbol) <= 5:
            filings[row["ACCESSION_NUMBER"]] = (symbol, filed)
    who = {}
    for row in owners:
        acc = row.get("ACCESSION_NUMBER")
        if acc in filings and acc not in who:
            rel = (row.get("RPTOWNER_RELATIONSHIP") or "").lower()
            who[acc] = (row.get("RPTOWNERCIK") or "", sec_role(row.get("RPTOWNER_TITLE"), "officer" in rel,
                                                             "director" in rel, "tenpercent" in rel))
    bought: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])      # value, shares, held after
    for row in transactions:
        acc = row.get("ACCESSION_NUMBER")
        if acc not in who or row.get("TRANS_CODE") != "P" or row.get("TRANS_ACQUIRED_DISP_CD") != "A":
            continue
        shares, price = _float(row.get("TRANS_SHARES")), _float(row.get("TRANS_PRICEPERSHARE"))
        if not shares or not price or shares <= 0 or price <= 0:
            continue
        slot = bought[acc]
        slot[0] += shares * price
        slot[1] += shares
        slot[2] = max(slot[2], _float(row.get("SHRS_OWND_FOLWNG_TRANS")) or 0.0)
    out = []
    for acc, (value, shares, after) in bought.items():
        if value < MIN_FILING_VALUE:
            continue
        before = after - shares
        symbol, filed = filings[acc]
        owner, role = who[acc]
        out.append(Purchase(symbol, filed, owner, role, value, 100.0 * shares / before if before > 0 else None))
    return out


# ================================================================ the cluster and its points
@dataclass(frozen=True)
class _Buyer:
    role: str


@dataclass(frozen=True)
class _Cluster:
    """What model_score.insider_part reads of a cluster."""
    source: str
    buyers: tuple
    holder_only: bool
    position_increase_pct: float | None


def cluster_points(window: list[Purchase]) -> float:
    """The insiders points of the purchases of one company filed in one window, or 0 when they are not a
    cluster by the bot's rule (SEC_MIN_BUYERS buyers and MIN_CLUSTER_VALUE, or one of SEC_SOLO_THRESHOLD).
    The size against the company and a first purchase are not known here: they add nothing."""
    by_owner: dict[str, list[Purchase]] = defaultdict(list)
    for p in window:
        by_owner[p.owner].append(p)
    total = sum(p.value for p in window)
    solo = max((sum(p.value for p in ps) for ps in by_owner.values()), default=0.0)
    if not ((len(by_owner) >= SEC_MIN_BUYERS and total >= MIN_CLUSTER_VALUE) or solo >= SEC_SOLO_THRESHOLD):
        return 0.0
    buyers = tuple(_Buyer(ps[0].role) for ps in by_owner.values())
    biggest = max(by_owner.values(), key=lambda ps: sum(p.value for p in ps))
    increases = [p.increase_pct for p in biggest if p.increase_pct is not None]
    holder_only = not any(b.role in INSIDER_ROLES for b in buyers)
    return model_score.insider_part(_Cluster("SEC", buyers, holder_only, max(increases) if increases else None)).points


def candidates(purchases: list[Purchase]) -> list[tuple[str, str, float]]:
    """(symbol, filing day, insiders points) for every day a company's window of filings scores
    CANDIDATE_POINTS or more -- the days momentum could still make a buy -- in date order."""
    by_symbol: dict[str, list[Purchase]] = defaultdict(list)
    for p in purchases:
        by_symbol[p.symbol].append(p)
    out = []
    for symbol, rows in by_symbol.items():
        rows.sort(key=lambda p: p.filed)
        for day in sorted({p.filed for p in rows}):
            start = (dt.date.fromisoformat(day) - dt.timedelta(days=SEC_WINDOW_DAYS)).isoformat()
            points = cluster_points([p for p in rows if start < p.filed <= day])
            if points >= CANDIDATE_POINTS:
                out.append((symbol, day, points))
    return sorted(out, key=lambda c: (c[1], c[0]))


# ================================================================ signals and trades
@dataclass(frozen=True)
class Signal:
    symbol: str
    filed: str
    points: float           # insiders + momentum
    entry_index: int        # the bar of the first close after the filing day


@dataclass(frozen=True)
class Trade:
    symbol: str
    entry_day: str
    exit_day: str
    ret: float              # after costs
    how: str                # stop | dead | time | end
    days: int


def signals_of(symbol: str, days: list[tuple[str, float]], closes: list[tuple[str, float]]) -> list[Signal]:
    """The buys of one company: of its candidate `days` (filing day, insiders points), those where the
    points with the momentum of the closes before the filing reach STOCK_BUY; none within RESIGNAL_DAYS of
    the last, nor while its trade is open (the bot signals nothing it holds). Needs a close after the day."""
    dates = [d for d, _c in closes]
    values = [c for _d, c in closes]
    out: list[Signal] = []
    busy_until = ""
    for day, insiders in sorted(days):
        if day <= busy_until:
            continue
        known = next((i for i, d in enumerate(dates) if d >= day), None)        # the first bar not before the filing
        if known is None or known < model_score.MIN_CLOSES:
            continue
        entry = known + 1 if dates[known] == day else known                     # the first close AFTER the filing day
        if entry >= len(values):
            continue
        before = values[:entry]                                                 # what was known at the entry's eve
        total = insiders + model_score.momentum_part(before).points
        if total < model_score.STOCK_BUY:
            continue
        signal = Signal(symbol, day, total, entry)
        trade = trade_of(signal, closes)
        hold_end = trade.exit_day if trade else dates[entry]
        lockout = (dt.date.fromisoformat(day) + dt.timedelta(days=RESIGNAL_DAYS)).isoformat()
        busy_until = max(hold_end, lockout)
        out.append(signal)
    return out


def trade_of(signal: Signal, closes: list[tuple[str, float]]) -> Trade | None:
    """The signal closed by the bot's price rules: the trailing stop (fixed at the entry from the closes
    before it, below the highest close since), dead money, MAX_DAYS; «end» when the prices run out first."""
    values = [c for _d, c in closes]
    dates = [d for d, _c in closes]
    i0 = signal.entry_index
    entry = values[i0]
    stop = model_score.stop_distance(values[:i0 + 1], "stock") or model_score.STOP_MIN["stock"]
    peak, start = entry, dt.date.fromisoformat(dates[i0])
    how, j = "end", len(values) - 1
    for k in range(i0 + 1, len(values)):
        price = values[k]
        peak = max(peak, price)
        held = (dt.date.fromisoformat(dates[k]) - start).days
        if price <= peak * (1 - stop):
            how, j = "stop", k
            break
        if k - i0 >= DEAD_MONEY_BDAYS and price / entry - 1 < DEAD_MONEY_MIN_RETURN:
            how, j = "dead", k
            break
        if held >= MAX_DAYS:
            how, j = "time", k
            break
    if j == i0:
        return None
    return Trade(signal.symbol, dates[i0], dates[j], values[j] / entry - 1 - COST, how,
                 (dt.date.fromisoformat(dates[j]) - start).days)


def forward(signal: Signal, closes: list[tuple[str, float]], horizon: int) -> float | None:
    values = [c for _d, c in closes]
    k = signal.entry_index + horizon
    return values[k] / values[signal.entry_index] - 1 if k < len(values) else None


def move_between(series: list[tuple[str, float]], start: str, end: str) -> float | None:
    """The return of `series` from its first close on or after `start` to its last on or before `end`."""
    a = next((c for d, c in series if d >= start), None)
    b = next((c for d, c in reversed(series) if d <= end), None)
    return b / a - 1 if a and b else None


# ================================================================ statistics
def summary(values: list[float]) -> dict:
    n = len(values)
    if not n:
        return {"n": 0}
    mean = sum(values) / n
    sd = statistics.stdev(values) if n > 1 else 0.0
    return {"n": n, "mean": mean, "median": statistics.median(values), "win": sum(1 for v in values if v > 0) / n,
            "t": mean / (sd / math.sqrt(n)) if sd else 0.0}


def profit_factor(values: list[float]) -> float:
    gain, loss = sum(v for v in values if v > 0), -sum(v for v in values if v < 0)
    return gain / loss if loss else math.inf


def verdict(first: dict, second: dict, whole: dict, trade_first: dict, trade_second: dict) -> str:
    """The reading fixed in the pre-registration, from the 126-day excess (A) and the per-trade excess (B)
    of the two halves and the t statistic of the whole."""
    halves = [first.get("mean"), second.get("mean"), trade_first.get("mean"), trade_second.get("mean")]
    if any(h is None for h in halves):
        return "not shown either way (a half has no signals)"
    if any(h < 0 for h in halves):
        return "the rules do NOT hold up: a half is below zero"
    if whole.get("t", 0.0) >= 2:
        return "the rules hold up: both halves above zero, t of the 126-day excess 2 or more"
    return "not shown either way: both halves above zero, but the excess is not distinguishable from chance"


# ================================================================ the loaders (network, cached)
def _rows(archive: zipfile.ZipFile, name: str):
    with archive.open(name) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8", errors="replace"), delimiter="\t")


def load_purchases(log=print) -> list[Purchase]:
    """Every quarter's purchases, each quarter kept as a JSON file once it is read."""
    import requests
    folder = CACHE / "purchases"
    folder.mkdir(parents=True, exist_ok=True)
    out: list[Purchase] = []
    for year in range(FIRST_YEAR, LAST_YEAR + 1):
        for q in range(1, 5):
            path = folder / f"{year}q{q}.json"
            if not path.exists():
                url = ("https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/"
                       f"{year}q{q}_form345.zip")
                resp = requests.get(url, headers=AGENT, timeout=120)
                if resp.status_code == 404:
                    log(f"{year}q{q}: not published")
                    continue
                resp.raise_for_status()
                archive = zipfile.ZipFile(io.BytesIO(resp.content))
                rows = purchases_of(_rows(archive, "SUBMISSION.tsv"), _rows(archive, "REPORTINGOWNER.tsv"),
                                    _rows(archive, "NONDERIV_TRANS.tsv"))
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps([list(p.__dict__.values()) for p in rows]), encoding="utf-8")
                tmp.replace(path)
                log(f"{year}q{q}: {len(rows)} purchases")
                time.sleep(0.2)
            out += [Purchase(*r) for r in json.loads(path.read_text(encoding="utf-8"))]
    return out


def _price_path(symbol: str) -> Path:
    return CACHE / "prices" / f"{symbol}.json"


class Throttled(RuntimeError):
    """Yahoo stopped answering: nothing of the batch is kept, the run goes on later."""


def load_prices(symbols: list[str], *, budget_seconds: float = 420, batch: int = 20, pause: float = 2.0,
                log=print) -> int:
    """Yahoo adjusted closes for the symbols not yet on disk, in small batches with a pause, until
    `budget_seconds` is spent. A symbol is kept as an empty file -- «Yahoo has nothing for it», not asked
    again -- only from a batch in which another symbol did come back: a batch that returns nothing at all
    is Yahoo refusing (a rate limit), so none of it is kept and the pass stops (Throttled). Returns how
    many symbols are still missing."""
    import yfinance as yf
    (CACHE / "prices").mkdir(parents=True, exist_ok=True)
    missing = [s for s in symbols if not _price_path(s).exists()]
    started = time.time()
    while missing and time.time() - started < budget_seconds:
        chunk = missing[:batch]
        frame = yf.download(chunk, start=f"{FIRST_YEAR - 2}-01-01", auto_adjust=True, progress=False,
                            group_by="ticker", threads=False)
        got = {}
        for symbol in chunk:
            try:
                closes = (frame[symbol]["Close"] if len(chunk) > 1 else frame["Close"]).dropna()
                got[symbol] = [[d.date().isoformat(), float(c)]
                               for d, c in zip(closes.index, closes.to_numpy().ravel()) if c == c and c > 0]
            except Exception:
                got[symbol] = []
        if not any(got.values()):
            raise Throttled(f"a batch of {len(chunk)} came back empty; {len(missing)} symbols to go")
        for symbol, rows in got.items():
            _price_path(symbol).write_text(json.dumps(rows), encoding="utf-8")
        missing = missing[batch:]
        log(f"prices: {len(missing)} symbols to go")
        time.sleep(pause)
    return len(missing)


def read_prices(symbol: str) -> list[tuple[str, float]]:
    path = _price_path(symbol)
    return [tuple(r) for r in json.loads(path.read_text(encoding="utf-8"))] if path.exists() else []


# ================================================================ the report
def _pct(x: float | None, places: int = 1) -> str:
    return "—" if x is None else f"{x * 100:+.{places}f} %"


def _row(label: str, s: dict, extra: str = "") -> str:
    if not s.get("n"):
        return f"| {label} | 0 | — | — | — | — |{extra}"
    return (f"| {label} | {s['n']} | {_pct(s['mean'])} | {_pct(s['median'])} | {s['win'] * 100:.0f} % | "
            f"{s['t']:+.2f} |{extra}")


def build_report(cands: list[tuple[str, str, float]], prices_of=read_prices, now: dt.datetime | None = None) -> str:
    """The whole test from the candidate days and a price reader; returns the report's text."""
    spy = prices_of("SPY")
    by_symbol: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for symbol, day, points in cands:
        by_symbol[symbol].append((day, points))
    signals: list[tuple[Signal, list]] = []
    priced_days = defaultdict(int)
    all_days = defaultdict(int)
    for symbol, days in by_symbol.items():
        closes = prices_of(symbol)
        for day, _p in days:
            all_days[day[:4]] += 1
            if closes and closes[0][0] <= day <= closes[-1][0]:
                priced_days[day[:4]] += 1
        if closes:
            signals += [(s, closes) for s in signals_of(symbol, days, closes)]

    def period(year: int) -> str:
        return "first" if year < SPLIT_YEAR else "second"

    study: dict = defaultdict(lambda: defaultdict(lambda: {"ret": [], "excess": []}))
    trades: dict = defaultdict(lambda: {"ret": [], "excess": [], "days": [], "how": defaultdict(int)})
    by_year: dict = defaultdict(lambda: {"excess126": [], "trade": [], "trade_excess": []})
    for signal, closes in signals:
        year = int(signal.filed[:4])
        entry_day = closes[signal.entry_index][0]
        for h in HORIZONS:
            r = forward(signal, closes, h)
            if r is None:
                continue
            b = move_between(spy, entry_day, closes[signal.entry_index + h][0])
            for key in ("all", period(year)):
                study[key][h]["ret"].append(r)
                if b is not None:
                    study[key][h]["excess"].append(r - b)
            if h == 126 and b is not None:
                by_year[year]["excess126"].append(r - b)
        trade = trade_of(signal, closes)
        if trade is None:
            continue
        b = move_between(spy, trade.entry_day, trade.exit_day)
        for key in ("all", period(year)):
            trades[key]["ret"].append(trade.ret)
            trades[key]["days"].append(trade.days)
            trades[key]["how"][trade.how] += 1
            if b is not None:
                trades[key]["excess"].append(trade.ret - b)
        by_year[year]["trade"].append(trade.ret)
        if b is not None:
            by_year[year]["trade_excess"].append(trade.ret - b)

    names = {"all": f"{FIRST_YEAR}–{LAST_YEAR}", "first": f"{FIRST_YEAR}–{SPLIT_YEAR - 1}",
             "second": f"{SPLIT_YEAR}–{LAST_YEAR}"}
    lines = ["# The stock rules on 20 years of SEC data: the results", "",
             f"Run {(now or dt.datetime.now()).isoformat(timespec='seconds')}. The rules of the test are in "
             "PREREGISTRATION.md, written before this run; they are the bot's own, nothing was fitted.", "",
             f"Candidate cluster days: {len(cands)} on {len(by_symbol)} tickers; with prices: "
             f"{sum(priced_days.values())} ({100 * sum(priced_days.values()) / max(1, len(cands)):.0f} %). "
             f"Buy signals: {len(signals)}.", ""]
    v = verdict(summary(study["first"][126]["excess"]), summary(study["second"][126]["excess"]),
                summary(study["all"][126]["excess"]), summary(trades["first"]["excess"]),
                summary(trades["second"]["excess"]))
    lines += [f"**Reading, as fixed beforehand: {v}.**", "",
              "Survivorship: companies later delisted have no prices at Yahoo and are missing, so every figure "
              "below is too good by an unknown amount.", "",
              "## A. The event study: the return after a buy signal, and over the S&P 500", ""]
    for key in ("all", "first", "second"):
        lines += [f"### {names[key]}", "", "| Trading days | Signals | Mean | Median | Above zero | t | "
                  "Mean over SPY | t of the excess |", "|---|---|---|---|---|---|---|---|"]
        for h in HORIZONS:
            ex = summary(study[key][h]["excess"])
            extra = f" {_pct(ex.get('mean'))} | {ex.get('t', 0.0):+.2f} |" if ex.get("n") else " — | — |"
            lines.append(_row(str(h), summary(study[key][h]["ret"]), extra))
        lines.append("")
    lines += ["## B. The bot's exits: trailing stop, dead money, one year; 0.30 % a round trip", "",
              "| Years | Trades | Mean | Median | In profit | t | Profit factor | Mean days held | Mean over SPY | "
              "t of the excess | Exits |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for key in ("all", "first", "second"):
        t = trades[key]
        s, ex = summary(t["ret"]), summary(t["excess"])
        if not s.get("n"):
            lines.append(f"| {names[key]} | 0 | — | — | — | — | — | — | — | — | — |")
            continue
        pf = profit_factor(t["ret"])
        how = ", ".join(f"{k} {n}" for k, n in sorted(t["how"].items()))
        lines.append(f"| {names[key]} | {s['n']} | {_pct(s['mean'])} | {_pct(s['median'])} | {s['win'] * 100:.0f} % | "
                     f"{s['t']:+.2f} | {'∞' if math.isinf(pf) else f'{pf:.2f}'} | {sum(t['days']) / s['n']:.0f} | "
                     f"{_pct(ex.get('mean'))} | {ex.get('t', 0.0):+.2f} | {how} |")
    lines += ["", "## By year", "",
              "| Year | Candidate days | With prices | Signals with a 126-day result | Mean over SPY at 126 days | "
              "Trades | Mean per trade | Mean over SPY per trade |", "|---|---|---|---|---|---|---|---|"]
    for year in range(FIRST_YEAR, LAST_YEAR + 1):
        y = by_year[year]
        e, tr, te = summary(y["excess126"]), summary(y["trade"]), summary(y["trade_excess"])
        lines.append(f"| {year} | {all_days[str(year)]} | {priced_days[str(year)]} | {e.get('n', 0)} | "
                     f"{_pct(e.get('mean'))} | {tr.get('n', 0)} | {_pct(tr.get('mean'))} | {_pct(te.get('mean'))} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    step = (argv or sys.argv[1:] or ["report"])[0]
    purchases = load_purchases()
    print(f"purchases: {len(purchases)}")
    cands = candidates(purchases)
    symbols = sorted({c[0] for c in cands})
    print(f"candidate days: {len(cands)} on {len(symbols)} tickers")
    if step == "purchases":
        return 0
    try:
        left = load_prices(["SPY"] + symbols)
    except Throttled as e:
        print(f"Yahoo is refusing requests ({e}); run this step again later")
        return 1
    if step == "prices" or left:
        print(f"prices still missing: {left}")
        return 0 if step == "prices" else 1
    text = build_report(cands)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(text[:1800])
    return 0


if __name__ == "__main__":
    sys.exit(main())
