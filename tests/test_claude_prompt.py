"""claude_analysis_prompt.txt: the commands Claude runs must never carry user text.

The queue stores Asset.key -- "$NVDA" for a US stock. Pasted into a double-quoted
`python3 -c "…"`, the shell expands "$NVDA" to nothing and the lookup breaks (or
worse, runs whatever the text says). So the commands take only the numeric queue id.
"""
from __future__ import annotations

import pathlib
import re

PROMPT = pathlib.Path(__file__).resolve().parent.parent / "claude_analysis_prompt.txt"


def _python_blocks(text: str) -> list[str]:
    return re.findall(r'python3 -c "(.*?)"', text, re.S)


def test_no_ticker_placeholder_inside_a_shell_command():
    blocks = _python_blocks(PROMPT.read_text(encoding="utf-8"))
    assert blocks, "expected the prompt's python3 -c commands"
    for block in blocks:
        assert "<TICKER>" not in block
        # The only placeholder a command may carry is the numeric queue id.
        assert set(re.findall(r"<[A-Z_]+>", block)) <= {"<ID>"}


def test_the_brief_is_built_from_the_queue_id():
    [brief] = [b for b in _python_blocks(PROMPT.read_text(encoding="utf-8"))
               if "research.format_brief" in b]
    assert "pending_analysis(conn))[<ID>]" in brief
