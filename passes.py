"""One pass per disclosure source: fetch what's new, store it, print and CSV-log
the purchases worth seeing. Split out of bot.py, which keeps the CLI, the signal
pass and the scheduling around these."""
from __future__ import annotations

import csv
import datetime as dt
import sys
from pathlib import Path

import requests

import bafin
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
import sweden
import telegram_notify
import universe
CSV_PATH = Path(__file__).parent / "data" / "purchases_log.csv"

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
                    # Counted either way: a PTR full of T-bills still proves the
                    # parser is alive, which is what the liveness check reads.
                    new_count += 1
                    if not house_ptr.is_feed_worthy(t.asset):
                        continue
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
