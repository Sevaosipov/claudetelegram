"""cfd/exits.py: the exits compared in spec 2026-10-05-cfd-signals.md §1.3, as one bar-by-bar state
machine (`LadderState`) that the backtest drives here and the live tracker will drive later, plus
`simulate`, which turns a signal and a list of bars into a `Trade` with its result in R.

The three exits (R is the distance from entry to the initial stop):
  E0  the setup's own exit: a stop trailing 3 ATR from the best extreme (each bar, long:
      stop = max(stop, high - 3 ATR)), starting from the initial stop; PB-H1-GOLD adds a hard
      target at 5R. One part, the whole position.
  E1  a quarter closes at each of TP1..TP4 = +1R..+4R; the stop moves to the entry after TP1,
      to TP1 after TP2, to TP2 after TP3; what is left closes at the stop.
  E2  the same three quarters at +1R, +2R, +3R with the same stop steps; the last quarter is a
      runner with a hard target at +5R. Once it is alone (after TP3) its stop is the baseline
      trailing stop -- kept from the entry exactly as E0 keeps it -- and never below the stepped
      stop. Before TP3 the stepped stops rule, so the first three quarters behave exactly as in E1.
      (Spec ambiguity, resolved this way and listed in the research report.)

Bar order (conservative, §1.3). Inside one bar the stop is checked before the next target. A bar
that opens beyond a target fills it at the open; a stop gapped through at the open fills at the
open. A target fill moves the stop at once, and the stop is checked again before the next target,
so a bar that reaches TP1 and then trades down to the entry stops the rest out at the entry. A
trailing stop updated from this bar's high is effective from the next bar (as in the Pine script,
where the exit order is refreshed at the close). The session-end close of PB-H1-GOLD applies to
every exit: after the bar's own stop and target handling, whatever is left closes at that bar's
close.

`LadderState.step(bar, atr, session_end=...)` is pure and free of I/O; its fields (stage, remaining,
stop, trail) are everything needed to stop and resume it from stored values.

Costs in R (§1.4): result = sum(fraction * signed move / R) - (round trip + nights * financing) *
entry / R, the percentages being of the entry price. Nights are the calendar-day boundaries between
the entry and the final exit for daily trades; the intraday gold trade has none.

Round 2 (docs/cfd/PREREGISTRATION_R2.md) generalises this without changing a round-1 result:
  EL    a ladder of four quarters at +0.5R, +1.0R, +1.5R and +2.0R. The stop stays at its initial
        level until TP2, moves to the entry after TP2 and to TP1 (+0.5R) after TP3; there is no
        trailing stop. (E1 and E2 are the same machine with their own legs and stop steps: the
        tables `_LEGS` and `_STOP_AFTER`.)
  E0c   one part with a stop and no target: the initial stop, which trails by `trail_atr` ATR from
        the best extreme (never loosening, effective from the next bar, as E0's does) when that is
        given -- H5 trails 6 ATR, H4 not at all.
Two ways of leaving belong to `simulate`, which knows the bar numbers; both fill at a bar's open and
close whatever is left, so they come before that bar's own stop and target handling (the price is
the open either way):
  time stop      `time_stop_bars=N`: the open of the Nth bar after the entry bar (the entry bar being
                 the bar whose open is the entry for a next-open entry).
  exit condition `exit_when(index, side)`: true at the close of a bar, at or after the entry bar's
                 close, means leaving at the next bar's open. A condition read at the signal bar's
                 close is before the entry and never counts.
`max_r_atr` raises the upper bound of the R rule (H5: 8 ATR, its stop being 6 ATR by construction).
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from cfd import indicators as ind
from cfd.data import Bar, Session
from cfd.instruments import Costs
from cfd.setups import Signal

E0, E1, E2 = "E0", "E1", "E2"
EXIT_KINDS = (E0, E1, E2)               # round 1: research.py runs these
E0C, EL = "E0c", "EL"                   # round 2: research2.py runs these
ROUND2_EXIT_KINDS = (E0C, EL)
ALL_EXIT_KINDS = EXIT_KINDS + ROUND2_EXIT_KINDS
ENTRY_MODES = ("next_open", "close")

TRAIL_ATR = 3.0          # E0 trailing distance, in ATR
MIN_R_ATR = 0.25         # a trade is skipped when R <= this many ATR (a gap through the stop level)
MAX_R_ATR = 6.0          # ... or when R is more than this many ATR
QUARTER = 0.25
RUNNER_STAGE = 3         # E2: the runner is alone once this many take-profits have filled

# (take-profit in R, share of the position, label), in the order they fill
_E1_LEGS = ((1.0, QUARTER, "TP1"), (2.0, QUARTER, "TP2"),
            (3.0, QUARTER, "TP3"), (4.0, QUARTER, "TP4"))
_E2_LEGS = ((1.0, QUARTER, "TP1"), (2.0, QUARTER, "TP2"),
            (3.0, QUARTER, "TP3"), (5.0, QUARTER, "TP4"))
_EL_LEGS = ((0.5, QUARTER, "TP1"), (1.0, QUARTER, "TP2"),
            (1.5, QUARTER, "TP3"), (2.0, QUARTER, "TP4"))
_WHOLE = "ALL"           # the label of E0's (and E0c's) single part
_LEGS = {E1: _E1_LEGS, E2: _E2_LEGS, EL: _EL_LEGS}
# Once `stage` take-profits have filled (the index) the stop moves, never loosening, to this many R
# from the entry; None: it stays where it is.
#   E1, E2: the entry after TP1, TP1 after TP2, TP2 after TP3.
#   EL:     the initial stop until TP2, then the entry after TP2 and TP1 (+0.5R) after TP3.
_STOP_AFTER = {E1: (None, 0, 1, 2), E2: (None, 0, 1, 2), EL: (None, None, 0, 0.5)}
_SINGLE_KINDS = (E0, E0C)               # one part, the whole position


@dataclass(frozen=True)
class Event:
    """One fill of the position. `kind` is "tp" (a take-profit leg), "target" (E0's hard target),
    "stop", "session", or, from round 2, "time" (the time stop) and "signal" (the exit condition);
    `parts` names the legs it closed ("TP3", "TP4"; ("ALL",) for E0);
    `fraction` is the share of the whole position closed; `stage` the take-profits reached after
    it; `stop` the stop in force afterwards (None once the position is closed); `gap` is True when
    it filled at the bar's open because the bar gapped through the level."""
    kind: str
    label: str
    price: float
    fraction: float
    parts: tuple[str, ...]
    stage: int
    stop: float | None
    gap: bool = False


@dataclass
class LadderState:
    """An open position under one of the exits. Created at the entry with the side, the entry
    price, the initial stop and the exit kind; `step` feeds it completed bars in order."""
    side: str
    entry: float
    stop0: float
    kind: str
    target_r: float | None = None     # E0's hard target in R (PB-H1-GOLD: 5); ignored by the ladders
    stage: int = 0                    # take-profits reached
    remaining: float = 1.0            # share of the original position still open
    stop: float | None = None         # the stop in force (starts at stop0)
    trail: float | None = None        # E2: the baseline trailing stop, kept from the entry
    closed: bool = False
    # The trailing distance in ATR of E0, E2's runner and E0c. None means the default: 3 for E0 and
    # E2 (TRAIL_ATR), no trailing stop at all for E0c. E1 and EL never trail.
    trail_atr: float | None = None

    def __post_init__(self):
        if self.side not in ("long", "short"):
            raise ValueError(f"side must be 'long' or 'short', not {self.side!r}")
        if self.kind not in ALL_EXIT_KINDS:
            raise ValueError(f"exit kind must be one of {ALL_EXIT_KINDS}, not {self.kind!r}")
        if (self.entry - self.stop0) * self._sign <= 0:
            raise ValueError("the initial stop must be on the losing side of the entry")
        if self.target_r is not None and self.target_r <= 0:
            raise ValueError("target_r must be positive")
        if self.trail_atr is not None and self.trail_atr <= 0:
            raise ValueError("trail_atr must be positive")
        if self.stop is None:
            self.stop = self.stop0
        if self.kind == E2 and self.trail is None:
            self.trail = self.stop0

    # ------------------------------------------------------------ geometry
    @property
    def _sign(self) -> int:
        return 1 if self.side == "long" else -1

    @property
    def r(self) -> float:
        return abs(self.entry - self.stop0)

    def level(self, multiple: float) -> float:
        """The price `multiple` R in the trade's favour of the entry."""
        return self.entry + self._sign * multiple * self.r

    def _legs(self) -> list[tuple[float, float, str]]:
        """The parts still open, in the order they fill: (R multiple, share, label)."""
        if self.kind in _SINGLE_KINDS:
            return [(self.target_r, 1.0, _WHOLE)] if self.target_r else [(None, 1.0, _WHOLE)]
        return list(_LEGS[self.kind][self.stage:])

    def _raise_stop(self, price: float) -> None:
        """Move the stop towards the trade's favour only: a stop never loosens."""
        s = self._sign
        self.stop = s * max(s * self.stop, s * price)

    # ------------------------------------------------------------ one bar
    def step(self, bar: Bar, atr: float | None = None, *, session_end: bool = False) -> list[Event]:
        """Advance one completed bar. Returns the fills in the order they happened (usually none).
        `atr` is the ATR at this bar (E0 and E2 trail with it); `session_end` closes whatever is
        left at the bar's close once the bar's own stop and target handling is done."""
        events: list[Event] = []
        if self.closed:
            return events
        s = self._sign
        op = s * bar.open
        best = s * (bar.high if s > 0 else bar.low)         # the most favourable price of the bar
        worst = s * (bar.low if s > 0 else bar.high)        # the most adverse
        at_open = True
        while not self.closed:
            stop = s * self.stop
            if at_open and op <= stop:                      # gapped through the stop that was in force
                events.append(self._close("stop", "STOP", bar.open, gap=True))
                break
            if worst <= stop:                               # stop before the next target
                events.append(self._close("stop", "STOP", self.stop, gap=False))
                break
            at_open = False
            legs = self._legs()
            multiple, fraction, label = legs[0] if legs else (None, 0.0, "")
            if multiple is None:                            # nothing left to take profit at
                break
            level = self.level(multiple)
            if op >= s * level:                             # opens beyond the target: fills at the open
                events.append(self._take(multiple, fraction, label, bar.open, gap=True))
            elif best >= s * level:
                events.append(self._take(multiple, fraction, label, level, gap=False))
            else:
                break
        if not self.closed:
            self._trail(best, atr)
            if session_end:
                events.append(self._close("session", "SESSION", bar.close, gap=False))
        return events

    def close_at(self, price: float, kind: str = "session") -> list[Event]:
        """Close whatever is left at `price` (the session ended in a gap of the data)."""
        if self.closed:
            return []
        return [self._close(kind, kind.upper(), price, gap=False)]

    # ------------------------------------------------------------ fills
    def _take(self, multiple: float, fraction: float, label: str, price: float, gap: bool) -> Event:
        self.remaining -= fraction
        single = self.kind in _SINGLE_KINDS
        if not single:
            self.stage += 1
        if self.remaining <= 1e-12:
            self.remaining, self.closed = 0.0, True
            return Event("target" if single else "tp", "TARGET" if single else label, price,
                         fraction, (label,), self.stage, None, gap)
        # the stop steps up on the kind's schedule (_STOP_AFTER)
        step = _STOP_AFTER[self.kind][self.stage]
        if step is not None:
            self._raise_stop(self.level(step))
        if self.kind == E2 and self.stage >= RUNNER_STAGE:
            self._raise_stop(self.trail)
        return Event("tp", label, price, fraction, (label,), self.stage, self.stop, gap)

    def _close(self, kind: str, label: str, price: float, gap: bool) -> Event:
        parts = tuple(leg[2] for leg in self._legs())
        fraction = self.remaining
        self.remaining, self.closed = 0.0, True
        return Event(kind, label, price, fraction, parts, self.stage, None, gap)

    def _trail_multiple(self) -> float | None:
        if self.trail_atr is not None:
            return self.trail_atr
        return None if self.kind == E0C else TRAIL_ATR

    def _trail(self, best: float, atr: float | None) -> None:
        """End of bar: ratchet the trailing stop from this bar's best price (effective next bar)."""
        if atr is None or self.kind in (E1, EL):
            return
        multiple = self._trail_multiple()
        if multiple is None:
            return
        s = self._sign
        candidate = s * (best - multiple * atr)              # back in price terms
        if self.kind in _SINGLE_KINDS:
            self._raise_stop(candidate)
        else:
            self.trail = s * max(s * self.trail, s * candidate)
            if self.stage >= RUNNER_STAGE:
                self._raise_stop(self.trail)


# ================================================================ the trade
@dataclass(frozen=True)
class Part:
    """One fill of a trade, with the bar it happened on."""
    fraction: float
    price: float
    kind: str            # "tp" | "target" | "stop" | "session"
    label: str           # "TP1".."TP4", "TARGET", "STOP", "SESSION"
    index: int           # the bar's index in the list that was simulated
    ts: dt.datetime      # that bar's start time
    gap: bool = False


@dataclass(frozen=True)
class Trade:
    """The outcome of one signal under one exit. `status` is "closed" (a result), "skipped" (the
    entry broke the R rules of §1.2: `note` says which) or "open" (the data ended before the
    trade did, or before it could enter). Only a closed trade carries parts and a result."""
    status: str
    side: str
    exit_kind: str
    signal_index: int
    entry_index: int | None = None
    exit_index: int | None = None
    entry_ts: dt.datetime | None = None
    exit_ts: dt.datetime | None = None
    entry_price: float | None = None
    stop0: float | None = None
    r: float | None = None               # |entry - stop0|, in price units
    parts: tuple[Part, ...] = ()
    gross_r: float = 0.0                 # sum(fraction * signed move / R), before costs
    cost_r: float = 0.0                  # (round trip + nights * financing) * entry / R
    result_r: float = 0.0                # gross_r - cost_r
    nights: int = 0
    note: str = ""

    @property
    def closed(self) -> bool:
        return self.status == "closed"

    @property
    def holding_days(self) -> float:
        """Calendar days from the entry bar to the exit bar (0.0 for a trade that did not close)."""
        if self.entry_ts is None or self.exit_ts is None:
            return 0.0
        return (self.exit_ts - self.entry_ts).total_seconds() / 86400.0


# An exit condition: (bar index, side) -> true when, at the close of that bar, the position should
# leave at the next bar's open (H4: the close is above the 5-day average; H5: the state is no longer
# the trade's side).
ExitWhen = Callable[[int, str], bool]


def simulate(bars: Sequence[Bar], signal: Signal, exit_kind: str, *, costs: Costs,
             entry: str = "next_open", session: Session | None = None,
             atr: Sequence[float | None] | None = None, e0_target_r: float | None = None,
             trail_atr: float | None = None, time_stop_bars: int | None = None,
             exit_when: ExitWhen | None = None, max_r_atr: float = MAX_R_ATR) -> Trade:
    """Run one signal through one exit over `bars`.

    entry="next_open" enters at the open of the bar after the signal bar (the daily setups);
    entry="close" at the signal bar's close (the gold setup). Either way the trade lives through the
    bars after the signal bar. The stop is the signal's, fixed at the signal; R is measured from the
    actual entry. `session` (PB-H1-GOLD) closes the trade at the close of the first bar outside it
    -- or at the last price seen, when the data skipped the end of the session -- and means no
    financing nights. `atr` is the ATR series aligned with `bars` (computed when omitted).
    `e0_target_r` is E0's hard target in R (5 for the gold setup).

    Round 2: `trail_atr` is the trailing distance in ATR (see LadderState; E0c trails only when it
    is given); `time_stop_bars=N` leaves at the open of the Nth bar after the entry bar;
    `exit_when(i, side)` leaves at the open of bar i + 1 when true at the close of bar i, from the
    entry bar's close on; `max_r_atr` is the upper bound of the R rule (6 ATR unless raised). When a
    trade leaves by time or condition it fills at that bar's open, before the bar's own stop and
    target handling -- the stop and a target would also fill at the open if the bar gapped through
    them. The earlier of the two exits wins; at the same bar the time stop is reported."""
    if exit_kind not in ALL_EXIT_KINDS:
        raise ValueError(f"exit kind must be one of {ALL_EXIT_KINDS}, not {exit_kind!r}")
    if entry not in ENTRY_MODES:
        raise ValueError(f"entry must be one of {ENTRY_MODES}, not {entry!r}")
    if time_stop_bars is not None and (isinstance(time_stop_bars, bool)
                                       or not isinstance(time_stop_bars, int) or time_stop_bars < 1):
        raise ValueError(f"time_stop_bars must be a whole number of bars, at least 1, not {time_stop_bars!r}")
    if max_r_atr <= 0:
        raise ValueError(f"max_r_atr must be positive, not {max_r_atr!r}")
    n = len(bars)
    first = signal.index + 1                       # the first bar the trade lives through
    base = dict(side=signal.side, exit_kind=exit_kind, signal_index=signal.index)

    entry_index = first if entry == "next_open" else signal.index
    if entry_index >= n:
        return Trade(status="open", note="no bar after the signal yet", **base)
    entry_price = bars[entry_index].open if entry == "next_open" else bars[entry_index].close
    entry_ts = bars[entry_index].ts
    distance = (entry_price - signal.stop) if signal.side == "long" else (signal.stop - entry_price)
    if distance <= MIN_R_ATR * signal.atr:
        return Trade(status="skipped", entry_index=entry_index, entry_ts=entry_ts,
                     entry_price=entry_price, stop0=signal.stop,
                     note="R <= 0.25 ATR (the entry is on or through the stop level)", **base)
    if distance > max_r_atr * signal.atr:
        return Trade(status="skipped", entry_index=entry_index, entry_ts=entry_ts,
                     entry_price=entry_price, stop0=signal.stop, r=distance,
                     note=f"R > {max_r_atr:g} ATR", **base)

    atr_series = atr if atr is not None else ind.atr(bars)
    state = LadderState(signal.side, entry_price, signal.stop, exit_kind,
                        target_r=e0_target_r if exit_kind == E0 else None, trail_atr=trail_atr)
    entry_day = session.local_date(entry_ts) if session is not None else None
    parts: list[Part] = []
    exit_index: int | None = None

    def record(events: list[Event], index: int) -> None:
        for e in events:
            parts.append(Part(e.fraction, e.price, e.kind, e.label, index, bars[index].ts, e.gap))

    for i in range(first, n):
        bar = bars[i]
        if session is not None and session.local_date(bar.ts) != entry_day:
            # the data skipped the end of the session: leave at the last price seen in it
            record(state.close_at(bars[i - 1].close), i - 1)
            exit_index = i - 1
            break
        leave = None                                # leaving at this bar's open: time stop, then condition
        if time_stop_bars is not None and i - entry_index >= time_stop_bars:
            leave = "time"
        elif exit_when is not None and i > entry_index and exit_when(i - 1, signal.side):
            leave = "signal"
        if leave is not None:
            record(state.close_at(bar.open, leave), i)
            exit_index = i
            break
        end = session is not None and not session.contains(bar.ts)
        record(state.step(bar, atr_series[i], session_end=end), i)
        if state.closed:
            exit_index = i
            break
    if not state.closed:
        return Trade(status="open", entry_index=entry_index, entry_ts=entry_ts,
                     entry_price=entry_price, stop0=signal.stop, r=distance,
                     note="not finished when the data ended", **base)

    exit_ts = bars[exit_index].ts
    nights = 0 if session is not None else max(0, (exit_ts.date() - entry_ts.date()).days)
    sign = 1 if signal.side == "long" else -1
    gross = sum(p.fraction * sign * (p.price - entry_price) / distance for p in parts)
    cost_pct = costs.round_trip_pct + nights * costs.financing_pct(signal.side)
    cost = cost_pct / 100.0 * entry_price / distance
    return Trade(status="closed", entry_index=entry_index, exit_index=exit_index,
                 entry_ts=entry_ts, exit_ts=exit_ts, entry_price=entry_price, stop0=signal.stop,
                 r=distance, parts=tuple(parts), gross_r=gross, cost_r=cost,
                 result_r=gross - cost, nights=nights, **base)
