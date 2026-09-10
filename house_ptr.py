"""Fetch and parse House of Representatives Periodic Transaction Reports (PTRs).

Data flow:
  1. Download the yearly filer index ZIP (financial-pdfs/{year}FD.zip) which lists
     every filer/DocID/FilingType for that year. FilingType == "P" means the filing
     is a Periodic Transaction Report (the STOCK Act disclosure that lists actual
     stock buys/sells, due within 30-45 days of the trade).
  2. For each PTR DocID, download the PDF from ptr-pdfs/{year}/{DocID}.pdf.
  3. Parse the PDF's transaction table into structured rows (owner, asset, ticker,
     transaction type P/S/E, dates, amount range).

The PDFs are generated from a fixed-column form. Column widths are stable but the
text layer wraps asset names and amount ranges across multiple physical lines within
one logical table row, so a naive line-by-line text read scrambles column order.
Instead we read word bounding boxes and reconstruct rows by x-position.
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree as ET

import pdfplumber
import requests

BASE = "https://disclosures-clerk.house.gov"
INDEX_ZIP_URL = BASE + "/public_disc/financial-pdfs/{year}FD.zip"
PTR_PDF_URL = BASE + "/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"

TYPE_RE = re.compile(r"^(P|S|E)$")
DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
AMOUNT_RE = re.compile(r"^\$[\d,]+$|^-$")
TICKER_RE = re.compile(r"\(([A-Za-z][A-Za-z.]{0,5})\)")


@dataclass
class Filer:
    doc_id: str
    last: str
    first: str
    filing_type: str
    state_dst: str
    year: str
    filing_date: str


@dataclass
class Transaction:
    doc_id: str
    member_name: str
    state_district: str
    owner: str
    asset: str
    ticker: str | None
    txn_type: str  # P, S, S (partial), E
    txn_date: str
    notification_date: str
    amount_range: str
    source_url: str


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "disclosure-bot research contact@example.com"})
    return s


def fetch_year_index(year: int, session: requests.Session | None = None) -> list[Filer]:
    """Download and parse the yearly filer index, returning every Filer entry."""
    session = session or new_session()
    resp = session.get(INDEX_ZIP_URL.format(year=year), timeout=30)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        xml_name = f"{year}FD.xml"
        raw = zf.read(xml_name).decode("utf-8-sig")

    root = ET.fromstring(raw)
    filers = []
    for m in root.findall("Member"):
        filers.append(
            Filer(
                doc_id=(m.findtext("DocID") or "").strip(),
                last=(m.findtext("Last") or "").strip(),
                first=(m.findtext("First") or "").strip(),
                filing_type=(m.findtext("FilingType") or "").strip(),
                state_dst=(m.findtext("StateDst") or "").strip(),
                year=(m.findtext("Year") or "").strip(),
                filing_date=(m.findtext("FilingDate") or "").strip(),
            )
        )
    return filers


def fetch_ptr_pdf_bytes(year: int, doc_id: str, session: requests.Session | None = None) -> bytes:
    session = session or new_session()
    resp = session.get(PTR_PDF_URL.format(year=year, doc_id=doc_id), timeout=30)
    resp.raise_for_status()
    return resp.content


def _cluster_rows(words, tol=2.5):
    """Group words into physical text lines by 'top' (y) coordinate."""
    rows = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if rows and abs(rows[-1][0]["top"] - w["top"]) <= tol:
            rows[-1].append(w)
        else:
            rows.append([w])
    return rows


def _find_col_starts(rows):
    """Locate x0 of each table column from the (possibly multi-line) header."""
    labels = {}
    for row in rows:
        for w in row:
            t = w["text"]
            if t in ("Owner", "Asset", "Transaction", "Notification", "Amount", "Cap.") and t not in labels:
                labels[t] = w["x0"]
            if t == "Date" and w["top"] > 0:
                # first "Date" belongs to Transaction Date, second to Notification Date
                labels.setdefault("_dates", []).append(w["x0"])
        if len(labels) >= 6 and "_dates" in labels and len(labels["_dates"]) >= 2:
            break
    dates = sorted(labels.get("_dates", [320, 380]))
    return {
        "owner": labels.get("Owner", 60),
        "asset": labels.get("Asset", 100),
        "type": labels.get("Transaction", 258),
        "date1": dates[0] if dates else 320,
        "date2": labels.get("Notification", dates[1] if len(dates) > 1 else 380),
        "amount": labels.get("Amount", 440),
        "capgains": labels.get("Cap.", 520),
    }


def _bucket(row, starts):
    cols = {k: [] for k in starts}
    order = sorted(starts.items(), key=lambda kv: kv[1])
    for w in row:
        col = order[0][0]
        for name, start in order:
            if w["x0"] >= start - 3:
                col = name
        cols[col].append(w)
    return {k: " ".join(w["text"] for w in sorted(v, key=lambda w: w["x0"])) for k, v in cols.items()}


def _is_metadata_line(text: str) -> bool:
    # Lines like "Filing Status:", "Description:", "Comment:", "Sub Holding Of:" render
    # with most letters as invisible control chars due to a subset font, but always
    # keep the colon and any following free text. Any NUL byte is a reliable marker.
    return "\x00" in text


def is_scanned_pdf(pdf_bytes: bytes) -> bool:
    """True if the PDF has no extractable text layer (a paper filing that the Clerk
    scanned to image rather than an electronically-filed, text-based PDF). About
    ~1-3% of PTRs are filed on paper; these can't be parsed by this tool at all."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        if not pdf.pages:
            return True
        return len(pdf.pages[0].extract_words()) == 0


def parse_ptr_pdf(pdf_bytes: bytes, doc_id: str, source_url: str) -> tuple[dict, list[Transaction]]:
    """Returns (filer_info, transactions) parsed from a single PTR PDF."""
    filer_info = {"name": "", "status": "", "state_district": ""}
    transactions: list[Transaction] = []
    current = None  # accumulating transaction dict; carries across page breaks --
                     # a transaction's asset name / amount can wrap onto the next page

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
            rows = _cluster_rows(words)

            # Filer header info (only present on page 0, harmless to re-check per page)
            for row in rows:
                text = " ".join(w["text"] for w in row)
                if text.startswith("Name:"):
                    filer_info["name"] = text[len("Name:"):].strip()
                elif text.startswith("Status:"):
                    filer_info["status"] = text[len("Status:"):].strip()
                elif text.startswith("State/District:"):
                    filer_info["state_district"] = text[len("State/District:"):].strip()

            # Locate header row range and footer to bound the transaction table
            header_top = None
            footer_top = None
            for row in rows:
                texts = [w["text"] for w in row]
                if "Owner" in texts and "Asset" in texts:
                    header_top = row[0]["top"]
                if any("complete" in t for t in texts):
                    footer_top = row[0]["top"]
                    break
            if header_top is None:
                continue

            starts = _find_col_starts([r for r in rows if header_top - 5 <= r[0]["top"] <= header_top + 40])

            table_rows = [
                r for r in rows
                if r[0]["top"] > header_top + 25 and (footer_top is None or r[0]["top"] < footer_top)
            ]

            for row in table_rows:
                cols = _bucket(row, starts)
                type_text = cols["type"].strip()
                date1_text = cols["date1"].strip()
                type_match = TYPE_RE.match(type_text.split()[0]) if type_text else None

                if type_match and DATE_RE.match(date1_text.split()[0] if date1_text else ""):
                    # start of a new transaction entry
                    if current:
                        transactions.append(_finalize(current, doc_id, filer_info, source_url))
                    txn_type = type_text
                    if "partial" in " ".join(w["text"] for w in row).lower():
                        txn_type = "S (partial)"
                    current = {
                        "owner": cols["owner"].strip(),
                        "asset_parts": [cols["asset"].strip()] if cols["asset"].strip() else [],
                        "type": txn_type,
                        "date1": date1_text.split()[0],
                        "date2": cols["date2"].strip().split()[0] if cols["date2"].strip() else "",
                        "amount_parts": [cols["amount"].strip()] if cols["amount"].strip() else [],
                    }
                elif current is not None:
                    asset_text = cols["asset"].strip()
                    if _is_metadata_line(asset_text):
                        transactions.append(_finalize(current, doc_id, filer_info, source_url))
                        current = None
                        continue
                    if asset_text:
                        current["asset_parts"].append(asset_text)
                    amount_text = cols["amount"].strip()
                    if amount_text:
                        current["amount_parts"].append(amount_text)

        if current:
            transactions.append(_finalize(current, doc_id, filer_info, source_url))

    return filer_info, transactions


def _finalize(cur: dict, doc_id: str, filer_info: dict, source_url: str) -> Transaction:
    asset_text = " ".join(cur["asset_parts"]).strip()
    ticker_matches = TICKER_RE.findall(asset_text)
    ticker = ticker_matches[-1] if ticker_matches else None
    amount_range = " ".join(cur["amount_parts"]).strip()
    amount_range = re.sub(r"\s*-\s*", " - ", amount_range)
    return Transaction(
        doc_id=doc_id,
        member_name=filer_info.get("name", ""),
        state_district=filer_info.get("state_district", ""),
        owner=cur["owner"],
        asset=asset_text,
        ticker=ticker,
        txn_type=cur["type"],
        txn_date=cur["date1"],
        notification_date=cur["date2"],
        amount_range=amount_range,
        source_url=source_url,
    )


def scan_new_ptrs(year: int, seen_doc_ids: set[str], session: requests.Session | None = None):
    """Yield (doc_id, list[Transaction]) for every not-yet-seen PTR filing in `year`.

    Yields ALL transaction types (P/S/S (partial)/E), not just purchases -- callers
    that only want purchases (the main terminal/CSV feed) should filter on
    txn_type == "P" themselves. Sales are kept so exit_signal.py can spot a buy
    cluster later unwinding. Every PTR doc_id looked at is yielded once (even with
    an empty list) so callers can mark it seen and never re-download/re-parse that
    PDF again.
    """
    session = session or new_session()
    filers = fetch_year_index(year, session)
    ptr_filers = [f for f in filers if f.filing_type == "P" and f.doc_id not in seen_doc_ids]
    for f in ptr_filers:
        try:
            pdf_bytes = fetch_ptr_pdf_bytes(year, f.doc_id, session)
        except requests.RequestException:
            continue  # transient failure: don't mark seen, retry next poll
        source_url = PTR_PDF_URL.format(year=year, doc_id=f.doc_id)
        _, txns = parse_ptr_pdf(pdf_bytes, f.doc_id, source_url)
        yield f.doc_id, txns
