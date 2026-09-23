"""Public companies buying (or selling) crypto for their own balance sheet, from SEC
8-K and 6-K filings -- Strategy (MSTR) and its imitators.

There is no structured form for this: a company that buys bitcoin says so in an 8-K
press release, in prose ("purchased 1,355 bitcoin at an average price of
approximately $79,475 per bitcoin") or, in Strategy's case, in a small table
("BTC Purchased ... 950 $ 75.7 $ 79,670"). So this module:

  1. asks EDGAR full-text search (efts.sec.gov, free, no key) which 8-K/6-K
     documents filed in a date range mention bitcoin or ether at all -- a few dozen
     a fortnight, most of them miners describing production;
  2. fetches each new document once and regexes the transactions back out.

Only an explicit past-tense trade with a unit count is extracted. A miner "acquired
1,000 bitcoin mining machines" is not a bitcoin purchase, "may purchase up to" is not
a purchase, and a document that merely mentions bitcoin yields nothing -- a missed
filing costs one signal, a fabricated one costs trust in all the others.

The same sentence routinely appears twice in one accession (the 8-K body and its
EX-99.1 press release), so rows are keyed by the trade, not by the document.
"""
from __future__ import annotations

import datetime as dt
import html
import re
import time
from dataclasses import dataclass

import requests

EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{adsh}/{filename}"
FORMS = "8-K,6-K"
# One query per coin: EFTS scores rather than filters, so a combined query ranks a
# long tail of passing mentions above the filings that matter.
QUERIES = {"BTC": '"bitcoin"', "ETH": '"ether" OR "ethereum"'}
PAGE_SIZE = 100
MAX_PAGES = 5
REQUEST_PAUSE_SECONDS = 0.15   # SEC fair access: <=10 req/s across its hosts

# Sanity bounds on a parsed per-coin price. A misread thousands separator turns
# $79,475 into $79 or $79,475,000; either is rejected rather than stored.
PLAUSIBLE_PRICE_USD = {"BTC": (1_000, 1_000_000), "ETH": (50, 100_000)}

_COIN_WORDS = {"bitcoin": "BTC", "bitcoins": "BTC", "btc": "BTC",
               "ether": "ETH", "ethereum": "ETH", "eth": "ETH"}
_NUM = r"\d[\d,]*(?:\.\d+)?"
_PROSE_RE = re.compile(
    rf"\b(?P<verb>purchased|acquired|bought|sold)\s+"
    rf"(?:an?\s+(?:aggregate|total)\s+(?:of\s+)?)?(?:approximately\s+|about\s+|roughly\s+)?"
    rf"(?P<units>{_NUM})\s+(?:(?:additional|more)\s+)?"
    rf"(?P<coin>bitcoins?|BTC|ether|ETH|ethereum)\b"
    # "1,000 bitcoin mining machines" is hardware, not bitcoin.
    rf"(?!\s*(?:mining|miners?|machines?|rigs?|ATMs?|hash|-denominated|treasury\s+compan))",
    re.IGNORECASE,
)
_AVG_RE = re.compile(
    rf"average\s+(?:purchase\s+|sales?\s+|sale\s+)?price\s+of\s+(?:approximately\s+|about\s+)?"
    rf"(?:US)?\$\s?(?P<avg>{_NUM})", re.IGNORECASE)
_TOTAL_RE = re.compile(
    rf"(?:for|aggregate\s+(?:purchase\s+price|gross\s+proceeds|consideration)\s+of|"
    rf"total\s+(?:cost|consideration)\s+of)\s+(?:approximately\s+|about\s+)?(?:US)?\$\s?"
    rf"(?P<total>{_NUM})\s*(?P<mult>million|billion|thousand|[mb]n?\b)?", re.IGNORECASE)
# Strategy's weekly table, flattened to text: header row, then "units $ agg $ avg".
_TABLE_HEAD_RE = re.compile(r"\b(?P<coin>BTC|ETH)\s+(?:Purchased|Acquired)\b", re.IGNORECASE)
_TABLE_ROW_RE = re.compile(rf"(?P<units>{_NUM})\s+\$\s*(?P<agg>{_NUM})\s+\$\s*(?P<avg>{_NUM})")
_MULT = {"thousand": 1e3, "million": 1e6, "m": 1e6, "mn": 1e6, "billion": 1e9, "b": 1e9, "bn": 1e9}
_MINED_RE = re.compile(r"\b(?:mined|produced|production)\b", re.IGNORECASE)
_DISPLAY_RE = re.compile(r"^(?P<name>.*?)\s+\((?P<tickers>[^)]*)\)\s+\(CIK")


@dataclass
class TreasuryTxn:
    accession: str
    company: str
    ticker: str | None
    cik: str
    coin: str               # "BTC" / "ETH"
    side: str               # "P" purchase / "S" sale
    units: float
    avg_price_usd: float | None
    total_usd: float | None
    filed_date: str         # ISO
    form: str
    source_url: str

    @property
    def value_usd(self) -> float | None:
        if self.total_usd:
            return self.total_usd
        if self.avg_price_usd:
            return self.units * self.avg_price_usd
        return None


def new_session() -> requests.Session:
    import sec_edgar
    return sec_edgar.new_session()


def html_to_text(raw: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", text).replace(" ", " ")


def _num(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _plausible(coin: str, avg: float | None) -> bool:
    if avg is None:
        return True
    lo, hi = PLAUSIBLE_PRICE_USD.get(coin, (0, float("inf")))
    return lo <= avg <= hi


def parse_text(text: str) -> list[dict]:
    """Every (coin, side, units, avg, total) trade stated in a filing's text."""
    out: list[dict] = []
    for m in _PROSE_RE.finditer(text):
        coin = _COIN_WORDS[m.group("coin").lower()]
        units = _num(m.group("units"))
        if not units:
            continue
        side = "S" if m.group("verb").lower() == "sold" else "P"
        # A miner selling what it mined ("mined 291.53 BTC ... sold 207.32 BTC") is
        # running its business, not making a treasury decision.
        head = text[max(0, m.start() - 300): m.start()].split(". ")[-1]
        if side == "S" and _MINED_RE.search(head):
            continue
        # Price details live in the rest of the same sentence.
        tail = text[m.end(): m.end() + 300].split(". ")[0]
        avg_m, total_m = _AVG_RE.search(tail), _TOTAL_RE.search(tail)
        avg = _num(avg_m.group("avg")) if avg_m else None
        total = None
        if total_m:
            total = _num(total_m.group("total"))
            mult = (total_m.group("mult") or "").lower()
            total = total * _MULT.get(mult, 1.0) if total else None
        if not _plausible(coin, avg):
            continue
        if total and not _plausible(coin, total / units):
            total = None
        out.append({"coin": coin, "side": side, "units": units, "avg": avg, "total": total})
    for h in _TABLE_HEAD_RE.finditer(text):
        window = text[h.end(): h.end() + 700]
        row = _TABLE_ROW_RE.search(window)
        if not row:
            continue
        coin = h.group("coin").upper()
        units, agg, avg = (_num(row.group(g)) for g in ("units", "agg", "avg"))
        header = window[: row.start()].lower()
        mult = 1e6 if "in millions" in header else 1e9 if "in billions" in header else 1.0
        total = agg * mult if agg else None
        # A misaligned row reads some other column as the price; the three numbers
        # have to agree with each other before any of them is trusted.
        if not (units and avg and total) or abs(units * avg - total) / total > 0.1:
            continue
        if not _plausible(coin, avg):
            continue
        out.append({"coin": coin, "side": "P", "units": units, "avg": avg, "total": total})
    # One trade stated twice in one document (summary sentence + table) is one trade.
    seen, unique = set(), []
    for t in out:
        key = (t["coin"], t["side"], round(t["units"], 6))
        if key not in seen:
            seen.add(key)
            unique.append(t)
    return unique


def _company(display_name: str) -> tuple[str, str | None]:
    m = _DISPLAY_RE.match(display_name or "")
    if not m:
        return (display_name or "").strip(), None
    first = m.group("tickers").split(",")[0].strip().upper()
    return m.group("name").strip(), first or None


def search(start: dt.date, end: dt.date, session: requests.Session) -> list[dict]:
    """EFTS hits (one per document) for every coin query, de-duplicated by doc id."""
    hits: dict[str, dict] = {}
    for q in QUERIES.values():
        for page in range(MAX_PAGES):
            resp = session.get(EFTS_URL, params={
                "q": q, "forms": FORMS, "dateRange": "custom",
                "startdt": start.isoformat(), "enddt": end.isoformat(),
                "from": page * PAGE_SIZE,
            }, timeout=30)
            resp.raise_for_status()
            batch = resp.json().get("hits", {}).get("hits", [])
            for h in batch:
                hits.setdefault(h["_id"], h)
            time.sleep(REQUEST_PAUSE_SECONDS)
            if len(batch) < PAGE_SIZE:
                break
    return list(hits.values())


def doc_url(hit: dict) -> str:
    adsh, filename = hit["_id"].split(":", 1)
    cik = str(int(hit["_source"]["ciks"][0]))
    return DOC_URL.format(cik=cik, adsh=adsh.replace("-", ""), filename=filename)


def txns_from_hit(hit: dict, text: str, cik_lookup=None) -> list[TreasuryTxn]:
    """`cik_lookup` (a cik_map.CikMap) supplies the company's primary ticker. EFTS
    lists every class alphabetically -- BitMine shows as "BMNP, BMNR", preferred
    first -- so the display name's first ticker is only the fallback."""
    src = hit["_source"]
    company, ticker = _company((src.get("display_names") or [""])[0])
    cik = (src.get("ciks") or [""])[0]
    if cik_lookup is not None:
        ticker = cik_lookup.ticker(cik) or ticker
    adsh = hit["_id"].split(":", 1)[0]
    return [
        TreasuryTxn(
            accession=adsh, company=company, ticker=ticker,
            cik=cik, coin=t["coin"], side=t["side"],
            units=t["units"], avg_price_usd=t["avg"], total_usd=t["total"],
            filed_date=src.get("file_date", ""), form=src.get("form", ""),
            source_url=doc_url(hit),
        )
        for t in parse_text(text)
    ]


def scan_new_filings(start: dt.date, end: dt.date, seen_doc_ids: set[str],
                     session: requests.Session | None = None, cik_lookup=None):
    """Yield (doc_id, [TreasuryTxn]) for every not-yet-seen matching document."""
    session = session or new_session()
    for hit in search(start, end, session):
        doc_id = hit["_id"]
        if doc_id in seen_doc_ids:
            continue
        resp = session.get(doc_url(hit), timeout=30)
        time.sleep(REQUEST_PAUSE_SECONDS)
        if resp.status_code == 404:
            yield doc_id, []
            continue
        resp.raise_for_status()
        yield doc_id, txns_from_hit(hit, html_to_text(resp.text), cik_lookup)
