"""Public companies buying (or selling) crypto for their own balance sheet, from SEC
8-K and 6-K filings -- Strategy (MSTR) and its imitators.

There is no structured form for this: a company that buys bitcoin says so in an 8-K
press release, in prose ("purchased 1,355 bitcoin at an average price of
approximately $79,475 per bitcoin") or, in Strategy's case, in a small table
("BTC Purchased ... 950 $ 75.7 $ 79,670"). So this module:

  1. asks EDGAR full-text search (efts.sec.gov, free, no key) which 8-K/6-K
     documents filed in a date range mention one of the thirteen coins at all
     (QUERIES: bitcoin, ether, solana, XRP, BNB, dogecoin, AVAX, hyperliquid,
     litecoin, ethena, chainlink, TRX/TRON, SUI) -- a few dozen a fortnight for
     bitcoin, most of them miners describing production;
  2. fetches each new document once and regexes the transactions back out.

Only an explicit past-tense trade with a unit count is extracted. A miner "acquired
1,000 bitcoin mining machines" is not a bitcoin purchase, "may purchase up to" is not
a purchase, and a document that merely mentions bitcoin yields nothing -- a missed
filing costs one signal, a fabricated one costs trust in all the others. An alt's full
name (solana, dogecoin ...) is read in any case, as bitcoin's is; its ticker only in
upper case ("5,000,000 HYPE tokens", not "hype" or "link" or "sol": ordinary words).

The same sentence routinely appears twice in one accession (the 8-K body and its
EX-99.1 press release), so rows are keyed by the trade, not by the document.
"""
from __future__ import annotations

import datetime as dt
import html
import re
import sys
import time
from dataclasses import dataclass

import requests

EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{adsh}/{filename}"
FORMS = "8-K,6-K"
# One query per coin: EFTS scores rather than filters, so a combined query ranks a
# long tail of passing mentions above the filings that matter.
QUERIES = {
    "BTC": '"bitcoin"', "ETH": '"ether" OR "ethereum"',
    "SOL": '"solana"', "XRP": '"XRP"', "BNB": '"BNB"', "DOGE": '"dogecoin"', "AVAX": '"AVAX"',
    "HYPE": '"hyperliquid"', "LTC": '"litecoin"', "ENA": '"ethena"', "LINK": '"chainlink"',
    "TRX": '"TRX" OR "TRON"', "SUI": '"SUI"',
}
PAGE_SIZE = 100
MAX_PAGES = 5
REQUEST_PAUSE_SECONDS = 0.15   # SEC fair access: <=10 req/s across its hosts
# EFTS and the archive now and then answer one request with a 5xx (or drop the
# connection) and the identical request succeeds seconds later. A year's backfill
# makes thousands of requests, so one such blip must not end it. One pause per retry:
# three attempts in all, then the failure is raised and the source reported as failed.
RETRY_STATUSES = {429, 500, 502, 503, 504}
RETRY_PAUSES_SECONDS = (2, 5)

# Sanity bounds on a parsed per-coin price. A misread thousands separator turns
# $79,475 into $79 or $79,475,000; either is rejected rather than stored.
PLAUSIBLE_PRICE_USD = {
    "BTC": (1_000, 1_000_000), "ETH": (50, 100_000), "SOL": (5, 5_000), "XRP": (0.05, 100),
    "BNB": (20, 20_000), "DOGE": (0.005, 20), "AVAX": (1, 2_000), "HYPE": (1, 5_000),
    "LTC": (5, 5_000), "ENA": (0.02, 100), "LINK": (1, 2_000), "TRX": (0.01, 20), "SUI": (0.1, 500),
}

# What a matched word means, lower-cased. The patterns below decide WHICH words match and in what
# case: an alt's full name in any case, as bitcoin's always was; its ticker only in upper case --
# "link", "hype", "sol" and "sui" are ordinary words in a filing -- optionally followed by
# "tokens" or "coins". BTC, ETH and their names keep matching in any case.
_COIN_WORDS = {"bitcoin": "BTC", "bitcoins": "BTC", "btc": "BTC",
               "ether": "ETH", "ethereum": "ETH", "eth": "ETH",
               "solana": "SOL", "sol": "SOL", "xrp": "XRP", "bnb": "BNB",
               "dogecoin": "DOGE", "doge": "DOGE", "avalanche": "AVAX", "avax": "AVAX",
               "hyperliquid": "HYPE", "hype": "HYPE", "litecoin": "LTC", "ltc": "LTC",
               "ethena": "ENA", "ena": "ENA", "chainlink": "LINK", "link": "LINK",
               "trx": "TRX", "tron": "TRX", "sui": "SUI"}
_ALT_NAMES = "solana|dogecoin|litecoin|avalanche|chainlink|hyperliquid|ethena"
_ALT_TICKERS = "SOL|XRP|BNB|DOGE|AVAX|HYPE|LTC|ENA|LINK|TRX|TRON|SUI"
# Scoped flags: the patterns below are compiled with IGNORECASE (the verbs), `(?-i:...)` turns it off.
_COIN_PATTERN = rf"(?i:bitcoins?|BTC|ether|ETH|ethereum|{_ALT_NAMES})|(?-i:{_ALT_TICKERS})"
_TABLE_COIN_PATTERN = rf"(?i:BTC|ETH|{_ALT_NAMES})|(?-i:{_ALT_TICKERS})"
_NUM = r"\d[\d,]*(?:\.\d+)?"
_PROSE_RE = re.compile(
    rf"\b(?P<verb>purchased|acquired|bought|sold)\s+"
    rf"(?:an?\s+(?:aggregate|total)\s+(?:of\s+)?)?(?:approximately\s+|about\s+|roughly\s+)?"
    rf"(?P<units>{_NUM})\s+(?:(?:additional|more)\s+)?"
    rf"(?P<coin>{_COIN_PATTERN})\b(?:\s+(?:tokens?|coins?)\b)?"
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
_TABLE_HEAD_RE = re.compile(rf"\b(?P<coin>{_TABLE_COIN_PATTERN})\s+(?:Purchased|Acquired)\b", re.IGNORECASE)
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
    coin: str               # a key of QUERIES: "BTC", "ETH", "SOL" ...
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
        coin = _COIN_WORDS[h.group("coin").lower()]
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


def _get(session: requests.Session, url: str, params: dict | None = None) -> requests.Response:
    """session.get, retried after a pause on a transient failure. The last attempt's
    response is returned whatever its status (the caller's raise_for_status decides),
    and its connection error is raised; anything else, a 404 included, is returned
    at once."""
    for pause in (*RETRY_PAUSES_SECONDS, None):
        try:
            resp = session.get(url, params=params, timeout=30)
        except (requests.ConnectionError, requests.Timeout) as e:
            if pause is None:
                raise
            problem = f"{type(e).__name__}: {e}"
        else:
            if pause is None or resp.status_code not in RETRY_STATUSES:
                return resp
            problem = f"HTTP {resp.status_code}"
        print(f"[crypto_treasury] {url} failed ({problem}); retrying in {pause}s",
              file=sys.stderr)
        time.sleep(pause)
    raise AssertionError("unreachable")


def _search_one(q: str, start: dt.date, end: dt.date, session: requests.Session,
                hits: dict[str, dict]) -> None:
    """The pages of one coin's query, each hit added to `hits` (keyed by doc id) as its page is
    read. Each request is _get's -- retried after a pause on a transient failure -- and followed by
    the fair-access pause."""
    for page in range(MAX_PAGES):
        resp = _get(session, EFTS_URL, params={
            "q": q, "forms": FORMS, "dateRange": "custom",
            "startdt": start.isoformat(), "enddt": end.isoformat(),
            "from": page * PAGE_SIZE,
        })
        resp.raise_for_status()
        batch = resp.json().get("hits", {}).get("hits", [])
        for h in batch:
            hits.setdefault(h["_id"], h)
        time.sleep(REQUEST_PAUSE_SECONDS)
        if len(batch) < PAGE_SIZE:
            break


def search(start: dt.date, end: dt.date, session: requests.Session) -> list[dict]:
    """EFTS hits (one per document) for every coin query, de-duplicated by doc id.

    One coin's query failing does not lose the others. A query that still fails after _get's
    retries (HTTP error, dropped connection, an answer that is not JSON) is reported on stderr and
    skipped -- the hits its earlier pages gave are kept -- and the search goes on with the next coin.
    It raises, with the first failure, only when EVERY query failed, so bot._run_source reports the
    source as failed rather than as quiet. A document a failed query missed is found on a later
    run: the trailing window is searched every day, and a document is marked seen only once read.

    (Before the alts were added the search ran bitcoin's query and then ether's, and the first
    failure -- after the same retries -- ended it: the other coin's hits were thrown away and the
    whole pass raised. With thirteen queries a single bad answer must not cost the day's filings.)"""
    hits: dict[str, dict] = {}
    failed: list[Exception] = []
    for coin, q in QUERIES.items():
        try:
            _search_one(q, start, end, session, hits)
        except (requests.RequestException, ValueError, KeyError, TypeError) as e:
            failed.append(e)
            print(f"[crypto_treasury] {coin} query failed: {type(e).__name__}: {str(e)[:120]}",
                  file=sys.stderr)
    if failed and len(failed) == len(QUERIES):
        raise failed[0]
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
        resp = _get(session, doc_url(hit))
        time.sleep(REQUEST_PAUSE_SECONDS)
        if resp.status_code == 404:
            yield doc_id, []
            continue
        resp.raise_for_status()
        yield doc_id, txns_from_hit(hit, html_to_text(resp.text), cik_lookup)


BACKFILL_SLICE_DAYS = 7   # EFTS returns at most MAX_PAGES x PAGE_SIZE hits per query


def backfill(conn, days: int, today: dt.date | None = None, scan=None, cik_lookup=None,
             reread: bool = False) -> int:
    """Read the past `days` of 8-K/6-K filings, one week-long slice at a time, through
    the same paced scanner and parser the daily run uses. A document already read is
    skipped, and each one is committed as it is read, so an interrupted backfill
    resumes where it stopped. Newest slice first: an interrupted run then leaves an
    unbroken recent history rather than an old filing that makes the missing months
    look like weeks without buying. Returns how many new trades were stored.

    `reread=True` (--reread) ignores the seen-document set for this run: a filing read before the
    parser knew a coin (the BTC/ETH-only one) yields that coin's trades now. The inserts are
    idempotent (the trade is the primary key, INSERT OR IGNORE), so what is stored is not
    duplicated, and every document read is still marked seen -- nothing is ever removed from
    crypto_treasury_seen. The daily pass never rereads."""
    import db
    scan = scan or scan_new_filings
    today = today or dt.date.today()
    seen = set() if reread else db.crypto_treasury_seen(conn)
    new = 0
    earliest = today - dt.timedelta(days=days)
    end = today
    while end >= earliest:
        start = max(end - dt.timedelta(days=BACKFILL_SLICE_DAYS - 1), earliest)
        print(f"[backfill] {start.isoformat()}..{end.isoformat()}")
        for doc_id, txns in scan(start, end, seen, cik_lookup=cik_lookup):
            for t in txns:
                if db.save_crypto_treasury_txn(conn, t):
                    new += 1
            db.mark_crypto_treasury_seen(conn, doc_id)
            seen.add(doc_id)
            conn.commit()
        end = start - dt.timedelta(days=1)
    return new


def main(argv: list[str] | None = None) -> int:
    import argparse
    from pathlib import Path

    import cik_map
    import db
    ap = argparse.ArgumentParser(description="Company crypto treasury trades from 8-K/6-K filings.")
    ap.add_argument("--backfill", type=int, metavar="DAYS", required=True,
                    help="read this many past days of filings (one-time: 365 gives the weekly "
                         "company-demand signal its year of history)")
    ap.add_argument("--reread", action="store_true",
                    help="also read the documents already read (the first backfill after the alts were added: "
                         "the parser read those filings for bitcoin and ether only); stored trades are not "
                         "duplicated and no document is forgotten")
    args = ap.parse_args(argv)
    conn = db.connect(Path(__file__).parent / "data" / "disclosures.db")
    try:
        cik_lookup = cik_map.CikMap()
    except Exception as e:
        print(f"[backfill] no CIK->ticker map ({e}); using EDGAR's first-listed ticker")
        cik_lookup = None
    options = {"reread": True} if args.reread else {}      # without the flag: called exactly as before
    new = backfill(conn, args.backfill, cik_lookup=cik_lookup, **options)
    print(f"[backfill] {new} new trade(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
