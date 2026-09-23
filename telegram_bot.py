"""Long-polls Telegram for inbound messages and replies with ONE merged
verdict per ticker: opinion.py's deterministic score, the real entry/target
prices, and a genuine qualitative news read, all in a single message.

Used to reply immediately with just the deterministic part and let a
scheduled pass send a qualitative follow-up later -- the user asked for one
merged message instead, explicitly accepting the tradeoff (~30-90s wait
instead of an instant reply). So a ticker lookup now enqueues
(db.enqueue_analysis) and synchronously runs run_claude_analysis.sh right
away rather than waiting for its next scheduled fire -- that script is the
one thing that actually reads news and reasons (see its own header for why:
no live-Claude hook exists inside this process). If that run fails or times
out, falls back to sending the fast opinion.py-only reply so the user isn't
left with total silence, and the queued row stays pending for the next
scheduled or triggered run to retry.

/backtest TICKER answers a different question: how did this ticker trade
after its OWN past SEC insider purchases (backtest.backtest_ticker(), a
10-year window). Not a backtest of opinion.py's score itself -- that didn't
exist historically, so there's nothing to check it against yet; see
db.journal_opinion() / backtest.py's --opinions mode, which starts
accumulating a real record from whenever a ticker first gets checked.

Only ever responds to TELEGRAM_CHAT_ID -- the same chat the rest of
disclosure-bot already alerts into. Any message from a different chat is
logged and dropped without a reply, so a stranger who finds the bot's
username by searching can't query it or run up API usage on your token.

Uses long polling (getUpdates), not a webhook: no public endpoint or hosting
decision needed, consistent with the rest of this project being all-outbound.
The last processed update_id is persisted in kv_cache (survives a restart).

Unlike every other entry point here, this one has to stay running to keep
polling -- it's meant to run under its own always-on LaunchAgent
(com.disclosurebot.telegram.plist), not the daily scrape job.

Usage:
    python telegram_bot.py            # run forever
    python telegram_bot.py --once     # process one batch of pending updates, exit (testing)
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests

import backtest
import db
import positions
import research
import telegram_notify

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "disclosures.db"
RUN_ANALYSIS_SCRIPT = BASE_DIR / "run_claude_analysis.sh"
RUN_ANALYSIS_TIMEOUT = 300  # generous cap; a normal run takes well under this

API_URL = "https://api.telegram.org/bot{token}/{method}"
LONG_POLL_SECONDS = 25
STATE_OFFSET = "telegram_bot_offset"
PERSIST_SECONDS = 30 * 365 * 24 * 3600

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{10}$")
POSITIONS_USAGE = ("/bought TICKER [цена] — отметить покупку (без цены — последнее закрытие)\n"
                   "/sold TICKER — отметить продажу\n"
                   "/positions — открытые позиции")

HELP_TEXT = ("Пришлите тикер (например, AAPL) — через ~30-90 сек придёт один "
             "разбор: опинион, вход/цель, новости, итоговый вердикт.\n"
             "/backtest TICKER — как этот тикер торговался после своих же "
             "прошлых инсайдерских покупок (почти всегда n слишком мал, чтобы "
             "что-то значить на уровне одного тикера).\n"
             + POSITIONS_USAGE)


def _get_updates(token: str, offset: int | None, session: requests.Session) -> list:
    params = {"timeout": LONG_POLL_SECONDS}
    if offset is not None:
        params["offset"] = offset
    try:
        resp = session.get(API_URL.format(token=token, method="getUpdates"),
                            params=params, timeout=LONG_POLL_SECONDS + 10)
    except requests.ReadTimeout:
        # A long poll that outlived its own timeout -- typically the Mac waking from
        # sleep with the connection half-dead. Nothing was lost (the offset hasn't
        # moved), so it's an empty poll, not a failure: treating it as one logged it
        # and backed off for up to 5 minutes, during which messages went unanswered.
        return []
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"getUpdates not ok: {data}")
    return data["result"]


def _extract_ticker(text: str) -> str | None:
    candidate = text.strip().split()[0] if text.strip() else ""
    candidate = candidate.lstrip("$").upper()
    return candidate if _TICKER_RE.match(candidate) else None


def _position_ticker(arg: str) -> str | None:
    t = arg.strip().lstrip("$").upper()
    return t if (_TICKER_RE.match(t) or _ISIN_RE.match(t)) else None


def _handle_positions_command(conn, text: str) -> bool:
    """/bought, /sold, /positions -- the positions positions.py tracks for close
    alerts. Returns False for anything else."""
    parts = text.split()
    cmd = parts[0].lower().split("@")[0] if parts else ""
    if cmd == "/positions":
        telegram_notify.send_text(telegram_notify.format_positions(
            positions.open_positions(conn), positions.last_close))
        return True
    if cmd not in ("/bought", "/sold"):
        return False
    ticker = _position_ticker(parts[1]) if len(parts) > 1 else None
    if not ticker:
        telegram_notify.send_text(POSITIONS_USAGE)
        return True
    if cmd == "/sold":
        pos = positions.close_position(conn, ticker)
        telegram_notify.send_text(f"Позиция {ticker} закрыта." if pos
                                  else f"По {ticker} нет открытой позиции.")
        return True
    price = None
    if len(parts) > 2:
        try:
            price = float(parts[2].replace(",", "."))
        except ValueError:
            telegram_notify.send_text(POSITIONS_USAGE)
            return True
        if price <= 0:
            telegram_notify.send_text(POSITIONS_USAGE)
            return True
    price = price or positions.last_close(ticker)
    if not price:
        telegram_notify.send_text(f"Не нашёл цену {ticker} — укажите её: /bought {ticker} 12.34")
        return True
    try:
        pos = positions.open_position(conn, ticker, price)
    except ValueError:
        telegram_notify.send_text(f"Позиция {ticker} уже открыта. /sold {ticker}, чтобы закрыть.")
        return True
    who = (f"слежу за продажами: {', '.join(pos.insiders)}" if pos.insiders
           else "сильного сигнала по нему не было — слежу только за сроком и стоп-лоссом")
    telegram_notify.send_text(f"Записал {ticker} по {price:,.2f}; {who}.")
    return True


def _handle_backtest(conn, text: str) -> None:
    arg = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""
    ticker = _extract_ticker(arg)
    if not ticker:
        telegram_notify.send_text("Использование: /backtest TICKER (например /backtest AAPL)")
        return
    try:
        result = backtest.backtest_ticker(conn, ticker)
        sent = telegram_notify.send_text(telegram_notify.format_ticker_backtest(result))
        print(f"[telegram_bot] backtest for {ticker} (sent={sent}, n={result['n_purchases']})")
    except Exception as e:
        print(f"[telegram_bot] backtest failed for {ticker}: {type(e).__name__}: {e}",
              file=sys.stderr)
        telegram_notify.send_text(f"Не удалось посчитать бэктест по {ticker} "
                                   f"({type(e).__name__}). Попробуйте позже.")


def _handle_message(conn, text: str) -> None:
    text = (text or "").strip()
    if _handle_positions_command(conn, text):
        return
    if text.lower().startswith("/backtest"):
        _handle_backtest(conn, text)
        return
    if not text or text.startswith("/"):
        telegram_notify.send_text(HELP_TEXT)
        return
    ticker = _extract_ticker(text)
    if not ticker:
        telegram_notify.send_text(
            f"Не похоже на тикер: {text[:40]!r}. " + HELP_TEXT)
        return
    db.enqueue_analysis(conn, ticker)
    print(f"[telegram_bot] queued {ticker}, running claude-analysis synchronously")
    try:
        result = subprocess.run([str(RUN_ANALYSIS_SCRIPT)], cwd=str(BASE_DIR),
                                 timeout=RUN_ANALYSIS_TIMEOUT,
                                 capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"exit {result.returncode}: {result.stderr[-2000:]}")
        print(f"[telegram_bot] claude-analysis run finished for {ticker}")
    except Exception as e:
        print(f"[telegram_bot] synchronous claude-analysis failed for {ticker}: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        # Fall back to the fast deterministic-only reply so the user isn't left
        # with total silence -- the queued row stays pending either way (only
        # marked processed on a confirmed send inside run_claude_analysis.sh),
        # so the next scheduled or triggered run will still pick it up.
        try:
            rep = research.build(conn, ticker)
            telegram_notify.send_text(
                telegram_notify.format_condensed(rep) +
                "\n\n(Полный разбор не завершился в этот раз — попробуется снова.)")
        except Exception as e2:
            print(f"[telegram_bot] fallback reply also failed for {ticker}: "
                  f"{type(e2).__name__}: {e2}", file=sys.stderr)
            telegram_notify.send_text(f"Не удалось получить данные по {ticker}. Попробуйте позже.")


def _poll_once(conn, token: str, chat_id: str, session: requests.Session) -> None:
    offset = db.get_cached_value(conn, STATE_OFFSET, PERSIST_SECONDS)
    offset = int(offset) if offset is not None else None
    updates = _get_updates(token, offset, session)
    for u in updates:
        db.save_cached_value(conn, STATE_OFFSET, float(u["update_id"] + 1))
        msg = u.get("message") or u.get("edited_message")
        if not msg:
            continue
        from_chat = str(msg.get("chat", {}).get("id", ""))
        if from_chat != str(chat_id):
            print(f"[telegram_bot] ignoring message from unauthorized chat {from_chat}")
            continue
        _handle_message(conn, msg.get("text", ""))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="process one batch of pending updates, then exit")
    args = ap.parse_args()

    # Long-running under launchd, stdout/stderr go to a plain file rather than a
    # TTY -- Python block-buffers those by default, so nothing would show up in
    # the log until the internal buffer filled or the process exited. bot.py hit
    # the same thing (see its _Tee class); line-buffering here is the simpler
    # fix for a process that has no per-run "finished" point to flush at.
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[telegram_bot] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set, exiting", file=sys.stderr)
        return 1

    conn = db.connect(DB_PATH)
    session = requests.Session()

    if args.once:
        _poll_once(conn, token, chat_id, session)
        return 0

    print("[telegram_bot] listening (Ctrl+C to stop)")
    backoff = 5
    while True:
        try:
            _poll_once(conn, token, chat_id, session)
            backoff = 5
        except requests.RequestException as e:
            print(f"[telegram_bot] poll failed: {e}; retrying in {backoff}s", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)


if __name__ == "__main__":
    sys.exit(main())
