"""Collect newly disclosed stock purchases from public filings, log every one of
them, journal the *signals* they form, and run the model portfolio (model.py) on them.
The model scores and sells on every run but buys only on the week's first full run from
Friday to Sunday, and the week's one Telegram message (what the model bought and sold, holds
and watches, the groups that started selling) goes out once that pass has got through -- on
Sunday regardless, with a warning if the model never did. Every other day Telegram gets only
the close alerts on your positions (/bought and the Trading 212 account), as they fire, and the
breakage warnings. A full run also syncs the Trading 212 account (t212_account.py, read only)
right before the close alerts are checked.

Sources:
    SEC Form 4        US insider purchases and sales (sec_edgar.py)
    SEC Schedule 13D/G  5%+ beneficial-ownership stakes -- who owns how much OF THE
                        COMPANY, rather than for how much money (sec_13dg.py)
    SEC Form 144      notices of INTENDED sales, filed before the sale and so before
                        the Form 4 recording it -- an early exit signal (sec_144.py)
    House Clerk       US House Periodic Transaction Reports (house_ptr.py)
    Senate eFD        the Senate half of the same, opt-in via --senate and currently
                        unreachable from here (senate_efd.py)
    BaFin             German Directors' Dealings (bafin.py)
    Oslo Børs         Norwegian Managers' Transactions (norway.py)
    Finansinspektionen  Swedish insider register (sweden.py)

The three SEC forms all come out of the same daily index, so scanning 13D/G and 144
alongside Form 4 costs no extra index requests. See cluster.py for what turns a
filing into a signal.

Usage:
    python bot.py --once                 # one pass over every source, then exit
    python bot.py --once --sec-only --forms 4      # just Form 4
    python bot.py --once --forms 13D,13G           # just beneficial-ownership stakes
    python bot.py --once --sweden-only --sweden-days 90   # backfill Sweden
    python bot.py --once --no-bafin      # skip BaFin (much the slowest source)
    python bot.py --once --insiders-only # officers and directors only, no 10% holders
    python bot.py --healthcheck          # scrape nothing; warn if no recent run
    python bot.py --interval 900         # poll every 15 minutes, forever

Env vars (see README.md for how to obtain them):
    TELEGRAM_BOT_TOKEN    required to send alerts -- from @BotFather
    TELEGRAM_CHAT_ID      required to send alerts -- your chat id
    SEC_USER_AGENT        strongly recommended: "YourApp your-email@example.com".
                           SEC now throttles the generic default with HTTP 503
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

import cik_map
import cluster
import db
import insider_score
import model
import paper_report
import positions
import sec_edgar
import strategy
import t212_account
import universe
import telegram_notify
from passes import (
    CSV_PATH,
    _sec_forms,
    run_144_pass,
    run_crypto_etf_pass,
    run_crypto_treasury_pass,
    run_farside_pass,
    run_bafin_pass,
    run_house_pass,
    run_norway_pass,
    run_onchain_pass,
    run_sec_pass,
    run_senate_pass,
    run_stake_pass,
    run_sweden_pass,
)

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "disclosures.db"
LOG_PATH = BASE_DIR / "data" / "bot.log"
LOG_MAX_BYTES = 5 * 1024 * 1024

# The weekly run (spec 2026-10-01): the first full run of an ISO week on a Friday, Saturday or
# Sunday -- Friday normally, the weekend only when the Mac missed it. Two independent kv_cache
# keys mark the week: the model's buys, and the Telegram message (set only once it is sent).
# The message waits for the week's model pass (the buys key) -- except on Sunday, when it goes
# out regardless, with a warning line if the model never got through.
WEEKLY_FROM_WEEKDAY = 4         # Friday; Monday is 0
LAST_WEEKLY_WEEKDAY = 6         # Sunday
BUYS_KEY = "model_buys_{week}"
MESSAGE_KEY = "weekly_message_{week}"
_WEEK_KEY_TTL = 30 * 86400


class _Tee:
    """Write to the original stream and to the log file at once.

    The run's output is a readable feed of what was found, and it was going only to
    launchd's stdout file -- which is truncated per run and, when the job failed to
    start at all, stayed empty while nothing else recorded that anything was wrong.
    Keeping a rotated copy alongside means there is a history to look at.
    """

    def __init__(self, stream, handle):
        self._stream = stream
        self._handle = handle

    def write(self, data):
        self._stream.write(data)
        self._handle.write(data)
        self._handle.flush()
        return len(data)

    def flush(self):
        self._stream.flush()
        self._handle.flush()

    def isatty(self):
        return self._stream.isatty()


def _open_log():
    """Append-mode log handle, rotated once when it gets large. One generation is
    kept -- enough to see what the previous runs did, without unbounded growth."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_MAX_BYTES:
            LOG_PATH.replace(LOG_PATH.with_suffix(".log.1"))
    except OSError:
        pass
    return LOG_PATH.open("a", encoding="utf-8")


def _run_source(name: str, fn, *fn_args, **fn_kwargs):
    """Run one source's whole pass, turning a failure into a reported miss rather
    than a dead run. The sources are independent: a bad day at SEC must not cost the
    run its BaFin, Norway and Sweden data, nor the signal computation afterwards --
    which is exactly what an uncaught exception here used to do. None means the
    source failed, which is deliberately distinct from 0 (ran, found nothing)."""
    try:
        return fn(*fn_args, **fn_kwargs)
    except Exception as e:
        print(f"[{name}] pass failed: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def _which_sources(args) -> dict:
    """Which sources are enabled for this run, honoring the mutually-exclusive
    *_only flags (any one of them switches off all the others) and each source's
    individual --no-* skip flag. Shared by main()'s poll loop and
    collect_new_signals() (via strategy.buy_side_signals) so the two never drift
    apart."""
    only_set = (args.sec_only or args.house_only or args.bafin_only or args.norway_only
                or args.sweden_only or args.crypto_only)
    sec = args.sec_only or not only_set
    return {
        "sec": sec,
        "house": args.house_only or not only_set,
        "bafin": (args.bafin_only or not only_set) and not args.no_bafin,
        "norway": (args.norway_only or not only_set) and not args.no_norway,
        "sweden": (args.sweden_only or not only_set) and not args.no_sweden,
        # Never implied by "all sources": it has to be asked for explicitly, because
        # it is currently unreachable from here (see senate_efd.py).
        "senate": args.senate,
        # Corporate treasury trades and spot-ETF flows. Congressional crypto buys
        # are House/Senate rows and come with those sources, not this flag.
        "crypto": (args.crypto_only or not only_set) and not args.no_crypto,
        # Not a disclosure (see crypto_onchain.py), so opt-in like the Senate.
        "onchain": args.onchain and (args.crypto_only or not only_set),
        # 13D/G stakes ride on the SEC source but have their own form-type gate
        # (--forms) -- kept here, alongside the source it depends on, rather than
        # re-derived inside strategy.buy_side_signals.
        "stakes": sec and bool(_sec_forms(args) & {"13D", "13G"}),
    }


def collect_new_signals(conn, args) -> tuple[list, list]:
    """The finders' output, from everything currently in SQLite (not just this run's new
    rows -- a cluster or exit accumulates across daily runs), keeping only what grew past
    what was last alerted. Returns (buy side, exits):
      - buy side: cluster, stake and crypto signals, bearish coin signals included
      - exits: people who bought together later selling together (--no-exit-signals skips)
    Nothing else is filtered: the model scores what matters (model.py) and the journal
    keeps the rest for history. _journal logs each signal, once it is enriched.
    """
    sources = _which_sources(args)
    buys = strategy.buy_side_signals(
        conn, sources=sources, onchain=sources["onchain"],
        cluster_kwargs={"min_value": args.min_cluster_value, "solo_threshold": args.solo_threshold},
        sec_kwargs={"include_derivatives": args.include_derivatives,
                   "insiders_only": args.insiders_only, "include_10b5_1": args.include_10b5_1},
        sweden_kwargs={"include_share_programs": args.include_share_programs,
                      "include_derivatives": args.include_derivatives},
        stake_kwargs={"min_percent": args.stake_min_percent, "min_increase_pp": args.stake_min_increase,
                     "activist_only": args.activist_only, "new_positions_only": args.new_positions_only,
                     "max_age_days": 30},
    )
    exits = [] if args.no_exit_signals else strategy.exit_signals(conn, sources=sources)
    return buys, exits


def _signal_features(sig) -> dict:
    """Flatten a signal into the row signal_journal stores.

    Recorded at emission with the values it actually had, so backtest.py measures
    the signals that were sent rather than re-deriving them from today's code and
    today's thresholds -- which would quietly make every measurement agree with
    whatever the constants currently are.
    """
    import json
    if hasattr(sig, "crypto_kind"):      # CryptoSignal
        kind, buyers, members = sig.crypto_kind, len(sig.member_names) or 1, sig.member_names
    elif hasattr(sig, "percent"):        # StakeSignal
        kind, buyers, members = "stake", 1, [sig.person]
    elif hasattr(sig, "seller_count"):   # ExitSignal
        kind, buyers, members = "exit", sig.total_buyers, sig.seller_names
    else:
        kind, buyers, members = sig.reason, sig.buyer_count, sig.member_names
    return {
        "source": sig.source,
        "kind": kind,
        "ticker": sig.ticker,
        "company": sig.company,
        "buyer_count": buyers,
        "total_value_eur": getattr(sig, "total_value", None),
        "holder_only": int(getattr(sig, "holder_only", False)),
        "has_officer": int(not getattr(sig, "holder_only", False)),
        "position_increase_pct": getattr(sig, "position_increase_pct", None),
        "first_buy": int(getattr(sig, "first_buy", False)),
        "lag_days": getattr(sig, "lag_days", None),
        "market_cap_eur": getattr(sig, "market_cap_eur", None),
        "value_pct_of_mcap": getattr(sig, "value_pct_of_mcap", None),
        "percent_of_class": getattr(sig, "percent", None),
        "score": getattr(sig, "score", None),
        "window_start": str(getattr(sig, "window_start", "") or ""),
        "window_end": str(getattr(sig, "window_end", "") or ""),
        "members": json.dumps(list(members or []), ensure_ascii=False),
        "corroborated_by": json.dumps(getattr(sig, "corroborated_by", None) or [], ensure_ascii=False),
        "tier": getattr(sig, "tier", None),
    }


def _commit_signals(conn, signals) -> None:
    """Record signals as alerted, so they don't re-fire until they actually grow --
    and journal them at the same moment.

    Journalling belongs here rather than where signals are computed: a signal that was
    only computed still returns on the next run, so recording it at computation time would
    enter the same signal into the journal repeatedly and quietly inflate every backtest
    group. One row per signal, written when _journal commits it -- whether or not the
    day's Telegram message goes out.
    """
    for s in signals:
        db.journal_signal(conn, _signal_features(s))
        if hasattr(s, "crypto_kind"):
            cluster.commit_crypto_alert(conn, s)
        elif hasattr(s, "seller_count"):
            cluster.commit_exit_alert(conn, s)
        elif hasattr(s, "percent"):
            cluster.commit_stake_alert(conn, s)
        else:
            cluster.commit_alert(conn, s)


_FILTER_FLAGS = ("sec_only", "house_only", "bafin_only", "norway_only", "sweden_only",
                 "crypto_only", "min_score", "min_liquidity")


def _filtered_run(args) -> bool:
    """True when a flag limits this run to some sources or scores, so it sees only part of
    the day's signals. The model then neither trades nor starts its clock, and the weekly
    message and the monthly report wait for a full run -- new signals are still journaled."""
    return any(getattr(args, name, False) for name in _FILTER_FLAGS)


def _today() -> dt.date:
    return dt.date.today()


def _week_id(day: dt.date) -> str:
    """The ISO week `day` is in, «2026-W41» (the ISO year: 2027-01-01 is in 2026-W53)."""
    iso = day.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _week_marked(conn, today: dt.date, key: str) -> bool:
    return db.get_cached_value(conn, key.format(week=_week_id(today)), _WEEK_KEY_TTL) is not None


def _weekly_due(conn, today: dt.date, key: str = MESSAGE_KEY) -> bool:
    """True on a Friday, Saturday or Sunday whose week has not yet set `key` (BUYS_KEY: the
    model's buys; MESSAGE_KEY, the default: the Telegram message). The caller adds the full-run
    condition, and sets the key (_mark_week) when what it stands for is done."""
    return today.weekday() >= WEEKLY_FROM_WEEKDAY and not _week_marked(conn, today, key)


def _mark_week(conn, today: dt.date, key: str) -> None:
    db.save_cached_value(conn, key.format(week=_week_id(today)), 1.0)


def _run_model(conn, args, *, buy: bool) -> "model.DayReport | None":
    """The model portfolio's daily pass (model.py) -- after the new signals are collected,
    whether or not Telegram is on. It trades virtual books only, and places buys only when
    `buy` (the week's one buying run); it sells and scores every time. A crash is reported
    like a failed source rather than taking the run down. None on a filtered run (which sees
    only part of the day's signals, so must not trade or start the books' clock, buy or not)
    and after a crash."""
    if _filtered_run(args):
        print("[model] skipped: filtered run")
        return None
    report = _run_source("MODEL", model.run, conn, buy=buy)
    if report is None and not args.no_telegram:
        telegram_notify.send_text("⚠️ disclosure-bot: модельный портфель упал в этом прогоне. "
                                  "Логи: data/launchd.err.log")
    return report


def _journal_cautions(conn, signals: list) -> list:
    """Journal today's bearish coin signals (tier `caution`) now, before the model runs, so a
    caution found today can close a MODEL-C coin today (model reads it back through
    positions._crypto_caution). Returns the other signals, for _journal after the model."""
    cautions = [s for s in signals if strategy.is_caution(s)]
    if cautions:
        _journal(conn, cautions, None)
    return [s for s in signals if not strategy.is_caution(s)]


def _journal(conn, signals: list, report) -> None:
    """Enrich the buy-side signals, set each one's journal tier, and commit them (alert
    state and journal row) -- in this run, whatever happens to the Telegram message.

    The tier is the model's decision for that ticker today (buy / watch / block / skip),
    `caution` for a bearish coin signal (positions._crypto_caution reads it), or None when
    the model did not score it (a filtered run, a crash, an unlisted name). Exit signals
    are committed too, with no tier. Each signal is logged as it is committed, so the line
    carries the market cap and score that enrichment found.
    """
    buys = [s for s in signals if not hasattr(s, "seller_count")]
    if buys:
        cluster.enrich_signals(conn, buys)
    for sig in buys:
        if strategy.is_caution(sig):
            sig.tier = strategy.CAUTION
        else:
            sig.tier = report.decisions.get(sig.ticker) if report is not None else None
    for sig in signals:
        print(telegram_notify.format_any_signal(sig))
    _commit_signals(conn, signals)


def _log_only(text: str) -> bool:
    """What a --no-telegram run does with a message: it goes to the log, not to Telegram."""
    print(text)
    return False


def _sync_t212(conn, args) -> None:
    """Bring the positions in step with the Trading 212 account (t212_account.sync: read only)
    before the exits are checked, so a holding bought or sold there today counts today. Only on a
    full run -- a filtered one leaves the positions alone, as it leaves the model. Without a key
    it does nothing; a crash is reported like a failed source."""
    if _filtered_run(args):
        return
    _run_source("T212", t212_account.sync, conn, notify=_log_only if args.no_telegram else None)


def _send_closes(conn, closes: list) -> bool:
    """The daily Telegram message: «🚪 Ваши позиции» and a close alert for each /bought position
    that fired, as soon as it fires (it is real money, so it does not wait for the week). They
    are marked alerted only if the send went through, so a failed send retries them next run.
    Nothing fired -> nothing sent, returns False."""
    if not closes:
        return False
    text = "\n".join([telegram_notify._b("🚪 Ваши позиции", True)]
                     + [telegram_notify.format_close_alert(a) for a in closes])
    if not telegram_notify.send_text(text):
        print(f"[telegram] send failed -- leaving {len(closes)} close alert(s) for the next run",
              file=sys.stderr)
        return False
    positions.mark_alerted(conn, closes)
    return True


def _send_weekly(conn, today: dt.date, report, *, model_failed: bool = False) -> bool:
    """The weekly model message (paper_report.format_week) -- however quiet the week. The week's
    message key is set only once it went through, so a failed Friday send is tried again on the
    next run of the same Friday-to-Sunday window. `model_failed` adds the warning line (the
    Sunday message with no model pass behind it). True when it was sent."""
    text = paper_report.format_week(conn, today, report, model_failed=model_failed)
    if not telegram_notify.send_text(text):
        print("[telegram] weekly message not sent -- will retry on the next run this week",
              file=sys.stderr)
        return False
    _mark_week(conn, today, MESSAGE_KEY)
    return True


def check_source_liveness(conn, new_by_source: dict, threshold: int) -> list[str]:
    """Name any source that has produced nothing for `threshold` runs in a row.

    A parser broken by a site redesign and a genuinely quiet source emit exactly the
    same thing: zero rows. Half of these scrapers read HTML, PDF word coordinates or
    free-text prose, so a silent break is the *likely* failure mode rather than an
    exotic one -- and nobody is watching the raw feed to notice. Streaks are counted
    per source in kv_cache and reset the moment a source produces anything.
    """
    stale = []
    for source, count in sorted(new_by_source.items()):
        key = f"zero_streak_{source}"
        if count > 0:
            db.save_cached_value(conn, key, 0)
            continue
        streak = (db.get_cached_value(conn, key, float("inf")) or 0) + 1
        db.save_cached_value(conn, key, streak)
        if streak >= threshold:
            stale.append(f"{source}: {streak:.0f} прогонов подряд без новых записей")
    return stale


def run_healthcheck(conn, args) -> int:
    """Alert if no run has completed recently. Returns a process exit code.

    This exists because of how the bot actually failed: the daily launchd job died
    on a macOS permissions error every morning for ten days and nothing said so --
    a scraper that has stopped running is indistinguishable from a market with no
    signals in it, since both produce silence. Touches no source and exits at once,
    so it's safe to schedule separately from (and more often than) the real run.
    """
    last = db.get_cached_value(conn, "last_successful_run", float("inf"))
    if last is None:
        msg = ("⚠️ disclosure-bot: в базе нет ни одного успешного запуска. "
               "Проверьте launchd и data/launchd.err.log")
    else:
        age_hours = (time.time() - last) / 3600
        if age_hours <= args.stale_hours:
            print(f"[healthcheck] ok -- last successful run {age_hours:.1f}h ago")
            return 0
        when = dt.datetime.fromtimestamp(last).strftime("%d.%m.%Y %H:%M")
        msg = (f"⚠️ disclosure-bot не отрабатывал {age_hours:.0f} ч "
               f"(последний успешный запуск: {when}, порог {args.stale_hours} ч).\n"
               f"Логи: data/launchd.err.log, data/launchd.out.log")
    print(msg, file=sys.stderr)
    if not args.no_telegram:
        telegram_notify.send_text(msg)
    return 1


def build_parser() -> argparse.ArgumentParser:
    """Split out of main() so tests (and anything else that wants a real, fully-
    defaulted args object for collect_new_signals) can do
    `bot.build_parser().parse_args([...])` instead of hand-maintaining a
    SimpleNamespace that has to be kept in sync with every flag added here."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="run a single pass and exit")
    ap.add_argument("--interval", type=int, default=900, help="seconds between polls (default 900 = 15min)")
    ap.add_argument("--sec-only", action="store_true")
    ap.add_argument("--house-only", action="store_true")
    ap.add_argument("--min-value", type=float, default=50_000,
                     help="minimum dollar value for a single transaction (purchase or sale, SEC or "
                          "House) to be tracked at all -- House amounts are bracket ranges, so this "
                          "compares against each bracket's lower bound (conservative). Default $50,000; "
                          "pass 0 to disable")
    ap.add_argument("--min-cluster-value", type=float, default=cluster.MIN_CLUSTER_VALUE,
                     help=f"minimum combined value across a cluster's buyers to make it a signal, in EUR "
                          f"(all sources are converted to EUR for signals -- see fx.py; House uses "
                          f"each buyer's disclosed range's lower bound; default "
                          f"€{cluster.MIN_CLUSTER_VALUE:,.0f})")
    ap.add_argument("--solo-threshold", type=float, default=cluster.SEC_SOLO_THRESHOLD,
                     help=f"a single buyer's own total in a ticker (within the cluster window) that's "
                          f"enough to make a signal on its own, without a second distinct buyer -- we don't "
                          f"have shares-outstanding data to measure '%% of the company', so this is a "
                          f"flat money bar instead, in EUR (default €{cluster.SEC_SOLO_THRESHOLD:,.0f})")
    ap.add_argument("--sec-days", type=int, default=7,
                     help="how far back (calendar days) to look for days that haven't been scanned "
                          "yet via SEC's daily index. Days already recorded in scanned_days cost "
                          "nothing, so this is a gap-recovery window, not a per-run workload: only "
                          "genuinely missed days are fetched. Default 7")
    ap.add_argument("--sec-max-backfill-days", type=int, default=3,
                     help="cap on how many previously-unscanned days one run will catch up on "
                          "(~800 filings/day each), newest first. Today is always scanned on top of "
                          "this. Keeps a long outage from turning into one enormous run; the "
                          "remaining backlog drains over the next few runs. Default 3")
    ap.add_argument("--house-min-value", type=float, default=15_000,
                     help="minimum value for a House PTR transaction to be tracked, compared against "
                          "the disclosed bracket's LOWER bound. Separate from --min-value because at "
                          "$50k the whole '$15,001 - $50,000' bracket -- the most commonly filed one "
                          "-- was silently dropped, which also starved the congress ranking. "
                          "Default $15,000")
    ap.add_argument("--include-derivatives", action="store_true",
                     help="include derivative-table code-P rows (warrants, options, convertibles) in "
                          "SEC cluster money totals. Off by default: they're priced at a strike, not "
                          "at what the stock costs, so summing them with common stock distorts the "
                          "headline value")
    ap.add_argument("--include-10b5-1", action="store_true",
                     help="count SEC purchases made under a Rule 10b5-1 plan. Off by default: "
                          "those are scheduled months ahead, so they carry no view on what the "
                          "insider thinks now. Form 4 always carried this flag; it was never read")
    ap.add_argument("--min-score", type=float, default=0,
                     help="marks a manual, filtered run (as do the --*-only flags): new signals are "
                          "still collected and journaled, but the model portfolio does not trade and "
                          "the weekly message and the monthly report wait for a full run. Default 0 "
                          "(a full run)")
    ap.add_argument("--min-liquidity", type=float, default=0,
                     help="marks a manual, filtered run, like --min-score. Default 0 (a full run)")
    ap.add_argument("--no-market-context", action="store_true",
                     help="no longer used: the daily run does not attach a TradingView technical-gauge "
                          "line to signals. Accepted so existing command lines keep working")
    ap.add_argument("--insiders-only", action="store_true",
                     help="in SEC clusters, count only officers and directors -- drop filers who are "
                          "purely >10%% holders. An institution adding to a stake is a different event "
                          "from the people running the company buying")
    ap.add_argument("--healthcheck", action="store_true",
                     help="don't scrape anything: check when the last successful run was and send a "
                          "Telegram warning if it's older than --stale-hours, then exit non-zero. "
                          "Meant for a separate, more frequent launchd job than the main run")
    ap.add_argument("--silent-source-runs", type=int, default=7,
                     help="warn when a source has produced no new rows for this many consecutive "
                          "runs -- a broken parser and a quiet source both look like zero rows, and "
                          "half these scrapers read HTML/PDF/prose. Default 7 (well past a long "
                          "weekend or a genuinely slow week)")
    ap.add_argument("--stale-hours", type=float, default=30,
                     help="how old the last successful run may be before --healthcheck complains "
                          "(default 30, i.e. one missed daily run plus slack)")
    ap.add_argument("--house-years", type=str, default=None,
                     help="comma-separated years to scan for House PTRs (default: current + previous year)")
    ap.add_argument("--bafin-only", action="store_true")
    ap.add_argument("--no-bafin", action="store_true",
                     help="skip BaFin (Germany) -- it's the slowest source (no 'recent activity' feed, "
                          "has to browse by name letter and drill 2 requests deep per notifier)")
    ap.add_argument("--bafin-letters", type=str, default=None,
                     help="comma-separated notifier-name first letters to scan (default: all of A-Z + "
                          "Sonstige). Useful to test on a subset, e.g. --bafin-letters A,B")
    ap.add_argument("--bafin-max-pages", type=int, default=None,
                     help="cap pages scanned per letter (20 notifiers/page) -- default unlimited")
    ap.add_argument("--norway-only", action="store_true")
    ap.add_argument("--no-norway", action="store_true",
                     help="skip Norway (Oslo Børs Newsweb Managers' Transactions)")
    ap.add_argument("--norway-days", type=int, default=2,
                     help="how many trailing calendar days (today back N-1 days) to scan via "
                          "Newsweb's date-range list endpoint; default 2 (raise for a backfill, "
                          "e.g. --norway-days 60)")
    ap.add_argument("--senate", action="store_true",
                     help="also scan US Senate PTRs (efdsearch.senate.gov). OFF by default and "
                          "never implied by the other flags: the site currently answers every "
                          "request from here with an edge-level HTTP 403, which no amount of "
                          "retrying fixes and which this bot will not try to evade. Enable it if "
                          "you're on a network it serves")
    ap.add_argument("--senate-days", type=int, default=30,
                     help="how far back to look for newly *filed* Senate PTRs (default 30)")
    ap.add_argument("--forms", type=str, default="4,13D,13G,144",
                     help="which SEC form types to scan, comma-separated: 4 (insider "
                          "purchases/sales), 13D / 13G (5%%+ beneficial-ownership stakes), 144 "
                          "(notices of intended sales, used as an early exit signal). All of them "
                          "come from the same daily index, so adding one costs no extra index "
                          "requests. Default all four")
    ap.add_argument("--stake-min-percent", type=float, default=cluster.STAKE_MIN_PERCENT,
                     help=f"minimum share of a company's class for a 13D/G stake to be "
                          f"a signal (default {cluster.STAKE_MIN_PERCENT}%%, the level at which "
                          f"filing becomes mandatory)")
    ap.add_argument("--stake-min-increase", type=float, default=cluster.STAKE_MIN_INCREASE_PP,
                     help=f"how many percentage points an already-alerted stake must grow before "
                          f"it's news again -- index funds file 13G/A amendments constantly over "
                          f"fractions of a point (default {cluster.STAKE_MIN_INCREASE_PP}pp)")
    ap.add_argument("--activist-only", action="store_true",
                     help="only Schedule 13D stakes (holders who may seek to influence "
                          "control), skipping passive 13G filers like index funds")
    ap.add_argument("--new-positions-only", action="store_true",
                     help="only a holder's first-ever stake filing on a ticker -- drops "
                          "13D/G amendments entirely, however large the increase, since an "
                          "already-known holder growing their stake is not a new activist showing up")
    ap.add_argument("--crypto-only", action="store_true",
                     help="only the crypto sources: company treasury trades and spot-ETF flows "
                          "(plus on-chain with --onchain)")
    ap.add_argument("--no-crypto", action="store_true",
                     help="skip company crypto-treasury trades (8-K/6-K) and spot-ETF flows")
    ap.add_argument("--crypto-days", type=int, default=7,
                     help="how far back (calendar days) to search 8-K/6-K filings for treasury "
                          "trades; documents already read are skipped. Default 7")
    ap.add_argument("--onchain", action="store_true",
                     help="also snapshot large exchange cold-wallet balances (mempool.space, a "
                          "public Ethereum RPC) and signal on big net flows. OFF by default: "
                          "not a disclosure, and exchange-internal transfers look the same")
    ap.add_argument("--sweden-only", action="store_true")
    ap.add_argument("--no-sweden", action="store_true",
                     help="skip Sweden (Finansinspektionen's insider register)")
    ap.add_argument("--sweden-days", type=int, default=7,
                     help="how far back (calendar days) to look for unscanned days in FI's export. "
                          "Like --sec-days this is a gap-recovery window, not a per-run workload. "
                          "Raise for a backfill, e.g. --sweden-only --sweden-days 90. Default 7")
    ap.add_argument("--include-share-programs", action="store_true",
                     help="count Swedish transactions tied to a share-incentive programme toward "
                          "signals. Off by default: those are compensation being disclosed, not a "
                          "decision to buy. Sweden is the only source here that marks the difference")
    ap.add_argument("--universe-filter", action="store_true",
                     help="restrict to the S&P 100 + Nasdaq-100 universe (off by default -- tracks "
                          "purchases/sales in any US-listed company)")
    ap.add_argument("--require-top-insider", action="store_true",
                     help="also gate SEC purchases (in the raw log, not just signals) on the buyer's "
                          "trailing-12mo track record beating the S&P 500 by "
                          f">={insider_score.OUTPERFORMANCE_THRESHOLD_PP:.0f}pp -- off by default, "
                          "since it rarely fires; use cluster signals instead")
    ap.add_argument("--no-exit-signals", action="store_true",
                     help="don't compute exit signals (people who bought a ticker together later "
                          "selling it together -- see EXIT_* constants in cluster/common.py)")
    ap.add_argument("--no-telegram", action="store_true", help="skip sending Telegram messages")
    return ap


def main():
    args = build_parser().parse_args()

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_handle = _open_log()
    sys.stdout = _Tee(sys.stdout, log_handle)
    sys.stderr = _Tee(sys.stderr, log_handle)
    print(f"=== {dt.datetime.now().isoformat(timespec='seconds')} "
          f"{' '.join(sys.argv[1:]) or '(no flags)'} ===")

    conn = db.connect(DB_PATH)

    if args.healthcheck:
        sys.exit(run_healthcheck(conn, args))

    current_year = dt.date.today().year
    years = (
        [int(y) for y in args.house_years.split(",")]
        if args.house_years
        else [current_year, current_year - 1]
    )

    sources = _which_sources(args)
    do_sec, do_house, do_bafin = sources["sec"], sources["house"], sources["bafin"]
    do_norway, do_sweden = sources["norway"], sources["sweden"]
    do_senate = sources["senate"]
    do_crypto, do_onchain = sources["crypto"], sources["onchain"]

    # 13D/G and 144 identify the issuer only by CIK, so they need the ticker map;
    # loading it is a single cached request and nothing else depends on it.
    cik_lookup = None
    if sources["sec"] and _sec_forms(args) & {"13D", "13G", "144"}:
        try:
            cik_lookup = cik_map.CikMap()
        except Exception as e:
            print(f"could not load the CIK->ticker map ({e}); 13D/G and 144 rows will have "
                  f"no ticker", file=sys.stderr)

    universe_index = None
    if args.universe_filter:
        try:
            universe_index = universe.Universe()
        except Exception as e:
            print(f"could not load S&P100/Nasdaq100 universe ({e}); proceeding unfiltered by index", file=sys.stderr)

    while True:
        started = time.time()
        print(f"--- poll started {dt.datetime.now().isoformat(timespec='seconds')} ---")
        new_by_source: dict[str, int] = {}

        if do_sec:
            session = sec_edgar.new_session()
            forms = _sec_forms(args)
            if "4" in forms:
                new_by_source["SEC"] = _run_source("SEC", run_sec_pass, conn, args, universe_index, session)
            if "13D" in forms or "13G" in forms:
                new_by_source["SEC13DG"] = _run_source("SEC13DG", run_stake_pass, conn, args, session, cik_lookup)
            if "144" in forms:
                new_by_source["SEC144"] = _run_source("SEC144", run_144_pass, conn, args, session, cik_lookup)
        if do_house:
            new_by_source["HOUSE"] = _run_source("HOUSE", run_house_pass, conn, args, universe_index, years)
        if do_bafin:
            new_by_source["BAFIN"] = _run_source("BAFIN", run_bafin_pass, conn, args)
        if do_norway:
            new_by_source["NORWAY"] = _run_source("NORWAY", run_norway_pass, conn, args)
        if do_sweden:
            new_by_source["SWEDEN"] = _run_source("SWEDEN", run_sweden_pass, conn, args)
        if do_senate:
            new_by_source["SENATE"] = _run_source("SENATE", run_senate_pass, conn, args)
        if do_crypto:
            new_by_source["CRYPTO_TREASURY"] = _run_source("CRYPTO_TREASURY", run_crypto_treasury_pass,
                                                           conn, args)
            new_by_source["CRYPTO_ETF"] = _run_source("CRYPTO_ETF", run_crypto_etf_pass, conn, args)
            new_by_source["CRYPTO_ETF_FARSIDE"] = _run_source("CRYPTO_ETF_FARSIDE", run_farside_pass, conn, args)
        if do_onchain:
            new_by_source["CRYPTO_ONCHAIN"] = _run_source("CRYPTO_ONCHAIN", run_onchain_pass, conn, args)

        # None means the source failed outright; it's reported separately rather
        # than being counted as "ran and found nothing".
        failed = sorted(k for k, v in new_by_source.items() if v is None)
        counted = {k: v for k, v in new_by_source.items() if v is not None}
        total_new = sum(counted.values())
        if failed and not args.no_telegram:
            telegram_notify.send_text(
                "⚠️ disclosure-bot: источник(и) упали в этом прогоне: " + ", ".join(failed)
                + "\nОстальные отработали. Логи: data/launchd.err.log"
            )

        stale_sources = check_source_liveness(conn, counted, args.silent_source_runs)
        if stale_sources and not args.no_telegram:
            telegram_notify.send_text(
                "⚠️ disclosure-bot: источник(и) молчат — вероятно, сломался парсер, "
                "а не рынок затих:\n" + "\n".join(f"• {x}" for x in stale_sources)
            )
        for x in stale_sources:
            print(f"[stale] {x}", file=sys.stderr)

        today = _today()
        filtered = _filtered_run(args)
        buys, exits = collect_new_signals(conn, args)
        rest = _journal_cautions(conn, buys)          # a caution found today counts today
        # The model buys only on the week's first full run from Friday to Sunday; its key is
        # set once that pass got through completely (no crash, scoring done, no sleeve failed),
        # so any other pass buys again on the next run of the window.
        buy = not filtered and _weekly_due(conn, today, BUYS_KEY)
        report = _run_model(conn, args, buy=buy)
        if buy and report is not None and report.complete:
            _mark_week(conn, today, BUYS_KEY)
        _journal(conn, rest + exits, report)
        _sync_t212(conn, args)
        closes = positions.check_exits(conn)
        for a in closes:
            print(telegram_notify.format_close_alert(a, html=False))
        if not args.no_telegram:
            _send_closes(conn, closes)
            if not filtered and _weekly_due(conn, today):
                # The message waits for the week's model pass: with the model down it would show
                # no buys, and the pass that buys next would not be in it. Sunday is the last
                # day of the window, so it goes out then whatever happened.
                model_ok = _week_marked(conn, today, BUYS_KEY)
                if model_ok or today.weekday() == LAST_WEEKLY_WEEKDAY:
                    # The monthly report follows the weekly message: only once it was sent.
                    if _run_source("WEEKLY", _send_weekly, conn, today, report, model_failed=not model_ok):
                        _run_source("PAPER_REPORT", paper_report.maybe_send_monthly_report, conn, today)
                else:
                    print("[telegram] weekly message waits for the week's model pass")

        # The pass got all the way through: record it. run_healthcheck reads this,
        # and it's the only evidence that distinguishes "nothing to report" from
        # "hasn't run in ten days".
        db.save_cached_value(conn, "last_successful_run", time.time())

        print(f"--- poll finished, {total_new} new purchase(s), {len(buys) + len(exits)} new signal(s), "
              f"{len(report.buys) if report else 0} model buy(s), "
              f"{len(report.sells) if report else 0} model sale(s), {len(closes)} close alert(s), "
              f"took {time.time()-started:.1f}s ---")

        if args.once:
            break
        elapsed = time.time() - started
        time.sleep(max(1.0, args.interval - elapsed))


if __name__ == "__main__":
    main()
