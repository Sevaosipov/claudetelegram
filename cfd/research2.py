"""cfd/research2.py: round 2 of the CFD research, exactly as docs/cfd/PREREGISTRATION_R2.md fixes it --
three hypotheses and no more, each judged on its own by the gate written there -- and the two outputs
of that document: docs/cfd/BACKTEST_REPORT_R2.md and the `round2` block of docs/cfd/enabled.json.

    python -m cfd.research2 --run [--refresh]

The hypotheses:
  H3 CR-BO     BO-D (cfd/setups.py, as in round 1) on the eleven alt coins; E0, E1 and E2 as in round 1;
               round trip 0.40 %, financing 0.06 % a night; in-sample to 2019-12-31. Judged on E1 or E2,
               whichever has the higher out-of-sample mean R.
  H4 IDX-DIP   `idx_dip` (close > SMA200 and RSI(2) < 10, long only) on the six indices; E0c (leave at the
               next open after a close above SMA5, at the open of the 11th bar after entry, or at the stop)
               and EL (four quarters at +0.5R, +1R, +1.5R, +2R; the rest at the open of the 16th bar).
               The round-1 indices costs. Judged on EL.
  H5 CARRY-FX  `carry_fx` on the eleven FX pairs with OECD 3-month rates (cfd/rates.py); E0c (leave at the
               next open after the state stops being the trade's side, or at a 6 ATR trailing stop) and EL
               (the same ladder, the remainder leaving on the state, no time stop). The round-1 FX round
               trips and 0.004 % a night. R is up to 8 ATR (the stop is 6 ATR away by construction).
               Judged on EL.
Everything the document does not restate is round 1's: data hygiene, ATR(14), the bar order (stop before
target), gaps, the skip rule (R <= 0.25 ATR or R > 6 ATR), one open trade per instrument per setup, the
result in R after costs, and the report columns. So the trade accounting, the statistics, the period split
and the one-open-trade walk are round 1's own functions (cfd/research.py).

The gate, per hypothesis, on the pooled out-of-sample trades of its judged exit: at least 100 trades (H5: 40),
profit factor >= 1.10 after costs, mean R > 0 with a one-sided t-statistic >= 2.33, and in-sample mean R > 0
when the in-sample period has at least 30 trades (otherwise reported, not required). A hypothesis that passes
is enabled for its whole universe, with no per-instrument picking; one that fails is not traded, and there
is no round 3 on this data.

`enabled.json` gains {"round2": {"enabled": [...], "exit": {...}, "run_at": ...}}; the round-1 keys stay as
they are. Everything that decides it is pure and tested on synthetic trade lists; the network sits behind
`fetch` (Yahoo bars, cfd/data.py) and `rates_fetch` (FRED, cfd/rates.py), both cached under data/cfd_cache/.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from cfd import data, exits
from cfd import indicators as ind
from cfd import instruments as ins
from cfd import rates as rates_mod
from cfd import research as r1
from cfd import setups

UTC = dt.timezone.utc

CR_BO, IDX_DIP, CARRY_FX = "CR-BO", "IDX-DIP", "CARRY-FX"
HYPOTHESES = (CR_BO, IDX_DIP, CARRY_FX)
LABELS = {CR_BO: "H3", IDX_DIP: "H4", CARRY_FX: "H5"}
UNIVERSES: dict[str, tuple[ins.Instrument, ...]] = {
    CR_BO: ins.ALT_COINS, IDX_DIP: ins.INDEX_UNIVERSE, CARRY_FX: ins.FX_UNIVERSE}
EXITS = {CR_BO: exits.EXIT_KINDS, IDX_DIP: exits.ROUND2_EXIT_KINDS, CARRY_FX: exits.ROUND2_EXIT_KINDS}

OUT_DIR = r1.OUT_DIR
REPORT_NAME = "BACKTEST_REPORT_R2.md"
ENABLED_NAME = "enabled.json"

# the exits (PREREGISTRATION_R2.md)
IDX_SMA_LEN = 5               # H4 E0c: leave after the first close above the 5-day average
IDX_E0C_TIME_STOP = 11        # H4 E0c: ... or at the open of the 11th bar after entry
IDX_EL_TIME_STOP = 16         # H4 EL: what is left leaves at the open of the 16th bar after entry
CARRY_TRAIL_ATR = 6.0         # H5 E0c: a trailing stop 6 ATR from the best extreme, never loosening
CARRY_MAX_R_ATR = 8.0         # H5: R may be up to 8 ATR (the stop is 6 ATR from the signal close)

# the gate (PREREGISTRATION_R2.md)
MIN_OOS_TRADES = {CR_BO: 100, IDX_DIP: 100, CARRY_FX: 40}
GATE_MIN_PF = 1.10
GATE_MIN_T = 2.33             # one-sided; Bonferroni for the five hypotheses of the two rounds at 5 %
GATE_MIN_IS_TRADES = 30       # the in-sample line is required only from this many trades

# the formatting of round 1, so both reports print their numbers the same way
_table, _num, _signed, _exit_label = r1._table, r1._num, r1._signed, r1._exit_label
_STATS_HEAD, stats_cells = r1._STATS_HEAD, r1.stats_cells


# ================================================================== records
@dataclass(frozen=True)
class RateCoverage:
    """What loaded of one currency's rate series (or why nothing did)."""
    currency: str
    series_id: str
    observations: int
    first: dt.date | None
    last: dt.date | None
    fetched: str | None
    error: str | None = None


@dataclass
class Results2(r1.Results):
    rate_coverage: list[RateCoverage] = field(default_factory=list)


@dataclass
class Gate2:
    """The gate of one hypothesis: its lines and the verdict."""
    name: str
    exit_kind: str
    checks: list[r1.Check]
    passed: bool

    @property
    def verdict(self) -> str:
        return "PASS" if self.passed else "FAIL"

    def failures(self) -> list[r1.Check]:
        return [c for c in self.checks if not c.passed]

    def summary(self) -> str:
        """PASS, or FAIL with every line that failed (what the command prints)."""
        if self.passed:
            return "PASS"
        return "FAIL (" + "; ".join(f"{c.rule} {c.value}, needs {c.threshold}"
                                    for c in self.failures()) + ")"


@dataclass
class Decision2:
    judged: dict[str, str]                         # hypothesis -> the exit it is judged on
    gates: dict[str, Gate2]
    enabled: list[str]                             # the hypotheses that passed
    stats: dict[str, dict[str, r1.Stats]]          # hypothesis -> period -> pooled stats of the judged exit


@dataclass
class Outcome2:
    results: Results2
    decision: Decision2
    report: str
    payload: dict                                  # the round2 block of enabled.json


# ================================================================== running the hypotheses
def _in_window(inst: ins.Instrument, bars: Sequence[data.Bar],
               signals: Sequence[setups.Signal]) -> list[setups.Signal]:
    """Signals before the study window (2006-01-01 outside crypto) are warm-up, not trades."""
    return [s for s in signals if r1.period_of(inst.gate_class, bars[s.index].ts.date()) is not None]


def _backtest(name: str, inst: ins.Instrument, bars: Sequence[data.Bar],
              signals: Sequence[setups.Signal], costs: ins.Costs,
              plan: Sequence[tuple[str, dict]]) -> tuple[list[r1.Row], dict[tuple[str, str], r1.Counts]]:
    """Round 1's one-open-trade walk for each exit of the plan (exit kind, extra simulate arguments)."""
    atr = ind.atr(bars)
    signals = _in_window(inst, bars, signals)
    rows: list[r1.Row] = []
    counts: dict[tuple[str, str], r1.Counts] = {}
    for exit_kind, kwargs in plan:
        trades, c = r1.take_trades(bars, signals, exit_kind, costs=costs, entry="next_open", atr=atr,
                                   **kwargs)
        counts[(name, exit_kind)] = c
        for t in trades:
            signal_ts = bars[t.signal_index].ts
            rows.append(r1.Row(name, exit_kind, inst.symbol, inst.name, inst.gate_class,
                               r1.period_of(inst.gate_class, signal_ts.date()), t.result_r,
                               signal_ts, t.exit_ts, t.holding_days))
    return rows, counts


def backtest_cr_bo(inst: ins.Instrument, bars: Sequence[data.Bar]
                   ) -> tuple[list[r1.Row], dict[tuple[str, str], r1.Counts]]:
    """H3: BO-D exactly as in round 1 under E0, E1 and E2, with the alt coin's own costs (a round trip of
    0.40 %, financing 0.06 % a night: `inst.costs`)."""
    plan = [(kind, {}) for kind in exits.EXIT_KINDS]
    return _backtest(CR_BO, inst, bars, setups.bo_d(bars), inst.costs, plan)


def backtest_idx_dip(inst: ins.Instrument, bars: Sequence[data.Bar]
                     ) -> tuple[list[r1.Row], dict[tuple[str, str], r1.Counts]]:
    """H4: the dip signal under E0c (the next open after the first close above SMA5, the open of the 11th
    bar after entry, or the stop) and EL (the open of the 16th bar after entry). The index costs of round
    1: a long pays 0.030 % and 0.020 % a night."""
    closes = ind.closes(bars)
    sma5 = ind.sma(closes, IDX_SMA_LEN)

    def back_above_sma5(j: int, side: str) -> bool:
        return sma5[j] is not None and closes[j] > sma5[j]
    plan = [(exits.E0C, dict(time_stop_bars=IDX_E0C_TIME_STOP, exit_when=back_above_sma5)),
            (exits.EL, dict(time_stop_bars=IDX_EL_TIME_STOP))]
    return _backtest(IDX_DIP, inst, bars, setups.idx_dip(bars), inst.costs, plan)


def backtest_carry_fx(inst: ins.Instrument, bars: Sequence[data.Bar], rates: rates_mod.Rates
                      ) -> tuple[list[r1.Row], dict[tuple[str, str], r1.Counts]]:
    """H5: the carry-gated trend of one FX pair. Each bar gets the rate in force on its date for the
    base and the quote currency (None while a currency has none, which makes the state flat). E0c leaves
    at the next open after the state stops being the trade's side, or at a 6 ATR trailing stop; EL is the
    ladder whose remainder leaves the same way; R may be up to 8 ATR. The pair's round-1 round trip and
    0.004 % a night."""
    days = [b.ts.date() for b in bars]
    base_rates = [rates.rate_on(inst.base, d) for d in days]
    quote_rates = [rates.rate_on(inst.quote, d) for d in days]
    states = setups.carry_states(bars, base_rates, quote_rates)

    def state_is_off(j: int, side: str) -> bool:
        return states[j] != side
    plan = [(exits.E0C, dict(trail_atr=CARRY_TRAIL_ATR, exit_when=state_is_off, max_r_atr=CARRY_MAX_R_ATR)),
            (exits.EL, dict(exit_when=state_is_off, max_r_atr=CARRY_MAX_R_ATR))]
    return _backtest(CARRY_FX, inst, bars, setups.carry_fx(bars, base_rates, quote_rates),
                     ins.carry_fx_costs(inst), plan)


# ================================================================== the exit that is judged, the gate
def _select(rows: Sequence[r1.Row], name: str, exit_kind: str, period: str | None = None) -> list[r1.Row]:
    return [r for r in rows if r.setup == name and r.exit_kind == exit_kind
            and (period is None or r.period == period)]


def judged_exit(rows: Sequence[r1.Row], name: str) -> str:
    """The four-stage exit a hypothesis is judged on: E1 or E2 for H3, whichever has the higher pooled
    out-of-sample mean R (round 1's rule: a tie, or no trades, goes to E1; E0 is never a candidate); EL
    for H4 and H5. E0 and E0c are reported beside it."""
    return r1.choose_exit(rows, name) if name == CR_BO else exits.EL


def gate_hypothesis(name: str, exit_kind: str, is_stats: r1.Stats, oos: r1.Stats) -> Gate2:
    """The gate of PREREGISTRATION_R2.md for one hypothesis with its judged exit: out-of-sample trades
    (H3, H4: at least 100; H5: at least 40), profit factor >= 1.10 after costs, mean R > 0 with a
    one-sided t-statistic >= 2.33, and in-sample mean R > 0 -- required only when the in-sample period
    has at least 30 trades, otherwise reported and not required."""
    need = MIN_OOS_TRADES[name]
    required = is_stats.n >= GATE_MIN_IS_TRADES
    in_sample = f"{_signed(is_stats.mean_r, 4)} ({is_stats.n} trades"
    in_sample += ")" if required else f", not required below {GATE_MIN_IS_TRADES})"
    checks = [
        r1.Check(name, "out-of-sample trades", str(oos.n), f">= {need}", oos.n >= need),
        r1.Check(name, "out-of-sample profit factor", _num(oos.profit_factor, 4),
                 f">= {GATE_MIN_PF:.2f}", oos.profit_factor >= GATE_MIN_PF),
        r1.Check(name, "out-of-sample mean R", _signed(oos.mean_r, 4), "> 0", oos.mean_r > 0),
        r1.Check(name, "out-of-sample t-statistic", _num(oos.t_stat, 4), f">= {GATE_MIN_T:.2f}",
                 oos.t_stat >= GATE_MIN_T),
        r1.Check(name, "in-sample mean R", in_sample, f"> 0 (required from {GATE_MIN_IS_TRADES} trades)",
                 (not required) or is_stats.mean_r > 0),
    ]
    return Gate2(name, exit_kind, checks, all(c.passed for c in checks))


def decide(rows: Sequence[r1.Row]) -> Decision2:
    """Choose each hypothesis's judged exit and apply its gate to the pooled trades."""
    judged: dict[str, str] = {}
    gates: dict[str, Gate2] = {}
    by_period: dict[str, dict[str, r1.Stats]] = {}
    for name in HYPOTHESES:
        exit_kind = judged_exit(rows, name)
        is_stats = r1.stats(_select(rows, name, exit_kind, "IS"))
        oos = r1.stats(_select(rows, name, exit_kind, "OOS"))
        judged[name] = exit_kind
        gates[name] = gate_hypothesis(name, exit_kind, is_stats, oos)
        by_period[name] = {"IS": is_stats, "OOS": oos}
    return Decision2(judged, gates, [n for n in HYPOTHESES if gates[n].passed], by_period)


def round2_payload(decision: Decision2, now: dt.datetime) -> dict:
    """The `round2` block of enabled.json: the hypotheses that passed, the exit each is judged on (all
    three, passed or not, as round 1 lists every setup), and when the run was made."""
    return {"enabled": list(decision.enabled), "exit": dict(decision.judged), "run_at": now.isoformat()}


# ================================================================== the outputs
def _merged_enabled(path: Path, round2: dict) -> tuple[dict, str]:
    """enabled.json with `round2` set (replaced where it already is, else appended) and everything else
    as it was. A file that is not a JSON object is refused: it is not ours to overwrite."""
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise ValueError(f"{path} is not valid JSON ({e}): round 2 is not merged into it") from e
        if not isinstance(existing, dict):
            raise ValueError(f"{path} is not a JSON object: round 2 is not merged into it")
    merged = {**existing, "round2": round2}
    return merged, json.dumps(merged, indent=2, ensure_ascii=False) + "\n"


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)                           # an interrupted run never leaves half a file


def merge_enabled(path: Path | str, round2: dict) -> dict:
    """Merge the round-2 block into enabled.json (created when missing); returns the merged object."""
    path = Path(path)
    merged, text = _merged_enabled(path, round2)
    _write_atomically(path, text)
    return merged


def write_outputs(report: str, round2: dict, out_dir: Path | str = OUT_DIR) -> list[Path]:
    """Write BACKTEST_REPORT_R2.md into `out_dir` and merge the round-2 block into its enabled.json.
    The merge is prepared first, so a file that cannot be merged into stops the run before anything is
    written. Round 1's report is never touched."""
    out_dir = Path(out_dir)
    report_path, json_path = out_dir / REPORT_NAME, out_dir / ENABLED_NAME
    _, text = _merged_enabled(json_path, round2)
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    _write_atomically(json_path, text)
    return [report_path, json_path]


# ================================================================== the report
_TITLES = {
    CR_BO: "trend breakout (BO-D) on the eleven alt coins",
    IDX_DIP: "buying a sharp dip in a rising equity index (long only)",
    CARRY_FX: "the carry-gated trend on the eleven FX pairs",
}
_RULES = {
    CR_BO: ("BO-D exactly as in round 1: the close above the highest high of the previous 55 bars and above "
            "SMA200 (mirrored for a short), entry at the next open, stop 2.5 ATR from the signal close. "
            "E0, E1 and E2 exactly as in round 1. Costs: round trip 0.40 %, financing 0.06 % a night. "
            "In-sample to 2019-12-31, out-of-sample from 2020-01-01. Judged on E1 or E2, whichever has "
            "the higher out-of-sample mean R."),
    IDX_DIP: ("Signal at the close of bar t: close above SMA200 and RSI(2) under 10; entry at the open of "
              "bar t+1; stop 2.5 ATR below the signal close. E0c: leave at the next open after the first "
              "close above SMA5, at the open of the 11th bar after entry, or at the stop. EL: a quarter "
              "at each of +0.5R, +1R, +1.5R, +2R, the stop at its initial level until TP2, at the entry "
              "after TP2 and at TP1 after TP3, what is left at the open of the 16th bar after entry. "
              "Costs: the round-1 Indices row. In-sample 2006-2016, out-of-sample from 2017. Judged on EL."),
    CARRY_FX: ("State at each close: long when the close is above 1.025 x SMA200 and rate(base) is above "
               "rate(quote), short when it is below 0.975 x SMA200 and rate(base) is below rate(quote), "
               "otherwise flat. Entry at the next open on a change from flat (or from the opposite side) "
               "into long or short; stop 6 ATR from the signal close. E0c: leave at the next open after "
               "the state stops being the trade's side, or at a 6 ATR trailing stop that never loosens. "
               "EL: the ladder of H4, its remainder also leaving at the next open after the state stops "
               "being the trade's side; no time stop. Costs: the round-1 FX round trip and 0.004 % a "
               "night. In-sample 2006-2016, out-of-sample from 2017. Judged on EL."),
}


def _gate_table(gate: Gate2) -> list[str]:
    body = [[c.rule, c.value, c.threshold, "PASS" if c.passed else "FAIL"] for c in gate.checks]
    out = [f"### {gate.name} (judged exit {gate.exit_kind})", ""]
    return out + _table(["Rule", "Value", "Threshold", "Result"], body)


def _universe_of(name: str, rows: Sequence[r1.Row]) -> list[ins.Instrument]:
    """The pre-registered instruments of a hypothesis that have trades, in the order of its universe."""
    have = {r.symbol for r in rows if r.setup == name}
    return [i for i in UNIVERSES[name] if i.symbol in have]


def _hypothesis_section(name: str, rows: Sequence[r1.Row], decision: Decision2) -> list[str]:
    judged = decision.judged[name]
    out = [f"## {name}", "", f"{LABELS[name]}: {_TITLES[name]}.", "", _RULES[name], "",
           f"### Pooled, by exit and period (* = the judged exit, {judged})", ""]
    body = []
    for exit_kind in EXITS[name]:
        for period in ("IS", "OOS"):
            body.append([_exit_label(exit_kind, judged), period,
                         *stats_cells(r1.stats(_select(rows, name, exit_kind, period)))])
    out += _table(["Exit", "Period", *_STATS_HEAD], body)
    out += [f"### By instrument (exit {judged}; for information, not a gate)", ""]
    body = []
    for inst in _universe_of(name, rows):
        mine = [r for r in _select(rows, name, judged) if r.symbol == inst.symbol]
        for period in ("IS", "OOS"):
            body.append([inst.name, inst.klass, period,
                         *stats_cells(r1.stats([r for r in mine if r.period == period]))])
    out += _table(["Instrument", "Class", "Period", *_STATS_HEAD], body)
    return out


def _data_section(results: Results2) -> list[str]:
    out = ["## Data", ""]
    body = []
    for c in results.coverage:
        if c.bars == 0:
            body.append([c.name, c.symbol, c.interval, "no data", "", "", "", c.fetched or ""])
            continue
        d = c.dropped
        dropped = "0" if d.total == 0 else (
            f"{d.total} (missing {d.missing}, inverted {d.inverted}, wick {d.wick}, "
            f"duplicate {d.duplicate}, unparsable {d.unparsable}, forming {d.incomplete})")
        body.append([c.name, c.symbol, c.interval, str(c.bars), c.first.isoformat(),
                     c.last.isoformat(), dropped, c.fetched or ""])
    out += _table(["Instrument", "Symbol", "Interval", "Bars", "First", "Last", "Dropped", "Fetched"], body)
    out += ["## Rates", "",
            "OECD 3-month interbank rates, monthly, from FRED. The value for month M is used from the 15th of "
            "month M+1; a month with no value carries the last value forward.", ""]
    body = []
    for c in results.rate_coverage:
        if c.error is not None or c.observations == 0:
            body.append([c.currency, c.series_id, "0", "", "", "", f"no rates: {c.error or 'empty'}"])
        else:
            body.append([c.currency, c.series_id, str(c.observations), c.first.isoformat()[:7],
                         c.last.isoformat()[:7], c.fetched or "", "ok"])
    out += _table(["Currency", "Series", "Observations", "First month", "Last month", "Fetched", "Status"],
                  body)
    return out


def _notes() -> list[str]:
    return [
        "- Results are in R after costs: one round trip per trade plus financing for every calendar night "
        "held. H3 pays a round trip of 0.40 % and 0.06 % a night, H4 the round-1 Indices row (0.030 % and "
        "0.020 % a night, long only), H5 the pair's round-1 FX round trip (0.015 % a major, 0.030 % a cross) "
        "and 0.004 % a night. H5 charges the broker's markup and credits none of the carry it earns, which "
        "is the conservative reading.",
        "- Everything the pre-registration does not restate is round 1's: data hygiene, ATR(14) (also for "
        "H5's stop and trailing stop), the bar order (stop before target within a bar), gaps filling at the "
        "open, the skip rule (R <= 0.25 ATR, or R > 6 ATR; H5: 8 ATR), one open trade per instrument per "
        "setup and exit, and 300 bars of history before a first signal.",
        "- Every stop is fixed at the signal (H4: the close - 2.5 ATR; H5: the close -/+ 6 ATR) and R is "
        "measured from the actual entry, the next open, as for round 1's setups; so R is about 6 ATR for H5 "
        "and the gap of the entry bar is what the 8 ATR bound allows for.",
        "- A time stop of N bars leaves at the open of the Nth bar after the entry bar (the bar whose open is "
        "the entry). A condition exit (the close above SMA5; the state no longer the trade's side) is read at "
        "each close from the entry bar's own close on and leaves at the next bar's open; a condition read at "
        "the signal bar, before the entry, never counts. Both fill at that open, before the bar's own stop and "
        "targets, and the earlier of the two wins.",
        "- H5's state includes the 2.5 % band (carry_strategy.py's frozen 2.5 % and 200 days), and so does the "
        "exit: a trade leaves when the close is back inside the band or the rates no longer agree, not when it "
        "crosses the 200-day average as carry_strategy.py's hold condition does.",
        "- The rate of a currency on a bar is the value of the latest month already published (the 15th of the "
        "next month reached, that day included); a bar with no rate for either currency has no state.",
        "- In-sample and out-of-sample are split by the signal date; signals before 2006-01-01 (crypto: no "
        "lower bound) are warm-up only. The in-sample line of the gate is required only with at least 30 "
        "in-sample trades; the coins that listed late have none.",
        "- The t-statistics pool trades across instruments and treat them as independent; trades that overlap "
        "in time in correlated markets make the true uncertainty larger. The bar of 2.33 is Bonferroni for the "
        "five hypotheses of the two rounds.",
        "- The gate judges each hypothesis on its pooled sample. A hypothesis that passes is enabled for its "
        "whole universe (H3: BTC and ETH too, on round 1's evidence); there is no per-instrument picking, and "
        "a hypothesis that fails is not traded.",
    ]


def render_report(results: Results2, decision: Decision2, now: dt.datetime) -> str:
    """The text of docs/cfd/BACKTEST_REPORT_R2.md: the outcome, the gate of each hypothesis, its pooled
    table per exit and period, a per-instrument table for information, and what the data was."""
    daily_last = [c.last for c in results.coverage if c.interval == "1d" and c.last]
    rates_last = [c.last for c in results.rate_coverage if c.last]
    through = f"daily bars through {max(daily_last).isoformat()}" if daily_last else "no daily bars"
    source = f"Source: Yahoo (yfinance), {through}; FRED, OECD 3-month interbank rates"
    if rates_last:
        source += f" through {max(rates_last).isoformat()[:7]}"
    out = ["# CFD backtest report, round 2", "",
           f"Run at {now.isoformat()}. {source}. Pre-registration: docs/cfd/PREREGISTRATION_R2.md.", ""]

    out += ["## Outcome", ""]
    if decision.enabled:
        out += ["Enabled for live signals: " + ", ".join(decision.enabled) + ".", ""]
    else:
        out += ["**No hypothesis passed the gate: nothing from round 2 goes live.**", ""]
    body = []
    for name in HYPOTHESES:
        oos = decision.stats[name]["OOS"]
        body.append([f"{LABELS[name]} {name}", decision.judged[name], decision.gates[name].verdict,
                     str(oos.n), _num(oos.profit_factor, 2), _signed(oos.mean_r, 3), _num(oos.t_stat, 2)])
    out += _table(["Hypothesis", "Judged exit", "Gate", "Out-of-sample trades", "PF", "Mean R", "t"], body)

    out += ["## Gate", "",
            "Each hypothesis on its own; every line is pass or fail. A hypothesis that fails is not traded, "
            "and there is no round 3 on this data.", ""]
    for name in HYPOTHESES:
        out += _gate_table(decision.gates[name])

    for name in HYPOTHESES:
        out += _hypothesis_section(name, results.rows, decision)

    out += ["## Trade accounting", ""]
    body = [[name, kind, *(str(getattr(results.counts[(name, kind)], f)) for f in
                           ("signals", "closed", "skipped", "open", "blocked"))]
            for name in HYPOTHESES for kind in EXITS[name] if (name, kind) in results.counts]
    out += _table(["Hypothesis", "Exit", "Signals", "Closed", "Skipped (R rules)", "Open at end of data",
                   "Blocked (a trade was open)"], body)

    out += _data_section(results)
    out += ["## Notes", "", *_notes(), ""]
    return "\n".join(out)


# ================================================================== the run
def _log(message: str) -> None:
    print(f"[cfd.research2] {message}", file=sys.stderr)


def run(*, fetch: data.Fetch | None = None, rates_fetch: rates_mod.Fetch | None = None,
        now: dt.datetime | None = None, cache_dir: Path | str = data.CACHE_DIR,
        out_dir: Path | str = OUT_DIR, refresh: bool = False,
        universes: Mapping[str, Sequence[ins.Instrument]] | None = None, write: bool = True,
        log: Callable[[str], None] = _log) -> Outcome2:
    """Load every series (through the caches), run the three hypotheses, decide, report. A series that
    fails to load is logged and left out, and the report lists it as having no data; a currency whose
    rates fail leaves every pair that needs it without a state. `universes` (hypothesis -> instruments;
    a missing hypothesis runs on nothing) restricts the run; the default is the pre-registered one."""
    now = (now or dt.datetime.now(UTC)).replace(microsecond=0)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if universes is None:
        universes = UNIVERSES
    universe = {name: tuple(universes.get(name, ())) for name in HYPOTHESES}
    source = data.cached_fetch(fetch or data.yahoo_fetch, cache_dir, refresh=refresh)
    results = Results2()

    needed = {c for inst in universe[CARRY_FX] for c in (inst.base, inst.quote)}
    currencies = [c for c in rates_mod.SERIES if c in needed]
    rates = (rates_mod.load_rates(currencies, fetch=rates_fetch, cache_dir=cache_dir, refresh=refresh,
                                  log=log) if currencies else rates_mod.Rates())
    for currency in currencies:
        series = rates.series.get(currency)
        if series is None:
            results.rate_coverage.append(RateCoverage(currency, rates_mod.SERIES[currency], 0, None, None,
                                                      None, rates.errors.get(currency, "not loaded")))
        else:
            months = [m for m, _ in series.observations]
            results.rate_coverage.append(RateCoverage(
                currency, series.series_id, len(months), months[0], months[-1],
                (rates_mod.cache_stamp(cache_dir, currency) or "")[:10] or None))

    for name in HYPOTHESES:
        for inst in universe[name]:
            try:
                loaded = data.load_daily(inst.symbol, fetch=source, today=now.date())
            except Exception as e:      # one dead symbol must not cost the whole run
                log(f"{inst.symbol}: no daily data ({type(e).__name__}: {str(e)[:120]})")
                loaded = None
            stamp = data.cache_stamp(cache_dir, inst.symbol, data.DAILY)
            results.coverage.append(r1._coverage(inst, data.DAILY, loaded, stamp[:10] if stamp else None))
            if loaded is None or not loaded.bars:
                continue
            log(f"{inst.symbol}: {len(loaded.bars)} daily bars ({name})")
            if name == CR_BO:
                rows, counts = backtest_cr_bo(inst, loaded.bars)
            elif name == IDX_DIP:
                rows, counts = backtest_idx_dip(inst, loaded.bars)
            else:
                rows, counts = backtest_carry_fx(inst, loaded.bars, rates)
            results.rows += rows
            for key, c in counts.items():
                results.counts.setdefault(key, r1.Counts()).merge(c)

    decision = decide(results.rows)
    report = render_report(results, decision, now)
    payload = round2_payload(decision, now)
    if write:
        write_outputs(report, payload, out_dir)
    return Outcome2(results, decision, report, payload)


def _summary_lines(name: str, rows: Sequence[r1.Row], decision: Decision2) -> list[str]:
    """What the command prints for a hypothesis: its judged exit and gate, and the numbers of every exit."""
    judged, gate = decision.judged[name], decision.gates[name]
    oos, ins_ = decision.stats[name]["OOS"], decision.stats[name]["IS"]
    lines = [f"{LABELS[name]} {name}: judged exit {judged}, gate {gate.verdict}; out-of-sample {oos.n} "
             f"trades, PF {_num(oos.profit_factor, 2)}, mean R {_signed(oos.mean_r, 3)}, "
             f"t {_num(oos.t_stat, 2)}; in-sample {ins_.n} trades, mean R {_signed(ins_.mean_r, 3)}"]
    if gate.failures():
        lines.append("  failed: " + "; ".join(f"{c.rule} {c.value}, needs {c.threshold}"
                                              for c in gate.failures()))
    for kind in EXITS[name]:
        o = r1.stats(_select(rows, name, kind, "OOS"))
        i = r1.stats(_select(rows, name, kind, "IS"))
        label = _exit_label(kind, judged)
        lines.append(f"  {label:<6} OOS {o.n} trades, PF {_num(o.profit_factor, 2)}, mean R "
                     f"{_signed(o.mean_r, 3)}, t {_num(o.t_stat, 2)} | IS {i.n} trades, PF "
                     f"{_num(i.profit_factor, 2)}, mean R {_signed(i.mean_r, 3)}")
    return lines


def main(argv: Sequence[str] | None = None, *, fetch: data.Fetch | None = None,
         rates_fetch: rates_mod.Fetch | None = None, now: dt.datetime | None = None,
         cache_dir: Path | str | None = None, out_dir: Path | str | None = None,
         universes: Mapping[str, Sequence[ins.Instrument]] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cfd.research2", description=__doc__.split("\n\n")[0])
    ap.add_argument("--run", action="store_true",
                    help="download (or read from data/cfd_cache/) the bars and rates, run round 2, write "
                         "docs/cfd/BACKTEST_REPORT_R2.md and merge the round2 block into docs/cfd/enabled.json")
    ap.add_argument("--refresh", action="store_true", help="download the bars and rates again")
    args = ap.parse_args(argv)
    if not args.run:
        print("Nothing to do: pass --run to run round 2 (it downloads from Yahoo and FRED on the first "
              "run). Usage: python -m cfd.research2 --run [--refresh]")
        return 2
    target = Path(out_dir if out_dir is not None else OUT_DIR)
    outcome = run(fetch=fetch, rates_fetch=rates_fetch, now=now, refresh=args.refresh,
                  cache_dir=cache_dir if cache_dir is not None else data.CACHE_DIR, out_dir=target,
                  universes=universes)
    for name in HYPOTHESES:
        for line in _summary_lines(name, outcome.results.rows, outcome.decision):
            print(line)
    missing = [f"{c.symbol} ({c.interval})" for c in outcome.results.coverage if c.bars == 0]
    if missing:      # the verdict above rests on a partial universe: re-run (the caches keep the rest)
        print("WARNING: no data for " + ", ".join(missing) + " -- the outcome covers the rest only")
    no_rates = [c.currency for c in outcome.results.rate_coverage if c.error is not None or c.observations == 0]
    if no_rates:
        print("WARNING: no rates for " + ", ".join(no_rates) + " -- the pairs that need them have no trades")
    print(f"enabled: {', '.join(outcome.decision.enabled) or 'none'}")
    print(f"written: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
