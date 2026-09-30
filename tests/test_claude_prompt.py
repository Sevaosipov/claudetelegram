"""The prompts Claude runs: the commands in them must never carry user text.

The queue stores Asset.key -- "$NVDA" for a US stock. Pasted into a double-quoted
`python3 -c "…"`, the shell expands "$NVDA" to nothing and the lookup breaks (or
worse, runs whatever the text says). So the commands take only the numeric queue id
(`python analyst.py question <ID>`, `python analyst.py context --queue <ID>`), and the
tickers Claude finds in a question itself go in single quotes and must pass
analyst.valid_ticker.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROMPT = ROOT / "claude_analysis_prompt.txt"
PROMPTS = [PROMPT, ROOT / "claude_ask_prompt.txt", ROOT / "analyst_method.txt"]


def _python_blocks(text: str) -> list[str]:
    return re.findall(r'python3 -c "(.*?)"', text, re.S)


def _analyst_commands(text: str) -> list[str]:
    """The arguments of every `python analyst.py …` command, up to a closing backtick or the
    end of its line."""
    return re.findall(r"python analyst\.py ([^`\n]*)", text)


def test_no_ticker_placeholder_inside_a_shell_command():
    blocks = _python_blocks(PROMPT.read_text(encoding="utf-8"))
    assert blocks, "expected the prompt's python3 -c commands"
    for block in blocks:
        assert "<TICKER>" not in block
        # The only placeholder a command may carry is the numeric queue id.
        assert set(re.findall(r"<[A-Z_]+>", block)) <= {"<ID>"}


def test_the_prompts_carry_no_python_block_with_user_text():
    for path in PROMPTS:
        for block in _python_blocks(path.read_text(encoding="utf-8")):
            assert "<TICKER>" not in block and "<QUESTION>" not in block, path.name
            assert set(re.findall(r"<[A-Z_]+>", block)) <= {"<ID>"}, path.name


def test_the_method_carries_analyst_commands():
    assert _analyst_commands((ROOT / "analyst_method.txt").read_text(encoding="utf-8"))


def test_the_queue_rows_are_read_by_id_through_the_analyst():
    text = PROMPT.read_text(encoding="utf-8")
    assert "python analyst.py question <ID>" in text
    assert "python analyst.py context --queue <ID>" in text
    # the old inline lookup, which pasted the queue's key into python code, is gone
    assert "research.format_brief" not in text


# What may follow `python analyst.py <subcommand>`: nothing, a queue id, or one single-quoted
# word (a ticker or a search query Claude composed itself, with nothing the shell would expand).
_ARGUMENT = re.compile(r"(<ID>|--queue <ID>|'[^'$`\\\n]*')?")


def test_a_command_argument_is_an_id_or_single_quoted():
    """Never a bare or double-quoted word: the shell would expand "$NVDA" and run what a
    question says."""
    for path in PROMPTS:
        for args in _analyst_commands(path.read_text(encoding="utf-8")):
            _sub, _, rest = args.strip().partition(" ")
            assert _ARGUMENT.fullmatch(rest.strip()), f"{path.name}: python analyst.py {args}"
