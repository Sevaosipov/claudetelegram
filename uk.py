"""Fetch and parse UK director dealings -- «Director/PDMR Shareholding» announcements (Art. 19 UK MAR), the
British equivalent of SEC Form 4 -- from the FCA's National Storage Mechanism, the official archive every
regulatory news service files into. Its search is a public JSON API (the one data.fca.org.uk's own page
calls; no key, no login): one POST lists the announcements of a date range by category, and each links to
the announcement's HTML.

The announcement carries the regulation's standard notification form as a table: who (1a), their position
(2a), the issuer and its LEI (3), and one block per transaction (4): the instrument and its ISIN, the
nature of the transaction, prices and volumes, the date, the place. The form's labels are fixed; what a
company writes in the cells is not, so this reads the cells by label and gives up on anything it cannot
size -- a price with no currency, a volume that is not a number -- rather than guess. Like BaFin and Sweden
the key is the ISIN: the form has no ticker.

Most of what is filed is not a decision to buy: share-plan awards, vesting, option exercises, dividend
reinvestment, monthly partnership-share purchases, transfers between a director's own accounts. `nature`
keeps the company's wording; `txn_type` is 'P' only for a plain purchase and 'S' for a plain sale, and
everything else is 'O' and stays out of the signals.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import re

import requests

SEARCH_URL = "https://api.data.fca.org.uk/search?index=nsm-search"
DOCUMENT_BASE = "https://data.fca.org.uk/artefacts/"
CATEGORY_CODE = "DSH"            # Director/PDMR Shareholding
PAGE_SIZE = 100
MAX_PLAUSIBLE_VALUE = 5e9        # GBP; the largest genuine director purchase is far below it

_ISIN_RE = re.compile(r"\b([A-Z]{2}[A-Z0-9]{9}\d)\b")
_NUM = r"\d[\d,]*(?:\.\d+)?"
# A price with its unit: pounds («£16.40», «GBP 16.40», «16.40 GBP») or pence («1,640p», «1640 pence», «GBX 1640»).
_POUNDS_RE = re.compile(rf"(?:£|GBP)\s*({_NUM})|({_NUM})\s*(?:GBP|pounds)\b", re.I)
_PENCE_RE = re.compile(rf"(?:GBX|GBp)\s*({_NUM})|({_NUM})\s*(?:p\b|pence|GBX|GBp)", re.I)
_PLAIN_RE = re.compile(rf"^\s*({_NUM})\s*(?:shares?|ordinary shares?)?\s*$", re.I)
_BUY_RE = re.compile(r"\b(purchas\w*|acqui\w*|bought|buy)\b", re.I)
_SELL_RE = re.compile(r"\b(sale|sold|sell|dispos\w*)\b", re.I)
# Not a decision to buy or sell: pay, plans, reinvested dividends, moves between a person's own accounts.
_ROUTINE_RE = re.compile(
    r"award|grant|vest|exercis|option|incentive|\bSIP\b|partnership|matching|dividend|\bDRIP\b|reinvest|scrip|"
    r"nil[- ]cost|\bLTIP\b|bonus|deferred|sharesave|\bSAYE\b|transfer|gift|arrangement with the company|"
    r"salary|\bfees?\b|\bplan\b|scheme|bed and|\bISA\b|tax|conversion|subscription|placing|allot|rights issue|"
    r"open offer|settle|inherit|pledge|contribution", re.I)
_SECTION = [(1, re.compile(r"details of (the )?(person discharging|pdmr)", re.I)),
            (2, re.compile(r"reasons? for (the )?notification", re.I)),
            (3, re.compile(r"details of the issuer", re.I)),
            (4, re.compile(r"details of the transaction", re.I))]
_LETTER_RE = re.compile(r"^[a-g]\)?\.?$", re.I)
_DATE_FORMATS = ("%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%Y", "%d %B, %Y", "%B %d, %Y",
                 "%d/%m/%y")


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "disclosure-bot research", "Origin": "https://data.fca.org.uk",
                      "Content-Type": "application/json"})
    return s


def list_filings(from_date: dt.date, to_date: dt.date, session: requests.Session) -> list[dict]:
    """The Director/PDMR Shareholding announcements submitted in a date range, newest first: each a dict
    of the archive's own fields (seq_id, company, lei, submitted_date, download_link, ...)."""
    out, start = [], 0
    while True:
        body = {"from": start, "size": PAGE_SIZE, "sort": "submitted_date", "sortorder": "desc",
                "criteriaObj": {"criteria": [{"name": "type_code", "value": CATEGORY_CODE},
                                             {"name": "latest_flag", "value": "Y"}],
                                "dateCriteria": [{"name": "submitted_date", "value": {
                                    "from": f"{from_date.isoformat()}T00:00:00Z",
                                    "to": f"{to_date.isoformat()}T23:59:59Z"}}]}}
        resp = session.post(SEARCH_URL, json=body, timeout=30)
        resp.raise_for_status()
        hits = resp.json().get("hits", {}).get("hits", [])
        out += [h["_source"] for h in hits]
        if len(hits) < PAGE_SIZE:
            return out
        start += PAGE_SIZE


def cells(document: str) -> list[str]:
    """The announcement's text as one string per table cell or paragraph, empty ones dropped."""
    text = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", document, flags=re.I)
    text = re.sub(r"<br\s*/?>|</p>|</tr>|</div>|</t[dh]>|</li>|</h\d>", "\n", text, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    out = []
    for line in text.split("\n"):
        line = re.sub(r"[ \t\xa0​]+", " ", line).strip()
        if line:
            out.append(line)
    return out


def _number(text: str) -> float | None:
    if re.fullmatch(r"\d+,\d{1,2}", text):          # «32,53 pence»: a decimal comma, not thousands
        text = text.replace(",", ".")
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def price_gbp(cell: str) -> float | None:
    """A cell's price in pounds, or None when it names no unit this can trust."""
    m = _POUNDS_RE.search(cell)
    if m:
        return _number(m.group(1) or m.group(2))
    m = _PENCE_RE.search(cell)
    if m:
        value = _number(m.group(1) or m.group(2))
        return value / 100.0 if value is not None else None
    return None


def parse_date(text: str) -> str | None:
    """ISO date of a form's «date of the transaction» cell (the first date in it), or None."""
    text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", text.strip())
    candidates = [text] + re.findall(r"\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{1,2} [A-Za-z]+,? \d{4}", text)
    for chunk in candidates:
        for fmt in _DATE_FORMATS:
            try:
                return dt.datetime.strptime(chunk.strip(), fmt).date().isoformat()
            except ValueError:
                continue
    return None


def nature_type(nature: str) -> str:
    """'P' for a plain purchase, 'S' for a plain sale, 'O' for everything else (see the module's note)."""
    if not nature or _ROUTINE_RE.search(nature):
        return "O"
    buy, sell = bool(_BUY_RE.search(nature)), bool(_SELL_RE.search(nature))
    return "P" if buy and not sell else "S" if sell and not buy else "O"


def _value_after(lines: list[str], i: int) -> str:
    """The first cell after label i that is not a form letter («a)») or another label fragment."""
    for line in lines[i + 1:i + 5]:
        if not _LETTER_RE.match(line) and not re.match(r"^\d\.?$", line):
            return line
    return ""


def parse_filing(document: str, meta: dict) -> list[dict]:
    """The transactions of one announcement, as dicts for db.save_uk_purchase. An announcement can hold
    several people and several transaction blocks; a block that cannot be sized is left out."""
    lines = cells(document)
    out: list[dict] = []
    person = position = issuer = lei = ""
    block: dict | None = None
    section, i = 0, 0

    def flush():
        nonlocal block
        if block and block.get("shares") and block.get("price") is not None and block.get("isin"):
            value = block["shares"] * block["price"]
            if 0 < value <= MAX_PLAUSIBLE_VALUE and block.get("txn_date"):
                key = "|".join([meta.get("seq_id", ""), person, block["isin"], block["txn_date"],
                                f"{block['shares']:.4f}", f"{block['price']:.6f}", block["nature"]])
                out.append({
                    "txn_key": hashlib.sha1(key.encode("utf-8")).hexdigest(),
                    "filing_id": meta.get("seq_id", ""), "published": meta.get("submitted_date", ""),
                    "person": person, "position": position,
                    "issuer_name": issuer or meta.get("company", ""), "lei": lei or meta.get("lei", ""),
                    "isin": block["isin"], "nature": block["nature"], "txn_type": nature_type(block["nature"]),
                    "txn_date": block["txn_date"], "shares": block["shares"], "price": block["price"],
                    "currency": "GBP", "value": value,
                    "related_party": bool(re.search(r"closely associated|\bPCA\b|spouse|wife|husband|son of|"
                                                    r"daughter of|partner of", position, re.I)),
                    "place": block.get("place", ""),
                    "source_url": DOCUMENT_BASE + meta.get("download_link", ""),
                })
        block = None

    while i < len(lines):
        line = lines[i]
        for number, pattern in _SECTION:
            if pattern.search(line):
                if number in (1, 4):
                    flush()
                if number == 4:
                    block = {"nature": "", "isin": "", "prices": [], "volumes": []}
                section = number
                break
        else:
            low = line.lower()
            if section == 1 and low in ("name", "full name", "first name"):
                person = _value_after(lines, i)
            elif section == 1 and low in ("last name", "surname") and person:
                person = f"{person} {_value_after(lines, i)}"
            elif section == 2 and low.startswith("position"):
                position = _value_after(lines, i)
            elif section == 3 and low in ("name", "full name", "full name of the entity"):
                issuer = _value_after(lines, i)
            elif section == 3 and (low == "lei" or low.startswith("legal entity identifier")):
                lei = _value_after(lines, i)
            elif section == 4 and block is not None:
                if not block["isin"] and (m := _ISIN_RE.search(line)) and "LEI" not in line:
                    block["isin"] = m.group(1)
                if low.startswith("nature of the transaction"):
                    block["nature"] = _value_after(lines, i)
                elif low.startswith("price(s) and volume(s)"):
                    block["mode"] = "pairs"
                elif low.startswith("aggregated information"):
                    block["mode"] = "aggregate"
                elif low.startswith("date of the transaction") or low.startswith("date of transaction"):
                    block["mode"] = None
                    block["txn_date"] = parse_date(_value_after(lines, i))
                elif low.startswith("place of the transaction") or low.startswith("place of transaction"):
                    block["place"] = _value_after(lines, i)
                elif block.get("mode") == "pairs":
                    price = price_gbp(line)
                    if price is not None:
                        block["prices"].append(price)
                    elif (m := _PLAIN_RE.match(line)) and block["prices"] and len(block["volumes"]) < len(block["prices"]):
                        block["volumes"].append(_number(m.group(1)))
                if block.get("prices") and len(block["prices"]) == len(block["volumes"]) and all(block["volumes"]):
                    shares = sum(block["volumes"])
                    block["shares"] = shares
                    block["price"] = sum(p * v for p, v in zip(block["prices"], block["volumes"])) / shares
        i += 1
    flush()
    return out


def scan_new_filings(from_date: dt.date, to_date: dt.date, seen: set[str], session=None):
    """Yield (filing id, [transactions]) for each announcement of the range not in `seen`. A filing that
    cannot be fetched is not yielded, so the next run asks again."""
    session = session or new_session()
    for meta in list_filings(from_date, to_date, session):
        filing_id = meta.get("seq_id") or ""
        link = meta.get("download_link") or ""
        if not filing_id or filing_id in seen:
            continue
        if not link.lower().endswith((".html", ".htm")):
            yield filing_id, []              # a PDF: nothing this can read
            continue
        try:
            resp = session.get(DOCUMENT_BASE + link, timeout=30)
            resp.raise_for_status()
        except requests.RequestException:
            continue
        yield filing_id, parse_filing(resp.text, meta)
