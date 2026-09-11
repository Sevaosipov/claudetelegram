"""Shared terminal-output formatting: TTY-gated ANSI color and simple
box-drawing helpers for headers and tables.

Every report-formatting function in this project (model_eval.py, backtest.py,
research.py, menu.py) used to hand-pad its own tables with f-strings and draw
its own "====" / "-- Section --" dividers -- the same padding math four times
over, drifting out of alignment whenever a value came out wider than expected.
This module is the one place that logic lives now.

Color is purely structural -- headers, labels, emphasis -- never "green means
good, red means bad" on a financial number. This project deliberately never
renders a verdict on a signal or a metric, and color-coding numbers by whether
they look favorable would quietly reintroduce one through the back door.
"""
from __future__ import annotations

import sys

_TTY = sys.stdout.isatty()

BOLD = "\033[1m" if _TTY else ""
DIM = "\033[2m" if _TTY else ""
CYAN = "\033[36m" if _TTY else ""
GREEN = "\033[92m" if _TTY else ""
RESET = "\033[0m" if _TTY else ""


def rule(width: int = 72, char: str = "─") -> str:
    return char * width


def _boxed_line(text: str, width: int) -> str:
    inner = width - 4  # "║ " + content + " ║"
    return f"║ {text:<{inner}} ║"


def header(title: str, subtitle: str = "", width: int = 72) -> str:
    """A boxed banner for a report's top-level title.

    Widens past `width` rather than truncating -- a clipped title is a worse
    failure than a slightly wider box.
    """
    inner = max(width - 4, len(title), len(subtitle))
    box_width = inner + 4
    top = "╔" + "═" * (box_width - 2) + "╗"
    bottom = "╚" + "═" * (box_width - 2) + "╝"
    lines = [top, _boxed_line(title, box_width)]
    if subtitle:
        lines.append(_boxed_line(subtitle, box_width))
    lines.append(bottom)
    return "\n".join(f"{CYAN}{line}{RESET}" for line in lines)


def section(title: str, width: int = 72) -> str:
    """A lightweight single-line divider for a subsection inside a report body
    (as opposed to `header()`, which is a report's own top-level banner)."""
    label = f"── {title} "
    dashes = "─" * max(4, width - len(label))
    return f"{BOLD}{label}{dashes}{RESET}"


def table(headers: list[str], rows: list[list[str]], *, align: list[str] | None = None) -> str:
    """A box-drawn table. Column widths are computed from content, so nothing
    misaligns when a value is wider than expected.

    `align` is a per-column list of 'l'/'r' (default: left for the first
    column, right for the rest -- a label column followed by numeric columns,
    which is every table this project renders).

    Cells must be plain text -- no ANSI color codes. Column widths are
    computed from `len(cell)`, which counts an escape sequence's invisible
    bytes as visible width and throws off every other cell's padding.
    """
    headers = [str(h) for h in headers]
    rows = [[str(c) for c in r] for r in rows]
    if align is None:
        align = ["l"] + ["r"] * (len(headers) - 1)

    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(cell))

    def fmt_row(cells):
        parts = [f"{cell:<{w}}" if a == "l" else f"{cell:>{w}}"
                 for cell, w, a in zip(cells, widths, align)]
        return "│ " + " │ ".join(parts) + " │"

    def border(left, mid, right):
        return left + mid.join("─" * (w + 2) for w in widths) + right

    lines = [border("┌", "┬", "┐"), fmt_row(headers), border("├", "┼", "┤")]
    lines += [fmt_row(r) for r in rows]
    lines.append(border("└", "┴", "┘"))
    return "\n".join(lines)
