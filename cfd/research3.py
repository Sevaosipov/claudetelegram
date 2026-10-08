"""cfd/research3.py: round 3 of the CFD research (forex), exactly as docs/cfd/PREREGISTRATION_R3.md fixes it --
two hypotheses and no more, each judged on its own by the gate written there -- and the two outputs of that
document: docs/cfd/BACKTEST_REPORT_R3.md and the `round3` block of docs/cfd/enabled.json.

    python -m cfd.research3 --run [--refresh]

The hypotheses:
  H6 FX-REV         `fx_rev` (ADX(14) < 20 and the close beyond SMA20 -/+ 2.0 x SD20; stop 2.0 ATR from the
                    signal close) on eight pairs of linked economies (EURCHF, EURGBP, AUDNZD, AUDCAD, NZDCAD,
                    USDCAD, EURNOK, EURSEK). One exit, E0c: the stop, or the next open after the first close
                    at or beyond SMA20 (long: close >= SMA20; short: close <= SMA20), or the open of the 16th
                    bar after entry. Each pair pays its own round trip (0.030 %; USDCAD 0.015 %; EURNOK and
                    EURSEK 0.060 %) and 0.010 % a night. Pooled over the universe.
  H7 CARRY-BASKET   the three highest-yielding of USD, EUR, GBP, JPY, AUD, CAD, CHF, NZD against the three
                    lowest, rebalanced on the first daily bar of each month (cfd/basket.py), the OECD 3-month
                    rates of round 2 (cfd/rates.py). Judged on monthly returns after costs.
Everything the document does not restate is rounds 1-2's: data hygiene (15 % wick rule), ATR(14) Wilder, the next
open as entry, a stop gapped through filling at the open, the stop before any other exit within a bar, the skip
rule (R <= 0.25 ATR or R > 6 ATR), one open trade per instrument, the result in R after costs, in-sample
2006-01-01..2016-12-31 and out-of-sample from 2017-01-01. So the trade accounting, the statistics and the
one-open-trade walk are round 1's own functions (cfd/research.py), reached through round 2's wiring.

The gate, per hypothesis, on its out-of-sample sample: at least 100 trades (H6) or months (H7), profit factor
>= 1.10 after costs, a mean R (month) above zero with a one-sided t-statistic >= 2.45 (Bonferroni for the seven
hypotheses of the three rounds at 5 %), and an in-sample mean above zero. A hypothesis that passes is enabled
for its whole universe, with no per-pair picking; one that fails is not traded, and there is no further round
on this data.

`enabled.json` gains {"round3": {"enabled": [...], "exit": {...}, "run_at": ...}}; the keys of rounds 1 and 2
stay as they are. Everything that decides it is pure and tested on synthetic trade lists and months; the network
sits behind `fetch` (Yahoo bars, cfd/data.py) and `rates_fetch` (FRED, cfd/rates.py), both cached under
data/cfd_cache/.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from cfd import basket as bk
from cfd import data, exits
from cfd import indicators as ind
from cfd import instruments as ins
from cfd import rates as rates_mod
from cfd import research as r1
from cfd import research2 as r2
from cfd import setups

UTC = dt.timezone.utc

FX_REV, CARRY_BASKET = "FX-REV", "CARRY-BASKET"
HYPOTHESES = (FX_REV, CARRY_BASKET)
LABELS = {FX_REV: "H6", CARRY_BASKET: "H7"}
UNIVERSES: dict[str, tuple[ins.Instrument, ...]] = {
    FX_REV: ins.H6_UNIVERSE, CARRY_BASKET: ins.H7_UNIVERSE}
MONTHLY = "monthly"                      # the "exit" of H7 in the payload: a month is held to the next rebalance
JUDGED = {FX_REV: exits.E0C, CARRY_BASKET: MONTHLY}
UNITS = {FX_REV: "trades", CARRY_BASKET: "months"}

OUT_DIR = r1.OUT_DIR
REPORT_NAME = "BACKTEST_REPORT_R3.md"
ENABLED_NAME = "enabled.json"
BLOCK = "round3"

# H6's exit (PREREGISTRATION_R3.md)
REV_TIME_STOP = 16            # leave at the open of the 16th bar after entry

# the gate (PREREGISTRATION_R3.md)
MIN_OOS = {FX_REV: 100, CARRY_BASKET: 100}
GATE_MIN_PF = 1.10
GATE_MIN_T = 2.45             # one-sided; Bonferroni for the seven hypotheses of the three rounds at 5 %

# the formatting of round 1, so all reports print their numbers the same way
_table, _num, _signed, _exit_label = r1._table, r1._num, r1._signed, r1._exit_label
_STATS_HEAD, stats_cells = r1._STATS_HEAD, r1.stats_cells
Gate3 = r2.Gate2
Check = r1.Check


def _pct(x: float, places: int = 3) -> str:
    """A fraction as a signed percentage: 0.00123 -> +0.123%."""
    return f"{x * 100:+.{places}f}%"


# ================================================================== records
@dataclass
class Results3(r2.Results2):
    months: list[bk.Month] = field(default_factory=list)
    period_of_month: dict[dt.date, str | None] = field(default_factory=dict)


@dataclass
class Decision3:
    judged: dict[str, str]                         # hypothesis -> its exit ("monthly" for H7)
    gates: dict[str, Gate3]
    enabled: list[str]                             # the hypotheses that passed
    rev: dict[str, r1.Stats]                       # period -> pooled stats of H6's trades
    basket: dict[str, bk.MonthStats]               # period -> stats of H7's months


@dataclass
class Outcome3:
    results: Results3
    decision: Decision3
    report: str
    payload: dict                                  # the round3 block of enabled.json


# ================================================================== H6: running the reversion
def beyond_sma(closes: Sequence[float], sma: Sequence[float | None]) -> Callable[[int, str], bool]:
    """The exit condition of H6, read at the close of bar j: a long is done when the close is at or above
    SMA20, a short when it is at or below (equal counts). No average yet, no condition."""
    def reached(j: int, side: str) -> bool:
        avg = sma[j]
        if avg is None:
            return False
        return closes[j] >= avg if side == "long" else closes[j] <= avg
    return reached


def backtest_fx_rev(inst: ins.Instrument, bars: Sequence[data.Bar]
                    ) -> tuple[list[r1.Row], dict[tuple[str, str], r1.Counts]]:
    """H6: the reversion signal of one pair under E0c with no trailing stop: the signal's stop, or the next
    open after the first close at or beyond SMA20, or the open of the 16th bar after entry. The pair's own
    costs (`inst.costs`: its round trip and 0.010 % a night)."""
    closes = ind.closes(bars)
    sma20 = ind.sma(closes, setups.REV_SMA_LEN)
    plan = [(exits.E0C, dict(time_stop_bars=REV_TIME_STOP, exit_when=beyond_sma(closes, sma20)))]
    return r2._backtest(FX_REV, inst, bars, setups.fx_rev(bars), inst.costs, plan)


# ================================================================== H7: running the basket
def basket_values(bars_by_symbol: Mapping[str, Sequence[data.Bar]]) -> dict[str, list[tuple[dt.date, float]]]:
    """The price in USD of each currency whose pair loaded (ins.H7_PAIRS): XXXUSD=X as it is, USDXXX=X
    inverted."""
    out: dict[str, list[tuple[dt.date, float]]] = {}
    for currency, (symbol, inverted) in ins.H7_PAIRS.items():
        bars = bars_by_symbol.get(symbol)
        if bars:
            out[currency] = bk.usd_values(bars, inverted)
    return out


def backtest_basket(bars_by_symbol: Mapping[str, Sequence[data.Bar]], rates: rates_mod.Rates
                    ) -> tuple[list[bk.Month], dict[dt.date, str | None]]:
    """H7: every complete month of the basket (cfd/basket.py) with its period, by the day it was set up
    (2006-01-01..2016-12-31 in-sample, from 2017-01-01 out-of-sample; earlier months are warm-up and
    belong to neither)."""
    months = bk.run_months(basket_values(bars_by_symbol), rates.rate_on)
    return months, {m.start: r1.period_of("FX", m.start) for m in months}


# ================================================================== the gate
def _gate(name: str, oos_n: int, oos_pf: float, oos_mean_text: str, oos_mean: float, oos_t: float,
          is_mean_text: str, is_mean: float, unit: str) -> Gate3:
    need = MIN_OOS[name]
    word = unit[:-1] if unit.endswith("s") else unit
    checks = [
        Check(name, f"out-of-sample {unit}", str(oos_n), f">= {need}", oos_n >= need),
        Check(name, "out-of-sample profit factor", _num(oos_pf, 4), f">= {GATE_MIN_PF:.2f}",
              oos_pf >= GATE_MIN_PF),
        Check(name, f"out-of-sample mean {word}" if unit == "months" else "out-of-sample mean R",
              oos_mean_text, "> 0", oos_mean > 0),
        Check(name, "out-of-sample t-statistic", _num(oos_t, 4), f">= {GATE_MIN_T:.2f}", oos_t >= GATE_MIN_T),
        Check(name, f"in-sample mean {word}" if unit == "months" else "in-sample mean R",
              is_mean_text, "> 0", is_mean > 0),
    ]
    return Gate3(name, JUDGED[name], checks, all(c.passed for c in checks))


def gate_fx_rev(is_stats: r1.Stats, oos: r1.Stats) -> Gate3:
    """The gate of H6: out-of-sample trades >= 100, profit factor >= 1.10 after costs, mean R > 0 with a
    one-sided t >= 2.45, and in-sample mean R > 0 (always required: the pre-registration has no
    minimum-trades exception for it; no in-sample trades is a mean of 0, which fails)."""
    return _gate(FX_REV, oos.n, oos.profit_factor, _signed(oos.mean_r, 4), oos.mean_r, oos.t_stat,
                 f"{_signed(is_stats.mean_r, 4)} ({is_stats.n} trades)", is_stats.mean_r, "trades")


def gate_carry_basket(is_stats: bk.MonthStats, oos: bk.MonthStats) -> Gate3:
    """The gate of H7: out-of-sample months >= 100, profit factor >= 1.10 on the months, a mean month > 0
    with a one-sided t >= 2.45, and an in-sample mean month > 0."""
    return _gate(CARRY_BASKET, oos.n, oos.profit_factor, _pct(oos.mean, 4), oos.mean, oos.t_stat,
                 f"{_pct(is_stats.mean, 4)} ({is_stats.n} months)", is_stats.mean, "months")


def decide(rows: Sequence[r1.Row], months: Sequence[bk.Month],
           period_of_month: Mapping[dt.date, str | None]) -> Decision3:
    """Apply each hypothesis's gate to its own pooled sample: H6 to the trades of FX-REV, H7 to the months
    of the basket (the months of a period are those whose start day falls in it)."""
    rev = {p: r1.stats(r2._select(rows, FX_REV, exits.E0C, p)) for p in ("IS", "OOS")}
    basket = {p: bk.stats_of([m for m in months if period_of_month.get(m.start) == p])
              for p in ("IS", "OOS")}
    gates = {FX_REV: gate_fx_rev(rev["IS"], rev["OOS"]),
             CARRY_BASKET: gate_carry_basket(basket["IS"], basket["OOS"])}
    return Decision3(dict(JUDGED), gates, [n for n in HYPOTHESES if gates[n].passed], rev, basket)


def round3_payload(decision: Decision3, now: dt.datetime) -> dict:
    """The `round3` block of enabled.json: the hypotheses that passed, the exit each is judged on (both,
    passed or not), and when the run was made."""
    return {"enabled": list(decision.enabled), "exit": dict(decision.judged), "run_at": now.isoformat()}


# ================================================================== the outputs
def _merged_enabled(path: Path, block: dict) -> tuple[dict, str]:
    """enabled.json with `round3` set (replaced where it already is, else appended) and everything else
    as it was. A file that is not a JSON object is refused: it is not ours to overwrite."""
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise ValueError(f"{path} is not valid JSON ({e}): round 3 is not merged into it") from e
        if not isinstance(existing, dict):
            raise ValueError(f"{path} is not a JSON object: round 3 is not merged into it")
    merged = {**existing, BLOCK: block}
    return merged, json.dumps(merged, indent=2, ensure_ascii=False) + "\n"


def merge_enabled(path: Path | str, block: dict) -> dict:
    """Merge the round-3 block into enabled.json (created when missing); returns the merged object."""
    path = Path(path)
    merged, text = _merged_enabled(path, block)
    r2._write_atomically(path, text)
    return merged


def write_outputs(report: str, block: dict, out_dir: Path | str = OUT_DIR) -> list[Path]:
    """Write BACKTEST_REPORT_R3.md into `out_dir` and merge the round-3 block into its enabled.json. The
    merge is prepared first, so a file that cannot be merged into stops the run before anything is written.
    The reports of rounds 1 and 2 are never touched."""
    out_dir = Path(out_dir)
    report_path, json_path = out_dir / REPORT_NAME, out_dir / ENABLED_NAME
    _, text = _merged_enabled(json_path, block)
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    r2._write_atomically(json_path, text)
    return [report_path, json_path]


# ================================================================== the report
_TITLES = {
    FX_REV: "range reversion in pairs of linked economies",
    CARRY_BASKET: "the three highest-yielding majors against the three lowest, monthly",
}
_RULES = {
    FX_REV: ("Signal at the close of bar t, when ADX(14) is under 20: long when the close is under SMA20 - 2.0 x "
             "SD20, short when it is over SMA20 + 2.0 x SD20 (SD20 the population standard deviation of the last "
             "20 closes). Entry at the open of bar t+1; stop 2.0 ATR(14) from the signal close, R measured from "
             "the actual entry. Exit, the earliest of: the stop; the next open after the first close at or beyond "
             "SMA20 (long: close >= SMA20; short: close <= SMA20); the open of the 16th bar after entry. Costs: "
             "round trip 0.030 % (USDCAD 0.015 %; EURNOK and EURSEK 0.060 %) and 0.010 % a night. Pooled over "
             "the eight pairs. In-sample 2006-2016, out-of-sample from 2017."),
    CARRY_BASKET: ("Currencies: USD, EUR, GBP, JPY, AUD, CAD, CHF, NZD, ranked on the first daily bar of each "
                   "calendar month by the OECD 3-month rate in force that day (month M usable from the 15th of "
                   "M+1); a currency with no rate is left out and at least six are needed. Long the top three, "
                   "short the bottom three, equal weights, held to the first bar of the next month. A leg's "
                   "return against USD = its spot change over the month + (its rate - the USD rate) / 12 / 100; "
                   "a month = mean(long legs) - mean(short legs). Costs per month: 0.015 % x 2 for every leg "
                   "that enters or leaves the basket, weighted 1/3, and a financing markup of 0.004 % a night "
                   "on each of the six legs, weighted 1/3 (0.008 % x the nights in the month). In-sample "
                   "2006-2016, out-of-sample from 2017, by the month's first day."),
}
_MONTH_HEAD = ["Months", "Win", "PF", "Mean month", "t", "Total", "Worst peak-to-trough"]


def month_cells(st: bk.MonthStats) -> list[str]:
    """MonthStats as the table cells of the report."""
    if st.n == 0:
        return ["0"] + ["–"] * 6
    return [str(st.n), f"{st.win_rate * 100:.1f}%", _num(st.profit_factor, 2), _pct(st.mean, 3),
            _num(st.t_stat, 2), _pct(st.total, 1), f"{st.max_drawdown_pct:.1f}%"]


def _gate_table(gate: Gate3) -> list[str]:
    body = [[c.rule, c.value, c.threshold, "PASS" if c.passed else "FAIL"] for c in gate.checks]
    out = [f"### {gate.name} (judged on {gate.exit_kind})", ""]
    return out + _table(["Rule", "Value", "Threshold", "Result"], body)


def _rev_section(rows: Sequence[r1.Row]) -> list[str]:
    out = [f"## {FX_REV}", "", f"{LABELS[FX_REV]}: {_TITLES[FX_REV]}.", "", _RULES[FX_REV], "",
           "### Pooled, by period", ""]
    body = [[period, *stats_cells(r1.stats(r2._select(rows, FX_REV, exits.E0C, period)))]
            for period in ("IS", "OOS")]
    out += _table(["Period", *_STATS_HEAD], body)
    out += ["### By pair (for information, not a gate)", ""]
    body = []
    have = {r.symbol for r in rows if r.setup == FX_REV}
    for inst in ins.H6_UNIVERSE:
        if inst.symbol not in have:
            continue
        mine = [r for r in r2._select(rows, FX_REV, exits.E0C) if r.symbol == inst.symbol]
        for period in ("IS", "OOS"):
            body.append([inst.name, period, *stats_cells(r1.stats([r for r in mine if r.period == period]))])
    return out + _table(["Pair", "Period", *_STATS_HEAD], body)


def _basket_section(months: Sequence[bk.Month], period_of_month: Mapping[dt.date, str | None]) -> list[str]:
    out = [f"## {CARRY_BASKET}", "", f"{LABELS[CARRY_BASKET]}: {_TITLES[CARRY_BASKET]}.", "",
           _RULES[CARRY_BASKET], "", "### Pooled, by period", ""]
    in_study = [m for m in months if period_of_month.get(m.start) is not None]
    body = [[period, *month_cells(bk.stats_of([m for m in in_study if period_of_month[m.start] == period]))]
            for period in ("IS", "OOS")]
    out += _table(["Period", *_MONTH_HEAD], body)
    out += ["### By year (for information, not a gate)", ""]
    body = [[str(year), *month_cells(st)] for year, st in bk.by_year(in_study)]
    out += _table(["Year", *_MONTH_HEAD], body)
    if in_study:
        costs = sum(m.cost for m in in_study) / len(in_study)
        turn = sum(m.events for m in in_study) / len(in_study)
        out += [f"Mean cost per month {costs * 100:.3f}%, mean legs entering or leaving per month {turn:.2f}, "
                f"{len(in_study)} months from {in_study[0].start.isoformat()} to "
                f"{in_study[-1].start.isoformat()}.", ""]
    return out


def _notes() -> list[str]:
    return [
        "- H6 results are in R after costs: one round trip per trade plus financing for every calendar night held; "
        "each pair pays its own round trip (0.030 %, USDCAD 0.015 %, EURNOK and EURSEK 0.060 %) and 0.010 % a "
        "night. H7 results are in % of the basket's notional per month, after the costs listed above.",
        "- Everything the pre-registration does not restate is rounds 1-2's: data hygiene (15 % wick rule), "
        "ATR(14), the bar order (stop before any other exit within a bar), gaps filling at the open, the skip "
        "rule (R <= 0.25 ATR, or R > 6 ATR), one open trade per pair, and 300 bars of history before a first "
        "signal.",
        "- H6's time stop of 16 bars leaves at the open of the 16th bar after the entry bar (the bar whose open is "
        "the entry); the SMA20 condition is read at each close from the entry bar's own close on and leaves at "
        "the next bar's open. Both fill at that open, before the bar's own stop; the earlier of the two wins. "
        "The stop is fixed at the signal (the close -/+ 2.0 ATR) and does not trail.",
        "- H7: the rebalance day is the first day of each calendar month on which any of the seven USD pairs has "
        "a bar; a pair that lacks that day is priced at its latest bar before it. A currency with no rate, or "
        "whose pair has no price covering the month, is left out of that month's ranking; fewer than six "
        "left means no basket and no month. Ties in the rate go to the earlier of USD, EUR, GBP, JPY, AUD, CAD, "
        "CHF, NZD. A leg that changes side counts as one leaving and one entering. The month still running when "
        "the data ends is not a month. The worst peak-to-trough is taken on the compounded curve of the months.",
        "- In-sample and out-of-sample are split by the signal date (H6) or the month's first day (H7); "
        "earlier ones are warm-up only. The in-sample line of the gate is always required.",
        "- The t-statistics pool trades across pairs and treat them as independent (trades that overlap in "
        "time in correlated pairs make the true uncertainty larger); H7's months overlap in nothing but share "
        "the dollar. The bar of 2.45 is Bonferroni for the seven hypotheses of the three rounds.",
        "- The gate judges each hypothesis on its pooled sample. A hypothesis that passes is enabled for its "
        "whole universe; there is no per-pair picking, a hypothesis that fails is not traded, and there is no "
        "further round on this data.",
    ]


def render_report(results: Results3, decision: Decision3, now: dt.datetime) -> str:
    """The text of docs/cfd/BACKTEST_REPORT_R3.md: the outcome, the gate of each hypothesis, its in-sample
    and out-of-sample table, per-pair (H6) and per-year (H7) tables for information, and what the data was."""
    daily_last = [c.last for c in results.coverage if c.interval == "1d" and c.last]
    rates_last = [c.last for c in results.rate_coverage if c.last]
    through = f"daily bars through {max(daily_last).isoformat()}" if daily_last else "no daily bars"
    source = f"Source: Yahoo (yfinance), {through}; FRED, OECD 3-month interbank rates"
    if rates_last:
        source += f" through {max(rates_last).isoformat()[:7]}"
    out = ["# CFD backtest report, round 3 (forex)", "",
           f"Run at {now.isoformat()}. {source}. Pre-registration: docs/cfd/PREREGISTRATION_R3.md.", ""]

    out += ["## Outcome", ""]
    if decision.enabled:
        out += ["Enabled for live signals: " + ", ".join(decision.enabled) + ".", ""]
    else:
        out += ["**No hypothesis passed the gate: nothing from round 3 goes live.**", ""]
    rev, basket = decision.rev["OOS"], decision.basket["OOS"]
    body = [
        [f"{LABELS[FX_REV]} {FX_REV}", decision.gates[FX_REV].verdict, f"{rev.n} trades",
         _num(rev.profit_factor, 2), f"{_signed(rev.mean_r, 3)} R", _num(rev.t_stat, 2)],
        [f"{LABELS[CARRY_BASKET]} {CARRY_BASKET}", decision.gates[CARRY_BASKET].verdict,
         f"{basket.n} months", _num(basket.profit_factor, 2), _pct(basket.mean, 3), _num(basket.t_stat, 2)],
    ]
    out += _table(["Hypothesis", "Gate", "Out-of-sample sample", "PF", "Mean", "t"], body)

    out += ["## Gate", "",
            "Each hypothesis on its own; every line is pass or fail. A hypothesis that fails is not traded, "
            "and there is no further round on this data.", ""]
    for name in HYPOTHESES:
        out += _gate_table(decision.gates[name])

    out += _rev_section(results.rows)
    out += _basket_section(results.months, results.period_of_month)

    out += ["## Trade accounting", ""]
    body = [[FX_REV, kind, *(str(getattr(results.counts[(FX_REV, kind)], f)) for f in
                             ("signals", "closed", "skipped", "open", "blocked"))]
            for kind in (exits.E0C,) if (FX_REV, kind) in results.counts]
    out += _table(["Hypothesis", "Exit", "Signals", "Closed", "Skipped (R rules)", "Open at end of data",
                   "Blocked (a trade was open)"], body)

    out += r2._data_section(results)
    out += ["## Notes", "", *_notes(), ""]
    return "\n".join(out)


# ================================================================== the run
def _log(message: str) -> None:
    print(f"[cfd.research3] {message}", file=sys.stderr)


def run(*, fetch: data.Fetch | None = None, rates_fetch: rates_mod.Fetch | None = None,
        now: dt.datetime | None = None, cache_dir: Path | str = data.CACHE_DIR,
        out_dir: Path | str = OUT_DIR, refresh: bool = False,
        universes: Mapping[str, Sequence[ins.Instrument]] | None = None, write: bool = True,
        log: Callable[[str], None] = _log) -> Outcome3:
    """Load every series (through the caches), run the two hypotheses, decide, report. A series that fails to
    load is logged and left out, and the report lists it as having no data; a currency whose rates fail is
    left out of every month's ranking. `universes` (hypothesis -> instruments; a missing hypothesis runs on
    nothing) restricts the run; the default is the pre-registered one."""
    now = (now or dt.datetime.now(UTC)).replace(microsecond=0)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if universes is None:
        universes = UNIVERSES
    universe = {name: tuple(universes.get(name, ())) for name in HYPOTHESES}
    source = data.cached_fetch(fetch or data.yahoo_fetch, cache_dir, refresh=refresh)
    results = Results3()

    rates = rates_mod.Rates()
    if universe[CARRY_BASKET]:
        currencies = list(rates_mod.SERIES)
        rates = rates_mod.load_rates(currencies, fetch=rates_fetch, cache_dir=cache_dir, refresh=refresh,
                                     log=log)
        for currency in currencies:
            series = rates.series.get(currency)
            if series is None:
                results.rate_coverage.append(r2.RateCoverage(currency, rates_mod.SERIES[currency], 0, None,
                                                             None, None, rates.errors.get(currency, "not loaded")))
            else:
                months = [m for m, _ in series.observations]
                results.rate_coverage.append(r2.RateCoverage(
                    currency, series.series_id, len(months), months[0], months[-1],
                    (rates_mod.cache_stamp(cache_dir, currency) or "")[:10] or None))

    loaded_by_symbol: dict[str, data.Loaded | None] = {}

    def load(inst: ins.Instrument, name: str) -> data.Loaded | None:
        if inst.symbol in loaded_by_symbol:            # USDCAD serves both hypotheses: loaded and listed once
            return loaded_by_symbol[inst.symbol]
        try:
            loaded = data.load_daily(inst.symbol, fetch=source, today=now.date(),
                                     max_wick=r2.max_wick_for(inst))
        except Exception as e:      # one dead symbol must not cost the whole run
            log(f"{inst.symbol}: no daily data ({type(e).__name__}: {str(e)[:120]})")
            loaded = None
        stamp = data.cache_stamp(cache_dir, inst.symbol, data.DAILY)
        results.coverage.append(r1._coverage(inst, data.DAILY, loaded, stamp[:10] if stamp else None))
        if loaded is not None and loaded.bars:
            log(f"{inst.symbol}: {len(loaded.bars)} daily bars ({name})")
        loaded_by_symbol[inst.symbol] = loaded
        return loaded

    for inst in universe[FX_REV]:
        loaded = load(inst, FX_REV)
        if loaded is None or not loaded.bars:
            continue
        rows, counts = backtest_fx_rev(inst, loaded.bars)
        results.rows += rows
        for key, c in counts.items():
            results.counts.setdefault(key, r1.Counts()).merge(c)

    bars_by_symbol: dict[str, list[data.Bar]] = {}
    for inst in universe[CARRY_BASKET]:
        loaded = load(inst, CARRY_BASKET)
        if loaded is not None and loaded.bars:
            bars_by_symbol[inst.symbol] = loaded.bars
    if universe[CARRY_BASKET]:
        results.months, results.period_of_month = backtest_basket(bars_by_symbol, rates)

    decision = decide(results.rows, results.months, results.period_of_month)
    report = render_report(results, decision, now)
    payload = round3_payload(decision, now)
    if write:
        write_outputs(report, payload, out_dir)
    return Outcome3(results, decision, report, payload)


def summary_lines(decision: Decision3) -> list[str]:
    """What the command prints for each hypothesis: its gate (with every failed line) and the key numbers
    out-of-sample and in-sample."""
    lines = []
    rev_o, rev_i = decision.rev["OOS"], decision.rev["IS"]
    gate = decision.gates[FX_REV]
    lines.append(f"{LABELS[FX_REV]} {FX_REV}: gate {gate.verdict}; out-of-sample {rev_o.n} trades, "
                 f"PF {_num(rev_o.profit_factor, 2)}, mean R {_signed(rev_o.mean_r, 3)}, "
                 f"t {_num(rev_o.t_stat, 2)}; in-sample {rev_i.n} trades, PF {_num(rev_i.profit_factor, 2)}, "
                 f"mean R {_signed(rev_i.mean_r, 3)}")
    if gate.failures():
        lines.append("  failed: " + "; ".join(f"{c.rule} {c.value}, needs {c.threshold}"
                                              for c in gate.failures()))
    b_o, b_i = decision.basket["OOS"], decision.basket["IS"]
    gate = decision.gates[CARRY_BASKET]
    lines.append(f"{LABELS[CARRY_BASKET]} {CARRY_BASKET}: gate {gate.verdict}; out-of-sample {b_o.n} months, "
                 f"PF {_num(b_o.profit_factor, 2)}, mean month {_pct(b_o.mean, 3)}, t {_num(b_o.t_stat, 2)}, "
                 f"worst peak-to-trough {b_o.max_drawdown_pct:.1f}%; in-sample {b_i.n} months, "
                 f"PF {_num(b_i.profit_factor, 2)}, mean month {_pct(b_i.mean, 3)}, "
                 f"t {_num(b_i.t_stat, 2)}, worst peak-to-trough {b_i.max_drawdown_pct:.1f}%")
    if gate.failures():
        lines.append("  failed: " + "; ".join(f"{c.rule} {c.value}, needs {c.threshold}"
                                              for c in gate.failures()))
    return lines


def main(argv: Sequence[str] | None = None, *, fetch: data.Fetch | None = None,
         rates_fetch: rates_mod.Fetch | None = None, now: dt.datetime | None = None,
         cache_dir: Path | str | None = None, out_dir: Path | str | None = None,
         universes: Mapping[str, Sequence[ins.Instrument]] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cfd.research3", description=__doc__.split("\n\n")[0])
    ap.add_argument("--run", action="store_true",
                    help="download (or read from data/cfd_cache/) the bars and rates, run round 3, write "
                         "docs/cfd/BACKTEST_REPORT_R3.md and merge the round3 block into docs/cfd/enabled.json")
    ap.add_argument("--refresh", action="store_true", help="download the bars and rates again")
    args = ap.parse_args(argv)
    if not args.run:
        print("Nothing to do: pass --run to run round 3 (it downloads from Yahoo and FRED on the first "
              "run). Usage: python -m cfd.research3 --run [--refresh]")
        return 2
    target = Path(out_dir if out_dir is not None else OUT_DIR)
    outcome = run(fetch=fetch, rates_fetch=rates_fetch, now=now, refresh=args.refresh,
                  cache_dir=cache_dir if cache_dir is not None else data.CACHE_DIR, out_dir=target,
                  universes=universes)
    for line in summary_lines(outcome.decision):
        print(line)
    missing = [f"{c.symbol} ({c.interval})" for c in outcome.results.coverage if c.bars == 0]
    if missing:      # the verdict above rests on a partial universe: re-run (the caches keep the rest)
        print("WARNING: no data for " + ", ".join(missing) + " -- the outcome covers the rest only")
    no_rates = [c.currency for c in outcome.results.rate_coverage if c.error is not None or c.observations == 0]
    if no_rates:
        print("WARNING: no rates for " + ", ".join(no_rates) + " -- they are left out of the basket")
    print(f"enabled: {', '.join(outcome.decision.enabled) or 'none'}")
    print(f"written: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
