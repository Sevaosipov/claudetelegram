"""The prompts Claude runs: every command in them is one of the three read commands
`.venv/bin/python analyst.py context 'TICKER' | portfolio | news 'QUERY'`.

The headless Claude's shell is allowed exactly those (analyst.BASH_RULES), so a hostile headline
or question can't make it run anything else, and no command carries user text: the question and
the bot's data are in the prompt itself (analyst.build_prompt), and the tickers and search
phrases Claude picks out of a question go in single quotes ('TICKER', 'QUERY'; the ticker must
pass analyst.valid_ticker). Nothing reads the queue, sends or writes a file any more: there is
no `cd`, `source`, `&&`, pipe or `python3 -c` in any of the three files.
"""
from __future__ import annotations

import pathlib
import re
import shlex

import pytest

import analyst

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROMPTS = [ROOT / "claude_analysis_prompt.txt", ROOT / "claude_ask_prompt.txt",
           ROOT / "analyst_method.txt"]

PREFIX = ".venv/bin/python analyst.py"
# The whole command language of the prompts. The only placeholders: 'TICKER' and 'QUERY'.
COMMAND_FORM = re.compile(re.escape(PREFIX) + r" (?:portfolio|context 'TICKER'|news 'QUERY')")
# A command runs to its closing backtick or the end of its line.
COMMAND = re.compile(re.escape(PREFIX) + r"[^`\n]*")
# Never anywhere in a prompt: what the old prompts ran, and what could chain or expand.
FORBIDDEN = ("python3", "source .venv", "source .env", "set -a", "&&", "||", "$(", "cd ~", "cd /",
             "|", "`;`")


def _commands(path: pathlib.Path) -> list[str]:
    return [m.group(0).rstrip() for m in COMMAND.finditer(path.read_text(encoding="utf-8"))]


def _allowed(command: str) -> bool:
    """Whether one of analyst.BASH_RULES lets `command` run: Bash(X:*) is the prefix X,
    Bash(X) exactly X."""
    for rule in analyst.BASH_RULES:
        inner = rule[len("Bash("):-1]
        if inner.endswith(":*"):
            if command == inner[:-2] or command.startswith(inner[:-2] + " "):
                return True
        elif command == inner:
            return True
    return False


def test_the_method_names_the_three_commands():
    commands = _commands(ROOT / "analyst_method.txt")
    for wanted in ("context 'TICKER'", "portfolio", "news 'QUERY'"):
        assert f"{PREFIX} {wanted}" in commands, wanted


def test_every_command_is_one_of_the_allowed_forms():
    for path in PROMPTS:
        for command in _commands(path):
            assert COMMAND_FORM.fullmatch(command), f"{path.name}: {command}"


def test_the_form_rejects_user_text_the_old_commands_and_anything_that_chains():
    for bad in (f'{PREFIX} context "$NVDA"', f"{PREFIX} context NVDA", f"{PREFIX} context $NVDA",
                f"{PREFIX} context 'NVDA'; ls", f"{PREFIX} question <ID>", f"{PREFIX} pending",
                f"{PREFIX} method", f"{PREFIX} send <ID> '<the whole message>'",
                f"{PREFIX} context 'TICKER' && ls", f"{PREFIX} news QUERY", f"{PREFIX} ask 'x'",
                f"{PREFIX} process-queue", "python analyst.py context 'TICKER'",
                f"{PREFIX} context '<TICKER>'", f"{PREFIX} portfolio | cat"):
        assert not COMMAND_FORM.fullmatch(bad), bad
    for good in (f"{PREFIX} portfolio", f"{PREFIX} context 'TICKER'", f"{PREFIX} news 'QUERY'"):
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


def test_every_command_is_a_real_subcommand_that_an_allow_rule_covers():
    parser = analyst._parser()
    for path in PROMPTS:
        for command in _commands(path):
            assert _allowed(command.replace("'TICKER'", "'NVDA'").replace("'QUERY'", "'nvidia'")), command
            argv = shlex.split(command)[2:]
            assert parser.parse_args(argv).command == argv[0], command


def test_the_allow_rules_cover_nothing_else():
    for other in (f"{PREFIX} ask 'x'", f"{PREFIX} process-queue", f"{PREFIX} portfolio extra",
                  f"{PREFIX} contextual", ".venv/bin/python -c 'print(1)'", "cat .env",
                  f"{PREFIX}", "python analyst.py portfolio"):
        assert not _allowed(other), other


@pytest.mark.parametrize("path", PROMPTS, ids=lambda p: p.name)
def test_no_prompt_reads_the_queue_or_sends(path):
    text = path.read_text(encoding="utf-8")
    assert "<ID>" not in text and "--queue" not in text
    for gone in ("pending", "method", "question", "send"):
        assert f"{PREFIX} {gone}" not in text, gone
    assert "research.format_brief" not in text and "mark_analysis_processed" not in text
    assert "telegram_notify" not in text
