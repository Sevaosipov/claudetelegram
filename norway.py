"""Fetch and parse Oslo Børs / Euronext Oslo "Newsweb" Managers' Transaction
notifications (category 1102, "MELDEPLIKTIG HANDEL FOR PRIMÆRINNSIDERE") -- the
Norwegian equivalent of SEC Form 4 / BaFin Directors' Dealings. Fully public JSON
API, no login or API key needed (found by reading Newsweb's own JS bundle for its
internal API calls -- there's no published developer documentation).

Unlike SEC/BaFin, transaction details (who, how many shares, at what price) are
NOT structured fields -- they're embedded in free-text prose (mostly English,
sometimes Norwegian) following Oslo Børs's standard notification wording, e.g.
"On 28 August 2026, Kona BidCo AS acquired 1,139 shares in Zalaris ASA... at a
price of NOK 100 per share." We regex-extract action/shares/price/currency from
that text; issuer name, ticker (issuerSign), and date are always reliable
structured JSON fields regardless. A notification whose prose doesn't match a
known phrasing (e.g. an incentive-programme summary, a private placement
announcement) is simply skipped -- we can't verify a $-equivalent value for it,
so it wouldn't pass --min-value or feed the signal detector anyway.
"""
from __future__ import annotations

import datetime as dt
import re

import requests

API_BASE = "https://api3.oslo.oslobors.no"
LIST_URL = API_BASE + "/v1/newsreader/list"
MESSAGE_URL = API_BASE + "/v1/newsreader/message"
MANAGERS_TRANSACTION_CATEGORY = 1102

# Newsweb names an issuer only by its Oslo ticker, but Trading 212 sells Norwegian
# companies solely as EUR listings in Frankfurt, keyed by ISIN (trading212.py) -- so
# the ticker has to be turned into an ISIN. Euronext's own instrument search does
# that exactly; yfinance's `isin` does not (it returned an Indian ISIN for Orkla).
EURONEXT_SEARCH_URL = "https://live.euronext.com/en/instrumentSearch/searchJSON"
# Oslo's markets: the main list, Euronext Expand, Euronext Growth. Anything else in a
# search result -- DOSL (Oslo derivatives), another exchange -- is another instrument.
OSLO_MICS = {"XOSL", "XOAS", "MERK"}
ISIN_MISS_TTL_DAYS = 7     # re-ask about a ticker Euronext didn't know after a week
_SYMBOL_RE = re.compile(r"class='symbol'>([^<]+)<")

_ACTION_RE = re.compile(r"\b(acquired|purchased|bought|kjøpt|sold|solgt)\b", re.I)
# Both number patterns require the match to END on a digit -- without that, a
# sentence-ending period right after the real number ("...NOK 218.50. Following
# ...") gets swept into the capture and corrupts decimal-vs-thousands detection.
_SHARES_RE = re.compile(r"(\d(?:[\d .,]*\d)?)\s*(?:shares?|stk\.?\s*egenkapitalbevis|aksjer)", re.I)
_PRICE_RE = re.compile(r"(?:price of|til kurs|til en kurs på|pris)\s*(NOK|SEK|EUR|USD|DKK)?\s*(\d(?:[\d .,]*\d)?)", re.I)
_DATE_PREFIX_RE = re.compile(r"^(?:On\s+)?\d{1,2}\.?\s*[A-Za-zæøåÆØÅ]*\.?\s*\d{4},?\s*", re.I)
_SELL_VERBS = {"sold", "solgt"}
_VERB_ALT = r"(?:acquired|purchased|bought|sold)"

# Better-targeted actor extraction for the common English "<dateline>, <actor>,
# ... <verb>" phrasings -- found by SEARCHING anywhere in the prefix (not
# anchored to body start like _DATE_PREFIX_RE below), so it isn't thrown off by
# the "<timestamp> CEST | <issuer> | Managers' transaction" header or a
# "<city>, <date>" dateline that Oslo Børs often prepends before the actual
# sentence. Tried in order; falls through to the older whole-prefix heuristic
# (_extract_person's tail) when none match -- mostly Norwegian-language notices
# and a handful of unusual passive-voice/aggregate-purchase phrasings.
_PAREN_DATE_ACTOR_RE = re.compile(
    r"\([A-Za-zæøåÆØÅ]+,\s*\d{1,2}\.?\s+[A-Za-zæøåÆØÅ]+\.?\s+\d{4}\)\s*"
    r"(.+?)(?=,|\s+(?:today\s+)?" + _VERB_ALT + r"\b)", re.I | re.S)
# 'On' must stay capitalized -- lower-case 'on' shows up mid-sentence in
# constructs like '...has on 19 August 2026, purchased...' where the actor is
# BEFORE 'on', not after it. Scoping case-sensitivity to just this literal (via
# the inline (?-i:...) group) keeps the rest of the pattern (month names etc,
# not ambiguous the same way) case-insensitive.
_ON_DATE_ACTOR_RE = re.compile(
    r"\b(?-i:On)\s+(?:\d{1,2}\.?\s+[A-Za-zæøåÆØÅ]+\.?\s+\d{4}"
    r"|[A-Za-zæøåÆØÅ]+\.?\s+\d{1,2}\.?,?\s+\d{4}),\s*"
    r"(.+?)(?=,|\s+" + _VERB_ALT + r"\b)", re.I | re.S)
_TODAY_HAS_VERB_RE = re.compile(r"\bToday,\s*(.+?)\s+has\s+" + _VERB_ALT + r"\b", re.I | re.S)
# Broadest/last-resort of the four: "<Actor>, <role clause>, has <verb>" with no
# date anchor at all. Each bracketed group is comma-bounded by construction, so
# it can't itself run past a role clause -- but when a sentence has *more* than
# one appositive clause before "has <verb>" (e.g. "Drew Holdings Ltd., a close
# associate of Mr. X, a director of ..., has purchased"), re.search's automatic
# retry-from-a-later-position can still land on the wrong (but still real, not
# garbage) name. Tried last, after the more reliably-anchored patterns above.
_ACTOR_ROLE_HAS_VERB_RE = re.compile(
    r"([A-ZÆØÅ][^,\n]{1,60}?),\s*[^,\n]{1,120}?,\s*has\s+" + _VERB_ALT + r"\b", re.S)

# A single legitimate PDMR trade essentially never totals above this many NOK
# (~$130M at typical rates) -- if shares*price exceeds it, something in the free
# text got mis-extracted (e.g. a phone/reference number, or a multi-person batch
# announcement where our single "shares...price" regex grabbed unrelated parts).
# Better to silently drop a plausible-looking-but-wrong number than alert on it.
_SANITY_MAX_NOK_EQUIVALENT = 1_500_000_000

# Rough currency -> NOK multipliers, used ONLY for the sanity ceiling above --
# it just needs the right order of magnitude to catch a mis-extracted number, so
# hardcoding is fine here. Anything user-facing (thresholds, signal amounts) goes
# through fx.py's real, cached rates instead. Storage stays in native currency.
NOK_PER_UNIT = {"NOK": 1, "SEK": 1, "DKK": 1.5, "EUR": 11, "USD": 10}


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "disclosure-bot research contact@example.com"})
    return s


def _parse_shares(s: str) -> float | None:
    digits = re.sub(r"[ ,.]", "", s.strip())
    return float(digits) if digits else None


def _parse_price(s: str) -> float | None:
    s = s.strip().replace(" ", "")
    if not s:
        return None
    has_comma, has_dot = "," in s, "." in s
    if has_comma and has_dot:
        # Whichever separator appears last is the decimal point; the other is
        # a thousands grouping (handles both "1,234.56" and "1.234,56" styles).
        dec_char = "," if s.rfind(",") > s.rfind(".") else "."
        thou_char = "." if dec_char == "," else ","
        return float(s.replace(thou_char, "").replace(dec_char, "."))
    if has_comma or has_dot:
        # Exactly one separator. Per-share prices in these notices are never
        # written with thousands grouping in practice (Norwegian uses a space
        # for that, e.g. "35 000"), so treat it as the decimal point regardless
        # of how many digits follow -- averaged prices like "454.7355" are common.
        sep = "," if has_comma else "."
        return float(s.replace(sep, "."))
    return float(s)


def _plausible_actor(s: str) -> bool:
    """Defensive cap against a regex capturing way more than an actor name (seen
    with a couple of passive-voice/aggregate-purchase announcements where none of
    the targeted patterns actually apply) -- a real name/entity is never this
    long or multi-line, so fall through to the next candidate instead."""
    return bool(s) and len(s) <= 80 and "\n" not in s


def _extract_person(body: str, verb_start: int) -> str:
    """Best-effort actor name. Tries the more targeted <dateline>-anchored
    patterns above first (search anywhere in the prefix, so they aren't thrown
    off by header/dateline boilerplate before the actual sentence); falls back
    to the older "everything before the verb" heuristic -- trimmed of a leading
    date prefix and any trailing appositive clause ("..., a person closely
    associated with ...,") -- when none of those match. Imperfect for some
    Norwegian phrasings and unusual passive-voice announcements."""
    window = body[:verb_start + 20]  # a little slack past verb_start for the lookahead
    for rx in (_PAREN_DATE_ACTOR_RE, _ON_DATE_ACTOR_RE, _TODAY_HAS_VERB_RE, _ACTOR_ROLE_HAS_VERB_RE):
        m = rx.search(window)
        if m:
            actor = m.group(1).strip()
            if _plausible_actor(actor):
                return actor

    prefix = body[:verb_start]
    prefix = _DATE_PREFIX_RE.sub("", prefix).strip()
    if "," in prefix:
        prefix = prefix.split(",", 1)[0].strip()
    # Trim trailing Norwegian filler words ("har i dag kjøpt" -> drop "har i dag").
    prefix = re.sub(r"\s+(har\s+i\s+dag|har|i\s+dag)$", "", prefix, flags=re.I).strip()
    if not prefix:
        return "?"
    if not _plausible_actor(prefix):
        # A handful of unusual templates (correction notices, third-party
        # "has been notified that..." announcements) have no clean actor for
        # this heuristic to land on and it grabs a whole boilerplate paragraph
        # instead -- collapse to one line and cap the length so it can't break
        # a Telegram message's formatting, rather than trying to perfect every
        # such template.
        prefix = re.sub(r"\s+", " ", prefix)[:80].strip()
    return prefix


def parse_transaction(body: str) -> dict | None:
    """Returns {person, txn_type, shares, price, currency} or None if the body
    text doesn't match a recognizable "person bought/sold N shares at price P"
    pattern (many Managers'-Transaction-category notices are summaries, private
    placements, incentive programmes, etc. rather than a single reportable trade)."""
    action = _ACTION_RE.search(body)
    shares = _SHARES_RE.search(body)
    price = _PRICE_RE.search(body)
    if not (action and shares and price):
        return None
    shares_val = _parse_shares(shares.group(1))
    price_val = _parse_price(price.group(2))
    if not shares_val or not price_val:
        return None
    currency = (price.group(1) or "NOK").upper()
    nok_equiv = shares_val * price_val * NOK_PER_UNIT.get(currency, 1)
    if nok_equiv > _SANITY_MAX_NOK_EQUIVALENT:
        return None
    return {
        "person": _extract_person(body, action.start()),
        "txn_type": "S" if action.group(1).lower() in _SELL_VERBS else "P",
        "shares": shares_val,
        "price": price_val,
        "currency": currency,
    }


def fetch_message_list(from_date: dt.date, to_date: dt.date, session: requests.Session | None = None) -> list[dict]:
    session = session or new_session()
    params = {
        "category": MANAGERS_TRANSACTION_CATEGORY,
        "issuer": "",
        "fromDate": from_date.isoformat(),
        "toDate": to_date.isoformat(),
        "market": "",
        "messageTitle": "",
    }
    resp = session.get(LIST_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()["data"]["messages"]


def fetch_message_detail(message_id: int, session: requests.Session | None = None) -> dict:
    session = session or new_session()
    resp = session.get(MESSAGE_URL, params={"messageId": message_id}, timeout=30)
    resp.raise_for_status()
    return resp.json()["data"]["message"]


def scan_new_filings(from_date: dt.date, to_date: dt.date, seen_message_ids: set[int],
                      session: requests.Session | None = None):
    """Yields (message_id, dict|None) for every not-yet-seen Managers' Transaction
    notification in the date range -- the dict is None when parse_transaction()
    couldn't confidently extract a trade from the body text (still yielded so the
    caller can mark it seen and not re-check it every run)."""
    session = session or new_session()
    for msg in fetch_message_list(from_date, to_date, session=session):
        mid = msg["id"]
        if mid in seen_message_ids:
            continue
        detail = fetch_message_detail(mid, session=session)
        txn = parse_transaction(detail.get("body", ""))
        if txn is not None:
            txn.update({
                "message_id": mid,
                "issuer_name": detail.get("issuerName", "").strip(),
                "ticker": detail.get("issuerSign", "").strip(),
                "txn_date": detail.get("publishedTime", "")[:10],  # ISO YYYY-MM-DD
                "source_url": f"https://newsweb.oslobors.no/message/{mid}",
            })
        yield mid, txn


def isin_for_ticker(ticker: str, session=None) -> str | None:
    """The ISIN of the Oslo listing with exactly this symbol; "" when Euronext has no
    such listing; None when the search couldn't be done (unknown is not "none").

    Only an exact symbol on an Oslo market counts: searching "BORR" also returns
    Borregaard (BRG), and "ORK" returns Orkla's stock options on DOSL.
    """
    try:
        resp = (session or requests).get(EURONEXT_SEARCH_URL, params={"q": ticker},
                                          headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        resp.raise_for_status()
        results = resp.json()
    except (requests.RequestException, ValueError):
        return None
    want = ticker.strip().upper()
    for r in results if isinstance(results, list) else []:
        m = _SYMBOL_RE.search(r.get("label") or "")
        if (r.get("mic") in OSLO_MICS and r.get("isin") and m
                and m.group(1).strip().upper() == want):
            return r["isin"].strip().upper()
    return ""


def cached_isin(conn, ticker: str, session=None) -> str | None:
    """isin_for_ticker with a database cache: an ISIN is kept for good, a "no such
    listing" answer for ISIN_MISS_TTL_DAYS, a failed search not at all."""
    ticker = ticker.strip().upper()
    row = conn.execute("SELECT isin, fetched_at FROM oslo_isins WHERE ticker = ?",
                       (ticker,)).fetchone()
    if row:
        isin, fetched = row
        age = dt.datetime.now() - dt.datetime.fromisoformat(fetched)
        if isin or age.days < ISIN_MISS_TTL_DAYS:
            return isin
    isin = isin_for_ticker(ticker, session)
    if isin is not None:
        conn.execute("INSERT OR REPLACE INTO oslo_isins (ticker, isin, fetched_at) VALUES (?,?,?)",
                     (ticker, isin, dt.datetime.now().isoformat(timespec="seconds")))
        conn.commit()
    return isin
