#!/bin/bash
# Wrapper invoked by the launchd job (com.disclosurebot.daily.plist). Loads secrets
# from .env, activates the venv, and runs one pass of the bot.
#
# It also reports its own failures. launchd puts a non-zero exit status nowhere the
# user will ever look: this job exited 126 every morning for ten days -- a macOS
# permissions error, not a bug -- while the bot appeared to simply have nothing to
# say. The EXIT trap below turns any failure into a Telegram message instead. (Note
# there is no `exec`: the shell has to outlive the python process for the trap to
# fire.)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

source .venv/bin/activate

# Called with --if-needed by the launchd job. RunAtLoad is on so a run missed while
# the machine was off or asleep happens on wake instead of being skipped until the
# next day; this guard stops that from also producing a second run on a day already
# covered. --healthcheck exits 0 exactly when a successful run is recent enough.
if [ "${1:-}" = "--if-needed" ]; then
  if python bot.py --healthcheck --stale-hours 20 --no-telegram >/dev/null 2>&1; then
    echo "--- skipped $(date '+%Y-%m-%d %H:%M:%S'): a run already completed in the last 20h ---"
    exit 0
  fi
fi

notify_failure() {
  local code=$?
  [ "$code" -eq 0 ] && return 0
  python - "$code" <<'PY' || true
import sys
import telegram_notify
telegram_notify.send_text(
    f"⚠️ disclosure-bot: run_daily.sh завершился с кодом {sys.argv[1]}. "
    f"Логи: data/launchd.err.log, data/launchd.out.log"
)
PY
}
trap notify_failure EXIT

python bot.py --once
