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
import csv
import datetime as dt
import sys
import time
from pathlib import Path

import requests

import bafin
import cik_map
import cluster
import db
import fx
import house_ptr
import insider_score
import norway
import sec_13dg
import sec_144
import sec_edgar
import senate_efd
import tradingview
import sweden
import universe
import telegram_notify

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "disclosures.db"
CSV_PATH = BASE_DIR / "data" / "purchases_log.csv"
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

CSV_FIELDS = [
    "found_at", "source", "date", "person", "role", "issuer_or_asset", "ticker",
    "amount", "url",
]


def _append_csv(row: dict) -> None:
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    new_file = not CSV_PATH.exists()
    with CSV_PATH.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new_file:
            w.writeheader()
        w.writerow(row)


def _days_to_scan(conn, source: str, lookback_days: int, max_new_days: int) -> list[dt.date]:
    """Which calendar days in the trailing window still need scanning.

    The old behaviour was a blind trailing window -- scan today and yesterday,
    every run. Under a once-a-day schedule that permanently loses a day whenever a
    run doesn't happen: a failed job, a long weekend, a laptop asleep at 08:00. The
    day is never revisited, and nothing says so. Recording what was actually
    scanned (db.scanned_days) means a gap backfills itself instead.

    Today is always included and never marked done -- its index is still being
    published, so it has to be re-scanned on later runs (cheap: already-seen
    accessions are skipped before any per-filing fetch). Older days are capped at
    `max_new_days` per run, newest first -- fresher days are worth more, and the
    backlog still drains completely because each run records what it scanned.
    """
    today = dt.date.today()
    done = db.scanned_days(conn, source)
    backlog = [
        today - dt.timedelta(days=i)
        for i in range(1, lookback_days)
        if (today - dt.timedelta(days=i)).isoformat() not in done
    ]
    backlog.sort()
    return backlog[-max_new_days:] + [today] if max_new_days > 0 else [today]


def _scan_day(conn, source: str, date: dt.date, today: dt.date, scan_fn) -> int | None:
    """Run one day's scan, record it as done, and keep a bad day from killing the run.

    SEC answers a date with no index in more than one way -- 403 for a weekend,
    holiday or an index not published yet, but 503 when it is throttling. The two
    must be handled differently: 403 means the day is genuinely empty and can be
    marked scanned, while 503 means try again later, so the day is deliberately left
    unmarked and the ledger brings it back on the next run. Returns None if the day
    could not be scanned.
    """
    try:
        found = scan_fn()
    except requests.RequestException as e:
        print(f"[{source}] {date.isoformat()} failed ({e}); leaving it unscanned to retry",
              file=sys.stderr)
        return None
    if date < today:
        db.mark_day_scanned(conn, source, date.isoformat(), found)
    return found


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


def run_sec_pass(conn, args, universe_index: universe.Universe | None, session) -> int:
    """Logs every new purchase to SQLite/CSV, and separately (no log/print/count)
    every matching sale to sec_sales for exit_signal.py. Does not touch Telegram --
    that only happens for signals, computed afterwards.

    Scans SEC's complete daily index rather than the 'latest ~100 filings'
    rolling-window feed -- SEC gets 700-1000+ Form 4s on a normal trading day, so a
    fixed-size window checked once a day would miss nearly all of them. Which days
    get scanned comes from the scanned_days ledger (see _days_to_scan), so a day
    missed while the bot was down is picked up later instead of lost.
    """
    seen = db.sec_seen_accessions(conn)
    new_count = 0
    today = dt.date.today()
    dates = _days_to_scan(conn, "SEC", args.sec_days, args.sec_max_backfill_days)
    print(f"[SEC] scanning {len(dates)} day(s): {', '.join(d.isoformat() for d in dates)}")
    for date in dates:
        # Only a day fully in the past is complete; today's index is still being
        # added to, so _scan_day leaves it unmarked and it stays in tomorrow's scan.
        def scan(date=date):
            seen_here = 0
            for accession, txns in sec_edgar.scan_daily_index(date, seen, session=session):
                seen_here += 1
                nonlocal new_count
                new_count += _handle_sec_filing(conn, args, universe_index, session, accession, txns)
            return seen_here
        _scan_day(conn, "SEC", date, today, scan)
    return new_count


def _handle_sec_filing(conn, args, universe_index, session, accession, txns) -> int:
    """Store one filing's transactions; returns how many new purchases it logged."""
    new_count = 0
    for p in txns:
        if universe_index is not None and not (universe_index.has_cik(p.issuer_cik) or universe_index.has_ticker(p.ticker)):
            continue
        if (p.value or 0) < args.min_value:
            continue

        if p.txn_code == "S":
            db.save_sec_sale(conn, p)
            continue

        if args.require_top_insider and not insider_score.is_top_insider(conn, p.owner_cik, p.owner_name, session=session):
            continue

        if db.save_sec_purchase(conn, p):
            new_count += 1
            print(telegram_notify.format_sec_line(p, None))
            _append_csv({
                "found_at": dt.datetime.now().isoformat(timespec="seconds"),
                "source": "SEC Form 4",
                "date": p.transaction_date,
                "person": p.owner_name,
                "role": p.officer_title or "",
                "issuer_or_asset": p.issuer_name,
                "ticker": p.ticker or "",
                "amount": p.value or "",
                "url": p.source_url,
            })
    db.mark_sec_accession_seen(conn, accession)
    conn.commit()
    return new_count


def run_stake_pass(conn, args, session, cik_lookup) -> int:
    """Store Schedule 13D/G beneficial-ownership positions. Returns how many new
    reporting-person rows were stored.

    These come out of the same daily index Form 4 does, so scanning them costs no
    extra index requests -- only the per-filing document fetches. They get their own
    scanned-days ledger because they're scanned independently of the Form 4 pass.
    """
    seen = db.sec_stake_seen_accessions(conn)
    new_count = 0
    today = dt.date.today()
    wanted = _sec_forms(args)
    dates = _days_to_scan(conn, "SEC13DG", args.sec_days, args.sec_max_backfill_days)
    print(f"[13D/G] scanning {len(dates)} day(s): {', '.join(d.isoformat() for d in dates)}")
    for date in dates:
        def scan(date=date):
            found = 0
            for accession, filings in sec_13dg.scan_daily_index(date, seen, session, cik_lookup=cik_lookup):
                found += 1
                for f in filings:
                    if "13D" not in wanted and f.form_type.startswith("SCHEDULE 13D"):
                        continue
                    if "13G" not in wanted and f.form_type.startswith("SCHEDULE 13G"):
                        continue
                    if db.save_sec_stake(conn, f):
                        nonlocal new_count
                        new_count += 1
                        print(telegram_notify.format_stake_line(f))
                db.mark_sec_stake_seen(conn, accession)
                seen.add(accession)
                conn.commit()
            return found
        _scan_day(conn, "SEC13DG", date, today, scan)
    return new_count


def run_144_pass(conn, args, session, cik_lookup) -> int:
    """Store Form 144 notices of intended sales. Returns how many new rows.

    Not part of the "purchases" feed at all -- these only feed exit signals, where
    they surface an unwinding cluster before the Form 4 sales that confirm it.
    """
    seen = db.sec_144_seen_accessions(conn)
    new_count = 0
    today = dt.date.today()
    dates = _days_to_scan(conn, "SEC144", args.sec_days, args.sec_max_backfill_days)
    print(f"[144] scanning {len(dates)} day(s): {', '.join(d.isoformat() for d in dates)}")
    for date in dates:
        def scan(date=date):
            found = 0
            for accession, sales in sec_144.scan_daily_index(date, seen, session, cik_lookup=cik_lookup):
                found += 1
                for sale in sales:
                    if (sale.market_value or 0) < args.min_value:
                        continue
                    if db.save_sec_proposed_sale(conn, sale):
                        nonlocal new_count
                        new_count += 1
                        print(telegram_notify.format_144_line(sale))
                db.mark_sec_144_seen(conn, accession)
                seen.add(accession)
                conn.commit()
            return found
        _scan_day(conn, "SEC144", date, today, scan)
    return new_count


def _sec_forms(args) -> set[str]:
    """Which SEC form types this run should scan, from --forms."""
    return {f.strip().upper() for f in args.forms.split(",") if f.strip()}


def run_house_pass(conn, args, universe_index: universe.Universe | None, years: list[int]) -> int:
    """Logs every new S&P100/Nasdaq100 *purchase* (txn_type 'P') to SQLite/CSV.
    Sales/exchanges are saved to the same table (for exit_signal.py) but not
    logged/printed/counted here -- they aren't "purchases" for this feed.

    Uses args.house_min_value rather than the shared args.min_value: House discloses
    a bracket, never an amount, and the filter runs against the bracket's *lower*
    bound. At the $50k shared default that silently dropped the entire
    "$15,001 - $50,000" bracket -- the single most common one members file -- which
    also starved congress_score's ranking pool of everyone who never traded bigger.
    """
    seen = db.house_seen_doc_ids(conn)
    new_count = 0
    for year in years:
        try:
            scan = house_ptr.scan_new_ptrs(year, seen)
        except Exception as e:
            print(f"[HOUSE] failed to fetch {year} index: {e}", file=sys.stderr)
            continue
        for doc_id, txns in scan:
            for t in txns:
                if universe_index is not None and not universe_index.has_ticker(t.ticker):
                    continue
                if cluster.parse_amount_low(t.amount_range) < args.house_min_value:
                    continue
                is_new = db.save_house_purchase(conn, t)
                if is_new and t.txn_type == "P":
                    new_count += 1
                    print(telegram_notify.format_house_line(t))
                    _append_csv({
                        "found_at": dt.datetime.now().isoformat(timespec="seconds"),
                        "source": "House PTR",
                        "date": t.txn_date,
                        "person": t.member_name,
                        "role": t.owner or "self",
                        "issuer_or_asset": t.asset,
                        "ticker": t.ticker or "",
                        "amount": t.amount_range,
                        "url": t.source_url,
                    })
            db.mark_house_doc_seen(conn, doc_id)
            seen.add(doc_id)
            conn.commit()
    return new_count


def run_bafin_pass(conn, args) -> int:
    """Logs every new BaFin (Germany) Directors' Dealings *purchase* to SQLite/CSV.
    Sales are saved too (for exit_signal.py) but not logged/printed/counted here.
    No universe filter -- BaFin only covers German-regulated issuers to begin
    with. min_value is compared directly against EUR (no FX conversion)."""
    seen = db.bafin_seen_ids(conn)
    new_count = 0
    letters = args.bafin_letters.split(",") if args.bafin_letters else None
    scan = bafin.scan_new_filings(seen, letters=letters, max_pages_per_letter=args.bafin_max_pages)
    for meldepflichtiger_id, results in scan:
        for f, detail in results:
            if (detail.volume_eur or 0) < args.min_value:
                continue
            source_url = f.detail_url or f"{bafin.BASE}/ergebnisListe.do?cmd=loadEmittentenAction&meldepflichtigerId={meldepflichtiger_id}"
            is_new = db.save_bafin_purchase(conn, f, detail, source_url)
            if is_new and f.txn_type == "P":
                new_count += 1
                print(telegram_notify.format_bafin_line(f, detail, source_url))
                _append_csv({
                    "found_at": dt.datetime.now().isoformat(timespec="seconds"),
                    "source": "BaFin",
                    "date": f.txn_date,
                    "person": f.notifier_name,
                    "role": f.position,
                    "issuer_or_asset": f.issuer_name,
                    "ticker": f.isin,
                    "amount": detail.volume_eur or "",
                    "url": source_url,
                })
        db.mark_bafin_id_seen(conn, meldepflichtiger_id)
        seen.add(meldepflichtiger_id)
        conn.commit()
    return new_count


def run_norway_pass(conn, args) -> int:
    """Logs every new Oslo Børs (Norway) Managers' Transaction *purchase* to
    SQLite/CSV. Sales are saved too (for exit_signal.py) but not logged/printed/
    counted here. No universe filter -- Newsweb only covers Oslo Børs/Euronext
    Oslo-listed issuers to begin with. Amounts are mostly NOK (an order of
    magnitude off the $/€ scale --min-value is calibrated for), so they're
    converted to EUR via fx.py and compared against min_value the same way
    BaFin's already-EUR volumes are."""
    seen = db.norway_seen_ids(conn)
    new_count = 0
    today = dt.date.today()
    # Newsweb takes a date range in one request, so unlike SEC there's no per-day
    # loop -- the ledger just decides how far back the range starts. Same purpose
    # though: a day missed while the bot was down gets picked up instead of lost.
    dates = _days_to_scan(conn, "NORWAY", args.norway_days, args.norway_days)
    from_date = min(dates)
    print(f"[NORWAY] scanning {from_date.isoformat()}..{today.isoformat()}")
    session = norway.new_session()
    scan = norway.scan_new_filings(from_date, today, seen, session=session)
    for message_id, txn in scan:
        if txn is not None:
            value = txn["shares"] * txn["price"]
            txn["value"] = value
            if fx.to_eur(value, txn["currency"], conn) >= args.min_value:
                is_new = db.save_norway_purchase(conn, txn)
                if is_new and txn["txn_type"] == "P":
                    new_count += 1
                    print(telegram_notify.format_norway_line(txn))
                    _append_csv({
                        "found_at": dt.datetime.now().isoformat(timespec="seconds"),
                        "source": "Oslo Børs",
                        "date": txn["txn_date"],
                        "person": txn["person"],
                        "role": "",
                        "issuer_or_asset": txn["issuer_name"],
                        "ticker": txn["ticker"] or "",
                        "amount": f"{value:,.0f} {txn['currency']}",
                        "url": txn["source_url"],
                    })
        db.mark_norway_id_seen(conn, message_id)
        seen.add(message_id)
        conn.commit()
    for d in dates:
        # filings=None: this source is scanned as a range, so there's no honest
        # per-day count to record here.
        if d < today:
            db.mark_day_scanned(conn, "NORWAY", d.isoformat(), None)
    return new_count


def run_sweden_pass(conn, args) -> int:
    """Logs every new Finansinspektionen (Sweden) insider *purchase* to SQLite/CSV.
    Sales are saved too (for exit signals) but not logged/printed/counted here. No
    universe filter -- FI only covers Swedish-regulated issuers to begin with.
    Amounts are mostly SEK, so they're converted to EUR via fx.py before being
    compared against min_value, the same way Norway's are."""
    seen = db.sweden_seen_ids(conn)
    new_count = 0
    today = dt.date.today()
    dates = _days_to_scan(conn, "SWEDEN", args.sweden_days, args.sweden_days)
    from_date = min(dates)
    print(f"[SE] scanning {from_date.isoformat()}..{today.isoformat()}")
    session = sweden.new_session()
    for row_id, txn in sweden.scan_new_filings(from_date, today, seen, session=session):
        if txn is not None and fx.to_eur(txn["value"], txn["currency"], conn) >= args.min_value:
            is_new = db.save_sweden_purchase(conn, txn)
            if is_new and txn["txn_type"] == "P":
                new_count += 1
                print(telegram_notify.format_sweden_line(txn))
                _append_csv({
                    "found_at": dt.datetime.now().isoformat(timespec="seconds"),
                    "source": "Finansinspektionen",
                    "date": txn["txn_date"],
                    "person": txn["person"],
                    "role": txn["position"],
                    "issuer_or_asset": txn["issuer_name"],
                    "ticker": txn["isin"],
                    "amount": f"{txn['value']:,.0f} {txn['currency']}",
                    "url": txn["source_url"],
                })
        db.mark_sweden_id_seen(conn, row_id)
        seen.add(row_id)
        conn.commit()
    for d in dates:
        # filings=None: scanned as a date range, so there's no honest per-day count.
        if d < today:
            db.mark_day_scanned(conn, "SWEDEN", d.isoformat(), None)
    return new_count


def run_senate_pass(conn, args) -> int:
    """Logs new Senate PTR purchases. Opt-in via --senate.

    Off by default because efdsearch.senate.gov currently refuses requests from
    this machine at the network edge (HTTP 403) -- see the note at the top of
    senate_efd.py. The failure is reported rather than swallowed: if you asked for
    this source and it can't be reached, that is worth saying.

    Uses --house-min-value: the Senate discloses the same amount brackets the House
    does, so the same lower-bound threshold applies.
    """
    seen = db.senate_seen_report_ids(conn)
    new_count = 0
    today = dt.date.today()
    start = today - dt.timedelta(days=args.senate_days)
    print(f"[SENATE] scanning reports filed {start.isoformat()}..{today.isoformat()}")
    for report_id, txns in senate_efd.scan_new_ptrs(start, today, seen):
        for t in txns:
            if cluster.parse_amount_low(t.amount_range) < args.house_min_value:
                continue
            if db.save_senate_purchase(conn, t) and t.txn_type == "P":
                new_count += 1
                print(telegram_notify.format_senate_line(t))
                _append_csv({
                    "found_at": dt.datetime.now().isoformat(timespec="seconds"),
                    "source": "Senate PTR",
                    "date": t.txn_date,
                    "person": t.member_name,
                    "role": t.owner or "self",
                    "issuer_or_asset": t.asset,
                    "ticker": t.ticker or "",
                    "amount": t.amount_range,
                    "url": t.source_url,
                })
        db.mark_senate_report_seen(conn, report_id)
        seen.add(report_id)
        conn.commit()
    return new_count


def _which_sources(args) -> dict:
    """Which sources are enabled for this run, honoring the mutually-exclusive
    *_only flags (any one of them switches off all the others) and each source's
    individual --no-* skip flag. Shared by main()'s poll loop and
    run_cluster_pass() so the two never drift apart."""
    only_set = (args.sec_only or args.house_only or args.bafin_only or args.norway_only
                or args.sweden_only)
    return {
        "sec": args.sec_only or not only_set,
        "house": args.house_only or not only_set,
        "bafin": (args.bafin_only or not only_set) and not args.no_bafin,
        "norway": (args.norway_only or not only_set) and not args.no_norway,
        "sweden": (args.sweden_only or not only_set) and not args.no_sweden,
        # Never implied by "all sources": it has to be asked for explicitly, because
        # it is currently unreachable from here (see senate_efd.py).
        "senate": args.senate,
    }


def run_cluster_pass(conn, args) -> list:
    """Recomputes both signal types from everything currently in SQLite (not just
    this run's new rows -- a cluster/exit accumulates across multiple daily runs)
    and returns only the ones that grew past what was last alerted:
      - cluster buy signals: a ticker bought by multiple distinct people at once
      - exit signals: people who bought together later selling together
    """
    sources = _which_sources(args)

    signals = []
    if sources["sec"]:
        signals += cluster.find_sec_clusters(conn, min_value=args.min_cluster_value,
                                              solo_threshold=args.solo_threshold,
                                              include_derivatives=args.include_derivatives,
                                              insiders_only=args.insiders_only,
                                              include_10b5_1=args.include_10b5_1)
    if sources["house"]:
        signals += cluster.find_house_clusters(conn, min_value=args.min_cluster_value, solo_threshold=args.solo_threshold)
    if sources["bafin"]:
        signals += cluster.find_bafin_clusters(conn, min_value=args.min_cluster_value, solo_threshold=args.solo_threshold)
    if sources["norway"]:
        signals += cluster.find_norway_clusters(conn, min_value=args.min_cluster_value, solo_threshold=args.solo_threshold)
    if sources["senate"]:
        signals += cluster.find_senate_clusters(conn, min_value=args.min_cluster_value,
                                                 solo_threshold=args.solo_threshold)
    if sources["sweden"]:
        signals += cluster.find_sweden_clusters(conn, min_value=args.min_cluster_value,
                                                 solo_threshold=args.solo_threshold,
                                                 include_share_programs=args.include_share_programs,
                                                 include_derivatives=args.include_derivatives)
    if sources["sec"] and ("13D" in _sec_forms(args) or "13G" in _sec_forms(args)):
        signals += cluster.find_stake_signals(conn, min_percent=args.stake_min_percent,
                                               min_increase_pp=args.stake_min_increase,
                                               activist_only=args.activist_only,
                                               new_positions_only=args.new_positions_only)
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
    # Attach company context (size, liquidity) and rank. Everything above this point
    # is pure SQL; this is the only step that reaches the network.
    signals = cluster.enrich_signals(conn, signals)

    if args.min_score:
        signals = [s for s in signals if getattr(s, "score", 0) >= args.min_score]
    if args.min_liquidity:
        # Keep signals whose liquidity is unknown: unknown is not the same as low,
        # and silently dropping every non-US name would be worse than the noise.
        signals = [s for s in signals
                   if getattr(s, "avg_daily_value", None) is None
                   or s.avg_daily_value >= args.min_liquidity]

    if not args.no_market_context:
        # Deliberately last: runs only on whatever survived every filter above,
        # so a daily run pays for this once per surviving signal, not once per
        # signal the finders produced.
        signals = tradingview.annotate_signals(signals)

    for s in signals:
        print(telegram_notify.format_any_signal(s))
    return signals


def _signal_features(sig) -> dict:
    """Flatten a signal into the row signal_journal stores.

    Recorded at emission with the values it actually had, so backtest.py measures
    the signals that were sent rather than re-deriving them from today's code and
    today's thresholds -- which would quietly make every measurement agree with
    whatever the constants currently are.
    """
    import json
    if hasattr(sig, "percent"):          # StakeSignal
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
    }


def _commit_signals(conn, signals) -> None:
    """Record signals as alerted, so they don't re-fire until they actually grow --
    and journal them at the same moment.

    Journalling belongs here rather than where signals are computed. A signal that
    was computed but not sent (Telegram down, --no-telegram, over the item limit
    before that path also committed) still returns on the next run, so recording it
    at computation time would enter the same signal into the journal repeatedly and
    quietly inflate every backtest group. One row per signal actually sent.
    """
    for s in signals:
        db.journal_signal(conn, _signal_features(s))
        if hasattr(s, "seller_count"):
            cluster.commit_exit_alert(conn, s)
        elif hasattr(s, "percent"):
            cluster.commit_stake_alert(conn, s)
        else:
            cluster.commit_alert(conn, s)


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


def main():
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
                          "selling it together -- see EXIT_* constants in cluster.py)")
    ap.add_argument("--no-telegram", action="store_true", help="skip sending Telegram alerts")
    ap.add_argument("--telegram-item-limit", type=int, default=40,
                     help="if a single run finds more new cluster signals than this (basically never, "
                          "but a safety net for e.g. a DB reset), send one short warning instead of "
                          "the full list, to avoid flooding the chat (default 40)")
    args = ap.parse_args()

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

        signals = run_cluster_pass(conn, args)

        if signals and not args.no_telegram:
            if len(signals) > args.telegram_item_limit:
                # Too many to send as a digest -- almost always a first population
                # of a new source or a reset database, not a remarkable day. Send one
                # short warning instead of flooding the chat, and then commit them
                # anyway: leaving them uncommitted would reproduce the same flood on
                # every subsequent run, warning about the same signals forever.
                sent = telegram_notify.send_text(
                    f"⚠️ Найдено {len(signals)} сигналов за один прогон — это выше лимита "
                    f"({args.telegram_item_limit}), обычно так выглядит первое наполнение "
                    f"источника или сброс базы, а не примечательный день. Полный список не "
                    f"отправляю, он в data/disclosures.db и в меню (пункт 3). Отмечаю их как "
                    f"отправленные, иначе это же предупреждение будет приходить каждый прогон."
                )
                if sent:
                    _commit_signals(conn, signals)
            else:
                # Only mark these as alerted if the send actually went through --
                # a signal re-fires only once its buyer count grows past the last
                # alerted one, so committing after a failed send (network blip,
                # bad token, Telegram outage, or credentials simply not set) would
                # silently drop the alert for good. Committing only on success
                # means the next run just re-sends it.
                sent = telegram_notify.send_text(telegram_notify.format_signals_digest(signals))
                if sent:
                    _commit_signals(conn, signals)
                else:
                    print(f"[telegram] send failed -- leaving {len(signals)} signal(s) uncommitted, "
                          f"they'll be retried on the next run", file=sys.stderr)

        # The pass got all the way through: record it. run_healthcheck reads this,
        # and it's the only evidence that distinguishes "nothing to report" from
        # "hasn't run in ten days".
        db.save_cached_value(conn, "last_successful_run", time.time())

        print(f"--- poll finished, {total_new} new purchase(s), {len(signals)} signal(s), "
              f"took {time.time()-started:.1f}s ---")

        if args.once:
            break
        elapsed = time.time() - started
        time.sleep(max(1.0, args.interval - elapsed))


if __name__ == "__main__":
    main()
