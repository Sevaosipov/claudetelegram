#!/bin/bash
# Wrapper of the Friday 17:00 launchd job (com.disclosurebot.friday.plist): sends the week's buy signals --
# picked by the morning's full run -- with the price of the hour and Claude's read of each chart, then the
# week's summary. It polls no source and scores nothing (bot.py --weekly-send); when the morning's run did
# not get through, it sends nothing and the next full run (Saturday, Sunday) picks and sends.
#
# Like run_daily.sh it reports its own failure to Telegram: launchd shows an exit code nowhere.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

source .venv/bin/activate

notify_failure() {
  local code=$?
  [ "$code" -eq 0 ] && return 0
  python - "$code" <<'PY' || true
import sys
import telegram_notify
telegram_notify.send_text(
    f"⚠️ disclosure-bot: run_weekly.sh завершился с кодом {sys.argv[1]}. "
    f"Логи: data/launchd.err.log, data/launchd.out.log"
)
PY
}
trap notify_failure EXIT

python bot.py --weekly-send
