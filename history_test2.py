"""history_test2.py: part 2 of the stock rules on history (docs/history/PREREGISTRATION_2.md): other exits
for the same signals, the liquidity floor, what the companies with no prices could do to the result, and
the activist stakes (Schedule 13D).

    python history_test2.py exits        section 1 -- no download
    python history_test2.py volume       the dollar volume of the signalled tickers (Yahoo, in passes)
    python history_test2.py stakes       the 13D filings (EDGAR, in passes), then their tickers' prices
    python history_test2.py report       everything that is ready -> docs/history/REPORT_2.md

The pure parts are tested in tests/test_history_test2.py; the loaders are the only network code and keep
what they fetch under data/history_cache, so a pass cut short is gone on with.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

import history_test as ht
import model_score

OUT = ht.BASE / "docs" / "history" / "REPORT_2.md"
MIN_DOLLAR_VOLUME = 100_000.0       # the liquidity floor, a day
ADV_BARS = 20
ASSUMED_EXCESS = (0.0, -0.10, -0.25, -0.50)
EXITS = ("E0", "E1", "E2", "E3", "E4")
EXIT_NAMES = {"E0": "the bot's: trailing 10–25 %, dead money, 365 days", "E1": "wide trailing 20–40 %, 365 days",
              "E2": "no stop, 126 trading days", "E3": "no stop, 252 trading days",
              "E4": "fixed stop −25 % from the entry, 252 trading days"}


# ================================================================ the signals of part 1, with their closes
def all_signals(cands, prices_of=ht.read_prices) -> list[tuple[ht.Signal, list]]:
    by_symbol: dict[str, list] = defaultdict(list)
    for symbol, day, points in cands:
        by_symbol[symbol].append((day, points))
    out = []
    for symbol, days in by_symbol.items():
        closes = prices_of(symbol)
        if closes:
            out += [(s, closes) for s in ht.signals_of(symbol, days, closes)]
    return out


# ================================================================ 1. the exits
def close_by(rule: str, signal: ht.Signal, closes: list[tuple[str, float]]) -> ht.Trade | None:
    """The signal closed by one of the five exits of the pre-registration; None with no bar after the entry."""
    if rule == "E0":
        return ht.trade_of(signal, closes)
    values = [c for _d, c in closes]
    dates = [d for d, _c in closes]
    i0 = signal.entry_index
    if i0 >= len(values) - 1:
        return None
    entry, start = values[i0], dt.date.fromisoformat(dates[i0])
    how, j = "end", len(values) - 1
    if rule in ("E2", "E3", "E4"):
        limit = i0 + (126 if rule == "E2" else 252)
        floor = entry * 0.75 if rule == "E4" else None
        for k in range(i0 + 1, len(values)):
            if floor is not None and values[k] <= floor:
                how, j = "stop", k
                break
            if k >= limit:
                how, j = "time", k
                break
    elif rule == "E1":
        move = model_score.typical_move(values[:i0 + 1])
        stop = min(max(6 * move, 0.20), 0.40) if move else 0.20
        peak = entry
        for k in range(i0 + 1, len(values)):
            peak = max(peak, values[k])
            if values[k] <= peak * (1 - stop):
                how, j = "stop", k
                break
            if (dt.date.fromisoformat(dates[k]) - start).days >= ht.MAX_DAYS:
                how, j = "time", k
                break
    else:
        raise ValueError(f"unknown exit {rule!r}")
    return ht.Trade(signal.symbol, dates[i0], dates[j], values[j] / entry - 1 - ht.COST, how,
                    (dt.date.fromisoformat(dates[j]) - start).days)


def exit_table(signals: list[tuple[ht.Signal, list]], spy: list) -> dict:
    """{half: {rule: {"ret": [...], "excess": [...]}}} for the halves "first" and "second"."""
    table: dict = {h: {r: {"ret": [], "excess": []} for r in EXITS} for h in ("first", "second")}
    for signal, closes in signals:
        half = "first" if int(signal.filed[:4]) < ht.SPLIT_YEAR else "second"
        for rule in EXITS:
            trade = close_by(rule, signal, closes)
            if trade is None:
                continue
            bench = ht.move_between(spy, trade.entry_day, trade.exit_day)
            table[half][rule]["ret"].append(trade.ret)
            if bench is not None:
                table[half][rule]["excess"].append(trade.ret - bench)
    return table


def choose_exit(table: dict) -> tuple[str, bool]:
    """(the exit the first half chooses -- the highest median excess per trade --, whether the second half
    confirms it: there both its median and its mean excess are above E0's). E0 chosen is nothing to confirm."""
    def med(half, rule):
        v = table[half][rule]["excess"]
        return statistics.median(v) if v else -math.inf

    def mean(half, rule):
        v = table[half][rule]["excess"]
        return sum(v) / len(v) if v else -math.inf

    chosen = max(EXITS, key=lambda r: med("first", r))
    confirmed = (chosen != "E0" and med("second", chosen) > med("second", "E0")
                 and mean("second", chosen) > mean("second", "E0"))
    return chosen, confirmed


# ================================================================ 2. liquidity
def adv_before(dollar_volume: list[tuple[str, float]], entry_day: str, bars: int = ADV_BARS) -> float | None:
    """The mean dollar volume of the `bars` bars before `entry_day`; None with fewer."""
    before = [v for d, v in dollar_volume if d < entry_day]
    return sum(before[-bars:]) / bars if len(before) >= bars else None


def liquid(signal: ht.Signal, closes: list, dollar_volume: list) -> bool:
    adv = adv_before(dollar_volume, closes[signal.entry_index][0])
    return adv is not None and adv >= MIN_DOLLAR_VOLUME


# ================================================================ 3. the companies with no prices
def sensitivity(seen_signals: int, seen_mean: float, seen_days: int, missing_days: int) -> dict:
    """What the missing companies could do to the mean excess: the missing candidate days are assumed to
    become signals at the rate of the seen ones; {"missing": that number, "at": {assumed excess: overall
    mean}, "zero": the excess of the missing at which the overall mean is nil}."""
    missing = round(missing_days * seen_signals / seen_days) if seen_days else 0
    total = seen_signals + missing

    def overall(x: float) -> float:
        return (seen_signals * seen_mean + missing * x) / total if total else 0.0

    return {"missing": missing, "at": {x: overall(x) for x in ASSUMED_EXCESS},
            "zero": -seen_signals * seen_mean / missing if missing else None}


# ================================================================ 4. the stakes
_IDX_FORMS = ("SC 13D", "SCHEDULE 13D")
_SUBJECT = re.compile(r"SUBJECT COMPANY:.*?CENTRAL INDEX KEY:\s*(\d+)", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")
_PERCENT = re.compile(r"PERCENT\s+OF\s+CLASS\s+REPRESENTED\s+BY\s+AMOUNT\s+IN\s+ROW[^0-9%]{0,40}\(?\s*1[13]\s*\)?"
                      r"[^0-9%]{0,250}?(\d{1,3}(?:\.\d+)?)\s*%", re.S | re.I)


def index_rows(text: str) -> list[tuple[str, str, str]]:
    """(accession file name, CIK, ISO date) of the initial 13D rows of a quarterly form index: «SC 13D» or
    «SCHEDULE 13D» exactly, never an amendment. A filing is listed once for each company named in it."""
    out = []
    for line in text.splitlines():
        if not line.startswith(_IDX_FORMS):
            continue
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) < 5 or parts[0] not in _IDX_FORMS:
            continue
        name, date, cik = parts[-1], parts[-2], parts[-3]
        if cik.isdigit() and name.endswith(".txt") and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            out.append((name, cik, date))
    return out


def subject_and_percent(head: str) -> tuple[str | None, float | None]:
    """(the subject company's CIK, the largest «percent of class» of the cover pages) from the head of a
    13D's text; either None when it is not there. A percent of 100 or more is not one."""
    m = _SUBJECT.search(head)
    plain = _TAGS.sub(" ", head).replace("&nbsp;", " ").replace("&#160;", " ")
    found = [float(x) for x in _PERCENT.findall(plain)]
    found = [x for x in found if 0 < x < 100]
    return (str(int(m.group(1))) if m else None), (max(found) if found else None)


def stake_points(percent: float) -> float:
    """model_score.stake_part for an initial activist 13D."""
    class _Stake:
        source, form_type, is_activist, prev_percent = "SEC13DG", "SCHEDULE 13D", True, None
    stake = _Stake()
    stake.percent = percent
    return model_score.stake_part(stake).points


def stake_candidates(filings: list[list]) -> list[tuple[str, str, float]]:
    """(ticker, filing day, stake points) of the filings that momentum could still make a buy."""
    out = []
    for _acc, _cik, ticker, day, percent in filings:
        if not ticker or percent is None:
            continue
        points = stake_points(percent)
        if points >= ht.CANDIDATE_POINTS:
            out.append((ticker, day, points))
    return sorted(set(out), key=lambda c: (c[1], c[0]))


# ================================================================ the loaders (network, cached)
def _dv_path(symbol: str) -> Path:
    return ht.CACHE / "dollar_volume" / f"{symbol}.json"


def read_dollar_volume(symbol: str) -> list[tuple[str, float]]:
    path = _dv_path(symbol)
    return [tuple(r) for r in json.loads(path.read_text(encoding="utf-8"))] if path.exists() else []


def load_dollar_volume(symbols: list[str], *, budget_seconds: float = 420, batch: int = 20, pause: float = 2.0,
                       log=print) -> int:
    """close × volume a day for the symbols not yet on disk (Yahoo, as ht.load_prices: small batches, a
    canary, an empty file only when Yahoo answered). Returns how many are still missing."""
    import yfinance as yf
    _dv_path("x").parent.mkdir(parents=True, exist_ok=True)
    missing = [s for s in symbols if not _dv_path(s).exists()]
    started = time.time()
    while missing and time.time() - started < budget_seconds:
        chunk = missing[:batch]
        asked = chunk + [ht.CANARY]
        frame = yf.download(asked, start=f"{ht.FIRST_YEAR - 1}-01-01", auto_adjust=True, progress=False,
                            group_by="ticker", threads=False)
        got = {}
        for symbol in asked:
            try:
                part = frame[symbol][["Close", "Volume"]].dropna()
                got[symbol] = [[d.date().isoformat(), float(c) * float(v)]
                               for d, c, v in zip(part.index, part["Close"].to_numpy().ravel(),
                                                  part["Volume"].to_numpy().ravel()) if c == c and v == v]
            except Exception:
                got[symbol] = []
        if not got.pop(ht.CANARY, None):
            raise ht.Throttled(f"{ht.CANARY} came back empty; {len(missing)} symbols to go")
        for symbol, rows in got.items():
            _dv_path(symbol).write_text(json.dumps(rows), encoding="utf-8")
        missing = missing[batch:]
        log(f"volume: {len(missing)} symbols to go")
        time.sleep(pause)
    return len(missing)


def tiingo_closes(symbol: str, key: str, session=None) -> list[tuple[str, float]]:
    """Adjusted daily closes from Tiingo, which keeps delisted tickers (section 3): the user's own key
    (TIINGO_API_KEY), 500 tickers a month on the free tier. [] when Tiingo has nothing."""
    import requests
    resp = (session or requests).get(f"https://api.tiingo.com/tiingo/daily/{symbol}/prices",
                                     params={"startDate": f"{ht.FIRST_YEAR - 2}-01-01", "token": key}, timeout=60)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    return [(row["date"][:10], float(row["adjClose"])) for row in resp.json() if row.get("adjClose")]


def load_missing_from_tiingo(symbols: list[str], key: str, *, limit: int = 450, log=print) -> int:
    """Fill the empty price files of `symbols` from Tiingo, at most `limit` tickers a call (the free tier's
    month). Returns how many were filled."""
    filled = 0
    for symbol in symbols:
        path = ht._price_path(symbol)
        if filled >= limit or not path.exists() or path.stat().st_size > 5:
            continue
        rows = tiingo_closes(symbol, key)
        if rows:
            path.write_text(json.dumps([list(r) for r in rows]), encoding="utf-8")
            filled += 1
        time.sleep(1.5)
    log(f"tiingo: {filled} tickers filled")
    return filled


def _stake_path(year: int, quarter: int) -> Path:
    return ht.CACHE / "stakes" / f"{year}q{quarter}.json"


def load_stakes(*, budget_seconds: float = 450, log=print) -> tuple[list[list], bool]:
    """([accession, subject CIK, ticker, day, percent], whether every quarter is read). A quarter's index is
    read, its initial 13Ds grouped by filing, those naming a company with a ticker fetched (the first 60 KB:
    the header and the cover pages) and parsed; the quarter is kept once done. Stops when the budget is spent."""
    import cik_map
    import requests
    session = requests.Session()
    session.headers.update(ht.AGENT)
    tickers = cik_map.CikMap()
    _stake_path(2000, 1).parent.mkdir(parents=True, exist_ok=True)
    started, out, complete = time.time(), [], True
    for year in range(ht.FIRST_YEAR, ht.LAST_YEAR + 1):
        for quarter in range(1, 5):
            path = _stake_path(year, quarter)
            if path.exists():
                out += json.loads(path.read_text(encoding="utf-8"))
                continue
            if time.time() - started > budget_seconds:
                complete = False
                continue
            resp = session.get(f"https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{quarter}/form.idx",
                               timeout=120)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            by_filing: dict[str, dict] = {}
            for name, cik, day in index_rows(resp.text):
                slot = by_filing.setdefault(name, {"day": day, "ciks": set()})
                slot["ciks"].add(str(int(cik)))
            rows, partial = [], path.with_suffix(".partial")
            done = {r[0]: r for r in (json.loads(partial.read_text(encoding="utf-8")) if partial.exists() else [])}
            cut_short = False
            for name, slot in sorted(by_filing.items()):
                if name in done:
                    rows.append(done[name])
                    continue
                if not any(tickers.ticker(c) for c in slot["ciks"]):
                    continue
                if time.time() - started > budget_seconds:
                    cut_short = True
                    break
                try:
                    head = session.get(f"https://www.sec.gov/Archives/{name}", headers={"Range": "bytes=0-60000"},
                                       timeout=60)
                    head.raise_for_status()
                    subject, percent = subject_and_percent(head.text[:80000])
                except requests.RequestException:
                    subject, percent = None, None
                rows.append([name, subject, tickers.ticker(subject) if subject else None, slot["day"], percent])
                time.sleep(0.12)
            if cut_short:
                partial.write_text(json.dumps(rows), encoding="utf-8")
                complete = False
                log(f"{year}q{quarter}: cut short at {len(rows)} filings")
                continue
            path.write_text(json.dumps(rows), encoding="utf-8")
            partial.unlink(missing_ok=True)
            log(f"{year}q{quarter}: {len(rows)} filings read")
            out += rows
    return out, complete


# ================================================================ the report
def _p(x: float | None) -> str:
    return "—" if x is None or x in (math.inf, -math.inf) else f"{x * 100:+.1f} %"


def exits_section(signals, spy) -> list[str]:
    table = exit_table(signals, spy)
    chosen, confirmed = choose_exit(table)
    lines = ["## 1. The exits", "",
             "| Exit | Rule | Half | Trades | Mean | Median | In profit | Mean over SPY | Median over SPY |",
             "|---|---|---|---|---|---|---|---|---|"]
    names = {"first": f"{ht.FIRST_YEAR}–{ht.SPLIT_YEAR - 1}", "second": f"{ht.SPLIT_YEAR}–{ht.LAST_YEAR}"}
    for rule in EXITS:
        for half in ("first", "second"):
            ret, ex = table[half][rule]["ret"], table[half][rule]["excess"]
            if not ret:
                continue
            lines.append(f"| {rule} | {EXIT_NAMES[rule]} | {names[half]} | {len(ret)} | {_p(sum(ret) / len(ret))} | "
                         f"{_p(statistics.median(ret))} | {100 * sum(1 for r in ret if r > 0) / len(ret):.0f} % | "
                         f"{_p(sum(ex) / len(ex) if ex else None)} | {_p(statistics.median(ex) if ex else None)} |")
    if chosen == "E0":
        verdict = "the first half chooses the bot's own exit: no exit shown to be better"
    elif confirmed:
        verdict = (f"the first half chooses {chosen} and the second half confirms it (median and mean excess "
                   f"above the bot's): {chosen} is better than the bot's exit on this history")
    else:
        verdict = f"the first half chooses {chosen}, the second half does not confirm it: no exit shown to be better"
    return lines + ["", f"**Reading, as fixed beforehand: {verdict}.**", ""]


def sensitivity_section(cands, signals, spy, prices_of=ht.read_prices) -> list[str]:
    seen_days = sum(1 for symbol, day, _p_ in cands
                    if (c := prices_of(symbol)) and c[0][0] <= day <= c[-1][0])
    excess = []
    for signal, closes in signals:
        r = ht.forward(signal, closes, 126)
        if r is None:
            continue
        b = ht.move_between(spy, closes[signal.entry_index][0], closes[signal.entry_index + 126][0])
        if b is not None:
            excess.append(r - b)
    if not excess:
        return ["## 3. The companies with no prices", "", "No signals to weigh.", ""]
    mean = sum(excess) / len(excess)
    s = sensitivity(len(excess), mean, seen_days, len(cands) - seen_days)
    lines = ["## 3. The companies with no prices", "",
             f"Signals with a 126-day result: {len(excess)}, mean over SPY {_p(mean)}. Candidate days with prices: "
             f"{seen_days}; without: {len(cands) - seen_days} -- at the same rate about {s['missing']} more signals.", "",
             "| If the missing signals ended this far from SPY | The overall mean over SPY would be |", "|---|---|"]
    lines += [f"| {_p(x)} | {_p(v)} |" for x, v in s["at"].items()]
    lines += ["", f"The overall mean is nil when the missing signals average {_p(s['zero'])} against SPY.", ""]
    return lines


def main(argv: list[str] | None = None) -> int:
    step = (argv or sys.argv[1:] or ["report"])[0]
    cands = ht.candidates(ht.load_purchases(log=lambda *a: None))
    spy = ht.read_prices("SPY")
    signals = all_signals(cands)
    print(f"signals: {len(signals)}")
    lines = ["# The stock rules on history, part 2: the results", "",
             f"Run {dt.datetime.now().isoformat(timespec='seconds')}. The rules are in PREREGISTRATION_2.md, "
             "written before this ran.", ""]
    if step == "volume":
        try:
            left = load_dollar_volume(sorted({s.symbol for s, _c in signals}))
        except ht.Throttled as e:
            print(f"Yahoo is refusing requests ({e}); run this step again later")
            return 1
        print(f"volume still missing: {left}")
        return 0
    stakes, stakes_done = [], False
    if step in ("stakes", "report"):
        stakes, stakes_done = load_stakes(budget_seconds=450 if step == "stakes" else 0)
        print(f"13D filings read: {len(stakes)}; all quarters: {stakes_done}")
        if step == "stakes":
            if stakes_done:
                symbols = sorted({c[0] for c in stake_candidates(stakes)})
                try:
                    print(f"prices still missing: {ht.load_prices(symbols)} of {len(symbols)}")
                except ht.Throttled as e:
                    print(f"Yahoo is refusing requests ({e}); run this step again later")
            return 0
    lines += exits_section(signals, spy)
    if step == "exits":
        print("\n".join(lines))
        return 0
    have_volume = [(s, c) for s, c in signals if _dv_path(s.symbol).exists()]
    if len(have_volume) == len(signals):
        kept = {(s.symbol, s.filed) for s, c in signals if liquid(s, c, read_dollar_volume(s.symbol))}
        liquid_cands = [(sym, day, p) for sym, day, p in cands]
        text = ht.build_report(liquid_cands, prices_of=ht.read_prices, keep=lambda s: (s.symbol, s.filed) in kept)
        lines += ["## 2. Liquidity: only signals trading $100 000 a day or more", "",
                  f"{len(kept)} of {len(signals)} signals pass the floor.", ""] + text.splitlines()[4:] + [""]
    else:
        lines += ["## 2. Liquidity", "", f"Not run: the volume of {len(signals) - len(have_volume)} signals is "
                  "not downloaded yet (`python history_test2.py volume`).", ""]
    lines += sensitivity_section(cands, signals, spy)
    if stakes_done:
        scands = stake_candidates(stakes)
        unread = sum(1 for r in stakes if r[2] and r[4] is None)
        named = sum(1 for r in stakes if r[2])
        text = ht.build_report(scands, prices_of=ht.read_prices)
        lines += ["## 4. Activist stakes (13D)", "",
                  f"Initial 13D filings on a company with a ticker today: {named}; percent not readable: {unread}; "
                  f"candidates (a stake of 12.5 % to under 50 %): {len(scands)}.", ""] + text.splitlines()[4:] + [""]
    else:
        lines += ["## 4. Activist stakes (13D)", "", "Not run: the filings are not all read yet "
                  "(`python history_test2.py stakes`).", ""]
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"written: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
