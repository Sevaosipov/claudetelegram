"""cfd/research.py: the research run of spec 2026-10-05-cfd-signals.md Part 1 -- load the bars, run
every setup under every exit, split in-sample from out-of-sample by the signal date, pool, apply
the gate of §1.6, and write docs/cfd/BACKTEST_REPORT.md and docs/cfd/enabled.json (§1.7).

    python -m cfd.research --run [--refresh]

`enabled.json` is the only thing the live module reads: {"exit": {"PB-D": "E2", ...},
"enabled": [["PB-D", "FX"], ...], "run_at": ...}. Everything that decides it is pure and tested on
synthetic trade lists; the only network sits behind `fetch` (data.py), and downloaded bars are
cached under data/cfd_cache/ (git-ignored) so a re-run, or a re-run after a hygiene change, is
offline. `--refresh` downloads again.

What a trade is counted as. Results are in R after costs. One open trade per instrument per setup
per exit: a signal read while a trade is open is not taken, so the trade set differs by exit. A
trade still open when the data ends is not counted (the accounting table says how many there were);
an entry that breaks the R rules of §1.2 is skipped. Signals before the study window (2006-01-01)
are not taken: that data is warm-up.

The gate is exactly §1.6. The ladder exit that goes live is the one of E1 / E2 with the higher
pooled out-of-sample mean R (a tie, or no trades at all, goes to E1); E0 is reported for
comparison. A setup or class that fails is not traded, and the report says so when nothing passes.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from cfd import data, exits, setups
from cfd import indicators as ind
from cfd import instruments as ins

UTC = dt.timezone.utc

PB_D, BO_D, GOLD = "PB-D", "BO-D", "PB-H1-GOLD"
SETUP_NAMES = (PB_D, BO_D, GOLD)
DAILY_SETUPS = ((PB_D, setups.pb_d), (BO_D, setups.bo_d))
LADDERS = (exits.E1, exits.E2)
GOLD_E0_TARGET_R = 5.0                    # E0 of PB-H1-GOLD also has a hard 5R target (§1.3)
GOLD_CLASS = ins.METALS

OUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "cfd"

# periods (§1.5)
IS_START = dt.date(2006, 1, 1)
IS_END = dt.date(2016, 12, 31)
CRYPTO_IS_END = dt.date(2019, 12, 31)

# the gate (§1.6)
GATE_MIN_OOS_TRADES = 150
GATE_MIN_OOS_PF = 1.15
GATE_MIN_T = 1.96                         # one-sided; Bonferroni for two setups
CLASS_MIN_TRADES = 30
CLASS_MIN_PF = 1.10
GOLD_MIN_PF = 1.10
GOLD_E0_PF_RANGE = (1.05, 1.35)           # the range the user's own tests gave

CARRY_NOTE = ("CARRY-D (the EUR/USD carry-gated trend) is not in this report: carry_strategy.py "
              "computes only the last bar's state and does not expose its historical entries and "
              "exits, and 15 trades could not pass a gate anyway; it is information-only.")


# ================================================================ records
@dataclass(frozen=True)
class Row:
    """One closed trade in the pooled results."""
    setup: str
    exit_kind: str
    symbol: str
    name: str
    klass: str            # the gate class (FX, Metals, Energy, Indices, Crypto)
    period: str           # "IS" | "OOS" | "ALL" (PB-H1-GOLD has no split)
    result_r: float
    signal_ts: dt.datetime
    exit_ts: dt.datetime
    hold_days: float


@dataclass(frozen=True)
class Stats:
    n: int
    win_rate: float
    profit_factor: float
    mean_r: float
    t_stat: float
    total_r: float
    max_drawdown_r: float
    mean_hold_days: float


@dataclass
class Counts:
    """What happened to the signals of one (setup, exit)."""
    signals: int = 0      # signals in the study window
    closed: int = 0       # taken and finished: the trades of the statistics
    skipped: int = 0      # the entry broke the R rules (R <= 0.25 ATR or > 6 ATR)
    open: int = 0         # taken, but the data ended before the trade did
    blocked: int = 0      # read while a trade was open

    def merge(self, other: "Counts") -> None:
        self.signals += other.signals
        self.closed += other.closed
        self.skipped += other.skipped
        self.open += other.open
        self.blocked += other.blocked


@dataclass(frozen=True)
class Coverage:
    symbol: str
    name: str
    interval: str
    bars: int
    first: dt.date | None
    last: dt.date | None
    dropped: data.Dropped
    fetched: str | None = None


@dataclass
class Results:
    rows: list[Row] = field(default_factory=list)
    counts: dict[tuple[str, str], Counts] = field(default_factory=dict)
    coverage: list[Coverage] = field(default_factory=list)


@dataclass(frozen=True)
class Check:
    """One line of the gate table."""
    scope: str
    rule: str
    value: str
    threshold: str
    passed: bool


@dataclass
class SetupGate:
    setup: str
    exit_kind: str
    checks: list[Check]
    passed: bool
    class_checks: dict[str, list[Check]]
    enabled_classes: list[str]

    @property
    def verdict(self) -> str:
        return "PASS" if self.passed else "FAIL"

    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    def summary(self) -> str:
        """PASS, or FAIL with every line that failed (what the command prints)."""
        if self.passed:
            return "PASS"
        return "FAIL (" + "; ".join(f"{c.rule} {c.value}, needs {c.threshold}"
                                    for c in self.failures()) + ")"


@dataclass
class Decision:
    chosen: dict[str, str]                         # setup -> E1 | E2
    gates: dict[str, SetupGate]
    enabled: list[tuple[str, str]]                 # (setup, gate class)
    stats: dict[str, dict[str, Stats]]             # setup -> period -> pooled stats of the chosen exit


@dataclass
class Outcome:
    results: Results
    decision: Decision
    report: str
    payload: dict


# ================================================================ statistics
def stats(rows: Sequence[Row]) -> Stats:
    """Trades, win rate, profit factor, mean R, one-sided t-statistic, total R, worst
    peak-to-trough in R (trades in exit-date order, 1R risked each, from a zero start) and mean
    holding days. The t-statistic is mean / (sample sd / sqrt n); with fewer than two trades, or no
    spread, it is not defined and is 0, which never passes a gate. A profit factor with no losing
    trade is infinite."""
    n = len(rows)
    if n == 0:
        return Stats(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    results = [r.result_r for r in rows]
    gross_win = sum(x for x in results if x > 0)
    gross_loss = -sum(x for x in results if x < 0)
    pf = gross_win / gross_loss if gross_loss > 0 else (math.inf if gross_win > 0 else 0.0)
    mean = sum(results) / n
    t = 0.0
    if n >= 2:
        sd = math.sqrt(sum((x - mean) ** 2 for x in results) / (n - 1))
        t = mean / (sd / math.sqrt(n)) if sd > 0 else 0.0
    equity = peak = drawdown = 0.0
    for r in sorted(rows, key=lambda r: (r.exit_ts, r.symbol, r.signal_ts)):
        equity += r.result_r
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return Stats(n, sum(1 for x in results if x > 0) / n, pf, mean, t, sum(results), drawdown,
                 sum(r.hold_days for r in rows) / n)


def period_of(klass: str, day: dt.date) -> str | None:
    """The period of a signal by its date (§1.5): in-sample 2006-01-01..2016-12-31, out-of-sample from
    2017-01-01; crypto is in-sample to 2019-12-31 and out-of-sample from 2020-01-01. A signal before
    the study window belongs to neither."""
    if klass == ins.CRYPTO:
        return "IS" if day <= CRYPTO_IS_END else "OOS"
    if day < IS_START:
        return None
    return "IS" if day <= IS_END else "OOS"


# ================================================================ running the setups
def take_trades(bars: Sequence[data.Bar], signals: Sequence[setups.Signal], exit_kind: str, *,
                costs: ins.Costs, entry: str, atr: Sequence[float | None],
                session: data.Session | None = None,
                e0_target_r: float | None = None, **sim_kw) -> tuple[list[exits.Trade], Counts]:
    """Walk the signals in order, one open trade at a time: a signal read before the previous trade
    exited is blocked (read at the close of the exit bar is allowed); a skipped entry takes no slot;
    a trade still open at the end of the data blocks everything after it. `sim_kw` goes to
    exits.simulate untouched (round 2's trail, time stop, exit condition and R bound)."""
    counts = Counts(signals=len(signals))
    trades: list[exits.Trade] = []
    busy_until = -1
    for k, sig in enumerate(signals):
        if sig.index < busy_until:
            counts.blocked += 1
            continue
        trade = exits.simulate(bars, sig, exit_kind, costs=costs, entry=entry, session=session,
                               atr=atr, e0_target_r=e0_target_r, **sim_kw)
        if trade.status == "closed":
            trades.append(trade)
            counts.closed += 1
            busy_until = trade.exit_index
        elif trade.status == "skipped":
            counts.skipped += 1
        else:
            counts.open += 1
            counts.blocked += len(signals) - k - 1
            break
    return trades, counts


def backtest_daily(inst: ins.Instrument, bars: Sequence[data.Bar]
                   ) -> tuple[list[Row], dict[tuple[str, str], Counts]]:
    """PB-D and BO-D on one instrument's daily bars under E0, E1 and E2."""
    atr = ind.atr(bars)
    rows: list[Row] = []
    counts: dict[tuple[str, str], Counts] = {}
    for setup, make_signals in DAILY_SETUPS:
        signals = [s for s in make_signals(bars)
                   if period_of(inst.gate_class, bars[s.index].ts.date()) is not None]
        for exit_kind in exits.EXIT_KINDS:
            trades, c = take_trades(bars, signals, exit_kind, costs=inst.costs, entry="next_open",
                                    atr=atr)
            counts[(setup, exit_kind)] = c
            for t in trades:
                signal_ts = bars[t.signal_index].ts
                rows.append(Row(setup, exit_kind, inst.symbol, inst.name, inst.gate_class,
                                period_of(inst.gate_class, signal_ts.date()), t.result_r,
                                signal_ts, t.exit_ts, t.holding_days))
    return rows, counts


def backtest_gold(bars_1h: Sequence[data.Bar]) -> tuple[list[Row], dict[tuple[str, str], Counts]]:
    """PB-H1-GOLD on hourly gold under E0 (with its hard 5R target), E1 and E2: entry at the signal
    bar's close, flat at the close of the first bar outside the London session, XAUUSD round trip and
    no financing. All of the history counts as one period."""
    atr = ind.atr(bars_1h)
    signals = setups.pb_h1_gold(bars_1h)
    inst = ins.by_symbol(ins.GOLD_SYMBOL)
    rows: list[Row] = []
    counts: dict[tuple[str, str], Counts] = {}
    for exit_kind in exits.EXIT_KINDS:
        trades, c = take_trades(bars_1h, signals, exit_kind, costs=ins.gold_intraday_costs(),
                                entry="close", atr=atr, session=data.LONDON_SESSION,
                                e0_target_r=GOLD_E0_TARGET_R)
        counts[(GOLD, exit_kind)] = c
        for t in trades:
            rows.append(Row(GOLD, exit_kind, inst.symbol, inst.name, GOLD_CLASS, "ALL", t.result_r,
                            bars_1h[t.signal_index].ts, t.exit_ts, t.holding_days))
    return rows, counts


# ================================================================ choosing the exit, the gate
def _select(rows: Sequence[Row], setup: str, exit_kind: str, period: str | None = None,
            klass: str | None = None) -> list[Row]:
    return [r for r in rows if r.setup == setup and r.exit_kind == exit_kind
            and (period is None or r.period == period) and (klass is None or r.klass == klass)]


def choose_exit(rows: Sequence[Row], setup: str) -> str:
    """The ladder exit that goes live: E1 or E2, whichever has the higher pooled out-of-sample mean
    R (PB-H1-GOLD has no split: all its trades). E0 is never a candidate; a tie goes to E1."""
    period = None if setup == GOLD else "OOS"
    seen = {e: stats(_select(rows, setup, e, period)) for e in LADDERS}
    if seen[exits.E1].n == 0 or seen[exits.E2].n == 0:        # an exit with no trades can't win
        return exits.E2 if seen[exits.E1].n == 0 and seen[exits.E2].n > 0 else exits.E1
    return exits.E2 if seen[exits.E2].mean_r > seen[exits.E1].mean_r else exits.E1


def _num(x: float, places: int) -> str:
    return "inf" if math.isinf(x) else f"{x:.{places}f}"


def _signed(x: float, places: int) -> str:
    return f"{x:+.{places}f}"


def gate_daily(setup: str, exit_kind: str, is_stats: Stats, oos: Stats,
               class_oos: Mapping[str, Stats]) -> SetupGate:
    """§1.6 for a daily setup with its chosen exit. Level 1, pooled over all instruments: in-sample
    mean R > 0; out-of-sample >= 150 trades, profit factor >= 1.15, mean R > 0 and one-sided t >= 1.96.
    Level 2, only when level 1 passed: a class is enabled when its out-of-sample mean R > 0, profit
    factor >= 1.10 and trades >= 30."""
    checks = [
        Check(setup, "in-sample mean R", _signed(is_stats.mean_r, 4), "> 0", is_stats.mean_r > 0),
        Check(setup, "out-of-sample trades", str(oos.n), f">= {GATE_MIN_OOS_TRADES}",
              oos.n >= GATE_MIN_OOS_TRADES),
        Check(setup, "out-of-sample profit factor", _num(oos.profit_factor, 4),
              f">= {GATE_MIN_OOS_PF:.2f}", oos.profit_factor >= GATE_MIN_OOS_PF),
        Check(setup, "out-of-sample mean R", _signed(oos.mean_r, 4), "> 0", oos.mean_r > 0),
        Check(setup, "out-of-sample t-statistic", _num(oos.t_stat, 4), f">= {GATE_MIN_T:.2f}",
              oos.t_stat >= GATE_MIN_T),
    ]
    passed = all(c.passed for c in checks)
    class_checks: dict[str, list[Check]] = {}
    enabled: list[str] = []
    if passed:
        for klass in ins.GATE_CLASSES:
            st = class_oos.get(klass) or stats([])
            scope = f"{setup} / {klass}"
            cc = [Check(scope, "out-of-sample trades", str(st.n), f">= {CLASS_MIN_TRADES}",
                        st.n >= CLASS_MIN_TRADES),
                  Check(scope, "out-of-sample profit factor", _num(st.profit_factor, 4),
                        f">= {CLASS_MIN_PF:.2f}", st.profit_factor >= CLASS_MIN_PF),
                  Check(scope, "out-of-sample mean R", _signed(st.mean_r, 4), "> 0", st.mean_r > 0)]
            class_checks[klass] = cc
            if all(c.passed for c in cc):
                enabled.append(klass)
    return SetupGate(setup, exit_kind, checks, passed, class_checks, enabled)


def gate_gold(exit_kind: str, chosen: Stats, e0: Stats) -> SetupGate:
    """§1.6 for PB-H1-GOLD: enabled when its port, with the chosen ladder exit, has profit factor
    >= 1.10 and mean R > 0 on the available hourly history, and its E0 profit factor lies within the
    range of the user's own tests (1.05 to 1.35, ends included). A port check, not a new test."""
    low, high = GOLD_E0_PF_RANGE
    checks = [
        Check(GOLD, "profit factor, chosen exit", _num(chosen.profit_factor, 4),
              f">= {GOLD_MIN_PF:.2f}", chosen.profit_factor >= GOLD_MIN_PF),
        Check(GOLD, "mean R, chosen exit", _signed(chosen.mean_r, 4), "> 0", chosen.mean_r > 0),
        Check(GOLD, "E0 profit factor, lower bound", _num(e0.profit_factor, 4), f">= {low:.2f}",
              e0.profit_factor >= low),
        Check(GOLD, "E0 profit factor, upper bound", _num(e0.profit_factor, 4), f"<= {high:.2f}",
              e0.profit_factor <= high),
    ]
    passed = all(c.passed for c in checks)
    return SetupGate(GOLD, exit_kind, checks, passed, {}, [GOLD_CLASS] if passed else [])


def decide(rows: Sequence[Row]) -> Decision:
    """Choose each setup's exit and apply the gate to the pooled trades."""
    chosen: dict[str, str] = {}
    gates: dict[str, SetupGate] = {}
    by_period: dict[str, dict[str, Stats]] = {}
    for setup, _ in DAILY_SETUPS:
        exit_kind = choose_exit(rows, setup)
        is_stats = stats(_select(rows, setup, exit_kind, "IS"))
        oos = stats(_select(rows, setup, exit_kind, "OOS"))
        class_oos = {k: stats(_select(rows, setup, exit_kind, "OOS", k)) for k in ins.GATE_CLASSES}
        chosen[setup] = exit_kind
        gates[setup] = gate_daily(setup, exit_kind, is_stats, oos, class_oos)
        by_period[setup] = {"IS": is_stats, "OOS": oos}
    exit_kind = choose_exit(rows, GOLD)
    all_gold = stats(_select(rows, GOLD, exit_kind))
    chosen[GOLD] = exit_kind
    gates[GOLD] = gate_gold(exit_kind, all_gold, stats(_select(rows, GOLD, exits.E0)))
    by_period[GOLD] = {"ALL": all_gold}
    enabled = [(setup, klass) for setup in SETUP_NAMES for klass in gates[setup].enabled_classes]
    return Decision(chosen, gates, enabled, by_period)


def enabled_payload(decision: Decision, now: dt.datetime) -> dict:
    """The machine-readable outcome the live module reads (§1.7)."""
    return {"exit": dict(decision.chosen),
            "enabled": [[setup, klass] for setup, klass in decision.enabled],
            "run_at": now.isoformat()}


# ================================================================ the report
_STATS_HEAD = ["Trades", "Win", "PF", "Mean R", "t", "Total R", "Max DD (R)", "Hold (days)"]


def stats_cells(st: Stats) -> list[str]:
    """A Stats as the table cells of the report."""
    if st.n == 0:
        return ["0"] + ["–"] * 7
    return [str(st.n), f"{st.win_rate * 100:.1f}%", _num(st.profit_factor, 2),
            _signed(st.mean_r, 3), _num(st.t_stat, 2), _signed(st.total_r, 1),
            f"{st.max_drawdown_r:.1f}", f"{st.mean_hold_days:.1f}"]


def _table(head: Sequence[str], body: Sequence[Sequence[str]]) -> list[str]:
    def line(cells):
        return "| " + " | ".join(cells) + " |"
    return [line(head), line(["---"] * len(head)), *(line(r) for r in body), ""]


def _exit_label(exit_kind: str, chosen: str) -> str:
    return f"{exit_kind} *" if exit_kind == chosen else exit_kind


def _gate_table(gate: SetupGate) -> list[str]:
    body = [[c.scope, "setup", c.rule, c.value, c.threshold, "PASS" if c.passed else "FAIL"]
            for c in gate.checks]
    for klass in ins.GATE_CLASSES:
        for c in gate.class_checks.get(klass, []):
            body.append([c.scope, "class", c.rule, c.value, c.threshold,
                         "PASS" if c.passed else "FAIL"])
    out = [f"### {gate.setup} (exit {gate.exit_kind})", ""]
    out += _table(["Scope", "Level", "Rule", "Value", "Threshold", "Result"], body)
    if not gate.passed and gate.setup != GOLD:
        out += ["Class level not evaluated: the setup failed level 1.", ""]
    return out


def _setup_section(setup: str, rows: Sequence[Row], decision: Decision) -> list[str]:
    chosen = decision.chosen[setup]
    periods = ["ALL"] if setup == GOLD else ["IS", "OOS"]
    out = [f"## {setup}", ""]
    if setup == GOLD:
        out += ["A port check, not a new test: the hourly history is the user's own fitting data.", ""]
    out += [f"### Pooled, by exit and period (* = the chosen exit, {chosen})", ""]
    body = []
    for exit_kind in exits.EXIT_KINDS:
        for period in periods:
            body.append([_exit_label(exit_kind, chosen), period,
                         *stats_cells(stats(_select(rows, setup, exit_kind, period)))])
    out += _table(["Exit", "Period", *_STATS_HEAD], body)
    if setup == GOLD:
        return out
    out += ["### By class", ""]
    body = []
    for klass in ins.GATE_CLASSES:
        for exit_kind in exits.EXIT_KINDS:
            for period in periods:
                body.append([klass, _exit_label(exit_kind, chosen), period,
                             *stats_cells(stats(_select(rows, setup, exit_kind, period, klass)))])
    out += _table(["Class", "Exit", "Period", *_STATS_HEAD], body)
    out += [f"### By instrument (exit {chosen})", ""]
    body = []
    for inst in ins.UNIVERSE:
        mine = [r for r in rows if r.setup == setup and r.exit_kind == chosen
                and r.symbol == inst.symbol]
        if not mine:
            continue
        for period in periods:
            body.append([inst.name, inst.klass, period,
                         *stats_cells(stats([r for r in mine if r.period == period]))])
    out += _table(["Instrument", "Class", "Period", *_STATS_HEAD], body)
    return out


def _notes() -> list[str]:
    return [
        f"- {CARRY_NOTE}",
        "- Results are in R after costs (§1.4): one round trip per trade plus financing for every "
        "calendar night held (indices charge longs and shorts differently).",
        "- Daily setups enter at the next bar's open; PB-H1-GOLD at the signal bar's close and is "
        "flat at the close of the first bar outside 07:00-16:00 London.",
        "- One open trade per instrument per setup and exit: a signal read while a trade is open "
        "is not taken, so the trade set differs by exit. A trade still open when the data ends is "
        "not counted.",
        "- In-sample and out-of-sample are split by the signal date; signals before 2006-01-01 "
        "are warm-up only. Crypto is in-sample to 2019-12-31.",
        "- The t-statistics pool trades across instruments and treat them as independent; trades "
        "that overlap in time in correlated markets make the true uncertainty larger.",
        "- E2: the last quarter's trailing stop starts once the first three quarters have filled; "
        "before that E2 is E1.",
        "- Yahoo's continuous futures (GC=F, SI=F, CL=F, BZ=F) are not back-adjusted: contract "
        "rolls are gaps in the data.",
        "- Hourly gold is as much history as Yahoo serves for 1h bars; the 4-hour trend uses "
        "UTC-aligned 4-hour blocks built from them.",
    ]


def render_report(results: Results, decision: Decision, now: dt.datetime) -> str:
    """The text of docs/cfd/BACKTEST_REPORT.md (§1.7)."""
    daily_last = [c.last for c in results.coverage if c.interval == "1d" and c.last]
    hourly = [c for c in results.coverage if c.interval == "1h" and c.last]
    through = f"daily bars through {max(daily_last).isoformat()}" if daily_last else "no daily bars"
    if hourly:
        through += (f"; hourly gold {hourly[0].first.isoformat()} to {hourly[0].last.isoformat()}")
    out = ["# CFD backtest report", "",
           f"Run at {now.isoformat()}. Source: Yahoo (yfinance), {through}. Pre-registration: "
           "docs/superpowers/specs/2026-10-05-cfd-signals.md, Part 1.", ""]

    out += ["## Outcome", ""]
    if decision.enabled:
        out += ["Enabled for live signals: " + ", ".join(f"{s} / {k}" for s, k in decision.enabled)
                + ".", ""]
    else:
        out += ["**Nothing passed the gate: no CFD signal goes live.**", ""]
    out += _table(["Setup", "Exit", "Gate", "Enabled classes"],
                  [[s, decision.chosen[s], decision.gates[s].verdict,
                    ", ".join(decision.gates[s].enabled_classes) or "none"] for s in SETUP_NAMES])

    out += ["## Gate", "",
            "Every line is pass or fail; a setup or class that fails is not traded.", ""]
    for setup in SETUP_NAMES:
        out += _gate_table(decision.gates[setup])

    for setup in SETUP_NAMES:
        out += _setup_section(setup, results.rows, decision)

    out += ["## Trade accounting", ""]
    body = [[s, e, *(str(getattr(results.counts[(s, e)], f)) for f in
                     ("signals", "closed", "skipped", "open", "blocked"))]
            for s in SETUP_NAMES for e in exits.EXIT_KINDS if (s, e) in results.counts]
    out += _table(["Setup", "Exit", "Signals", "Closed", "Skipped (R rules)", "Open at end of data",
                   "Blocked (a trade was open)"], body)

    out += ["## Data", ""]
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
    out += _table(["Instrument", "Symbol", "Interval", "Bars", "First", "Last", "Dropped",
                   "Fetched"], body)

    out += ["## Notes", "", *_notes(), ""]
    return "\n".join(out)


def write_outputs(report: str, payload: dict, out_dir: Path | str = OUT_DIR) -> list[Path]:
    """Write BACKTEST_REPORT.md and enabled.json into `out_dir` (created when missing)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "BACKTEST_REPORT.md"
    json_path = out_dir / "enabled.json"
    report_path.write_text(report, encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return [report_path, json_path]


# ================================================================ the run
def _log(message: str) -> None:
    print(f"[cfd.research] {message}", file=sys.stderr)


def _coverage(inst: ins.Instrument, interval: str, loaded: data.Loaded | None,
              fetched: str | None) -> Coverage:
    if loaded is None or not loaded.bars:
        return Coverage(inst.symbol, inst.name, interval, 0, None, None, data.Dropped(), fetched)
    return Coverage(inst.symbol, inst.name, interval, len(loaded.bars), loaded.bars[0].ts.date(),
                    loaded.bars[-1].ts.date(), loaded.dropped, fetched)


def run(*, fetch: data.Fetch | None = None, now: dt.datetime | None = None,
        cache_dir: Path | str = data.CACHE_DIR, out_dir: Path | str = OUT_DIR,
        refresh: bool = False, universe: Sequence[ins.Instrument] = ins.UNIVERSE,
        write: bool = True, log: Callable[[str], None] = _log) -> Outcome:
    """Load every series (through the cache), run all setups and exits, decide, report. A series
    that fails to load is logged and left out; the report lists it as having no data. The gold
    hourly series is always loaded for PB-H1-GOLD; `universe` restricts the daily setups."""
    now = (now or dt.datetime.now(UTC)).replace(microsecond=0)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    source = data.cached_fetch(fetch or data.yahoo_fetch, cache_dir, refresh=refresh)
    results = Results()

    def add(rows: list[Row], counts: dict[tuple[str, str], Counts]) -> None:
        results.rows += rows
        for key, c in counts.items():
            results.counts.setdefault(key, Counts()).merge(c)

    for inst in universe:
        try:
            loaded = data.load_daily(inst.symbol, fetch=source, today=now.date())
        except Exception as e:      # one dead symbol must not cost the whole run
            log(f"{inst.symbol}: no daily data ({type(e).__name__}: {str(e)[:120]})")
            loaded = None
        stamp = data.cache_stamp(cache_dir, inst.symbol, data.DAILY)
        results.coverage.append(_coverage(inst, data.DAILY, loaded, stamp[:10] if stamp else None))
        if loaded is None or not loaded.bars:
            continue
        log(f"{inst.symbol}: {len(loaded.bars)} daily bars")
        add(*backtest_daily(inst, loaded.bars))

    gold = ins.by_symbol(ins.GOLD_SYMBOL)
    try:
        hourly = data.load_hourly(gold.symbol, fetch=source, now=now)
    except Exception as e:
        log(f"{gold.symbol}: no hourly data ({type(e).__name__}: {str(e)[:120]})")
        hourly = None
    stamp = data.cache_stamp(cache_dir, gold.symbol, data.HOURLY)
    results.coverage.append(_coverage(gold, data.HOURLY, hourly, stamp[:10] if stamp else None))
    if hourly is not None and hourly.bars:
        log(f"{gold.symbol}: {len(hourly.bars)} hourly bars")
        add(*backtest_gold(hourly.bars))

    decision = decide(results.rows)
    report = render_report(results, decision, now)
    payload = enabled_payload(decision, now)
    if write:
        write_outputs(report, payload, out_dir)
    return Outcome(results, decision, report, payload)


def main(argv: Sequence[str] | None = None, *, fetch: data.Fetch | None = None,
         now: dt.datetime | None = None, cache_dir: Path | str | None = None,
         out_dir: Path | str | None = None,
         universe: Sequence[ins.Instrument] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cfd.research", description=__doc__.split("\n\n")[0])
    ap.add_argument("--run", action="store_true",
                    help="download (or read from data/cfd_cache/) the bars, run the research, "
                         "write docs/cfd/BACKTEST_REPORT.md and docs/cfd/enabled.json")
    ap.add_argument("--refresh", action="store_true", help="download the bars again")
    args = ap.parse_args(argv)
    if not args.run:
        print("Nothing to do: pass --run to run the research (it downloads from Yahoo on the first "
              "run). Usage: python -m cfd.research --run [--refresh]")
        return 2
    outcome = run(fetch=fetch, now=now, refresh=args.refresh,
                  cache_dir=cache_dir if cache_dir is not None else data.CACHE_DIR,
                  out_dir=out_dir if out_dir is not None else OUT_DIR,
                  universe=universe if universe is not None else ins.UNIVERSE)
    for setup in SETUP_NAMES:
        gate = outcome.decision.gates[setup]
        enabled = ", ".join(gate.enabled_classes) or "none"
        print(f"{setup}: exit {outcome.decision.chosen[setup]}, gate {gate.summary()}, "
              f"enabled: {enabled}")
    missing = [f"{c.symbol} ({c.interval})" for c in outcome.results.coverage if c.bars == 0]
    if missing:      # the verdict above rests on a partial universe: re-run (the cache keeps the rest)
        print("WARNING: no data for " + ", ".join(missing) + " -- the outcome covers the rest only")
    pairs = ", ".join(f"{s}/{k}" for s, k in outcome.decision.enabled) or "none"
    print(f"enabled: {pairs}")
    print(f"written: {Path(out_dir if out_dir is not None else OUT_DIR)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
