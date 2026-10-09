"""chart_check.py: the chart verdict of a signal -- by fixed rules first, then reviewed by Claude.

A verdict that only a language model gives can differ between two runs on the same chart. So the verdict
is computed here, from the daily closes, by rules that give the same answer every time; Claude (the
analyst, with TradingView's data, the news and the earnings calendar) then reviews it: it confirms the
verdict or changes it, and when it changes it the line says what the rules had said. When Claude does not
answer, the rules' verdict goes out alone, marked as such -- a signal always carries a verdict.

The rules, for a buy (a sell mirrors them):
  against  the close is under its 200-day average; or the price jumped JUMP or more in a day within the
           last 60 bars and has stood within PIN_RANGE since (an offer, a peg: nowhere to go); or it is
           DRAWDOWN or more under its 60-bar high and under its 50-day average;
  for      the close is above its 50-day average and its 200-day average (when there are 200 bars), the
           50-day is not under the 200-day, RSI(14) is under RSI_HOT and the close is not within
           NEAR_HIGH of its 1-year high;
  neutral  anything else (overbought at the high, between the averages, too little history).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

FOR, NEUTRAL, AGAINST = "за", "нейтрален", "против"
VERDICT_TEXT = {FOR: "график за", NEUTRAL: "график нейтрален", AGAINST: "график против"}
JUMP, PIN_RANGE, PIN_MIN_BARS = 0.15, 0.05, 5
DRAWDOWN = 0.20
RSI_HOT, RSI_COLD = 75.0, 25.0
NEAR_HIGH = 0.03
MIN_BARS = 60


@dataclass(frozen=True)
class Facts:
    last: float
    sma20: float | None
    sma50: float | None
    sma200: float | None
    rsi: float | None
    high_60: float
    low_60: float
    high_252: float
    low_252: float
    ret_20: float | None
    jump: float                 # the largest one-day rise (a buy) or fall (a sell) of the last 60 bars
    pinned: bool                # ... of JUMP or more, and the closes since within PIN_RANGE
    bars: int


def _sma(values: list[float], n: int) -> float | None:
    return sum(values[-n:]) / n if len(values) >= n else None


def _rsi(values: list[float], n: int = 14) -> float | None:
    """Wilder's RSI of the last close."""
    if len(values) <= n:
        return None
    moves = [b - a for a, b in zip(values, values[1:])]
    gain = sum(max(m, 0.0) for m in moves[:n]) / n
    loss = sum(max(-m, 0.0) for m in moves[:n]) / n
    for m in moves[n:]:
        gain = (gain * (n - 1) + max(m, 0.0)) / n
        loss = (loss * (n - 1) + max(-m, 0.0)) / n
    return 100.0 if loss == 0 else 100.0 - 100.0 / (1.0 + gain / loss)


def facts(closes: list[float], side: str = "long") -> Facts | None:
    """The numbers the rules read, from daily closes oldest first; None with fewer than MIN_BARS."""
    values = [c for c in closes if c]
    if len(values) < MIN_BARS:
        return None
    sign = 1 if side == "long" else -1
    recent = values[-61:]
    moves = [(i, sign * (b / a - 1)) for i, (a, b) in enumerate(zip(recent, recent[1:]))]
    at, jump = max(moves, key=lambda m: m[1])
    since = recent[at + 1:]
    pinned = (jump >= JUMP and len(since) >= PIN_MIN_BARS
              and max(since) / min(since) - 1 <= PIN_RANGE)
    year = values[-252:]
    return Facts(values[-1], _sma(values, 20), _sma(values, 50), _sma(values, 200), _rsi(values),
                 max(values[-60:]), min(values[-60:]), max(year), min(year),
                 values[-1] / values[-21] - 1 if len(values) > 20 else None, jump, pinned, len(values))


def verdict(f: Facts | None, side: str = "long") -> tuple[str, str]:
    """(the verdict, why in a few Russian words) by the rules of the module docstring."""
    if f is None:
        return NEUTRAL, "мало истории цен"
    long = side == "long"
    above = (lambda a, b: a > b) if long else (lambda a, b: a < b)
    if f.pinned:
        return AGAINST, f"после скачка {f.jump * 100:+.0f}% цена стоит на месте"
    if f.sma200 is not None and not above(f.last, f.sma200):
        return AGAINST, f"цена {'ниже' if long else 'выше'} 200-дн. средней"
    extreme = f.high_60 if long else f.low_60
    off = abs(f.last / extreme - 1)
    if off >= DRAWDOWN and f.sma50 is not None and not above(f.last, f.sma50):
        return AGAINST, f"{off * 100:.0f}% от {'максимума' if long else 'минимума'} 60 дней и за 50-дн. средней"
    trend = (f.sma50 is not None and above(f.last, f.sma50)
             and (f.sma200 is None or (above(f.last, f.sma200) and not above(f.sma200, f.sma50))))
    hot = f.rsi is not None and (f.rsi >= RSI_HOT if long else f.rsi <= RSI_COLD)
    year_extreme = f.high_252 if long else f.low_252
    at_extreme = abs(f.last / year_extreme - 1) <= NEAR_HIGH
    if trend and not hot and not at_extreme:
        return FOR, f"цена {'выше' if long else 'ниже'} 50- и 200-дн. средних"
    if trend and (hot or at_extreme):
        return NEUTRAL, ("RSI " + f"{f.rsi:.0f}" if hot else
                         f"цена у годового {'максимума' if long else 'минимума'}") + ", тренд по сигналу"
    return NEUTRAL, "тренд не подтверждён средними"


def _n(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x:.{2 if abs(x) >= 1 else 4 if abs(x) >= 0.01 else 6}f}"


def facts_text(f: Facts | None, rule: str, why: str) -> str:
    """The rules' numbers and verdict as lines for the analyst's prompt."""
    lines = [f"Вердикт по правилам бота: {VERDICT_TEXT[rule]} ({why})"]
    if f is not None:
        lines += [f"Последнее закрытие (данные бота): {_n(f.last)}",
                  f"Средние 20/50/200 дней: {_n(f.sma20)} / {_n(f.sma50)} / {_n(f.sma200)}",
                  f"RSI(14): {_n(f.rsi)}",
                  f"Диапазон 60 дней: {_n(f.low_60)}–{_n(f.high_60)}; год: {_n(f.low_252)}–{_n(f.high_252)}",
                  f"Крупнейший дневной скачок за 60 дней: {f.jump * 100:+.0f}%"
                  + (" — после него цена стоит на месте" if f.pinned else "")]
    return "\n".join(lines)


def _split(note: str) -> tuple[str | None, str]:
    """(the verdict a note opens with, the rest of it)."""
    low = note.lower()
    for v in (FOR, NEUTRAL, AGAINST):
        head = VERDICT_TEXT[v]
        if low.startswith(head):
            return v, note[len(head):].lstrip(" —-:.,")
    return None, note


def compose(rule: str, why: str, note: str | None) -> str:
    """The line under a signal: Claude's verdict and sentence; when it differs from the rules', what the
    rules had said; with no note from Claude, the rules' verdict alone, marked."""
    if note:
        claude, text = _split(note)
        if claude is not None:
            head = VERDICT_TEXT[claude].capitalize()
            if claude != rule:
                head += f" (по правилам: {rule})"
            return f"{head} — {text}" if text else head
    return f"{VERDICT_TEXT[rule].capitalize()} (по правилам, без Claude) — {why}"


def line(name: str, ticker: str, source: str | None, side: str, extra: str = "", *, closes_fn=None,
         note_fn=None) -> str | None:
    """The whole check of one signal: the rules on the bot's own daily closes, Claude's review (asked
    twice at most), the line. `name` is what the chart calls the asset, `side` "long" or "short". Never
    raises: a failed step is logged and the line is made of what there is -- None when there is neither a
    price history nor a review."""
    f = None
    try:
        import positions
        rows = (closes_fn or positions.daily_closes)(ticker, source)
        f = facts([c for _d, c in rows], side)
    except Exception as e:
        print(f"[chart] {ticker}: no price history: {type(e).__name__}: {e}", file=sys.stderr)
    rule, why = verdict(f, side)
    note = None
    try:
        import analyst
        ask = note_fn or analyst.signal_note
        text = "\n".join(x for x in (extra, facts_text(f, rule, why)) if x)
        for _attempt in range(2):
            note = ask(name, "покупка" if side == "long" else "продажа", text)
            if note and _split(note)[0] is not None:
                break
            note = None
    except Exception as e:
        print(f"[chart] {ticker}: review failed: {type(e).__name__}: {e}", file=sys.stderr)
    if f is None and note is None:              # no history and no review: nothing worth a line
        return None
    return compose(rule, why, note)


WORDS = {FOR: "For", NEUTRAL: "Neutral", AGAINST: "Against"}


def word(line: str | None) -> str | None:
    """The verdict of a line() as one word for a signal's block: «For», «Neutral» or «Against»; None for
    no line."""
    verdict_, _rest = _split(line or "")
    return WORDS.get(verdict_)

