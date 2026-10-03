"""Long-polls Telegram for inbound messages and answers them.

A single ticker or coin (NVDA, BTC, EQNR.OL) gets ONE merged verdict: opinion.py's
deterministic score, the real entry/target prices, and a genuine qualitative news and chart
read, all in a single message.

Used to reply immediately with just the deterministic part and let a
scheduled pass send a qualitative follow-up later -- the user asked for one
merged message instead, explicitly accepting the tradeoff (~30-90s wait
instead of an instant reply). So a ticker lookup now enqueues
(db.enqueue_analysis) and synchronously runs run_claude_analysis.sh right
away rather than waiting for its next scheduled fire -- that script (analyst.py process-queue)
is the one thing that actually reads news and the chart and reasons (see its own header for why:
no live-Claude hook exists inside this process); analyst.py sends the answer itself. If that
row is still unanswered when the run ends (it failed, timed out, or never reached the row),
falls back to sending the fast opinion.py-only reply so the user isn't left with total
silence, and the queued row stays pending for the next scheduled or triggered run to retry.

Any other text -- several words, or one word that is not an asset -- and /ask TEXT is a
question for the analyst: it is queued (db.enqueue_question, at most 2000 characters), the same
run answers it, and the analyst sends the answer itself. If it is still unanswered after the
run, the user is told the question stays queued. /model sends the model portfolio's summary
(paper_report.format_summary) at once, without Claude. A ticker lookup and a question share one
runner, _run_analysis: a process group of its own, RUN_ANALYSIS_TIMEOUT seconds, SIGTERM first
(analyst.py stops its Claude on it) and SIGKILL after a grace period; it succeeds when the row
it was run for is marked processed, whatever the run's exit code (the pass also covers other
rows). Only new messages are handled: an edited message is not a second request.

/backtest TICKER answers a different question: how did this ticker trade
after its OWN past SEC insider purchases (backtest.backtest_ticker(), a
10-year window). Not a backtest of opinion.py's score itself -- that didn't
exist historically, so there's nothing to check it against yet; see
db.journal_opinion() / backtest.py's --opinions mode, which starts
accumulating a real record from whenever a ticker first gets checked.

/bought TICKER [price], /sold TICKER and /portfolio (/positions too) track what the
user reports actually buying (positions.py) -- entirely separate from the ticker
lookup above, and (with the Trading 212 sync below) the only place this bot writes state
instead of just reading and replying. /portfolio shows the user's own positions, each with how it
stands now (positions.portfolio_rows, telegram_notify.format_my_portfolio), without
Claude (the prices come from Yahoo). /bought and /sold take an Oslo or Stockholm listing written the
Yahoo way (EQNR.OL, VOLV-B.ST): it is stored the way the signal tables know it, the
bare ticker with its source (EQNR, NORWAY), so it is priced on its own exchange.
See POSITIONS_USAGE and _handle_positions_command.

The Trading 212 account is tracked too (t212_account.py), read only: the bot never places an
order. The loop syncs the account's holdings into the positions at start and then every 15 minutes
(T212_SYNC_SECONDS; a failed sync is logged and the polling goes on), so a holding is watched
without a /bought, and one sold there is closed by itself. /portfolio opens with «💼 Trading 212»,
asked live (t212_account.portfolio_view), and lists the /bought positions after it. /bought and
/sold of a ticker held in Trading 212 only say that the account is where it is tracked from.

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
import datetime as dt
import math
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests

import assets
import backtest
import crypto
import db
import paper_report
import positions
import research
import sources
import t212_account
import telegram_notify

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "disclosures.db"
RUN_ANALYSIS_SCRIPT = BASE_DIR / "run_claude_analysis.sh"
RUN_ANALYSIS_TIMEOUT = 420   # a chart read per asset takes minutes; a question can take 1-5
RUN_ANALYSIS_KILL_GRACE = 15  # seconds between SIGTERM and SIGKILL to a timed-out run's group
RUN_ANALYSIS_REAP_WAIT = 5    # seconds to wait for the pipes to close after the SIGKILL
QUESTION_MAX_CHARS = 2000
ASK_USAGE = "/ask ваш вопрос"
THINKING = "Думаю над вопросом… (1–5 мин)"
QUESTION_LATER = "Не успел ответить — вопрос в очереди, ответ придёт позже."

API_URL = "https://api.telegram.org/bot{token}/{method}"
LONG_POLL_SECONDS = 25
T212_SYNC_SECONDS = 900          # the Trading 212 account is synced at start and then this often
T212_KEY_RETRY_SECONDS = 3600    # ... but after a 401 or a 403 only this often: a key does not heal by itself
STATE_OFFSET = "telegram_bot_offset"
PERSIST_SECONDS = 30 * 365 * 24 * 3600

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{10}$")
POSITIONS_USAGE = ("/bought TICKER [цена] — отметить покупку (без цены — последнее закрытие); "
                   "биржи Осло/Стокгольма: EQNR.OL, VOLV-B.ST\n"
                   "/sold TICKER — отметить продажу\n"
                   "/portfolio — ваши позиции")

LOOKUP_HINT = ("Любой тикер или монета: NVDA, BTC, SOL, EQNR.OL, VOLV-B.ST. "
               "$BTC — акция с таким тикером, BTC-USD — монета.")

HELP_TEXT = ("Пришлите тикер (например, AAPL) — через ~30-90 сек придёт один "
             "разбор: опинион, вход/цель, новости, итоговый вердикт.\n"
             "Любой вопрос текстом (или /ask …) — ответит аналитик с графиком "
             "TradingView и данными бота.\n"
             "/portfolio — ваш счёт Trading 212 и позиции /bought, /model — модельный портфель.\n"
             "Сводка модельного портфеля приходит по пятницам.\n"
             "/backtest TICKER — как этот тикер торговался после своих же "
             "прошлых инсайдерских покупок (почти всегда n слишком мал, чтобы "
             "что-то значить на уровне одного тикера).\n"
             + POSITIONS_USAGE
             + "\n" + LOOKUP_HINT)


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
    """A ticker positions.py can track, or None. Bare crypto symbols ("BTC") and
    the "CRYPTO:BTC" form both map to this project's crypto ticker convention --
    without this, /bought BTC would open a position in "BTC" the literal string,
    which positions.last_close would then price as the Grayscale Bitcoin Mini
    Trust ETF instead of the coin. An Oslo or Stockholm listing written the Yahoo
    way (EQNR.OL, VOLV-B.ST) passes as typed when its bare ticker is shaped like one,
    so ESSITY-B.ST is fine though it is longer than a ticker may be: _position_listing
    splits it."""
    t = arg.strip().lstrip("$").upper()
    if t.startswith(crypto.PREFIX):
        sym = crypto.symbol_of(t)
        return crypto.ticker(sym) if sym in crypto.SYMBOLS else None
    if t in crypto.SYMBOLS:
        return crypto.ticker(t)
    venue = positions.split_venue(t)
    if venue:
        return t if _TICKER_RE.match(venue[0]) else None
    return t if (_TICKER_RE.match(t) or _ISIN_RE.match(t)) else None


def _position_listing(arg: str) -> tuple[str, str | None] | None:
    """(ticker, source) of what /bought or /sold was given, or None. An Oslo or Stockholm listing
    written the Yahoo way is stored the way the signal tables know it -- the bare ticker with the
    source that discloses it ("EQNR", "NORWAY") -- so it is priced on its own exchange, not as
    "EQNR-OL" or as the unrelated US EQNR. Anything else is _position_ticker's, with no source
    of its own: the journal's latest signal decides."""
    ticker = _position_ticker(arg)
    if ticker is None:
        return None
    return positions.split_venue(ticker) or (ticker, None)


def _handle_my_portfolio(conn) -> None:
    """/portfolio and /positions: «💼 Trading 212» -- the account, asked live (or what the last
    sync stored, with why) -- then the positions recorded with /bought, each with how it stands
    now."""
    try:
        today = dt.date.today()
        view = t212_account.portfolio_view(conn, today)
        rows = positions.portfolio_rows(conn, today, origin=positions.MANUAL)
        telegram_notify.send_text(telegram_notify.format_my_portfolio(rows, t212=view))
    except Exception as e:
        print(f"[telegram_bot] /portfolio failed: {type(e).__name__}: {e}", file=sys.stderr)
        telegram_notify.send_text(f"Не удалось собрать список позиций ({type(e).__name__}). "
                                  "Попробуйте позже.")


def _handle_positions_command(conn, text: str) -> bool:
    """/portfolio (/positions too), /bought, /sold -- the positions that positions.py tracks for
    close alerts. Returns False for anything else."""
    parts = text.split()
    cmd = parts[0].lower().split("@")[0] if parts else ""
    if cmd in ("/portfolio", "/positions"):
        _handle_my_portfolio(conn)
        return True
    if cmd not in ("/bought", "/sold"):
        return False
    listing = _position_listing(parts[1]) if len(parts) > 1 else None
    if not listing:
        telegram_notify.send_text(POSITIONS_USAGE)
        return True
    ticker, venue_source = listing
    # A ticker the Trading 212 account holds -- by its key or, when nothing was /bought under that
    # ticker, by the name the holding is shown under (SAP for an ISIN-keyed SAPd_EQ) -- is tracked
    # from the account: the sync sees a buy or a sale there by itself.
    held = positions.find_open(conn, ticker) or positions.find_holding(conn, ticker)
    if held is not None and held.origin == positions.T212:
        name = telegram_notify._esc(ticker)
        telegram_notify.send_text(
            f"{name} отслеживается из Trading 212: продайте там — бот увидит продажу сам."
            if cmd == "/sold" else f"{name} уже отслеживается из Trading 212.")
        return True
    if cmd == "/sold":
        pos = positions.close_position(conn, ticker)
        telegram_notify.send_text(f"Позиция {ticker} закрыта." if pos
                                  else f"По {ticker} нет открытой позиции.")
        return True
    user_price = None
    if len(parts) > 2:
        try:
            user_price = float(parts[2].replace(",", "."))
        except ValueError:
            telegram_notify.send_text(POSITIONS_USAGE)
            return True
        if not math.isfinite(user_price) or user_price <= 0:
            telegram_notify.send_text(POSITIONS_USAGE)
            return True

    source = venue_source or positions.position_source(conn, ticker)
    market_price = positions.last_close(ticker, source)
    note = ""
    if user_price is not None:
        if market_price and abs(user_price / market_price - 1) > 0.4:
            symbol = positions.yahoo_symbol(ticker, source)
            telegram_notify.send_text(
                f"Цена {user_price:,.2f} сильно отличается от последнего закрытия "
                f"{ticker} ({symbol}): {market_price:,.2f} — стоп-лосс будет считаться "
                f"неверно. Проверьте тикер/цену.")
            return True
        if not market_price:
            note = (" (стоп-лосс не отслеживается для этого тикера — только "
                    "инсайдерские продажи, новости и срок в год)")
        price = user_price
    else:
        price = market_price
    if not price:
        # a command to copy: an Oslo or Stockholm listing keeps its suffix (/bought EQNR is the US EQNR)
        typed = positions.yahoo_symbol(ticker, venue_source) if venue_source else ticker
        telegram_notify.send_text(f"Не нашёл цену {ticker} — укажите её: /bought {typed} 12.34")
        return True
    try:
        pos = positions.open_position(conn, ticker, price, source=source)
    except ValueError:
        telegram_notify.send_text(f"Позиция {ticker} уже открыта. /sold {ticker}, чтобы закрыть.")
        return True
    insiders = ", ".join(telegram_notify._esc(n) for n in pos.insiders)
    if pos.insiders:
        who = f"слежу за продажами: {insiders}"
    elif pos.source == "SWEDEN":
        # Finansinspektionen's signals are keyed by ISIN, so a Stockholm ticker can't be matched to them.
        who = ("за продажами инсайдеров Стокгольма не слежу (сигналы идут по ISIN) — "
               "слежу за стопом, сроком и новостями")
    else:
        who = "сильного сигнала по нему не было — слежу за сроком и новостями"
    # With no market price the stop can't be watched (the note says so): don't state one.
    stop = "" if note else (f"стоп −{pos.stop_pct * 100:.0f}% от максимума; " if pos.stop_pct is not None
                            else "стоп — по умолчанию; ")
    shown = f"{price:,.2f}".replace(",", " ").replace(".", ",")
    telegram_notify.send_text(f"Записал {ticker} по {shown}; {stop}{who}{note}.")
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


def _stop_group(proc: subprocess.Popen) -> None:
    """A run that outlived its timeout: SIGTERM to its process group (analyst.py stops its own
    Claude on it, which lives in a session of its own), SIGKILL to the group when it is still
    there after RUN_ANALYSIS_KILL_GRACE seconds. Never blocks for good: a grandchild that
    escaped the group may hold the pipes. A run that has already exited is not signalled: its
    group id may belong to somebody else by now."""
    steps = ((signal.SIGTERM, RUN_ANALYSIS_KILL_GRACE), (signal.SIGKILL, RUN_ANALYSIS_REAP_WAIT))
    for sig, wait in steps:
        if proc.returncode is not None:
            return
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return                                  # the group is gone
        try:
            proc.communicate(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def _run_analysis(conn, queue_id: int, label: str) -> bool:
    """Runs run_claude_analysis.sh (the whole queue) and waits for it. True when queue row
    `queue_id` -- the one this request queued -- has been answered (marked processed) by the
    time it is over; False when it is still pending: the run failed, timed out or never got to
    it. The caller then falls back or says the row stays queued. The exit code alone decides
    nothing: 0 may mean the pass never reached the row, and a failure may be another row's."""
    _run_script(label)
    try:
        answered = db.analysis_processed(conn, queue_id)
    except Exception as e:
        print(f"[telegram_bot] could not read queue row {queue_id}: {type(e).__name__}: {e}",
              file=sys.stderr)
        return False
    if not answered:
        print(f"[telegram_bot] {label}: queue row {queue_id} still pending after the run",
              file=sys.stderr)
    return answered


def _run_script(label: str) -> bool:
    """One run of run_claude_analysis.sh in a process group of its own, waited for at most
    RUN_ANALYSIS_TIMEOUT seconds. True when it exited 0 (logged either way)."""
    proc = None
    try:
        proc = subprocess.Popen([str(RUN_ANALYSIS_SCRIPT)], cwd=str(BASE_DIR),
                                start_new_session=True, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                errors="replace")
        try:
            _, err = proc.communicate(timeout=RUN_ANALYSIS_TIMEOUT)
        except BaseException:                       # a timeout, or an interrupt: no Claude left running
            if proc.returncode is None:
                _stop_group(proc)
            raise
        if proc.returncode != 0:
            raise RuntimeError(f"exit {proc.returncode}: {(err or '')[-2000:]}")
        print(f"[telegram_bot] claude-analysis run finished for {label}")
        return True
    except Exception as e:
        print(f"[telegram_bot] synchronous claude-analysis failed for {label}: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return False
    finally:
        for pipe in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass


def _handle_ask(conn, text: str) -> None:
    """A question for the analyst: queued, answered by the same run that answers tickers. The
    analyst sends the answer itself (and marks the row processed), so when the row is answered
    this sends nothing more."""
    qid = db.enqueue_question(conn, text[:QUESTION_MAX_CHARS])
    print(f"[telegram_bot] queued question {qid}, running claude-analysis synchronously")
    telegram_notify.send_text(THINKING)
    if not _run_analysis(conn, qid, f"question {qid}"):
        telegram_notify.send_text(QUESTION_LATER)


def _handle_ticker(conn, asset) -> None:
    # Spec §1.6: an unrecognised symbol is not queued. A coin in the coin list is known
    # to exist; anything else (a stock, CRYPTO:FOO, FOO-USD) needs a price somewhere.
    listed_coin = asset.kind == "crypto" and asset.symbol in sources.cached_coin_symbols(conn)
    if not asset.is_isin and not listed_coin and sources.current_price(asset)[0] is None:
        telegram_notify.send_text(f"Не нашёл такой тикер: {telegram_notify._esc(asset.symbol)} "
                                  "(или источники цен сейчас не отвечают). " + LOOKUP_HINT)
        return
    ticker = asset.key
    queue_id = db.enqueue_analysis(conn, ticker)
    print(f"[telegram_bot] queued {ticker}, running claude-analysis synchronously")
    if _run_analysis(conn, queue_id, ticker):
        return
    # Fall back to the fast deterministic-only reply so the user isn't left with total
    # silence -- the queued row stays pending (only marked processed on a confirmed send
    # inside analyst.py), so the next scheduled or triggered run will still pick it up.
    try:
        rep = research.build(conn, ticker)
        telegram_notify.send_text(
            telegram_notify.format_condensed(rep) +
            "\n\n(Полный разбор не завершился в этот раз — попробуется снова.)")
    except Exception as e2:
        print(f"[telegram_bot] fallback reply also failed for {ticker}: "
              f"{type(e2).__name__}: {e2}", file=sys.stderr)
        telegram_notify.send_text(f"Не удалось получить данные по {ticker}. Попробуйте позже.")


def _handle_model(conn) -> None:
    """/model: the model portfolio's summary."""
    try:
        telegram_notify.send_text(paper_report.format_summary(conn, dt.date.today(), html=True))
    except Exception as e:
        print(f"[telegram_bot] /model failed: {type(e).__name__}: {e}", file=sys.stderr)
        telegram_notify.send_text(f"Не удалось собрать сводку портфеля ({type(e).__name__}). "
                                  "Попробуйте позже.")


def _handle_message(conn, text: str) -> None:
    """Routing: the positions commands (/portfolio, /positions, /bought, /sold), /backtest, /model,
    /ask, any other /command (help); then a single token that is an asset with a price -- the
    ticker analysis; anything else -- a question for the analyst."""
    text = (text or "").strip()
    if _handle_positions_command(conn, text):
        return
    if text.lower().startswith("/backtest"):
        _handle_backtest(conn, text)
        return
    if not text:
        telegram_notify.send_text(HELP_TEXT)
        return
    if text.startswith("/"):
        command = text.split()[0].lower().split("@")[0]
        if command == "/model":
            _handle_model(conn)
        elif command == "/ask":
            question = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
            if question:
                _handle_ask(conn, question)
            else:
                telegram_notify.send_text(ASK_USAGE)
        else:                                       # /start, /help, anything unknown
            telegram_notify.send_text(HELP_TEXT)
        return
    asset = None
    if len(text.split()) == 1:
        asset = assets.resolve(text, coins=lambda: sources.cached_coin_symbols(conn),
                               stocks=sources.stock_universe_symbols)
    if asset is None:
        _handle_ask(conn, text)
        return
    _handle_ticker(conn, asset)


def _sync_t212(conn) -> int:
    """One sync of the Trading 212 account (t212_account.sync), and how many seconds until the
    next: T212_SYNC_SECONDS, or T212_KEY_RETRY_SECONDS after a 401 or a 403. Whatever it raises is
    logged by its type alone -- never its text -- and the polling goes on."""
    try:
        result = t212_account.sync(conn)
    except Exception as e:
        print(f"[telegram_bot] Trading 212 sync failed: {type(e).__name__}", file=sys.stderr)
        return T212_SYNC_SECONDS
    kind = result.error_kind if result is not None else None
    return T212_KEY_RETRY_SECONDS if kind in ("unauthorized", "forbidden") else T212_SYNC_SECONDS


def _serve(conn, token: str, chat_id: str, session: requests.Session, *, clock=time.time,
           sleep=time.sleep, rounds: int | None = None) -> None:
    """The always-on loop: long-poll Telegram, and sync the Trading 212 account at start and then
    every T212_SYNC_SECONDS (between two polls, so at most one long poll or one answer late). A
    poll that fails on the network is retried after a growing pause. `rounds` ends the loop after
    that many polls (tests); `clock` and `sleep` are the time seams.

    The clock is the wall clock: time.monotonic stands still while the Mac sleeps, and the first
    thing wanted after a wake is a sync. A clock set back by more than the interval syncs at once
    too, rather than waiting out the difference. After a 401 or a 403 the next sync is an hour
    away, not 15 minutes: the key will not heal by itself."""
    next_sync, wait = clock(), T212_SYNC_SECONDS
    backoff = 5
    done = 0
    while rounds is None or done < rounds:
        done += 1
        now = clock()
        if now >= next_sync or next_sync - now > wait:
            wait = _sync_t212(conn)
            next_sync = now + wait
        try:
            _poll_once(conn, token, chat_id, session)
            backoff = 5
        except requests.RequestException as e:
            # requests puts the whole URL -- the bot token in it -- into its error text
            print(f"[telegram_bot] poll failed: {telegram_notify._redact(str(e), token)}; "
                  f"retrying in {backoff}s", file=sys.stderr)
            sleep(backoff)
            backoff = min(backoff * 2, 300)


def _poll_once(conn, token: str, chat_id: str, session: requests.Session) -> None:
    offset = db.get_cached_value(conn, STATE_OFFSET, PERSIST_SECONDS)
    offset = int(offset) if offset is not None else None
    updates = _get_updates(token, offset, session)
    for u in updates:
        db.save_cached_value(conn, STATE_OFFSET, float(u["update_id"] + 1))
        msg = u.get("message")          # not an edited_message: an edit is not a new request
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
    _serve(conn, token, chat_id, session)
    return 0


if __name__ == "__main__":
    sys.exit(main())
