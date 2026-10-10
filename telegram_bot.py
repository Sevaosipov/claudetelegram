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
run, the user is told the question stays queued. A ticker lookup and a question share one
runner, _run_analysis: a process group of its own, RUN_ANALYSIS_TIMEOUT seconds, SIGTERM first
(analyst.py stops its Claude on it) and SIGKILL after a grace period; it succeeds when the row
it was run for is marked processed, whatever the run's exit code (the pass also covers other
rows). Only new messages are handled: an edited message is not a second request. The bot holds
no portfolio of its own: your Trading 212 account is the one it tracks, and a command it does not
know gets the help text.

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

/cfd shows the experimental CFD signals of the 13 coins (cfd/live.py: the open ones, the record of the
closed ones, the settings) and /cfd balance|risk|maxrisk|off|on changes the settings; the daily run in bot.py
sends the signals themselves.

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
import positions
import research
import sources
import signal_context
import signal_record
import t212_account
import watch
import telegram_notify
from cfd import league as cfd_league
from cfd import live as cfd_live
from cfd import plan as cfd_plan

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
                   "Сколько купили: /bought XRP 1.37 100 (штук) или /bought XRP 1.37 €200 (на сумму)\n"
                   "/sold TICKER — отметить продажу\n"
                   "Просто /bought или /sold — бот спросит, что именно\n"
                   "Можно и так: /buy XRP 1.37, /sell XRP, «купил XRP €1,37», «продал XRP»\n"
                   "Портфель — в приложении Trading 212: здесь его нет")
PORTFOLIO_GONE = ("Портфель смотрите в приложении Trading 212 — здесь его больше нет. "
                  "/bought и /sold остаются: по записанным позициям приходят сигналы на продажу.")
_GONE_VIEWS = ("/portfolio", "/positions", "/crypto", "/coins")

LOOKUP_HINT = ("Любой тикер или монета: NVDA, BTC, SOL, EQNR.OL, VOLV-B.ST. "
               "$BTC — акция с таким тикером, BTC-USD — монета.")

HELP_TEXT = ("Пришлите тикер или монету (NVDA, BTC) — примерно через минуту придёт короткий разбор: "
             "решение бота, цена, балл, график, тренд, диапазон, стоп и вывод в одну строку.\n"
             "Любой вопрос текстом (или /ask …) — ответит аналитик с графиком "
             "TradingView и данными бота.\n"
             "/bought и /sold — записать покупку и продажу вне Trading 212 (сам счёт бот видит).\n"
             "/cfd — CFD-сигналы по 13 монетам (эксперимент): открытые, итоги, настройки.\n"
             "/cfd plan XAUUSD buy 4461.80 stop 4449.10 — ваша CFD-сделка: 4 цели, риск и объём, "
             "сообщение на каждой цели и на стопе.\n"
             "Сигналы на покупку приходят по пятницам, сигнал на продажу по вашим позициям — сразу.\n"
             "/size — на сколько евро покупать по сигналу: недельный бюджет (/size budget 30) или риск "
             "и предел доли счёта.\n"
             "/league — бумажная лига форекс-идей: тест на 13 недель, счёт по каждой идее.\n"
             "/watch — уровни, которых бот ждёт после разборов: при закрытии за уровнем придёт свежий "
             "разбор сам.\n"
             "/signals — как сработали пятничные сигналы бота: через 1, 4 и 12 недель, в сравнении с рынком.\n"

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


def _handle_league_command(conn, text: str) -> bool:
    """/league: the forex paper league's scoreboard and its mute switch (cfd/league.py). Returns False for
    any other message."""
    parts = text.split()
    if not parts or parts[0].lower().split("@")[0] != "/league":
        return False
    try:
        telegram_notify.send_text(cfd_league.handle_command(conn, text))
    except Exception as e:
        print(f"[telegram_bot] /league failed: {type(e).__name__}: {e}", file=sys.stderr)
        telegram_notify.send_text(f"Не удалось выполнить /league ({type(e).__name__}). Попробуйте позже.")
    return True


def _handle_watch_command(conn, text: str) -> bool:
    """/watch: the levels the bot waits for after its analyses, and /watch off TICKER (watch.py). Returns
    False for any other message."""
    parts = text.split()
    if not parts or parts[0].lower().split("@")[0] != "/watch":
        return False
    try:
        answer = watch.handle_command(conn, text)
        telegram_notify.send_text(answer if answer.startswith("<pre>") else telegram_notify._esc(answer))
    except Exception as e:
        print(f"[telegram_bot] /watch failed: {type(e).__name__}: {e}", file=sys.stderr)
        telegram_notify.send_text(f"Не удалось выполнить /watch ({type(e).__name__}). Попробуйте позже.")
    return True


def _handle_signals_command(conn, text: str) -> bool:
    """/signals: how the bot's own buy signals did (signal_record.py). Returns False for any other
    message."""
    parts = text.split()
    if not parts or parts[0].lower().split("@")[0] != "/signals":
        return False
    try:
        telegram_notify.send_text(signal_record.report(conn))
    except Exception as e:
        print(f"[telegram_bot] /signals failed: {type(e).__name__}: {e}", file=sys.stderr)
        telegram_notify.send_text(f"Не удалось выполнить /signals ({type(e).__name__}). Попробуйте позже.")
    return True


def _handle_size_command(conn, text: str) -> bool:
    """/size: how many euros of a weekly buy signal to buy (signal_context: the risk on one buy and the
    largest share of the account, the user's own settings). Returns False for any other message."""
    parts = text.split()
    if not parts or parts[0].lower().split("@")[0] != "/size":
        return False
    try:
        telegram_notify.send_text(telegram_notify._esc(signal_context.handle_command(conn, text)))
    except Exception as e:
        print(f"[telegram_bot] /size failed: {type(e).__name__}: {e}", file=sys.stderr)
        telegram_notify.send_text(f"Не удалось выполнить /size ({type(e).__name__}). Попробуйте позже.")
    return True


def _handle_cfd_command(conn, text: str) -> bool:
    """/cfd and its settings (cfd/live.py): the experimental CFD signals of the 13 coins -- the open ones,
    the record, the balance, the risk and the pause. Returns False for any other message."""
    parts = text.split()
    if not parts or parts[0].lower().split("@")[0] != "/cfd":
        return False
    try:
        telegram_notify.send_text(cfd_live.handle_command(conn, text))
    except Exception as e:
        print(f"[telegram_bot] /cfd failed: {type(e).__name__}: {e}", file=sys.stderr)
        telegram_notify.send_text(f"Не удалось выполнить /cfd ({type(e).__name__}). Попробуйте позже.")
    return True


_BUY_WORDS = {"/bought", "/buy", "/b", "bought", "buy", "купил", "купила", "куплено", "купить", "/купил"}
_SELL_WORDS = {"/sold", "/sell", "/s", "sold", "sell", "продал", "продала", "продано", "продать", "/продал"}
_FILLER = {"по", "at", "@", "за", "for", "price", "цена"}
_PRICE_RE = re.compile(r"^(?P<pre>[€$])?(?P<num>\d[\d\s.,]*)(?P<post>[€$]|eur|usd|евро)?$", re.IGNORECASE)
BUY_OR_SELL_HINT = "{t} по {p} — купили или продали? Нажмите /bought или /sold."
ASK_BOUGHT = ("Что купили? Пришлите тикер и цену, например: XRP 1.37 "
              "(без цены — по последнему закрытию; сколько купили — третьим: XRP 1.37 100).")
ASK_SOLD = "Что продали? Пришлите тикер: {names}."
NOTHING_TO_SELL = ("Позиций, записанных через /bought, сейчас нет. "
                   "Продажи на счёте Trading 212 бот видит сам.")
PENDING_SECONDS = 300
# What a bare /bought or /sold (a tap on the command) is waiting for, in this process only:
# {"kind": "buy" | "sell" | "which", "ticker": ..., "price": ..., "at": time}. "which" is a ticker and
# a price sent with no verb: the next bare /bought or /sold completes it.
_PENDING: dict = {}


def _pending(now: float | None = None) -> dict | None:
    """The request a bare command left open, unless it is older than PENDING_SECONDS."""
    now = time.time() if now is None else now
    if _PENDING and 0 <= now - _PENDING.get("at", 0) <= PENDING_SECONDS:
        return dict(_PENDING)
    _PENDING.clear()
    return None


def _set_pending(kind: str, ticker: str | None = None, price: str | None = None) -> None:
    _PENDING.clear()
    _PENDING.update(kind=kind, ticker=ticker, price=price, at=time.time())


def _complete_pending(text: str) -> str | None:
    """The full command a message completes, or None when it completes nothing: a bare /bought or
    /sold after «XRP 1.37»; or, after a bare /bought or /sold, the ticker (and for a buy the price)
    it asked for. Anything else is an ordinary message and the open request is forgotten."""
    pending = _pending()
    if pending is None:
        return None
    parts = [w for w in text.split() if w.lower() not in _FILLER]
    first = parts[0].lower().split("@")[0] if parts else ""
    if pending["kind"] == "which":
        if len(parts) == 1 and (first in _BUY_WORDS or first in _SELL_WORDS):
            _PENDING.clear()
            if first in _SELL_WORDS:
                return f"/sold {pending['ticker']}"
            return f"/bought {pending['ticker']} {pending['price']}"
        return None
    qty_token = None
    if pending["kind"] == "buy" and not first.startswith("/"):
        stripped, qty_token = _split_quantity("/bought " + text)
        if qty_token:
            parts = [w for w in stripped.split()[1:] if w.lower() not in _FILLER]
    if first.startswith("/") or not 1 <= len(parts) <= 2 or not _position_listing(parts[0]):
        if not (len(parts) == 1 and (first in _BUY_WORDS or first in _SELL_WORDS)):
            _PENDING.clear()                    # another command, a question: the request is dropped
        return None
    if len(parts) == 2 and _price_token(parts[1]) is None:
        _PENDING.clear()
        return None
    _PENDING.clear()
    if pending["kind"] == "sell":
        return f"/sold {parts[0]}"
    return " ".join(["/bought", *parts, *([qty_token] if qty_token else [])])


def _ask_what(conn, cmd: str) -> None:
    """A bare /bought or /sold: ask for the rest, and wait for it (_PENDING)."""
    if cmd == "/bought":
        _set_pending("buy")
        telegram_notify.send_text(ASK_BOUGHT)
        return
    mine = [p for p in positions.open_positions(conn) if p.origin != positions.T212]
    if not mine:
        _PENDING.clear()
        telegram_notify.send_text(NOTHING_TO_SELL)
        return
    _set_pending("sell")
    names = ", ".join(telegram_notify._esc(p.ticker.removeprefix("CRYPTO:")) for p in mine)
    telegram_notify.send_text(ASK_SOLD.format(names=names))


def _price_token(token: str) -> tuple[float, str | None] | None:
    """A typed price -> (number, currency or None): 1.3676, 1,3676, €1.3676, 1.37$, 180eur."""
    m = _PRICE_RE.match(token.strip())
    if not m:
        return None
    raw = m.group("num").replace(" ", "")
    if "," in raw and "." in raw:           # 1,234.5 or 1.234,5: the last separator is the decimal one
        dec = "," if raw.rfind(",") > raw.rfind(".") else "."
        raw = raw.replace("." if dec == "," else ",", "").replace(dec, ".")
    else:
        raw = raw.replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        return None
    mark = (m.group("pre") or m.group("post") or "").lower()
    currency = {"€": "EUR", "eur": "EUR", "евро": "EUR", "$": "USD", "usd": "USD"}.get(mark)
    return value, currency


_QTY_RE = re.compile(r"^[x×*]?(?P<num>\d[\d\s.,]*)(шт\.?|pcs)?$", re.IGNORECASE)
_VENUE_CURRENCY = {"NORWAY": "NOK", "SWEDEN": "SEK"}        # anything else /bought prices is in dollars


def _split_quantity(text: str) -> tuple[str, str | None]:
    """How much was bought, taken out of a buy typed with it -> (the text without it, the token).
    The quantity stands before the ticker («/bought 100 XRP 1.37», «купил 100 XRP по 1,37») or after
    the price («/bought XRP 1.37 100», «... x100», «... 100шт»); after the price a token with a
    currency sign is the money spent instead («/bought XRP 1.37 €200»). (text, None) when there is
    none."""
    words = text.split()
    parts = [w for w in words if w.lower() not in _FILLER]
    if not parts or parts[0].lower().split("@")[0] not in _BUY_WORDS:
        return text, None
    token = None
    if len(parts) >= 3 and _QTY_RE.match(parts[1]) and _position_listing(parts[2]) \
            and not _position_listing(parts[1]):
        token = parts[1]
    elif len(parts) == 4 and _position_listing(parts[1]) and _price_token(parts[2]) \
            and (_QTY_RE.match(parts[3]) or _price_token(parts[3])):
        token = parts[3]
    if token is None:
        return text, None
    words.remove(token)
    return " ".join(words), token


def _quantity(token: str, price: float, price_currency: str, conn) -> float | None:
    """The number bought that `token` tells: a count as it is; money spent (a currency sign) over the
    price, euros being turned into the price's currency first. None for nothing usable."""
    m = _QTY_RE.match(token)
    if m:
        typed = _price_token(m.group("num"))
        qty = typed[0] if typed else None
    else:
        typed = _price_token(token)
        if typed is None or not price:
            return None
        amount, currency = typed
        if currency and currency != price_currency:
            import fx
            amount = amount / fx.per_eur(currency, conn) * fx.per_eur(price_currency, conn)
        qty = amount / price
    return qty if qty and math.isfinite(qty) and qty > 0 else None


def _normalise_trade_text(text: str) -> tuple[str, str | None, str | None] | None:
    """What the user meant by a buy or a sale typed loosely -> ("/bought" | "/sold" | "?", ticker text,
    price text) or None when the message is not one. Accepts /buy, /sell, the words without a slash,
    Russian verbs, fillers ("по", "at") and a price with a currency sign. "?" is a ticker and a price
    with no verb ("XRP €1.37"): the reply asks which it was."""
    parts = [w for w in text.split() if w.lower() not in _FILLER]
    if not parts:
        return None
    first = parts[0].lower().split("@")[0]
    if first in _BUY_WORDS or first in _SELL_WORDS:
        cmd = "/bought" if first in _BUY_WORDS else "/sold"
        if not first.startswith("/") and (len(parts) < 2 or len(parts) > 3 or not _position_listing(parts[1])
                                          or (len(parts) == 3 and _price_token(parts[2]) is None)):
            return None                         # an ordinary sentence that starts with "sold ...": a question
        return cmd, (parts[1] if len(parts) > 1 else None), (parts[2] if len(parts) > 2 else None)
    if len(parts) == 2 and not first.startswith("/") and _price_token(parts[1]) and _position_listing(parts[0]):
        return "?", parts[0], parts[1]
    return None


def _handle_positions_command(conn, text: str) -> bool:
    """/bought and /sold -- the positions that positions.py tracks for close alerts (the views
    /portfolio and /crypto are gone: the user reads the portfolio in the Trading 212 app, and the
    commands say so). Returns False for anything else. A buy or a sale may be typed loosely
    (_normalise_trade_text): /buy, /sell, without the slash, with a currency sign on the price."""
    text = _complete_pending(text) or text
    text, qty_token = _split_quantity(text)
    parts = text.split()
    cmd = parts[0].lower().split("@")[0] if parts else ""
    if cmd in _GONE_VIEWS:
        telegram_notify.send_text(PORTFOLIO_GONE)
        return True
    trade = _normalise_trade_text(text)
    if trade is None:
        return False
    cmd, ticker_text, price_text = trade
    if cmd == "?":
        _set_pending("which", ticker_text, price_text)
        telegram_notify.send_text(BUY_OR_SELL_HINT.format(
            t=telegram_notify._esc(ticker_text.upper()), p=telegram_notify._esc(price_text)))
        return True
    if ticker_text is None:                 # a bare /bought or /sold: a tap on the command
        _ask_what(conn, cmd)
        return True
    parts = [cmd] + [x for x in (ticker_text, price_text) if x]
    if cmd not in ("/bought", "/sold"):
        return False
    listing = _position_listing(parts[1]) if len(parts) > 1 else None
    if not listing:
        telegram_notify.send_text(POSITIONS_USAGE)
        return True
    ticker, venue_source = listing
    # A ticker the Trading 212 account holds is tracked from the account: the sync sees a buy or a
    # sale there by itself. It is matched by the position's own ticker or by the symbol of the
    # t212_ticker stored with it (Meta, held as FB_US_EQ and tracked as META, answers to both) --
    # not by the name a holding keyed by its ISIN is shown under: hundreds of non-US instruments
    # share a US company's symbol, so SAP here is the US stock and the Frankfurt SAPd_EQ is named
    # by its ISIN.
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
    user_price, converted = None, ""
    source = venue_source or positions.position_source(conn, ticker)
    if len(parts) > 2:
        typed = _price_token(parts[2])
        if typed is None or not math.isfinite(typed[0]) or typed[0] <= 0:
            telegram_notify.send_text(POSITIONS_USAGE)
            return True
        user_price, currency = typed
        # A coin or a US stock is priced in dollars: a price typed in euros is converted, and said so.
        if currency == "EUR" and (crypto.is_crypto(ticker) or not source or source in ("SEC", "HOUSE", "SENATE", "SEC13DG")):
            import fx
            rate = fx.per_eur("USD", conn)
            if rate:
                converted = f" (€{parts[2].strip('€').strip()} = ${user_price * rate:,.4g})"
                user_price *= rate

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
    currency = _VENUE_CURRENCY.get(source, "USD")
    qty = _quantity(qty_token, price, currency, conn) if qty_token else None
    if qty_token and qty is None:
        telegram_notify.send_text(POSITIONS_USAGE)
        return True
    mine = positions.find_open(conn, ticker)
    if mine is not None and qty and mine.quantity:      # more of what is already recorded: averaged
        pos = positions.add_to_position(conn, mine, price, qty)
        telegram_notify.send_text(
            f"Докупили {ticker}: теперь {telegram_notify.quantity(pos.quantity)} шт., "
            f"средняя {telegram_notify._price(pos.entry_price)}.")
        return True
    try:
        pos = positions.open_position(conn, ticker, price, source=source, quantity=qty, currency=currency)
    except ValueError:
        telegram_notify.send_text(f"Позиция {ticker} уже открыта. /sold {ticker.removeprefix('CRYPTO:')}, "
                                  "чтобы закрыть; докупить можно, когда у обеих покупок указано количество.")
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
    lot = ""
    if qty:
        lot = (f": {telegram_notify.quantity(qty)} шт. на "
               f"{telegram_notify.money_cents(qty * price, currency).lstrip('+')}")
    telegram_notify.send_text(f"Записал {ticker} по {shown}{converted}{lot}; {stop}{who}{note}.")
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


def _handle_message(conn, text: str) -> None:
    """Routing: the positions commands (/bought, /sold), /backtest, /ask,
    any other /command (help, /model included); then a single token that is an asset with a price --
    the ticker analysis; anything else -- a question for the analyst."""
    text = (text or "").strip()
    if (_handle_positions_command(conn, text) or _handle_cfd_command(conn, text)
            or _handle_size_command(conn, text) or _handle_league_command(conn, text)
            or _handle_watch_command(conn, text) or _handle_signals_command(conn, text)):
        return
    if text.lower().startswith("/backtest"):
        _handle_backtest(conn, text)
        return
    if not text:
        telegram_notify.send_text(HELP_TEXT)
        return
    if text.startswith("/"):
        command = text.split()[0].lower().split("@")[0]
        if command == "/ask":
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


def _watch_intraday(conn) -> None:
    """The watched levels against the price of the hour (watch.intraday), with each account sync. Whatever
    it raises is logged and the polling goes on."""
    try:
        watch.intraday(conn)
    except Exception as e:
        print(f"[telegram_bot] watch failed: {type(e).__name__}: {e}", file=sys.stderr)


def _track_cfd_plans(conn) -> None:
    """One pass over the user's open CFD trades (cfd/plan.py: a message at each stage and at the close).
    Whatever it raises is logged and the polling goes on."""
    try:
        cfd_plan.run(conn)
    except Exception as e:
        print(f"[telegram_bot] CFD plans failed: {type(e).__name__}: {e}", file=sys.stderr)


def _serve(conn, token: str, chat_id: str, session: requests.Session, *, clock=time.time,
           sleep=time.sleep, rounds: int | None = None) -> None:
    """The always-on loop: long-poll Telegram, and sync the Trading 212 account at start and then
    every T212_SYNC_SECONDS (between two polls, so at most one long poll or one answer late), and
    follow the user's open CFD trades every cfd_plan.TRACK_SECONDS the same way. A
    poll that fails on the network is retried after a growing pause. `rounds` ends the loop after
    that many polls (tests); `clock` and `sleep` are the time seams.

    The clock is the wall clock: time.monotonic stands still while the Mac sleeps, and the first
    thing wanted after a wake is a sync. A clock set back by more than the interval syncs at once
    too, rather than waiting out the difference. After a 401 or a 403 the next sync is an hour
    away, not 15 minutes: the key will not heal by itself."""
    next_sync, wait = clock(), T212_SYNC_SECONDS
    next_plans = clock()
    backoff = 5
    done = 0
    while rounds is None or done < rounds:
        done += 1
        now = clock()
        if now >= next_sync or next_sync - now > wait:
            wait = _sync_t212(conn)
            next_sync = now + wait
            _watch_intraday(conn)
        if now >= next_plans or next_plans - now > cfd_plan.TRACK_SECONDS:
            _track_cfd_plans(conn)
            next_plans = now + cfd_plan.TRACK_SECONDS
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
