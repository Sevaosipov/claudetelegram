"""The prompts Claude runs: every command in them is `.venv/bin/python analyst.py <subcommand>`.

The headless Claude's shell is scoped to that one prefix (analyst.ALLOWED_TOOLS), so a hostile
headline or question can't make it run anything else, and the commands never carry user text:
the queue stores Asset.key -- "$NVDA" for a US stock -- which a double-quoted shell command would
expand to nothing (or worse, run what the text says). So a queue row is read by its numeric id
(`question <ID>`, `context --queue <ID>`, `send <ID>`), and the tickers and search phrases Claude
finds in a question itself go in single quotes ('TICKER', 'QUERY'; the ticker must pass
analyst.valid_ticker). The commands find .env themselves: there is no `cd`, `source`, `&&`,
pipe or `python3 -c` in any of the three files.
"""
from __future__ import annotations

import pathlib
import re
import shlex

import analyst

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROMPT = ROOT / "claude_analysis_prompt.txt"
PROMPTS = [PROMPT, ROOT / "claude_ask_prompt.txt", ROOT / "analyst_method.txt"]

PREFIX = ".venv/bin/python analyst.py"
# The whole command language of the prompts. The only placeholders: <ID>, 'TICKER', 'QUERY'.
COMMAND_FORM = re.compile(
    re.escape(PREFIX) + r" (?:pending|method|portfolio|question <ID>|send <ID>|context --queue <ID>"
    r"|context 'TICKER'|news 'QUERY')")
# A command runs to its closing backtick or the end of its line.
COMMAND = re.compile(re.escape(PREFIX) + r"[^`\n]*")
# Never anywhere in a prompt: what the old prompts ran, and what could chain or expand.
FORBIDDEN = ("python3", "source .venv", "source .env", "set -a", "&&", "||", "$(", "cd ~", "cd /",
             "|", "`;`")


def _commands(path: pathlib.Path) -> list[str]:
    return [m.group(0).rstrip() for m in COMMAND.finditer(path.read_text(encoding="utf-8"))]


def test_every_prompt_names_analyst_commands():
    for path in PROMPTS:
        assert _commands(path), path.name


def test_every_command_is_one_of_the_allowed_forms():
    for path in PROMPTS:
        for command in _commands(path):
            assert COMMAND_FORM.fullmatch(command), f"{path.name}: {command}"


def test_the_form_rejects_user_text_and_anything_that_chains():
    for bad in (f'{PREFIX} context "$NVDA"', f"{PREFIX} context NVDA", f"{PREFIX} context $NVDA",
                f"{PREFIX} context 'NVDA'; ls", f"{PREFIX} question 12", f"{PREFIX} send",
                f"{PREFIX} context 'TICKER' && ls", f"{PREFIX} news QUERY", f"{PREFIX} ask 'x'",
                f"{PREFIX} context --queue 5", f"python analyst.py context 'TICKER'",
                f"{PREFIX} context '<TICKER>'", f"{PREFIX} question <ID> | cat"):
        assert not COMMAND_FORM.fullmatch(bad), bad
    for good in (f"{PREFIX} pending", f"{PREFIX} question <ID>", f"{PREFIX} context 'TICKER'",
                 f"{PREFIX} context --queue <ID>", f"{PREFIX} send <ID>", f"{PREFIX} news 'QUERY'"):
        assert COMMAND_FORM.fullmatch(good), good


def test_no_prompt_line_starts_a_command_any_other_way():
    for path in PROMPTS:
        for line in path.read_text(encoding="utf-8").splitlines():
            text = line.strip().lstrip("`").strip()
            assert not re.match(r"(python|cd|cat|source|export|set|bash|sh)\s", text), (
                f"{path.name}: {line}")
            assert "analyst.py" not in re.sub(re.escape(PREFIX), "", line), f"{path.name}: {line}"


def test_no_prompt_uses_a_shell_construct():
    for path in PROMPTS:
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN:
            assert token not in text, f"{path.name} contains {token!r}"
        for command in _commands(path):
            assert not re.search(r"[;&|$\\]", command), command


def test_every_command_is_a_real_analyst_subcommand_the_allowed_shell_prefix_covers():
    assert analyst.BASH_TOOL == f"Bash({PREFIX}:*)"
    assert analyst.BASH_TOOL in analyst.ALLOWED_TOOLS
    parser = analyst._parser()
    for path in PROMPTS:
        for command in _commands(path):
            assert command.startswith(PREFIX + " ")
            argv = shlex.split(command.replace("<ID>", "1"))[2:]
            assert parser.parse_args(argv).command == argv[0], command


def test_the_queue_rows_are_read_and_answered_by_id_through_the_analyst():
    commands = _commands(PROMPT)
    for wanted in ("pending", "method", "question <ID>", "context --queue <ID>", "send <ID>"):
        assert f"{PREFIX} {wanted}" in commands, wanted
    text = PROMPT.read_text(encoding="utf-8")
    # the old inline lookups, which pasted the queue's key into python code, are gone
    assert "research.format_brief" not in text and "mark_analysis_processed" not in text
    assert "telegram_notify" not in text


def test_the_method_carries_the_ticker_and_query_placeholders():
    commands = _commands(ROOT / "analyst_method.txt")
    assert f"{PREFIX} context 'TICKER'" in commands and f"{PREFIX} news 'QUERY'" in commands
    assert f"{PREFIX} portfolio" in commands
