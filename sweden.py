"""Fetch Swedish insider transactions from Finansinspektionen's insider register
("Insynsregistret", marknadssok.fi.se) -- Sweden's equivalent of SEC Form 4, BaFin
Directors' Dealings and Oslo Børs Managers' Transactions.

Of the European sources this project has looked at, this is by far the easiest to
consume correctly: FI publishes a CSV export with no key, no login and no agreement
gate, filterable by transaction date, carrying price, volume, currency, instrument
type and role as *structured columns*. Compare BaFin (no recent-activity feed at
all, so it has to be crawled A-Z, ~30-40 minutes for a first pass) and Norway
(amounts live in free-text prose and have to be regexed back out).

Three quirks shape the code below:

  1. The export ignores its own `Page` parameter -- every page returns the same
     first EXPORT_ROW_CAP rows. Ask for a period containing more than that and the
     remainder is dropped with no error and nothing to notice it by, which is the
     exact failure this project keeps getting bitten by. So fetch_rows() walks the
     period in chunks and halves any chunk that comes back sitting on the cap.
  2. The CSV is UTF-16, semicolon-separated, with decimal commas ("1,196794").
  3. Amounts are mostly SEK but cross-listed issuers report in whatever the trade
     settled in -- CAD, CHF and EUR all appear within a single week. Conversion is
     left to fx.py at signal time, as with the other sources; nothing is converted
     on the way into the database.

FI also publishes something none of the other sources do: whether a transaction is
tied to a share-incentive programme. A CEO handed shares by a comp plan and a CEO
buying shares with their own money are different events and only the second is a
signal -- see the share_program flag on each parsed row.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import sys

import requests

BASE = "https://marknadssok.fi.se"
EXPORT_URL = BASE + "/Publiceringsklient/sv-SE/Search/Search"
# Human-readable page for the same register, used as each row's source_url. FI has
# no per-transaction permalink, so this points at the search UI rather than a
# specific filing.
SEARCH_PAGE = BASE + "/Publiceringsklient/sv-SE/Search/Search?SearchFunctionType=Insyn"

# The export silently truncates here regardless of Page. Treated as "the answer was
# cut off", never as "that's all there was".
EXPORT_ROW_CAP = 1000

# Days per request. ~40 rows/business day observed, so a week is comfortably clear
# of the cap while keeping the number of requests small for a long backfill.
DEFAULT_CHUNK_DAYS = 7

# Upper bound on a believable transaction value, in the filing's own currency.
#
# "Volym x Pris" is only a transaction value when Pris is a price per unit. For some
# instruments it isn't: swaps are filed with the notional in BOTH columns, so the
# product comes out at 1e16 SEK -- a quadrillion-krona "purchase" sitting at the top
# of the log. Rather than trying to enumerate which of FI's instrument types price
# per unit, anything past this bar is treated as unparseable and skipped, the same
# way norway.py discards a notification whose wording it can't size. The largest
# genuine Swedish insider transaction seen while building this was ~3.4bn SEK (a
# control block changing hands), so this leaves three orders of magnitude of room.
MAX_PLAUSIBLE_VALUE = 1e12

# "Karaktär" -- the transaction's nature. Everything else (Teckning/subscription,
# Tilldelning/allotment, Lösen/option exercise, gifts, inheritance) is stored under
# its own Swedish label and left out of P/S signals, the same way bafin.py keeps
# unmapped German labels rather than guessing at them.
TXN_TYPE_MAP = {"Förvärv": "P", "Avyttring": "S"}

# "Instrumenttyp". Only actual shares are counted toward money signals by default;
# options/warrants/bonds are priced off a strike or a face value rather than off
# what the share costs, the same reason SEC derivative rows are excluded.
SHARE_INSTRUMENTS = {"Aktie", "BTA (betald tecknad aktie)", "Depåbevis", "IDR"}

# Column headers, exactly as FI emits them.
C_PUBLISHED = "Publiceringsdatum"
C_ISSUER = "Emittent"
C_NOTIFIER = "Anmälningsskyldig"
C_PERSON = "Person i ledande ställning"
C_POSITION = "Befattning"
C_RELATED = "Närstående"
C_SHARE_PROGRAM = "Är kopplad till aktieprogram"
C_NATURE = "Karaktär"
C_INSTRUMENT_TYPE = "Instrumenttyp"
C_ISIN = "ISIN"
C_TXN_DATE = "Transaktionsdatum"
C_VOLUME = "Volym"
C_VOLUME_UNIT = "Volymsenhet"
C_PRICE = "Pris"
C_CURRENCY = "Valuta"
C_STATUS = "Status"


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "disclosure-bot research contact@example.com"})
    return s


def _parse_number(raw: str) -> float | None:
    """Swedish decimal comma: '560183,0' -> 560183.0, '1,196794' -> 1.196794."""
    text = (raw or "").strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


# FI's export carries no id column, so identity has to be derived. Two different
# ids are needed, because "one row in the export" and "one transaction" are not the
# same thing:
#
#   txn_key  identifies the *transaction* -- everything except when it was
#            published. A filing that is corrected is republished in full, and the
#            correction does NOT reliably supersede the original in the data: one
#            share purchase by Vin & Vind AB appears five times for the same day,
#            volume and price, of which THREE are marked "Reviderad" and TWO are
#            still marked "Aktuell". Summing what survives a status filter therefore
#            double-counts, and did: it turned a real ~€100M cluster into a €307M
#            one. Keyed this way, the latest publication simply replaces the earlier
#            one. (The cost is that two genuinely identical trades by the same
#            person on the same day at the same price collapse into one. That is
#            rare, and undercounting a duplicate-looking trade is much safer than
#            multiplying a real one.)
#
#   row_id   identifies the *publication*, so each row is only ever processed once
#            even though several of them may describe the same transaction.
def txn_key(row: dict) -> str:
    parts = [row.get(c, "") for c in
             (C_ISSUER, C_NOTIFIER, C_ISIN, C_TXN_DATE, C_VOLUME, C_PRICE, C_NATURE)]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def row_id(row: dict) -> str:
    return hashlib.sha1((row.get(C_PUBLISHED, "") + "|" + txn_key(row)).encode("utf-8")).hexdigest()


def parse_row(row: dict) -> dict | None:
    """Turn one CSV row into a transaction dict, or None if it can't be trusted.

    Rejected rather than guessed at: a volume that isn't a share count (FI can
    express some instruments as a nominal amount, which isn't shares x price), a
    missing ISIN (it's the grouping key, as with BaFin -- Sweden discloses no
    ticker), and a value past MAX_PLAUSIBLE_VALUE (see above -- swaps carry the
    notional in both the volume and the price column).
    """
    if (row.get(C_VOLUME_UNIT) or "").strip() not in ("Antal", ""):
        return None
    isin = (row.get(C_ISIN) or "").strip()
    if not isin:
        return None

    volume = _parse_number(row.get(C_VOLUME, ""))
    price = _parse_number(row.get(C_PRICE, ""))
    if volume is None or price is None:
        return None
    if volume * price > MAX_PLAUSIBLE_VALUE:
        return None

    nature = (row.get(C_NATURE) or "").strip()
    txn_date = (row.get(C_TXN_DATE) or "").strip()[:10]  # "2026-09-03 00:00:00" -> ISO
    person = (row.get(C_NOTIFIER) or "").strip() or (row.get(C_PERSON) or "").strip()
    if not person or not txn_date:
        return None

    return {
        "row_id": row_id(row),
        "txn_key": txn_key(row),
        "published": (row.get(C_PUBLISHED) or "").strip(),
        "person": person,
        "pdmr": (row.get(C_PERSON) or "").strip(),
        "position": (row.get(C_POSITION) or "").strip(),
        "issuer_name": (row.get(C_ISSUER) or "").strip(),
        "isin": isin,
        "instrument_type": (row.get(C_INSTRUMENT_TYPE) or "").strip(),
        "txn_type": TXN_TYPE_MAP.get(nature, nature),
        "txn_date": txn_date,
        "shares": volume,
        "price": price,
        "currency": (row.get(C_CURRENCY) or "SEK").strip().upper(),
        "value": volume * price,
        # "Ja" when the transaction comes out of a share-incentive programme --
        # i.e. compensation, not a decision to buy. Kept as a flag rather than
        # dropped, so the raw log still matches the register.
        "share_program": (row.get(C_SHARE_PROGRAM) or "").strip().lower() == "ja",
        "related_party": (row.get(C_RELATED) or "").strip().lower() == "ja",
        # "Aktuell" is the live version of a filing; "Reviderad"/corrected rows
        # also appear and are excluded from signals downstream.
        "status": (row.get(C_STATUS) or "").strip(),
        "source_url": SEARCH_PAGE,
    }


def _fetch_window(from_date: dt.date, to_date: dt.date, session: requests.Session) -> list[dict]:
    """One export request. Returns raw CSV rows as dicts, unparsed."""
    resp = session.get(
        EXPORT_URL,
        params={
            "SearchFunctionType": "Insyn",
            "Transaktionsdatum.From": from_date.isoformat(),
            "Transaktionsdatum.To": to_date.isoformat(),
            "button": "export",
            "Page": 1,
        },
        timeout=60,
    )
    resp.raise_for_status()
    # UTF-16 with a BOM; requests guesses the encoding wrong, so decode explicitly.
    text = resp.content.decode("utf-16", errors="replace")
    return [r for r in csv.DictReader(io.StringIO(text), delimiter=";") if any(r.values())]


def fetch_rows(from_date: dt.date, to_date: dt.date, session: requests.Session | None = None,
                chunk_days: int = DEFAULT_CHUNK_DAYS) -> list[dict]:
    """Every row in [from_date, to_date], working around the export's row cap.

    The cap is silent -- a truncated answer looks exactly like a complete one -- so
    any window that comes back sitting on it is split in half and re-fetched. A
    single day that still hits the cap can't be split further; that's reported
    loudly rather than quietly returning partial data.
    """
    session = session or new_session()
    out: list[dict] = []
    seen_ids: set[str] = set()

    windows = []
    start = from_date
    while start <= to_date:
        end = min(start + dt.timedelta(days=chunk_days - 1), to_date)
        windows.append((start, end))
        start = end + dt.timedelta(days=1)

    while windows:
        w_start, w_end = windows.pop(0)
        rows = _fetch_window(w_start, w_end, session)
        if len(rows) >= EXPORT_ROW_CAP:
            if w_start == w_end:
                print(f"[SE] {w_start} alone returned the {EXPORT_ROW_CAP}-row export cap; "
                      f"that day is truncated and cannot be split further", file=sys.stderr)
            else:
                mid = w_start + (w_end - w_start) // 2
                windows.insert(0, (mid + dt.timedelta(days=1), w_end))
                windows.insert(0, (w_start, mid))
                continue
        for r in rows:
            rid = row_id(r)
            if rid not in seen_ids:      # overlapping halves can repeat a row
                seen_ids.add(rid)
                out.append(r)
    return out


def scan_new_filings(from_date: dt.date, to_date: dt.date, seen_ids: set[str],
                      session: requests.Session | None = None):
    """Yield (row_id, txn_dict_or_None) for every not-yet-seen transaction in the
    period. Every row looked at is yielded exactly once even when it can't be
    parsed, so callers can mark it seen and never re-examine it -- the same
    contract as the other sources' scan_new_filings.
    """
    session = session or new_session()
    for row in fetch_rows(from_date, to_date, session=session):
        rid = row_id(row)
        if rid in seen_ids:
            continue
        yield rid, parse_row(row)
