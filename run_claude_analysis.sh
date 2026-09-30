#!/bin/bash
# Wrapper invoked by the launchd job (com.disclosurebot.claude-analysis.plist) and by
# telegram_bot.py's synchronous run.
# Runs `python analyst.py process-queue`: if claude_analysis_queue (see db.py) is empty it
# returns at once; otherwise, for each queued row (the tickers and free-form questions
# telegram_bot.py has queued, at most 10 a pass), analyst.py builds the prompt -- the template in
# claude_analysis_prompt.txt, the method in analyst_method.txt and the row's data: the question,
# or the bot's own context on the ticker -- and runs ONE headless Claude Code on it. Claude reads
# the chart in the user's TradingView Desktop through the TradingView MCP server, reasons about
# the news, and its final message is the answer; analyst.py sends it to Telegram and only then
# marks the row processed (three failed runs on a row -> a «не удалось ответить» message). This is
# the layer telegram_bot.py itself can't do on its own -- it's an unattended long-polling process
# with no live Claude session to call (see telegram_bot.py's module docstring for why the
# alternative, a scheduled *cloud* agent, doesn't work either: it has no access to the local DB,
# Telegram credentials or TradingView).
#
# The prompts live in plain files, not inline: this machine's /bin/bash is Apple's ancient 3.2
# build, which has a real quirk where a single quote inside a <<'quoted' heredoc body (even one
# that's supposed to be fully literal) can break the parser -- reproduced directly before settling
# on plain prompt files instead, which sidesteps the whole class of shell-quoting issues.
#
# analyst.py builds the claude command (analyst.claude_command: the absolute ~/.local/bin/claude,
# not relying on launchd's PATH; --restricted, so the Claude settings files are ignored;
# --tools Bash, so no file tools; only the TradingView MCP server; --permission-mode dontAsk; the
# shell allowed only `<project>/.venv/bin/python <project>/analyst.py context|portfolio|news`, by
# absolute path, plus nine read-and-navigate TradingView tools), runs it in a fresh empty temporary
# folder outside the project, and hands it an environment without any key (analyst.claude_env). This script's own
# process keeps the keys: analyst.py loads .env itself to send the answers. It also holds a lock on
# data/analyst.lock so the launchd job, the bot's own run and the terminal never answer the same
# rows twice or drive the chart at once, stops a Claude run that takes longer than 15 minutes, and
# takes its claude down with it on SIGTERM. launchd runs with a minimal PATH that lacks
# /usr/local/bin and /opt/homebrew/bin, where `node` -- which starts the TradingView MCP server --
# lives (same class of gotcha as python/venv elsewhere in this project), so both are put in front
# of PATH here and again in the environment analyst.py hands to claude.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"
exec ./.venv/bin/python analyst.py process-queue
