"""Collect newly disclosed stock purchases from public filings, log every one of
them, and push a Telegram alert only for *signals*.

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
import positions
import sec_edgar
import strategy
import tradingview
import trading212
import universe
import telegram_notify
from passes import (
    CSV_PATH,
    _sec_forms,
    run_144_pass,
    run_crypto_etf_pass,
    run_crypto_treasury_pass,
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


def _run_source(name: str, fn, *fn_args):
    """Run one source's whole pass, turning a failure into a reported miss rather
    than a dead run. The sources are independent: a bad day at SEC must not cost the
    run its BaFin, Norway and Sweden data, nor the signal computation afterwards --
    which is exactly what an uncaught exception here used to do. None means the
    source failed, which is deliberately distinct from 0 (ran, found nothing)."""
    try:
        return fn(*fn_args)
    except Exception as e:
        print(f"[{name}] pass failed: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def _which_sources(args) -> dict:
    """Which sources are enabled for this run, honoring the mutually-exclusive
    *_only flags (any one of them switches off all the others) and each source's
    individual --no-* skip flag. Shared by main()'s poll loop and
    run_cluster_pass() (via strategy.buy_side_signals) so the two never drift
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


def run_cluster_pass(conn, args) -> strategy.Selection:
    """Recomputes both signal types from everything currently in SQLite (not just
    this run's new rows -- a cluster/exit accumulates across multiple daily runs),
    keeping only the ones that grew past what was last alerted:
      - cluster buy signals: a ticker bought by multiple distinct people at once
      - exit signals: people who bought together later selling together
    and tiers the buy side into strategy.Selection.strong / .candidates (see
    strategy.select).
    """
    sources = _which_sources(args)

    signals = strategy.buy_side_signals(
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
    if not args.no_exit_signals:
        if sources["sec"]:
            signals += cluster.find_sec_exit_signals(conn)
        if sources["house"]:
            signals += cluster.find_house_exit_signals(conn)
        if sources["bafin"]:
            signals += cluster.find_bafin_exit_signals(conn)
        if sources["norway"]:
            signals += cluster.find_norway_exit_signals(conn)
        if sources["sweden"]:
            signals += cluster.find_sweden_exit_signals(conn)
        if sources["senate"]:
            signals += cluster.find_senate_exit_signals(conn)
    # Tiers (strategy.py): buy side, disclosed in the last few days, on Trading 212,
    # above the size floors -- enrich_signals runs inside select(), only on what
    # survives the cheap filters.
    selection = strategy.select(conn, signals, trading212.availability(conn))

    def keep(t):
        s = t.signal
        if args.min_score and getattr(s, "score", 0) < args.min_score:
            return False
        adv = getattr(s, "avg_daily_value", None)
        return not (args.min_liquidity and adv is not None and adv < args.min_liquidity)
    selection.strong = [t for t in selection.strong if keep(t)]
    selection.candidates = [t for t in selection.candidates if keep(t)]

    if not args.no_market_context:
        # Last, so it runs only on what will actually be shown.
        tradingview.annotate_signals([t.signal for t in selection.strong + selection.candidates])

    for t in selection.strong + selection.candidates:
        print(f"[{t.tier}] " + telegram_notify.format_any_signal(t.signal))
    return selection


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

    Journalling belongs here rather than where signals are computed. A signal that
    was computed but not sent (Telegram down, --no-telegram) still returns on the
    next run, so recording it at computation time would enter the same signal into
    the journal repeatedly and quietly inflate every backtest group. One row per
    signal actually sent.
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


def _send_digest(conn, selection, closes) -> bool:
    """One Telegram message -- 🔥 Сильные, 👀 Кандидаты, 🚪 Закрыть -- and, only if it
    went through, the bookkeeping: alert state and journal for the signals, and the
    once-only mark for close alerts. A failed send leaves both untouched so the next
    run retries. Nothing to say -> nothing sent, returns False."""
    tiered = selection.strong + selection.candidates
    if not tiered and not closes:
        return False
    if not telegram_notify.send_text(telegram_notify.format_tiered_digest(selection, closes)):
        print(f"[telegram] send failed -- leaving {len(tiered)} signal(s) and "
              f"{len(closes)} close alert(s) for the next run", file=sys.stderr)
        return False
    _commit_signals(conn, [t.signal for t in tiered])
    positions.mark_alerted(conn, closes)
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
    defaulted args object for run_cluster_pass) can do
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
                     help=f"minimum combined value across a cluster's buyers to alert on it, in EUR "
                          f"(all sources are converted to EUR for signals -- see fx.py; House uses "
                          f"each buyer's disclosed range's lower bound; default "
                          f"€{cluster.MIN_CLUSTER_VALUE:,.0f})")
    ap.add_argument("--solo-threshold", type=float, default=cluster.SEC_SOLO_THRESHOLD,
                     help=f"a single buyer's own total in a ticker (within the cluster window) that's "
                          f"enough to alert on its own, without a second distinct buyer -- we don't "
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
                     help="only alert on signals scoring at least this (see cluster.score_signal). "
                          "The score ranks signals by how much attention they warrant -- it is not "
                          "a prediction of return. Default 0 (no filter)")
    ap.add_argument("--min-liquidity", type=float, default=0,
                     help="drop signals in names trading less than this much value per day, in EUR. "
                          "Signals whose liquidity can't be resolved are kept, since unknown is not "
                          "the same as low. Default 0 (no filter)")
    ap.add_argument("--no-market-context", action="store_true",
                     help="skip attaching a TradingView technical-gauge line to surviving signals. "
                          "On by default; runs only on whatever is left after --min-score/"
                          "--min-liquidity, since it costs a live request per signal")
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
                     help=f"minimum share of a company's class for a 13D/G stake to be worth "
                          f"alerting on (default {cluster.STAKE_MIN_PERCENT}%%, the level at which "
                          f"filing becomes mandatory)")
    ap.add_argument("--stake-min-increase", type=float, default=cluster.STAKE_MIN_INCREASE_PP,
                     help=f"how many percentage points an already-alerted stake must grow before "
                          f"it's news again -- index funds file 13G/A amendments constantly over "
                          f"fractions of a point (default {cluster.STAKE_MIN_INCREASE_PP}pp)")
    ap.add_argument("--activist-only", action="store_true",
                     help="only alert on Schedule 13D stakes (holders who may seek to influence "
                          "control), skipping passive 13G filers like index funds")
    ap.add_argument("--new-positions-only", action="store_true",
                     help="only alert on a holder's first-ever stake filing on a ticker -- drops "
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
    ap.add_argument("--no-telegram", action="store_true", help="skip sending Telegram alerts")
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

        selection = run_cluster_pass(conn, args)
        closes = positions.check_exits(conn)
        for a in closes:
            print(telegram_notify.format_close_alert(a, html=False))
        if not args.no_telegram:
            _send_digest(conn, selection, closes)
        signal_count = len(selection.strong) + len(selection.candidates)

        # The pass got all the way through: record it. run_healthcheck reads this,
        # and it's the only evidence that distinguishes "nothing to report" from
        # "hasn't run in ten days".
        db.save_cached_value(conn, "last_successful_run", time.time())

        print(f"--- poll finished, {total_new} new purchase(s), {signal_count} signal(s), "
              f"{len(closes)} close alert(s), took {time.time()-started:.1f}s ---")

        if args.once:
            break
        elapsed = time.time() - started
        time.sleep(max(1.0, args.interval - elapsed))


if __name__ == "__main__":
    main()
