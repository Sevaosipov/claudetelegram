#!/bin/bash
# Wrapper invoked by the launchd job (com.disclosurebot.claude-analysis.plist).
# Runs a headless Claude Code pass over claude_analysis_queue (see db.py):
# for each ticker telegram_bot.py has queued, reads its news + opinion.py's
# already-computed score, adds a real qualitative read, and sends ONE merged
# message to Telegram. This is the layer telegram_bot.py itself can't do on
# its own -- it's an unattended long-polling process with no live Claude
# session to call (see telegram_bot.py's module docstring for why the
# alternative, a scheduled *cloud* agent, doesn't work either: it has no
# access to the local DB or Telegram credentials).
#
# The prompt lives in claude_analysis_prompt.txt, not inline here: this
# machine's /bin/bash is Apple's ancient 3.2 build, which has a real quirk
# where a single quote inside a <<'quoted' heredoc body (even one that's
# supposed to be fully literal) can break the parser -- reproduced directly
# before settling on a plain prompt file instead, which sidesteps the whole
# class of shell-quoting issues.
#
# --allowedTools "Bash" --permission-mode acceptEdits: scoped to shell
# commands in this repo (python), no interactive prompts -- verified to run
# with zero prompts in this exact combination. No file edits are actually
# needed for this task; acceptEdits is just the permission mode that lets
# Bash calls through without stopping to ask.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

claude -p "$(cat claude_analysis_prompt.txt)" --allowedTools "Bash" --permission-mode acceptEdits
