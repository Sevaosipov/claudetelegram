"""analyst.py: the Claude analyst -- free-form questions and ticker lookups, answered from the
bot's own data plus the user's TradingView Desktop (the TradingView MCP server).

Two callers, one way of running Claude (`claude_command`):
  * the Telegram queue: run_claude_analysis.sh -> `python analyst.py process-queue` -> a headless
    Claude with claude_analysis_prompt.txt answers every queued row and sends the messages itself;
  * the terminal: `python analyst.py ask "вопрос"` -> claude_ask_prompt.txt; the answer is printed,
    nothing goes to Telegram.

The rest of the commands print text for that Claude to read. They are how the prompts avoid ever
putting user text into a shell command (spec 2026-09-30, section 7):
    python analyst.py question ID          the queued question's text
    python analyst.py context 'TICKER'     the model's score, the positions, the dossier
    python analyst.py context --queue ID   the same for a queued ticker row (the row's key as stored)
    python analyst.py portfolio            the model summary, the watchlist, today's buys
    python analyst.py news 'QUERY'         up to 10 Google News headlines with dates

This module never imports telegram_bot (the bot imports the analyst, not the other way round) and
does nothing on import that needs the network.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
from pathlib import Path

import db
import model
import model_score
import paper
import paper_report
import positions
import research
import sources
from telegram_notify import money_eur, signed_pct

BASE_DIR = Path(__file__).parent
CLAUDE_BIN = Path.home() / ".local" / "bin" / "claude"

# The TradingView tools Claude may call: read the chart, move it to an asset and back, and look a
# symbol up. Nothing that edits indicators, drawings, alerts, Pine scripts or layouts, and not
# data_get_study_values (it dumps the user's private scripts) -- see analyst_method.txt.
TV_TOOLS = ("tv_health_check", "tv_launch", "chart_get_state", "chart_set_symbol",
            "chart_set_timeframe", "quote_get", "data_get_ohlcv", "symbol_info", "symbol_search")
ALLOWED_TOOLS = ("Bash",) + tuple(f"mcp__tradingview__{t}" for t in TV_TOOLS)
# launchd's PATH lacks these; the TradingView MCP server is started with `node` from one of them.
EXTRA_PATH = ("/usr/local/bin", "/opt/homebrew/bin")
TICKER_RE = re.compile(r"^\$?[A-Z0-9][A-Z0-9.\-]{0,14}$")

ANALYSIS_PROMPT = "claude_analysis_prompt.txt"
ASK_PROMPT = "claude_ask_prompt.txt"
WATCHLIST_MAX = 10
NEWS_MAX = 10
_DECISION = {model_score.BUY: "покупка", model_score.WATCH: "наблюдение",
             model_score.BLOCK: "блок", model_score.SKIP: "пропуск"}


# ---------------------------------------------------------------- running Claude
def claude_command(prompt: str) -> list[str]:
    """The one command both paths run: a headless Claude with the shell and the TradingView
    tools above, and no permission prompts."""
    return [str(CLAUDE_BIN), "-p", prompt, "--permission-mode", "acceptEdits",
            "--allowedTools", *ALLOWED_TOOLS]


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


def process_queue(conn, *, run=None) -> int:
    """One headless Claude pass over the queue (claude_analysis_prompt.txt). 0 when nothing is
    queued -- no Claude run then. Otherwise Claude's exit code."""
    if not db.pending_analysis(conn):
        return 0
    run = run or subprocess.run
    try:
        proc = run(claude_command(_prompt(ANALYSIS_PROMPT)), cwd=BASE_DIR, env=claude_env(),
                   check=False)
    except FileNotFoundError as e:
        return _claude_missing(e)
    return proc.returncode


def ask(question: str, *, run=None) -> int:
    """Answer `question` from the terminal: Claude runs, the answer is printed (bold tags
    removed) and nothing is sent anywhere. Returns Claude's exit code."""
    run = run or subprocess.run
    prompt = _prompt(ASK_PROMPT) + "\n\nВОПРОС:\n" + question
    try:
        proc = run(claude_command(prompt), cwd=BASE_DIR, env=claude_env(),
                   capture_output=True, text=True)
    except FileNotFoundError as e:
        return _claude_missing(e)
    print(re.sub(r"</?b>", "", proc.stdout or "", flags=re.I).strip())
    if proc.returncode and getattr(proc, "stderr", None):
        print(proc.stderr.strip(), file=sys.stderr)
    return proc.returncode


# ---------------------------------------------------------------- tickers
def valid_ticker(text: str) -> str | None:
    """`text` as a ticker without its leading "$", or None when it isn't shaped like one --
    the gate every ticker typed on a command line goes through."""
    ticker = (text or "").strip().upper()
    return ticker.removeprefix("$") if TICKER_RE.match(ticker) else None


def _spellings(ticker: str) -> tuple[set[str], bool]:
    """How the bot's tables may spell `ticker` ("$NVDA" -> NVDA, "BTC" -> BTC or CRYPTO:BTC),
    and whether it was written as a stock on purpose ("$BTC" is the Grayscale ETF, not bitcoin)."""
    key = ticker.strip().upper()
    stock_only = key.startswith("$")
    key = key.removeprefix("$")
    if stock_only or key.startswith("CRYPTO:"):
        return {key}, stock_only
    return {key, f"CRYPTO:{key}"}, False


def _pts(x: float) -> str:
    return str(round(x)).replace("-", "−")


def _stop(stop_pct: float | None) -> str:
    return f"стоп −{stop_pct * 100:.0f}% от максимума" if stop_pct else "стоп: мало истории"


def _score_of(scored: list, ticker: str):
    names, stock_only = _spellings(ticker)
    for s in scored:
        coin = getattr(s, "coin", None)
        if s.ticker.upper() in names or (coin and not stock_only and coin.upper() in names):
            return s
    return None


# ---------------------------------------------------------------- context
def _model_lines(scored: list, ticker: str) -> list[str]:
    s = _score_of(scored, ticker)
    if s is None:
        return [f"МОДЕЛЬ: свежего сигнала за {model.MODEL_SIGNAL_DAYS} дней нет"]
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
    names, _stock_only = _spellings(ticker)
    lines = []
    for code in model.BOOKS:
        for p in paper.open_positions(conn, code):
            if p["ticker"].upper() in names:
                result = (p["last_value"] / p["cost_eur"] - 1
                          if p["last_value"] is not None and p["cost_eur"] else None)
                lines.append(f"МОДЕЛЬ ДЕРЖИТ: {code} с {p['fill_date']}, результат "
                             f"{signed_pct(result)}, {_stop(p['stop_pct'])}")
    for p in positions.open_positions(conn):
        if p.ticker.upper() in names:
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


def _score_today(conn) -> tuple[list | None, str | None]:
    """(the model's scores, None), or (None, the error's type name): scoring reaches for the
    network and the finders, and what the analyst has besides it is still worth having."""
    try:
        return model.score_today(conn), None
    except Exception as e:
        return None, type(e).__name__


def context(conn, ticker: str, *, scored=None) -> str:
    """What the bot knows about `ticker` (its key as the queue stores it, "$NVDA" included):
    the model's score and decision, the model's and the user's positions in it, the dossier.
    `scored` is model.score_today's list (else it is computed)."""
    error = None
    if scored is None:
        scored, error = _score_today(conn)
    model_lines = ([f"МОДЕЛЬ: не посчитана: {error}"] if scored is None
                   else _model_lines(scored, ticker))
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


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="analyst.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    q = sub.add_parser("question", help="text of a queued question")
    q.add_argument("id", type=int)
    c = sub.add_parser("context", help="the model's score, positions and the dossier")
    c.add_argument("ticker", nargs="?")
    c.add_argument("--queue", type=int, metavar="ID", help="take the ticker from a queue row")
    sub.add_parser("portfolio", help="model summary, watchlist, today's buys")
    n = sub.add_parser("news", help="Google News headlines")
    n.add_argument("query", nargs="+")
    a = sub.add_parser("ask", help="ask the analyst from the terminal")
    a.add_argument("text", nargs="+")
    sub.add_parser("process-queue", help="answer the Telegram queue with a headless Claude")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "context":
        return _context_command(args)
    if args.command == "question":
        return _question_command(args)
    if args.command == "portfolio":
        print(portfolio(_open_db()))
        return 0
    if args.command == "news":
        print(news(" ".join(args.query)))
        return 0
    if args.command == "ask":
        return ask(" ".join(args.text))
    return process_queue(_open_db())


if __name__ == "__main__":
    raise SystemExit(main())
