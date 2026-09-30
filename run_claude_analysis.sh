#!/bin/bash
# Wrapper invoked by the launchd job (com.disclosurebot.claude-analysis.plist) and by
# telegram_bot.py's synchronous run.
# Runs `python analyst.py process-queue`: if claude_analysis_queue (see db.py) is empty it
# returns at once; otherwise it starts a headless Claude Code pass (analyst.claude_command)
# over the queue. The queue holds the tickers and the free-form questions telegram_bot.py has
# queued. For each row Claude reads the bot's own context (analyst.py context / question), reads
# the chart in the user's TradingView Desktop through the TradingView MCP server, reasons about
# the news, and sends ONE message to Telegram. This is the layer telegram_bot.py itself can't do
# on its own -- it's an unattended long-polling process with no live Claude session to call (see
# telegram_bot.py's module docstring for why the alternative, a scheduled *cloud* agent, doesn't
# work either: it has no access to the local DB, Telegram credentials or TradingView).
#
# The prompt lives in claude_analysis_prompt.txt (and the method it points to in
# analyst_method.txt), not inline: this machine's /bin/bash is Apple's ancient 3.2 build, which
# has a real quirk where a single quote inside a <<'quoted' heredoc body (even one that's
# supposed to be fully literal) can break the parser -- reproduced directly before settling on
# plain prompt files instead, which sidesteps the whole class of shell-quoting issues.
#
# analyst.py builds the claude command (the absolute ~/.local/bin/claude, not relying on launchd's
# PATH; --allowedTools scoped to ONE shell command prefix, `.venv/bin/python analyst.py`, plus a
# short list of mcp__tradingview__ tools -- no Write, no edit mode, so anything else is denied
# without a prompt and nothing runnable can be written). It also holds a lock on
# data/analyst.lock so the launchd job and the bot's own run never answer the same rows twice,
# stops a run that takes longer than 15 minutes, and takes its claude down with it on SIGTERM. launchd runs with a minimal PATH that lacks /usr/local/bin and
# /opt/homebrew/bin, where `node` -- which starts the TradingView MCP server -- lives (same class
# of gotcha as python/venv elsewhere in this project), so both are put in front of PATH here and
# again in the environment analyst.py hands to claude.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"
exec ./.venv/bin/python analyst.py process-queue
