"""analyst.py: the Claude analyst -- free-form questions and ticker lookups, answered from the
bot's own data plus the user's TradingView Desktop (the TradingView MCP server).

Two callers, one way of running Claude (`claude_command`):
  * the Telegram queue: run_claude_analysis.sh -> `python analyst.py process-queue` -> a headless
    Claude with claude_analysis_prompt.txt answers every queued row and sends the messages itself
    (through `send`);
  * the terminal: `python analyst.py ask "вопрос"` -> claude_ask_prompt.txt; the answer is printed,
    nothing goes to Telegram.

Claude's shell is scoped to ONE command prefix (`.venv/bin/python analyst.py`, see ALLOWED_TOOLS)
and it may write no files at all, so a headless run steered by a hostile headline or question can
do nothing else. Everything it needs is a subcommand here, run from the repository (its working
directory); the subcommands load .env themselves. They also keep user text out of every shell
command, apart from `send`, whose one single-quoted argument is Claude's own reply (spec
2026-09-30, section 7):
    python analyst.py pending              the pending (id, ticker) rows, or «очередь пуста»
    python analyst.py method               analyst_method.txt: how to answer
    python analyst.py question ID          the queued question's text
    python analyst.py context 'TICKER'     the model's score, the positions, the dossier
    python analyst.py context --queue ID   the same for a queued ticker row (the row's key as stored)
    python analyst.py portfolio            the model summary, the watchlist, today's buys
    python analyst.py news 'QUERY'         up to 10 Google News headlines with dates
    python analyst.py send ID 'TEXT'       sends TEXT to Telegram, then marks row ID processed

This module never imports telegram_bot (the bot imports the analyst, not the other way round) and
does nothing on import that needs the network.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import assets
import db
import marketcap
import model
import model_score
import paper
import paper_report
import positions
import research
import sources
import telegram_notify
from telegram_notify import money_eur, signed_pct

BASE_DIR = Path(__file__).parent
CLAUDE_BIN = Path.home() / ".local" / "bin" / "claude"

# What Claude may call. The shell only as `.venv/bin/python analyst.py ...` (Claude runs in
# BASE_DIR) and the TradingView tools: read the chart, move it to an asset and back, look a
# symbol up. No Write and no edit mode: a file it could write (analyst.py, a shim in .venv) would
# be run by that same allowed prefix. Nothing that edits indicators, drawings, alerts, Pine
# scripts or layouts, and not data_get_study_values (it dumps the user's private scripts) --
# see analyst_method.txt.
TV_TOOLS = ("tv_health_check", "tv_launch", "chart_get_state", "chart_set_symbol",
            "chart_set_timeframe", "quote_get", "data_get_ohlcv", "symbol_info", "symbol_search")
BASH_TOOL = "Bash(.venv/bin/python analyst.py:*)"
ALLOWED_TOOLS = (BASH_TOOL,) + tuple(f"mcp__tradingview__{t}" for t in TV_TOOLS)
# launchd's PATH lacks these; the TradingView MCP server is started with `node` from one of them.
EXTRA_PATH = ("/usr/local/bin", "/opt/homebrew/bin")
TICKER_RE = re.compile(r"^\$?[A-Z0-9][A-Z0-9.\-]{0,14}$")

ANALYSIS_PROMPT = "claude_analysis_prompt.txt"
ASK_PROMPT = "claude_ask_prompt.txt"
METHOD_FILE = "analyst_method.txt"
LOCK_FILE = Path("data") / "analyst.lock"
LOCK_WAIT_SECONDS = 600         # a second run waits this long for the first one to finish
LOCK_POLL_SECONDS = 2
CLAUDE_TIMEOUT_SECONDS = 900    # a headless run that takes longer is stopped
TIMEOUT_EXIT = 124
TERMINATED_EXIT = 143           # 128 + SIGTERM: the analyst was stopped, and took its Claude with it
KILL_GRACE_SECONDS = 3          # between SIGTERM and SIGKILL to a timed-out run's process group
WATCHLIST_MAX = 10
NEWS_MAX = 10
_DECISION = {model_score.BUY: "покупка", model_score.WATCH: "наблюдение",
             model_score.BLOCK: "блок", model_score.SKIP: "пропуск"}
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ENV_QUOTED = re.compile(r"""^(["'])(.*?)\1\s*(?:#.*)?$""")
_TEXT_COMMANDS = ("ask", "news", "send")     # free text, which may start with "-"


# ---------------------------------------------------------------- running Claude
def claude_command(prompt: str) -> list[str]:
    """The one command both paths run: a headless Claude that may use only the tools above.
    There is no permission mode: anything not allowed is denied, edits included."""
    return [str(CLAUDE_BIN), "-p", prompt, "--allowedTools", *ALLOWED_TOOLS]


def claude_env(base: dict | None = None) -> dict:
    """A copy of the environment (or of `base`) with EXTRA_PATH in front of PATH, each entry
    only when it is not already there."""
    env = dict(os.environ if base is None else base)
    parts = [p for p in env.get("PATH", "").split(":") if p]
    env["PATH"] = ":".join([p for p in EXTRA_PATH if p not in parts] + parts)
    return env


def _prompt(name: str) -> str:
    return (BASE_DIR / name).read_text(encoding="utf-8")


def _claude_missing(error: FileNotFoundError) -> int:
    print(f"claude не найден: {error.filename or CLAUDE_BIN}")
    return 127


def _prompt_unreadable(name: str, error: OSError) -> int:
    print(f"не удалось прочитать {name}: {error.strerror or error}")
    return 2


def _kill_group(proc: subprocess.Popen) -> None:
    """Stop `proc` and everything it started (the MCP server, node ...): SIGTERM to its process
    group, then SIGKILL to whatever is still in it after a moment."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return                                  # the group is gone
        try:
            proc.wait(timeout=KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass


def _stop_on_sigterm(_signum, _frame):
    """The handler while Claude runs: what the analyst was asked to stop, its Claude stops too."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)      # a second signal must not cut the cleanup
    raise SystemExit(TERMINATED_EXIT)


def _run_group(argv, *, timeout=None, capture_output=False, text=False, check=False, **kwargs):
    """subprocess.run in a session of its own, so that a timeout, an interrupt or a SIGTERM sent
    to this process kills the whole process group, not just the Claude at its head. Raises
    TimeoutExpired like run; a SIGTERM ends this process with 143 after the group is gone
    (Claude is in a session of its own, so the signal that stops the analyst never reaches it)."""
    kwargs.pop("start_new_session", None)
    if capture_output:
        kwargs["stdout"] = kwargs["stderr"] = subprocess.PIPE
    # signal.signal only works in the main thread; elsewhere there is nobody to send it anyway
    on_main = threading.current_thread() is threading.main_thread()
    previous = signal.signal(signal.SIGTERM, _stop_on_sigterm) if on_main else None
    try:
        with subprocess.Popen(argv, start_new_session=True, text=text, **kwargs) as proc:
            try:
                out, err = proc.communicate(timeout=timeout)
            except BaseException:
                if on_main:
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)   # the cleanup is not interrupted
                _kill_group(proc)
                raise
    finally:
        if on_main:
            signal.signal(signal.SIGTERM, previous if previous is not None else signal.SIG_DFL)
    result = subprocess.CompletedProcess(argv, proc.returncode, out, err)
    if check:
        result.check_returncode()
    return result


@contextlib.contextmanager
def _queue_lock():
    """An exclusive lock on data/analyst.lock: the launchd job and the bot's own run must not
    answer the same rows twice. Waits up to LOCK_WAIT_SECONDS for it; yields whether it got it."""
    path = BASE_DIR / LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as f:
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(LOCK_POLL_SECONDS)
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def process_queue(conn, *, run=None) -> int:
    """One headless Claude pass over the queue (claude_analysis_prompt.txt). 0 when nothing is
    queued -- no Claude run then -- or when another run keeps the queue for more than
    LOCK_WAIT_SECONDS. Otherwise Claude's exit code; 124 when it ran past CLAUDE_TIMEOUT_SECONDS
    and was stopped (its rows stay queued), 127 without the claude binary, 2 without the prompt."""
    if not db.pending_analysis(conn):
        return 0
    try:
        prompt = _prompt(ANALYSIS_PROMPT)
    except OSError as e:
        return _prompt_unreadable(ANALYSIS_PROMPT, e)
    run = run or _run_group
    with _queue_lock() as locked:
        if not locked:
            print("очередь занята другим прогоном")
            return 0
        if not db.pending_analysis(conn):       # the run we waited for answered everything
            return 0
        try:
            proc = run(claude_command(prompt), cwd=BASE_DIR, env=claude_env(), check=False,
                       timeout=CLAUDE_TIMEOUT_SECONDS, start_new_session=True)
        except FileNotFoundError as e:
            return _claude_missing(e)
        except subprocess.TimeoutExpired:
            print(f"claude не уложился в {CLAUDE_TIMEOUT_SECONDS} с и остановлен; "
                  "строки остались в очереди")
            return TIMEOUT_EXIT
        return proc.returncode


def ask(question: str, *, run=None) -> int:
    """Answer `question` from the terminal: Claude runs, the answer is printed (bold tags
    removed) and nothing is sent anywhere. Returns Claude's exit code."""
    try:
        prompt = _prompt(ASK_PROMPT) + "\n\nВОПРОС:\n" + question
    except OSError as e:
        return _prompt_unreadable(ASK_PROMPT, e)
    run = run or subprocess.run
    try:
        proc = run(claude_command(prompt), cwd=BASE_DIR, env=claude_env(),
                   capture_output=True, text=True)
    except FileNotFoundError as e:
        return _claude_missing(e)
    print(re.sub(r"</?b>", "", proc.stdout or "", flags=re.I).strip())
    if proc.returncode and getattr(proc, "stderr", None):
        print(proc.stderr.strip(), file=sys.stderr)
    return proc.returncode


def load_env(path: Path | None = None, environ: dict | None = None) -> list[str]:
    """Load KEY=VALUE lines of `path` (default BASE_DIR/.env) into `environ` (default
    os.environ), like the `set -a; source .env` the prompts used to run first: an optional
    `export `, matching quotes stripped, comments and blank lines skipped. A variable that is
    already set is left alone. Prints nothing; returns the names it set."""
    env = os.environ if environ is None else environ
    try:
        text = (path or BASE_DIR / ".env").read_text(encoding="utf-8")
    except OSError:
        return []
    loaded: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export ") or line.startswith("export\t"):
            line = line[len("export"):].lstrip()
        key, eq, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not eq or not _ENV_KEY.fullmatch(key) or (key in env and key not in loaded):
            continue
        quoted = _ENV_QUOTED.match(value)
        env[key] = quoted.group(2) if quoted else re.split(r"\s+#", value, maxsplit=1)[0].strip()
        if key not in loaded:
            loaded.append(key)
    return loaded


def send_message(conn, queue_id: int, text: str, *, send=None) -> tuple[bool, str | None]:
    """Send `text` to Telegram as the answer to queue row `queue_id`, and mark the row processed
    only when the send is confirmed. Returns (sent, note): why it was not sent, or after a
    confirmed send a note when the row could not be marked (it will be answered again)."""
    text = (text or "").strip()
    if not text:
        return False, "пустое сообщение"
    if queue_id not in dict(db.pending_analysis(conn)):
        return False, f"в очереди нет строки {queue_id}"
    try:
        sent = bool((send or telegram_notify.send_text)(text))
    except Exception as e:
        return False, f"ошибка отправки: {type(e).__name__}"
    if not sent:
        return False, "Telegram не принял сообщение"
    try:
        db.mark_analysis_processed(conn, queue_id)
    except Exception as e:
        return True, f"строка {queue_id} не отмечена: {type(e).__name__}"
    return True, None


# ---------------------------------------------------------------- tickers
def valid_ticker(text: str) -> str | None:
    """`text` as a ticker without its leading "$", or None when it isn't shaped like one --
    the gate every ticker typed on a command line goes through."""
    ticker = (text or "").strip().upper()
    return ticker.removeprefix("$") if TICKER_RE.match(ticker) else None


def _split_venue(key: str) -> tuple[str, str] | None:
    """("EQNR", "NORWAY") for "EQNR.OL": the signal tables know a Norwegian or Swedish stock as
    the bare ticker with the source that disclosed it. None for any other key."""
    for source, venue in marketcap.SOURCE_VENUE.items():
        if venue and key.endswith(venue) and len(key) > len(venue):
            return key[:-len(venue)], source
    return None


class _Spellings:
    """How the bot's tables may spell a ticker: "$NVDA" is NVDA, a bare "BTC" is BTC or
    CRYPTO:BTC, "EQNR.OL" is also the bare EQNR of the source NORWAY. `stock_only` is a "$" written
    on purpose ("$BTC" is the Grayscale ETF, not bitcoin). `names` is every spelling to look for;
    `matches` is the exact test for a table row (a bare name from a venue needs its source too)."""

    def __init__(self, ticker: str):
        key = ticker.strip().upper()
        self.stock_only = key.startswith("$")
        key = key.removeprefix("$")
        self.venue = _split_venue(key)
        self.names = {key}
        if self.venue:
            self.names.add(self.venue[0])
        elif not (self.stock_only or key.startswith("CRYPTO:") or "." in key):
            self.names.add(f"CRYPTO:{key}")

    def matches(self, ticker: str | None, source: str | None = None) -> bool:
        name = (ticker or "").upper()
        if self.venue and name == self.venue[0]:
            return source == self.venue[1]
        return name in self.names


def _pts(x: float) -> str:
    return str(round(x)).replace("-", "−")


def _stop(stop_pct: float | None) -> str:
    return f"стоп −{stop_pct * 100:.0f}% от максимума" if stop_pct else "стоп: мало истории"


def _score_of(scored: list, ticker: str):
    keys = _Spellings(ticker)
    for s in scored:
        coin = getattr(s, "coin", None)
        if (keys.matches(s.ticker, getattr(s, "source", None))
                or (coin and not keys.stock_only and coin.upper() in keys.names)):
            return s
    return None


# ---------------------------------------------------------------- context
def _quiet_lines(conn, ticker: str) -> list[str]:
    """For a stock the model has no fresh signal on: the two parts of the score that need no
    signal -- momentum, from the listing's completed daily closes, and the last weeks' news."""
    try:
        asset = assets.resolve(ticker)
        if asset is None or asset.kind != "stock" or asset.is_isin:
            return []
        if asset.exchange:      # EQNR.OL: the signal tables know it as EQNR with source NORWAY
            venue = _split_venue(asset.symbol)
            if venue is None:
                return []
            name, source = venue
        else:
            name, source = asset.symbol, positions.position_source(conn, asset.symbol)
        listed = paper.listing(name, source)
        closes = [c for _d, c in paper.Prices(None, dt.date.today()).bars(listed[0])] if listed else []
        news_part, red = model_score.news_part(model.default_news(name, source))
    except Exception as e:
        return [f"  импульс и новости недоступны: {type(e).__name__}"]

    def part(label: str, p: model_score.Part) -> str:
        lines = [ln.removeprefix(f"{label}: ") for ln in p.lines]
        return f"{label} {_pts(p.points)}" + (f" ({', '.join(lines)})" if lines else "")

    momentum = (part("импульс", model_score.momentum_part(closes)) if closes
                else "импульс: нет истории цен")
    line = f"  {momentum} · {part('новости', news_part)}"
    return [line + (f" · красный флаг: {red}" if red else "")]


def _model_lines(conn, scored: list, ticker: str) -> list[str]:
    s = _score_of(scored, ticker)
    if s is None:
        return ([f"МОДЕЛЬ: свежего сигнала за {model.MODEL_SIGNAL_DAYS} дней нет"]
                + _quiet_lines(conn, ticker))
    lines = [f"МОДЕЛЬ: балл {_pts(s.total)} — {_DECISION.get(s.decision, s.decision)}"]
    if s.kind == "crypto":
        lines.append(f"  части: тренд {_pts(s.trend)} · потоки {_pts(s.flows)} · "
                     f"новости {_pts(s.news)}")
        why = s.block
    else:
        lines.append(f"  части: инсайдеры {_pts(s.insiders)} · поводы {_pts(s.triggers)} · "
                     f"импульс {_pts(s.momentum)} · новости {_pts(s.news)}")
        why = s.block or s.untradeable
    lines += [f"  • {r}" for r in s.reasons]
    if why:
        lines.append(f"  почему не покупка: {why}")
    lines.append(f"  {_stop(s.stop_pct)}")
    t212 = getattr(s, "t212", None)
    lines.append({True: "  T212: есть", False: "  нет на T212"}.get(t212, "  T212: не проверено"))
    return lines


def _position_lines(conn, ticker: str) -> list[str]:
    keys = _Spellings(ticker)
    lines = []
    for code in model.BOOKS:
        for p in paper.open_positions(conn, code):
            if keys.matches(p["ticker"], p["source"]):
                result = (p["last_value"] / p["cost_eur"] - 1
                          if p["last_value"] is not None and p["cost_eur"] else None)
                lines.append(f"МОДЕЛЬ ДЕРЖИТ: {code} с {p['fill_date']}, результат "
                             f"{signed_pct(result)}, {_stop(p['stop_pct'])}")
    for p in positions.open_positions(conn):
        if keys.matches(p.ticker, p.source):
            lines.append(f"ВАША ПОЗИЦИЯ (/bought): с {p.opened_at}, вход {p.entry_price:,.2f}, "
                         f"{_stop(p.stop_pct)}")
    return lines


def _dossier(conn, ticker: str) -> str:
    try:
        return "ДОСЬЕ:\n" + research.format_brief(research.build(conn, ticker))
    except research.NotATicker:
        return "ДОСЬЕ: не похоже на тикер"
    except Exception as e:
        return f"ДОСЬЕ: досье недоступно: {type(e).__name__}"


def _score_today(conn, ticker: str | None = None) -> tuple[list | None, str | None]:
    """(the model's scores, None), or (None, the error's type name): scoring reaches for the
    network and the finders, and what the analyst has besides it is still worth having. With a
    `ticker`, only that name's signals are looked at and enriched (the coins are always scored)."""
    try:
        if ticker is None:
            return model.score_today(conn), None
        today = dt.date.today()
        signals = model.candidate_signals(conn, today, tickers=_Spellings(ticker).names)
        return model.score_today(conn, today, signals=signals), None
    except Exception as e:
        return None, type(e).__name__


def context(conn, ticker: str, *, scored=None) -> str:
    """What the bot knows about `ticker` (its key as the queue stores it, "$NVDA" included):
    the model's score and decision, the model's and the user's positions in it, the dossier.
    `scored` is model.score_today's list (else it is computed)."""
    error = None
    if scored is None:
        scored, error = _score_today(conn, ticker)
    model_lines = ([f"МОДЕЛЬ: не посчитана: {error}"] if scored is None
                   else _model_lines(conn, scored, ticker))
    return "\n".join(model_lines + _position_lines(conn, ticker) + [_dossier(conn, ticker)])


# ---------------------------------------------------------------- portfolio, news
def _buys_today(conn, today: dt.date) -> list[str]:
    rows = conn.execute(
        f"SELECT book, ticker, amount_eur, score, stop_pct FROM paper_orders "
        f"WHERE created = ? AND side = 'buy' AND status IN ('pending', 'filled') "
        f"AND book IN ({','.join('?' * len(model.BOOKS))}) ORDER BY id",
        (today.isoformat(), *model.BOOKS)).fetchall()
    lines = []
    for book, ticker, amount, score, stop in rows:
        line = f"  • {ticker} ({book})"
        if amount is not None:
            line += f": {money_eur(amount)}"
        if score is not None:
            line += f", балл {_pts(score)}"
        if stop:
            line += f", {_stop(stop)}"
        lines.append(line)
    return lines


def _watchlist(scored: list) -> list[str]:
    watch = sorted((s for s in scored if s.decision == model_score.WATCH),
                   key=lambda s: s.total, reverse=True)[:WATCHLIST_MAX]
    if not watch:
        return ["НАБЛЮДЕНИЕ: нет"]
    return ["НАБЛЮДЕНИЕ:"] + [
        f"  • {s.ticker} — {_pts(s.total)}" + (f": {s.reasons[0]}" if s.reasons else "")
        for s in watch]


def portfolio(conn, *, scored=None) -> str:
    """The model summary, the stocks and coins it is watching and today's buys."""
    today = dt.date.today()
    error = None
    if scored is None:
        scored, error = _score_today(conn)
    watch_lines = ([f"НАБЛЮДЕНИЕ: не посчитано: {error}"] if scored is None
                   else _watchlist(scored))
    buys = _buys_today(conn, today)
    buy_lines = ["ПОКУПКИ СЕГОДНЯ:"] + buys if buys else ["ПОКУПКИ СЕГОДНЯ: нет"]
    return "\n".join([paper_report.format_summary(conn, today), ""] + watch_lines + [""]
                     + buy_lines)


def news(query: str) -> str:
    """Up to 10 Google News headlines as «DD.MM · publisher · title»."""
    try:
        items = sources._google_news(query)[:NEWS_MAX]
    except Exception:
        return "новости недоступны"
    lines = []
    for i in items:
        try:
            day = dt.date.fromisoformat(i.get("published") or "").strftime("%d.%m")
        except ValueError:
            day = "—"
        lines.append(f"{day} · {i.get('publisher') or 'Google News'} · {i.get('title', '')}")
    return "\n".join(lines) or "новости недоступны"


# ---------------------------------------------------------------- command line
def _error(text: str) -> int:
    print(text, file=sys.stderr)
    return 2


def _open_db():
    return db.connect(BASE_DIR / "data" / "disclosures.db")


def _context_command(args) -> int:
    if (args.ticker is None) == (args.queue is None):
        return _error("нужен один аргумент: тикер или --queue ID")
    if args.queue is not None:
        conn = _open_db()
        key = dict(db.pending_analysis(conn)).get(args.queue)
        if key is None:
            return _error(f"в очереди нет строки {args.queue}")
        if key == db.QUESTION_TICKER:
            return _error(f"строка {args.queue} — вопрос: python analyst.py question {args.queue}")
        print(context(conn, key))
        return 0
    ticker = valid_ticker(args.ticker)
    if ticker is None:
        return _error(f"не похоже на тикер: {args.ticker!r}. Примеры: NVDA, BTC, EQNR.OL, VOLV-B.ST")
    print(context(_open_db(), ticker))
    return 0


def _question_command(args) -> int:
    text = db.queued_question(_open_db(), args.id)
    if text is None:
        return _error(f"в очереди нет вопроса {args.id}")
    print(text)
    return 0


def _send_command(queue_id: int, text: str) -> int:
    """Prints `sent: True` or `sent: False (why)`; nothing else it prints can carry a traceback
    or a secret. Exit 0 only when the message went out."""
    try:
        sent, note = send_message(_open_db(), queue_id, text)
    except Exception as e:
        sent, note = False, type(e).__name__
    print(f"sent: {sent}" + (f" ({note})" if note else ""))
    return 0 if sent else 1


def _method_command(_args) -> int:
    try:
        print(_prompt(METHOD_FILE))
    except OSError as e:
        return _error(f"не удалось прочитать {METHOD_FILE}: {e.strerror or e}")
    return 0


def _pending_command(_args) -> int:
    rows = db.pending_analysis(_open_db())
    print(rows if rows else "очередь пуста")
    return 0


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="analyst.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("pending", help="the pending queue rows")
    sub.add_parser("method", help="how to answer (analyst_method.txt)")
    q = sub.add_parser("question", help="text of a queued question")
    q.add_argument("id", type=int)
    c = sub.add_parser("context", help="the model's score, positions and the dossier")
    c.add_argument("ticker", nargs="?")
    c.add_argument("--queue", type=int, metavar="ID", help="take the ticker from a queue row")
    sub.add_parser("portfolio", help="model summary, watchlist, today's buys")
    sub.add_parser("news", help="Google News headlines: news QUERY").add_argument("query", nargs="*")
    sub.add_parser("ask", help="ask the analyst from the terminal: ask TEXT").add_argument(
        "text", nargs="*")
    sd = sub.add_parser("send", help="send TEXT to Telegram and mark a queue row processed")
    sd.add_argument("id", type=int)
    sd.add_argument("text", nargs="*")
    sub.add_parser("process-queue", help="answer the Telegram queue with a headless Claude")
    return ap


def _free_text(words: list[str]) -> str:
    """The rest of the command line as one text; a leading `--` is dropped."""
    return " ".join(words[1:] if words[:1] == ["--"] else words).strip()


def _text_command(name: str, words: list[str]) -> int:
    """`ask TEXT`, `news QUERY` and `send ID TEXT`: the rest of the command line is one free text,
    which argparse would take for options when it starts with a dash."""
    if name == "send":
        try:
            queue_id, words = int(words[0]), words[1:]
        except (IndexError, ValueError):
            return _error("нужен номер строки и текст: python analyst.py send ID ТЕКСТ")
    text = _free_text(words)
    if not text and name != "send":         # `send ID` with no text answers itself: sent: False
        return _error(f"нужен текст: python analyst.py {name} ТЕКСТ")
    if name == "ask":
        return ask(text)
    load_env()
    if name == "news":
        print(news(text))
        return 0
    return _send_command(queue_id, text)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in _TEXT_COMMANDS and argv[1:2] not in (["-h"], ["--help"]):
        return _text_command(argv[0], argv[1:])
    args = _parser().parse_args(argv)
    if args.command != "process-queue":
        # The commands Claude runs find their keys here, not in a shell it would have to source.
        # process-queue starts Claude and must not hand it the keys in its environment.
        load_env()
    if args.command == "context":
        return _context_command(args)
    if args.command == "question":
        return _question_command(args)
    if args.command == "method":
        return _method_command(args)
    if args.command == "pending":
        return _pending_command(args)
    if args.command == "portfolio":
        print(portfolio(_open_db()))
        return 0
    return process_queue(_open_db())


if __name__ == "__main__":
    raise SystemExit(main())
