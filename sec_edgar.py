"""Fetch and parse SEC EDGAR Form 4 insider transactions (open-market purchases).

Two ways to find new filings:
  1. scan_new_filings(): the EDGAR "latest filings" Atom feed -- a ROLLING WINDOW
     of only the most recent ~100 filings company-wide (all form types mixed in,
     Form 4 filtered client-side). Cheap, near-real-time, but if you only check it
     once a day you miss almost everything: SEC gets 700-1000+ Form 4s on a normal
     trading day, so a fixed ~100-item window from one moment in time is a tiny,
     effectively random sliver of the day. Fine for frequent (e.g. every 15-30 min)
     polling, useless for a once-a-day job.
  2. scan_daily_index(): SEC's daily index file, which lists EVERY filing of every
     type for one calendar date -- complete coverage of that day, no rolling-window
     gap. This is what the once-a-day bot run actually uses.

Either way, for each new accession number we fetch the filing's index.json to find
the raw ownership XML document (usually "primary_doc.xml"), then parse it for
non-derivative and derivative transactions with transactionCode "P" (open market
purchase) or "S" (open market sale) -- sales are kept only so exit_signal.py can
spot a buy-cluster later unwinding, not for the main purchase-tracking pipeline.

SEC's fair-access policy requires a descriptive User-Agent with contact info and a
soft rate limit of <=10 req/s across www.sec.gov + efts.sec.gov. Set SEC_USER_AGENT
to "YourApp your-email@example.com" before running for real use.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import time
from dataclasses import dataclass
from xml.etree import ElementTree as ET

import requests

CURRENT_FEED_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&type=4&company=&dateb=&owner=include&count={count}&output=atom"
)
DAILY_INDEX_URL = "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{quarter}/form.{yyyymmdd}.idx"
INDEX_JSON_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/index.json"
DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{doc}"

REQUEST_DELAY = 0.15  # stay comfortably under SEC's 10 req/s

# Daily-index lines look like (fixed-ish columns, but company names can contain
# spaces, so anchor on the numeric CIK / 8-digit date / trailing file path instead):
# "4                10x Genomics, Inc.                     1770787   20260828   edgar/data/1770787/0001610717-26-000393.txt"
#
# The form types this project reads, all of which arrive in the same daily index
# file -- so 144s and 13D/Gs cost no extra index requests, only the per-filing
# document fetches. A normal trading day carries roughly 900 Form 4s, 300 Form
# 144s and 200 Schedule 13D/Gs. Listed explicitly rather than matched loosely, so a
# form nobody wrote a parser for can't slip through.
FORM_4 = ("4", "4/A")
FORM_144 = ("144", "144/A")
FORM_13DG = ("SCHEDULE 13D", "SCHEDULE 13D/A", "SCHEDULE 13G", "SCHEDULE 13G/A")
KNOWN_FORMS = FORM_4 + FORM_144 + FORM_13DG

_DAILY_INDEX_LINE_RE = re.compile(
    r"^(4(?:/A)?|144(?:/A)?|SCHEDULE 13[DG](?:/A)?)\s+.+?\s+(\d+)\s+(\d{8})"
    r"\s+edgar/data/\d+/([\d-]+)\.txt\s*$"
)


# Values that occupy issuerTradingSymbol without being a ticker. Non-traded funds
# and some shell issuers file Form 4 with a literal "NONE"/"N/A" here (seen from
# Blackstone Private Equity Strategies Fund L.P., among others). Passed through,
# such a value becomes a grouping key downstream and merges unrelated issuers into
# one fake symbol -- which has already produced a live cluster alert. cluster.py
# reads this tuple too, so both ends agree on what to discard.
JUNK_TICKERS = ("NONE", "N/A", "NA", "N.A.", "-", "--")


def clean_ticker(raw: str | None) -> str | None:
    """Normalise issuerTradingSymbol to a real ticker or None."""
    t = (raw or "").strip().upper()
    return None if not t or t in JUNK_TICKERS else t


def _user_agent() -> str:
    return os.environ.get("SEC_USER_AGENT", "disclosure-bot research contact@example.com")


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": _user_agent()})
    return s


@dataclass
class FilingRef:
    accession: str
    cik: str
    form_type: str
    filed: str
    index_url: str


@dataclass
class InsiderPurchase:
    """Despite the name, txn_code distinguishes "P" (open-market purchase) from
    "S" (open-market sale) -- parse_form4_xml keeps both so exit_signal.py can spot
    the same insiders who clustered into a buy later clustering into a sell. Code
    that means "purchase" specifically must filter on txn_code == "P" itself."""
    accession: str
    issuer_name: str
    issuer_cik: str
    ticker: str | None
    owner_name: str
    owner_cik: str
    is_officer: bool
    is_director: bool
    is_ten_pct_owner: bool
    officer_title: str | None
    txn_code: str  # "P" or "S"
    transaction_date: str
    shares: float | None
    price: float | None
    value: float | None
    security_title: str
    derivative: bool
    source_url: str
    # Filed under a Rule 10b5-1 plan: the trade was arranged in advance, on a
    # schedule set months earlier, so it carries no information about what the
    # insider thinks today. Document-level flag, applied to every transaction in
    # the filing.
    is_10b5_1: bool = False
    # Shares held after the transaction. Lets a purchase be expressed as a share of
    # the position the insider already had -- doubling a stake and adding half a
    # percent are otherwise indistinguishable.
    shares_owned_after: float | None = None
    # "D" (held directly) or "I" (through a trust, LLC, fund...).
    ownership_type: str | None = None
    # When SEC received the filing, from the daily index -- not the transaction
    # date. The gap between the two is the disclosure lag.
    filed_date: str = ""


def fetch_recent_filings(count: int = 100, session: requests.Session | None = None) -> list[FilingRef]:
    """Fetch the 'latest filings' feed and return only exact Form 4 / 4-A entries.

    The getcurrent endpoint's `type` query param is a prefix match, not an exact
    one (type=4 also returns 424B2, 485BPOS, etc.), so we over-fetch and filter
    client-side on the feed's <category term=...> which is exact.
    """
    session = session or new_session()
    resp = session.get(CURRENT_FEED_URL.format(count=count), timeout=30)
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    seen = set()
    for entry in root.findall("a:entry", ns):
        cat_el = entry.find("a:category", ns)
        form_type = cat_el.get("term") if cat_el is not None else ""
        if form_type not in ("4", "4/A"):
            continue
        id_text = entry.findtext("a:id", default="", namespaces=ns)
        acc = id_text.split("accession-number=")[-1].strip()
        if not acc or acc in seen:
            continue
        seen.add(acc)
        link_el = entry.find("a:link", ns)
        href = link_el.get("href") if link_el is not None else ""
        cik = ""
        if "/data/" in href:
            cik = href.split("/data/")[1].split("/")[0]
        out.append(FilingRef(accession=acc, cik=cik, form_type=form_type, filed="", index_url=href))
    return out


def _find_ownership_xml(cik: str, acc_nodash: str, session: requests.Session) -> str | None:
    resp = session.get(INDEX_JSON_URL.format(cik=cik, acc_nodash=acc_nodash), timeout=30)
    resp.raise_for_status()
    items = resp.json().get("directory", {}).get("item", [])
    names = [i["name"] for i in items if i["name"].lower().endswith(".xml")]
    if "primary_doc.xml" in names:
        return "primary_doc.xml"
    return names[0] if names else None


def _text(el, path):
    node = el.find(path)
    return node.findtext("value") if node is not None else None


def parse_form4_xml(xml_bytes: bytes, accession: str, source_url: str,
                     filed_date: str = "") -> list[InsiderPurchase]:
    root = ET.fromstring(xml_bytes)

    issuer_name = root.findtext("issuer/issuerName") or ""
    issuer_cik = (root.findtext("issuer/issuerCik") or "").strip()
    ticker = clean_ticker(root.findtext("issuer/issuerTradingSymbol"))

    owner_name = root.findtext("reportingOwner/reportingOwnerId/rptOwnerName") or ""
    owner_cik = (root.findtext("reportingOwner/reportingOwnerId/rptOwnerCik") or "").strip()
    rel = root.find("reportingOwner/reportingOwnerRelationship")
    is_officer = (rel.findtext("isOfficer") if rel is not None else "0") == "1"
    is_director = (rel.findtext("isDirector") if rel is not None else "0") == "1"
    is_ten_pct = (rel.findtext("isTenPercentOwner") if rel is not None else "0") == "1"
    officer_title = rel.findtext("officerTitle") if rel is not None else None

    # Document-level, and written either as a boolean word or as 1/0 depending on
    # the filing agent.
    aff = (root.findtext("aff10b5One") or "").strip().lower()
    is_10b5_1 = aff in ("true", "1", "yes")

    purchases = []
    for table, derivative in (("nonDerivativeTable", False), ("derivativeTable", True)):
        table_el = root.find(table)
        if table_el is None:
            continue
        tag = "nonDerivativeTransaction" if not derivative else "derivativeTransaction"
        for txn in table_el.findall(tag):
            # transactionCode is a direct text node (no <value> wrapper), unlike
            # most other fields in this schema.
            code = txn.findtext("transactionCoding/transactionCode")
            if code not in ("P", "S"):
                continue
            shares_s = _text(txn, "transactionAmounts/transactionShares")
            price_s = _text(txn, "transactionAmounts/transactionPricePerShare")
            shares = float(shares_s) if shares_s else None
            price = float(price_s) if price_s else None
            owned_after_s = _text(txn, "postTransactionAmounts/sharesOwnedFollowingTransaction")
            try:
                owned_after = float(owned_after_s) if owned_after_s else None
            except ValueError:
                owned_after = None
            purchases.append(
                InsiderPurchase(
                    accession=accession,
                    issuer_name=issuer_name,
                    issuer_cik=issuer_cik,
                    ticker=ticker,
                    owner_name=owner_name,
                    owner_cik=owner_cik,
                    is_officer=is_officer,
                    is_director=is_director,
                    is_ten_pct_owner=is_ten_pct,
                    officer_title=officer_title,
                    txn_code=code,
                    transaction_date=_text(txn, "transactionDate") or "",
                    shares=shares,
                    price=price,
                    value=(shares * price) if shares and price else None,
                    security_title=_text(txn, "securityTitle") or "",
                    derivative=derivative,
                    source_url=source_url,
                    is_10b5_1=is_10b5_1,
                    shares_owned_after=owned_after,
                    ownership_type=_text(txn, "ownershipNature/directOrIndirectOwnership"),
                    filed_date=filed_date,
                )
            )
    return purchases


SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:0>10}.json"


def fetch_owner_recent_purchases(
    owner_cik: str, since: str, session: requests.Session | None = None, max_filings: int = 30
) -> list[InsiderPurchase]:
    """Return this reporting owner's own Form 4 'P' purchases (any issuer) filed on
    or after `since` (YYYY-MM-DD). Used to score an insider's trading track record.

    Queries data.sec.gov/submissions, which lists an entity's full filing history
    regardless of which company it concerns -- individuals who file Section 16
    reports get their own CIK, separate from any issuer's CIK.
    """
    session = session or new_session()
    cik_num = owner_cik.lstrip("0") or "0"
    resp = session.get(SUBMISSIONS_URL.format(cik=cik_num), timeout=30)
    resp.raise_for_status()
    recent = resp.json().get("filings", {}).get("recent", {})

    candidates = []
    for form, acc, filing_date in zip(recent.get("form", []), recent.get("accessionNumber", []), recent.get("filingDate", [])):
        if form in ("4", "4/A") and filing_date >= since:
            candidates.append(acc)
    candidates = candidates[:max_filings]

    purchases = []
    for acc in candidates:
        acc_nodash = acc.replace("-", "")
        time.sleep(REQUEST_DELAY)
        try:
            doc_name = _find_ownership_xml(cik_num, acc_nodash, session)
            if not doc_name:
                continue
            doc_url = DOC_URL.format(cik=cik_num, acc_nodash=acc_nodash, doc=doc_name)
            time.sleep(REQUEST_DELAY)
            resp = session.get(doc_url, timeout=30)
            resp.raise_for_status()
        except requests.RequestException:
            continue
        purchases.extend(
            p for p in parse_form4_xml(resp.content, acc, doc_url)
            if not p.derivative and p.txn_code == "P"
        )
    return purchases


def scan_new_filings(seen_accessions: set[str], count: int = 100, session: requests.Session | None = None):
    """Yield (accession, list[InsiderPurchase]) for every not-yet-seen Form 4 filing.

    Every accession this generator looks at is yielded exactly once, even when its
    purchase list is empty -- callers should mark it seen regardless of whether it
    contained a purchase, so it is never re-fetched on the next poll.
    """
    session = session or new_session()
    filings = fetch_recent_filings(count=count, session=session)
    for f in filings:
        if f.accession in seen_accessions or not f.cik:
            continue
        acc_nodash = f.accession.replace("-", "")
        time.sleep(REQUEST_DELAY)
        try:
            doc_name = _find_ownership_xml(f.cik, acc_nodash, session)
            if not doc_name:
                yield f.accession, []
                continue
            doc_url = DOC_URL.format(cik=f.cik, acc_nodash=acc_nodash, doc=doc_name)
            time.sleep(REQUEST_DELAY)
            resp = session.get(doc_url, timeout=30)
            resp.raise_for_status()
        except requests.RequestException:
            continue  # transient failure: don't mark seen, retry next poll
        purchases = parse_form4_xml(resp.content, f.accession, doc_url)
        yield f.accession, purchases


def fetch_daily_index_accessions(date: dt.date, session: requests.Session | None = None,
                                  forms: tuple[str, ...] = FORM_4) -> list[tuple[str, str, str, str]]:
    """Returns [(form_type, cik, accession, date_filed)] for every filing of the
    requested form types on `date`, per SEC's complete daily index -- every filing for the day is
    listed, no rolling-window gap like the 'latest filings' feed has.

    One request covers every form type, so asking for 144s or 13D/Gs alongside
    Form 4 costs nothing extra here.
    """
    session = session or new_session()
    quarter = (date.month - 1) // 3 + 1
    url = DAILY_INDEX_URL.format(year=date.year, quarter=quarter, yyyymmdd=date.strftime("%Y%m%d"))
    resp = session.get(url, timeout=30)
    if resp.status_code in (403, 404):
        # 404 = weekend/holiday, no index for that date. 403 = SEC's S3 bucket
        # returns this (not 404) for today's index before it's been published yet.
        return []
    resp.raise_for_status()
    wanted = set(forms)
    out = []
    # One filing is listed once per co-filer, so the same accession appears several
    # times under different CIKs -- a Schedule 13D covering three affiliated
    # entities gets three lines. EDGAR serves an accession's folder under any of its
    # filers' CIKs, so the first is as good as any; keeping only that one avoids
    # re-fetching and re-parsing the identical document (and re-emitting its
    # contents) once per co-filer.
    seen_here: set[str] = set()
    for line in resp.text.splitlines():
        m = _DAILY_INDEX_LINE_RE.match(line)
        if m:
            form_type, cik, date_filed, accession = m.groups()
            if form_type in wanted and accession not in seen_here:
                seen_here.add(accession)
                # date_filed is YYYYMMDD in the index; ISO everywhere else here.
                iso_filed = f"{date_filed[:4]}-{date_filed[4:6]}-{date_filed[6:]}"
                out.append((form_type, cik, accession, iso_filed))
    return out


def fetch_primary_xml(cik: str, accession: str, session: requests.Session) -> tuple[str, bytes] | None:
    """Fetch a filing's primary ownership XML as (doc_url, bytes), or None if the
    filing has no XML document. Raises requests.RequestException on a transient
    failure so callers can decline to mark the accession seen.

    Shared by the Form 4, Form 144 and Schedule 13D/G scanners -- all three are
    reached the same way (index.json, then the document), and all three are
    structured XML despite covering very different disclosures.
    """
    acc_nodash = accession.replace("-", "")
    time.sleep(REQUEST_DELAY)
    doc_name = _find_ownership_xml(cik, acc_nodash, session)
    if not doc_name:
        return None
    doc_url = DOC_URL.format(cik=cik, acc_nodash=acc_nodash, doc=doc_name)
    time.sleep(REQUEST_DELAY)
    resp = session.get(doc_url, timeout=30)
    resp.raise_for_status()
    return doc_url, resp.content


def scan_daily_index(date: dt.date, seen_accessions: set[str], session: requests.Session | None = None):
    """Like scan_new_filings, but scans SEC's complete daily index for one specific
    calendar date instead of the rolling 'latest ~100 filings' window -- use this
    for full same-day coverage rather than near-real-time monitoring.

    Form 4/A (amendments) are skipped before the XML is even fetched, not just
    filtered later: an amendment usually corrects an already-public original
    (a typo, a footnote), and its OWN transactionDate can be many months
    older than its filing date -- found by backtest.py turning up purchases
    with e.g. a 2025-03 transaction "filed" in 2026-09. Treating that as a
    fresh disclosure would score it as maximally stale-to-fresh in cluster.py
    and, worse, feed backtest.py an entry date the market didn't actually
    react on (it already knew from the original, prompt Form 4). Same
    reasoning sec_13dg.py's --new-positions-only already applies to 13D/A --
    this closes the same gap for Form 4.
    """
    session = session or new_session()
    for form, cik, accession, filed in fetch_daily_index_accessions(date, session=session, forms=FORM_4):
        if accession in seen_accessions or not cik:
            continue
        if form == "4/A":
            yield accession, []
            continue
        try:
            fetched = fetch_primary_xml(cik, accession, session)
        except requests.RequestException:
            continue  # transient failure: don't mark seen, retry next run
        if fetched is None:
            yield accession, []
            continue
        doc_url, xml_bytes = fetched
        yield accession, parse_form4_xml(xml_bytes, accession, doc_url, filed_date=filed)
