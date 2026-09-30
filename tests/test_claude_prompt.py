"""The prompts Claude runs: every command in them is one of the three read commands
`{ANALYST_CMD} context 'TICKER' | portfolio | news 'QUERY'`, where build_prompt puts the absolute
analyst.ANALYST_CMD (`<BASE_DIR>/.venv/bin/python <BASE_DIR>/analyst.py`) for the placeholder.

Claude runs in an empty folder outside the project, and its shell is allowed exactly those three
commands, by their absolute paths (analyst.BASH_RULES), so a hostile headline or question can't
make it run anything else, and no command carries user text: the question and the bot's data
are in the prompt itself (analyst.build_prompt), and the tickers and search phrases Claude picks
out of a question go in single quotes ('TICKER', 'QUERY'; the ticker must pass
analyst.valid_ticker). Nothing reads the queue, sends or writes a file: there is no `cd`,
`source`, `&&`, pipe or `python3 -c` in any of the three files, and no relative path.
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

PLACEHOLDER = "{ANALYST_CMD}"
# The whole command language of the prompts. The only placeholders: 'TICKER' and 'QUERY'.
FORMS = r" (?:portfolio|context 'TICKER'|news 'QUERY')"
COMMAND_FORM = re.compile(re.escape(PLACEHOLDER) + FORMS)
# A command runs to its closing backtick or the end of its line.
COMMAND = re.compile(re.escape(PLACEHOLDER) + r"[^`\n]*")
# Never anywhere in a prompt: what the old prompts ran, and what could chain or expand.
FORBIDDEN = ("python3", "source .venv", "source .env", "set -a", "&&", "||", "$(", "cd ~", "cd /",
             "|", "`;`", ".venv/bin/python", "analyst.py ")


def _commands(text: str, prefix: str = PLACEHOLDER) -> list[str]:
    return [m.group(0).rstrip() for m in re.finditer(re.escape(prefix) + r"[^`\n]*", text)]


def _file_commands(path: pathlib.Path) -> list[str]:
    return _commands(path.read_text(encoding="utf-8"))


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


def _built() -> list[str]:
    return [analyst.build_prompt("telegram", question="?"),
            analyst.build_prompt("telegram", ticker="$NVDA", ticker_context="МОДЕЛЬ: балл 64"),
            analyst.build_prompt("terminal", question="?")]


def test_the_method_and_both_templates_name_the_commands_by_the_placeholder():
    commands = _file_commands(ROOT / "analyst_method.txt")
    for wanted in ("context 'TICKER'", "portfolio", "news 'QUERY'"):
        assert f"{PLACEHOLDER} {wanted}" in commands, wanted
    for name in ("claude_analysis_prompt.txt", "claude_ask_prompt.txt"):
        assert _file_commands(ROOT / name), name


def test_every_command_is_one_of_the_allowed_forms():
    for path in PROMPTS:
        for command in _file_commands(path):
            assert COMMAND_FORM.fullmatch(command), f"{path.name}: {command}"


def test_the_form_rejects_user_text_the_old_commands_and_anything_that_chains():
    p = PLACEHOLDER
    for bad in (f'{p} context "$NVDA"', f"{p} context NVDA", f"{p} context $NVDA",
                f"{p} context 'NVDA'; ls", f"{p} question <ID>", f"{p} pending", f"{p} method",
                f"{p} send <ID> '<the whole message>'", f"{p} context 'TICKER' && ls",
                f"{p} news QUERY", f"{p} ask 'x'", f"{p} process-queue",
                ".venv/bin/python analyst.py context 'TICKER'", f"{p} context '<TICKER>'",
                f"{p} portfolio | cat"):
        assert not COMMAND_FORM.fullmatch(bad), bad
    for good in (f"{p} portfolio", f"{p} context 'TICKER'", f"{p} news 'QUERY'"):
        assert COMMAND_FORM.fullmatch(good), good


def test_no_prompt_file_runs_anything_any_other_way():
    for path in PROMPTS:
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN:
            assert token not in text, f"{path.name} contains {token!r}"
        for line in text.splitlines():
            stripped = line.strip().lstrip("`").strip()
            assert not re.match(r"(python|cd|cat|source|export|set|bash|sh)\s", stripped), (
                f"{path.name}: {line}")
        for command in _file_commands(path):
            assert not re.search(r"[;&|$\\]", command), command


def test_the_built_prompts_carry_absolute_commands_only():
    absolute = re.compile(re.escape(analyst.ANALYST_CMD) + FORMS)
    for prompt in _built():
        assert PLACEHOLDER not in prompt
        commands = _commands(prompt, analyst.ANALYST_CMD)
        assert len(commands) >= 3
        for command in commands:
            assert absolute.fullmatch(command), command
        assert prompt.count(".venv/bin/python") == prompt.count(analyst.ANALYST_CMD)   # none relative
        assert prompt.count("analyst.py ") == prompt.count(analyst.ANALYST_CMD + " ")


def test_every_built_command_is_a_real_subcommand_an_allow_rule_covers():
    parser = analyst._parser()
    for prompt in _built():
        for command in _commands(prompt, analyst.ANALYST_CMD):
            filled = command.replace("'TICKER'", "'NVDA'").replace("'QUERY'", "'nvidia'")
            assert _allowed(filled), command
            argv = shlex.split(filled)[2:]
            assert parser.parse_args(argv).command == argv[0], command


def test_the_allow_rules_cover_nothing_else():
    cmd = analyst.ANALYST_CMD
    for other in (f"{cmd} ask 'x'", f"{cmd} process-queue", f"{cmd} portfolio extra",
                  f"{cmd} contextual", f"{cmd.split()[0]} -c 'print(1)'", "cat .env",
                  f"cat {analyst.BASE_DIR}/.env", cmd, ".venv/bin/python analyst.py portfolio",
                  "python analyst.py portfolio"):
        assert not _allowed(other), other


@pytest.mark.parametrize("path", PROMPTS, ids=lambda p: p.name)
def test_no_prompt_reads_the_queue_or_sends(path):
    text = path.read_text(encoding="utf-8")
    assert "<ID>" not in text and "--queue" not in text
    for gone in ("pending", "method", "question", "send"):
        assert f"{PLACEHOLDER} {gone}" not in text, gone
    assert "research.format_brief" not in text and "mark_analysis_processed" not in text
    assert "telegram_notify" not in text
