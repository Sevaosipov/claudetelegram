"""Fetch US Senate Periodic Transaction Reports from the eFD search system.

The Senate half of the STOCK Act disclosures that house_ptr.py already collects for
the House of Representatives. Same law, same 30-45 day filing deadline, same
amount-bracket disclosure ("$50,001 - $100,000") -- but a completely different
delivery system: a Django app with a click-through agreement and a JSON search
endpoint, rather than the House Clerk's yearly ZIP of PDFs.

STATUS: UNVERIFIED FROM THIS MACHINE, AND OFF BY DEFAULT (--senate to enable).

efdsearch.senate.gov currently answers every request from here with HTTP 403 --
including with an ordinary browser User-Agent, and before the agreement step is
even reached. The body is an Akamai edge block ("Access Denied", errors.edgesuite.net),
not a Django response, so it is a network-level block rather than anything this
code does wrong. It may well work from a different network.

Deliberately NOT done in response to that: no User-Agent spoofing, no TLS
fingerprint games, no proxying. This project already drew that line once -- the
README rejected the UK's FCA/LSE data for requiring an account or a geo-block
bypass -- and the same standard applies here. Accepting the site's own
click-through agreement is a form it serves and expects; getting around a WAF that
is refusing us is not.

So the flow below is written to the documented shape of the service:
  1. GET  /search/home/            -> CSRF token (form field + cookie)
  2. POST /search/home/            -> accept the prohibition agreement, get a session
  3. POST /search/report/data/     -> paged JSON list of reports
  4. GET  the report's own page    -> HTML table of individual transactions

If the block lifts, the thing to check first is that step 3 still returns rows
shaped as described in _parse_report_row, and that step 4's table still has the
column order _parse_transaction_table assumes.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

BASE = "https://efdsearch.senate.gov"
HOME_URL = BASE + "/search/home/"
SEARCH_URL = BASE + "/search/"
DATA_URL = BASE + "/search/report/data/"

# Report types in eFD's own numbering; 11 is the Periodic Transaction Report, the
# only one that lists individual trades.
REPORT_TYPE_PTR = "11"
PAGE_SIZE = 100

TXN_TYPE_MAP = {
    "purchase": "P",
    "sale": "S",
    "sale (partial)": "S (partial)",
    "sale (full)": "S",
    "exchange": "E",
}

_TICKER_RE = re.compile(r"^[A-Z][A-Z.\-]{0,5}$")


class AccessBlocked(RuntimeError):
    """Raised when eFD refuses the request outright (currently an edge-level 403).

    A distinct type so bot.py can report it as "this source is unavailable here"
    rather than as a parser failure -- they need very different responses.
    """


@dataclass
class SenateTransaction:
    report_id: str
    member_name: str
    office: str
    owner: str
    asset: str
    ticker: str | None
    txn_type: str          # P / S / S (partial) / E
    txn_date: str          # ISO
    amount_range: str      # "$50,001 - $100,000", as disclosed
    source_url: str


def new_session() -> requests.Session:
    s = requests.Session()
    # The same descriptive research User-Agent the rest of the project sends. Not a
    # browser string: see the note about the edge block at the top of this module.
    s.headers.update({"User-Agent": "disclosure-bot research contact@example.com"})
    return s


def _csrf_token(session: requests.Session, html: str) -> str:
    m = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', html)
    if m:
        return m.group(1)
    return session.cookies.get("csrftoken", "")


def _check(resp: requests.Response) -> requests.Response:
    if resp.status_code == 403:
        raise AccessBlocked(
            f"efdsearch.senate.gov refused the request (HTTP 403) for {resp.url}. "
            f"This is an edge-level block, not the agreement gate; see the note in "
            f"senate_efd.py. Nothing to fix in this code -- try from another network."
        )
    resp.raise_for_status()
    return resp


def accept_agreement(session: requests.Session) -> str:
    """Steps 1-2: take the click-through agreement the site itself serves, which is
    what unlocks searching. Returns the CSRF token for subsequent posts."""
    resp = _check(session.get(HOME_URL, timeout=30))
    token = _csrf_token(session, resp.text)
    _check(session.post(
        HOME_URL,
        data={"prohibition_agreement": "1", "csrfmiddlewaretoken": token},
        headers={"Referer": HOME_URL},
        timeout=30,
    ))
    return token


def _parse_report_row(row: list) -> dict | None:
    """One row of the search endpoint's JSON: [first, last, office, link_html, date].

    The fourth cell is an <a> whose href is the report's own page and whose text is
    the report title; everything else is plain text.
    """
    if len(row) < 5:
        return None
    first, last, office, link_html, filed = row[:5]
    href = None
    m = re.search(r'href="([^"]+)"', link_html or "")
    if m:
        href = m.group(1)
    if not href:
        return None
    return {
        "member_name": f"{(first or '').strip()} {(last or '').strip()}".strip(),
        "office": (office or "").strip(),
        "url": href if href.startswith("http") else BASE + href,
        "report_id": href.rstrip("/").split("/")[-1],
        "filed": (filed or "").strip(),
    }


def fetch_report_list(session: requests.Session, token: str, start_date: dt.date,
                       end_date: dt.date) -> list[dict]:
    """Step 3: every PTR filed in the window, paging until the rows run out."""
    out: list[dict] = []
    offset = 0
    while True:
        resp = _check(session.post(
            DATA_URL,
            data={
                "start": str(offset),
                "length": str(PAGE_SIZE),
                "report_types": f"[{REPORT_TYPE_PTR}]",
                "filer_types": "[]",
                "submitted_start_date": start_date.strftime("%m/%d/%Y 00:00:00"),
                "submitted_end_date": end_date.strftime("%m/%d/%Y 23:59:59"),
                "candidate_state": "",
                "senator_state": "",
                "office_id": "",
                "first_name": "",
                "last_name": "",
                "csrfmiddlewaretoken": token,
            },
            headers={"Referer": SEARCH_URL},
            timeout=45,
        ))
        payload = resp.json()
        rows = payload.get("data", [])
        for row in rows:
            parsed = _parse_report_row(row)
            if parsed:
                out.append(parsed)
        if len(rows) < PAGE_SIZE:
            return out
        offset += PAGE_SIZE


def _clean_ticker(raw: str) -> str | None:
    """eFD writes the ticker in its own column, using "--" where there isn't one
    (funds, real estate, and anything not exchange-traded)."""
    text = (raw or "").strip().upper()
    if not text or text in ("--", "-", "N/A", "NONE"):
        return None
    return text if _TICKER_RE.match(text) else None


def _iso_date(raw: str) -> str:
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime((raw or "").strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return (raw or "").strip()


def parse_transaction_table(html: str, report: dict) -> list[SenateTransaction]:
    """Step 4: the report page's transaction table.

    Columns, in order: #, transaction date, owner, ticker, asset name, asset type,
    transaction type, amount, comment. Some PTRs are filed on paper and scanned, in
    which case there is no table at all and the report yields nothing -- the same
    limitation house_ptr.py has with scanned PDFs.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        return []

    out = []
    for tr in table.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(cells) < 8:
            continue
        _num, txn_date, owner, ticker, asset, _asset_type, txn_type, amount = cells[:8]
        mapped = TXN_TYPE_MAP.get(txn_type.strip().lower(), txn_type.strip())
        out.append(SenateTransaction(
            report_id=report["report_id"],
            member_name=report["member_name"],
            office=report["office"],
            owner=owner,
            asset=asset,
            ticker=_clean_ticker(ticker),
            txn_type=mapped,
            txn_date=_iso_date(txn_date),
            amount_range=amount.strip(),
            source_url=report["url"],
        ))
    return out


def scan_new_ptrs(start_date: dt.date, end_date: dt.date, seen_report_ids: set[str],
                   session: requests.Session | None = None):
    """Yield (report_id, list[SenateTransaction]) for every not-yet-seen PTR filed
    in the window. Every report examined is yielded exactly once even when it parses
    to nothing (paper filings), so callers can mark it seen -- the same contract as
    the other sources' scanners.
    """
    session = session or new_session()
    token = accept_agreement(session)
    for report in fetch_report_list(session, token, start_date, end_date):
        if report["report_id"] in seen_report_ids:
            continue
        try:
            resp = _check(session.get(report["url"], timeout=30))
        except requests.RequestException:
            continue  # transient failure: don't mark seen, retry next run
        yield report["report_id"], parse_transaction_table(resp.text, report)
