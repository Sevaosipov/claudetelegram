"""termstyle.py -- box-drawing and TTY-gated color, all pure functions."""
from __future__ import annotations

import termstyle as ts


def test_header_contains_title_and_subtitle_verbatim():
    text = ts.header("TITLE HERE", "a subtitle")
    assert "TITLE HERE" in text
    assert "a subtitle" in text


def test_header_widens_rather_than_truncating_a_long_title():
    long_title = "X" * 100
    text = ts.header(long_title, width=72)
    assert long_title in text  # not clipped
    # every box line is the same width
    lines = text.split("\n")
    widths = {len(_strip_ansi(l)) for l in lines}
    assert len(widths) == 1


def test_header_box_lines_are_all_equal_width_at_default_size():
    text = ts.header("short")
    lines = [_strip_ansi(l) for l in text.split("\n")]
    assert len(set(len(l) for l in lines)) == 1


def test_header_without_subtitle_has_no_blank_extra_line():
    text = ts.header("only a title")
    assert len(text.split("\n")) == 3  # top border, title, bottom border


def test_table_headers_and_cells_appear():
    out = ts.table(["name", "n"], [["alpha", "3"], ["beta", "12"]])
    assert "name" in out and "alpha" in out and "beta" in out


def test_table_columns_align_by_content_width():
    out = ts.table(["label", "value"], [["short", "1"], ["a longer label", "22"]])
    lines = [l for l in out.split("\n") if l.startswith("│")]
    # every data/header row is the same total width
    assert len(set(len(l) for l in lines)) == 1


def test_table_default_alignment_is_left_first_right_rest():
    out = ts.table(["label", "n"], [["a", "1"]])
    rows = out.split("\n")
    data_row = [l for l in rows if "a" in l and "label" not in l][0]
    # left-aligned label starts right after the border+space
    assert data_row.startswith("│ a")
    # right-aligned number ends right before the trailing " │"
    assert data_row.rstrip().endswith("1 │")


def test_table_handles_no_rows():
    out = ts.table(["only", "headers"], [])
    assert "only" in out and "headers" in out


def test_colors_are_empty_strings_when_not_a_tty():
    # The test runner's stdout is not a tty, so termstyle's module-level
    # constants must already be the empty string -- this locks that behavior
    # rather than re-deriving it, since _TTY is computed once at import time.
    assert ts.BOLD == "" and ts.DIM == "" and ts.CYAN == "" and ts.GREEN == "" and ts.RESET == ""


def _strip_ansi(s: str) -> str:
    import re
    return re.sub(r"\033\[[0-9;]*m", "", s)
