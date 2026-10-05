"""cfd/indicators.py: ATR, EMA, SMA, ADX, RSI and the rolling highest/lowest the setups are built on --
pure functions, each returning a list aligned with its input (None until there is enough history).

The smoothing is Wilder's where the spec says so (ATR and ADX, §1.2), done the way Pine's ta.atr
and ta.dmi do it, because PB-H1-GOLD is a port of a Pine script and its numbers must match the
chart it was fitted on: a Wilder average (RMA) is seeded with the simple average of its first
`length` values and then moves by (previous * (length - 1) + x) / length. The EMA is seeded the
same way (a simple average of the first `length` closes), so no value exists before its window
is full.

Every value at index t uses bars up to t only: indicators are "computed on completed bars only".
"""
from __future__ import annotations

from collections.abc import Sequence

from cfd.data import Bar


def _check(length: int) -> None:
    if length < 1:
        raise ValueError(f"length must be at least 1, got {length}")


# ---------------------------------------------------------------- columns
def highs(bars: Sequence[Bar]) -> list[float]:
    return [b.high for b in bars]


def lows(bars: Sequence[Bar]) -> list[float]:
    return [b.low for b in bars]


def closes(bars: Sequence[Bar]) -> list[float]:
    return [b.close for b in bars]


# ---------------------------------------------------------------- averages and windows
def sma(values: Sequence[float], length: int) -> list[float | None]:
    _check(length)
    return [None if i + 1 < length else sum(values[i + 1 - length:i + 1]) / length
            for i in range(len(values))]


def ema(values: Sequence[float], length: int) -> list[float | None]:
    """Exponential average, alpha = 2 / (length + 1), seeded with the SMA of the first `length`."""
    _check(length)
    out: list[float | None] = [None] * len(values)
    if len(values) < length:
        return out
    alpha = 2.0 / (length + 1)
    prev = sum(values[:length]) / length
    out[length - 1] = prev
    for i in range(length, len(values)):
        prev = alpha * values[i] + (1.0 - alpha) * prev
        out[i] = prev
    return out


def highest(values: Sequence[float], length: int) -> list[float | None]:
    """The highest of the last `length` values, the current one included (Pine's ta.highest)."""
    _check(length)
    return [None if i + 1 < length else max(values[i + 1 - length:i + 1])
            for i in range(len(values))]


def lowest(values: Sequence[float], length: int) -> list[float | None]:
    """The lowest of the last `length` values, the current one included (Pine's ta.lowest)."""
    _check(length)
    return [None if i + 1 < length else min(values[i + 1 - length:i + 1])
            for i in range(len(values))]


# ---------------------------------------------------------------- Wilder
def _rma(values: Sequence[float | None], length: int) -> list[float | None]:
    """Wilder's moving average (Pine's ta.rma). `values` may start with Nones; the average is
    seeded at the length-th real value with their simple average."""
    out: list[float | None] = [None] * len(values)
    start = next((i for i, v in enumerate(values) if v is not None), None)
    if start is None or len(values) < start + length:
        return out
    seed = start + length - 1
    prev = sum(values[start:seed + 1]) / length          # type: ignore[arg-type]
    out[seed] = prev
    for i in range(seed + 1, len(values)):
        prev = (prev * (length - 1) + values[i]) / length   # type: ignore[operator]
        out[i] = prev
    return out


def rsi(closes: Sequence[float], n: int = 14) -> list[float | None]:
    """Wilder's relative strength index of a close series (Pine's ta.rsi): the average gain and the
    average loss of the close-to-close changes are Wilder-smoothed (an RMA seeded with the simple
    average of the first `n` changes), and RSI = 100 - 100 / (1 + average gain / average loss). The
    first value is at index `n`. Like Pine, a window without losses reads 100 (also when there were
    no gains either) and one without gains reads 0. H4 (IDX-DIP) reads RSI(2)."""
    _check(n)
    gains: list[float | None] = [None] * len(closes)
    losses: list[float | None] = [None] * len(closes)
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains[i] = change if change > 0 else 0.0
        losses[i] = -change if change < 0 else 0.0
    up, down = _rma(gains, n), _rma(losses, n)
    out: list[float | None] = [None] * len(closes)
    for i in range(len(closes)):
        if up[i] is None or down[i] is None:
            continue
        if down[i] == 0:                                      # type: ignore[operator]
            out[i] = 100.0
        elif up[i] == 0:                                      # type: ignore[operator]
            out[i] = 0.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + up[i] / down[i])  # type: ignore[operator]
    return out


def true_range(bars: Sequence[Bar]) -> list[float]:
    """High - low on the first bar, then the largest of high - low and the distances from the
    previous close (Pine's ta.tr(true), the range ta.atr smooths)."""
    out: list[float] = []
    for i, b in enumerate(bars):
        if i == 0:
            out.append(b.high - b.low)
        else:
            pc = bars[i - 1].close
            out.append(max(b.high - b.low, abs(b.high - pc), abs(b.low - pc)))
    return out


def atr(bars: Sequence[Bar], length: int = 14) -> list[float | None]:
    """Average true range, Wilder-smoothed (Pine's ta.atr): first value at bar `length`."""
    _check(length)
    return _rma(true_range(bars), length)


def adx(bars: Sequence[Bar], length: int = 14) -> list[float | None]:
    """Average directional index with Wilder smoothing, as Pine's ta.dmi(length, length) computes
    it: +DM / -DM / true range are RMA-smoothed from bar 1, DX = |+DI - -DI| / (+DI + -DI), and
    ADX = 100 * RMA(DX). The first value is at bar 2 * length - 1. A window with no range at all
    keeps the previous +DI/-DI (Pine's fixnan), starting from zero."""
    _check(length)
    n = len(bars)
    tr: list[float | None] = [None] * n
    plus_dm: list[float | None] = [None] * n
    minus_dm: list[float | None] = [None] * n
    for i in range(1, n):
        b, p = bars[i], bars[i - 1]
        tr[i] = max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close))
        up, down = b.high - p.high, p.low - b.low
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0
    tr_s, plus_s, minus_s = _rma(tr, length), _rma(plus_dm, length), _rma(minus_dm, length)
    dx: list[float | None] = [None] * n
    plus_di = minus_di = 0.0
    for i in range(n):
        if tr_s[i] is None:
            continue
        if tr_s[i] > 0:                                       # type: ignore[operator]
            plus_di = 100.0 * plus_s[i] / tr_s[i]             # type: ignore[operator]
            minus_di = 100.0 * minus_s[i] / tr_s[i]           # type: ignore[operator]
        total = plus_di + minus_di
        dx[i] = abs(plus_di - minus_di) / (total if total != 0 else 1.0)
    return [None if v is None else 100.0 * v for v in _rma(dx, length)]
