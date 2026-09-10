"""Fetch and parse BaFin's Directors' Dealings database (Art. 19 MAR notifications
for German-regulated issuers) -- the closest German equivalent to SEC Form 4.

No flat "recent activity" feed exists here: the search UI only supports browsing by
the first letter of either the issuer name or the notifying person's name. We
browse by notifying-person letter (A-Z, plus "Sonstige" for everything else),
paginating each letter's results, then drill two levels deeper per entry:

  1. sucheForm.do?meldepflichtigerName={letter}[&d-XXXXX-p={page}]
     -> one row per (notifier, transaction date), linking to a meldepflichtigerId
  2. ergebnisListe.do?cmd=loadEmittentenAction&meldepflichtigerId={id}
     -> per-issuer rows: issuer name + BaFin-ID, ISIN, notifier name, position,
        instrument type, Kauf/Verkauf (buy/sell), date, venue -- and a link
        carrying the meldungId needed for the detail page
  3. transaktionListe.do?cmd=loadTransaktionenAction&emittentBafinId={bafinId}&meldungId={id}
     -> aggregated price + volume in EUR (what step 2 doesn't have)

BaFin requires reporting once a notifier's transactions exceed EUR 50,000 in a
calendar year (raised from EUR 20,000 as of 2026-01-01) -- conveniently close to
this project's own $50k floor for SEC/House.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

BASE = "https://portal.mvp.bafin.de/database/DealingsInfo"
LETTERS = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ") + ["Sonstige"]
REQUEST_DELAY = 0.3

TXN_TYPE_MAP = {"Kauf": "P", "Verkauf": "S"}  # matches this project's P/S convention

_AMOUNT_RE = re.compile(r"([\d.,]+)\s*EUR")
_ID_RE = re.compile(r"meldepflichtigerId=(\d+)")
_PAGE_PARAM_RE = re.compile(r"(d-\d+-p)=(\d+)")


@dataclass
class Filing:
    meldepflichtiger_id: str
    notifier_name: str
    position: str
    issuer_name: str
    issuer_bafin_id: str
    isin: str
    instrument_type: str
    txn_type: str  # "P" or "S"
    txn_date: str  # DD.MM.YYYY, as BaFin gives it
    venue: str
    meldung_id: str | None
    detail_url: str | None


@dataclass
class TransactionDetail:
    price_eur: float | None
    volume_eur: float | None


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "disclosure-bot research contact@example.com"})
    return s


def _get(session: requests.Session, url: str) -> str:
    time.sleep(REQUEST_DELAY)
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    # BaFin's responses don't declare a charset in Content-Type, so requests
    # defaults to guessing ISO-8859-1 (per the old HTTP spec default) even though
    # the actual content is UTF-8 -- German umlauts (ö/ü/ä) come out as mojibake
    # ("WÃ¶hrmann") unless we force the encoding we know is correct.
    resp.encoding = "utf-8"
    return resp.text


def _parse_eur(text: str) -> float | None:
    m = _AMOUNT_RE.search(text or "")
    if not m:
        return None
    # German number format: "24.038,77" -> 24038.77
    return float(m.group(1).replace(".", "").replace(",", "."))


def list_notifier_ids(session: requests.Session, letter: str, page: int = 1) -> tuple[list[str], int]:
    """Returns (meldepflichtiger_ids on this page, total_pages) for one letter."""
    url = f"{BASE}/sucheForm.do?meldepflichtigerName={letter}"
    html = _get(session, url) if page == 1 else None
    if page > 1:
        # Discover the displaytag pagination param name from a page-1 fetch first.
        html = _get(session, url)
    soup = BeautifulSoup(html, "html.parser")

    param_match = _PAGE_PARAM_RE.search(html)
    total_pages = 1
    if param_match:
        param_name = param_match.group(1)
        page_numbers = [int(n) for n in re.findall(rf"{re.escape(param_name)}=(\d+)", html)]
        if page_numbers:
            total_pages = max(page_numbers)
        if page > 1:
            html = _get(session, f"{url}&{param_name}={page}")
            soup = BeautifulSoup(html, "html.parser")

    ids = []
    for a in soup.find_all("a", href=True):
        m = _ID_RE.search(a["href"])
        if m:
            ids.append(m.group(1))
    return ids, total_pages


def list_filings_for_notifier(session: requests.Session, meldepflichtiger_id: str) -> list[Filing]:
    """Level 2: every (issuer, transaction) row for one notifying person/entity."""
    url = f"{BASE}/ergebnisListe.do?cmd=loadEmittentenAction&meldepflichtigerId={meldepflichtiger_id}"
    return parse_notifier_page(_get(session, url), meldepflichtiger_id)


def parse_notifier_page(html: str, meldepflichtiger_id: str) -> list[Filing]:
    """Parse a level-2 notifier page. Split out from the fetch so it can be tested
    against a saved page: the table is read positionally by column index, so a
    redesign that inserts or reorders a column silently changes what every field
    means rather than raising."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        return []

    filings = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 9:
            continue
        cells = [td.get_text(strip=True) for td in tds]
        issuer_name, bafin_id, isin, notifier, position, instrument, txn_de, txn_date, venue = cells[:9]
        link = tds[0].find("a")
        meldung_id = detail_url = None
        if link and link.get("href"):
            href = link["href"]
            m = re.search(r"meldungId=(\d+)", href)
            if m:
                meldung_id = m.group(1)
                detail_url = href if href.startswith("http") else f"{BASE}/{href}"
        filings.append(Filing(
            meldepflichtiger_id=meldepflichtiger_id,
            notifier_name=notifier,
            position=position,
            issuer_name=issuer_name,
            issuer_bafin_id=bafin_id,
            isin=isin,
            instrument_type=instrument,
            txn_type=TXN_TYPE_MAP.get(txn_de, txn_de),
            txn_date=txn_date,
            venue=venue,
            meldung_id=meldung_id,
            detail_url=detail_url,
        ))
    return filings


def fetch_transaction_detail(session: requests.Session, filing: Filing) -> TransactionDetail:
    """Level 3: aggregated price + volume in EUR for one filing."""
    if not filing.detail_url:
        return TransactionDetail(None, None)
    return parse_transaction_detail(_get(session, filing.detail_url))


def parse_transaction_detail(html: str) -> TransactionDetail:
    """Parse a level-3 detail page. Split out from the fetch for the same reason as
    parse_notifier_page -- these numbers come out of free German text
    ("Aggregiertes Volumen: 1.234,56 EUR"), so the format is worth pinning in a
    test."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n")
    price = volume = None
    m = re.search(r"Preis:\s*\n?\s*([\d.,]+)\s*EUR", text)
    if m:
        price = float(m.group(1).replace(".", "").replace(",", "."))
    m = re.search(r"Aggregiertes Volumen:\s*\n?\s*([\d.,]+)\s*EUR", text)
    if m:
        volume = float(m.group(1).replace(".", "").replace(",", "."))
    return TransactionDetail(price_eur=price, volume_eur=volume)


def scan_letter(session: requests.Session, letter: str, seen_meldepflichtiger_ids: set[str],
                 max_pages: int | None = None):
    """Yields (meldepflichtiger_id, list[(Filing, TransactionDetail)]) for every
    not-yet-seen notifier under this letter. Only fetches level 2/3 for IDs not
    already in seen_meldepflichtiger_ids."""
    session = session or new_session()
    ids, total_pages = list_notifier_ids(session, letter, page=1)
    all_ids = list(ids)
    pages_to_fetch = total_pages if max_pages is None else min(total_pages, max_pages)
    for page in range(2, pages_to_fetch + 1):
        more_ids, _ = list_notifier_ids(session, letter, page=page)
        all_ids.extend(more_ids)

    for mid in all_ids:
        if mid in seen_meldepflichtiger_ids:
            continue
        filings = list_filings_for_notifier(session, mid)
        results = []
        for f in filings:
            detail = fetch_transaction_detail(session, f)
            results.append((f, detail))
        yield mid, results


def scan_new_filings(seen_meldepflichtiger_ids: set[str], letters: list[str] | None = None,
                      max_pages_per_letter: int | None = None, session: requests.Session | None = None):
    """Yields (meldepflichtiger_id, list[(Filing, TransactionDetail)]) across all
    (or the given) letters -- top-level entry point for bot.py, mirroring
    sec_edgar.scan_daily_index / house_ptr.scan_new_ptrs.

    There's no way to ask BaFin for "just what's new since last time" the way
    SEC's daily index or House's yearly zip allow -- every run re-lists every
    letter's notifiers (cheap: 20 rows/page) and only drills into (expensive:
    2 more requests) the ones not already in seen_meldepflichtiger_ids.
    """
    session = session or new_session()
    for letter in (letters or LETTERS):
        yield from scan_letter(session, letter, seen_meldepflichtiger_ids, max_pages=max_pages_per_letter)
