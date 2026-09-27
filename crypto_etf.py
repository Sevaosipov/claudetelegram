"""Spot crypto ETF flows, measured from the issuer's own published share count.

A spot ETF creates shares when money comes in and redeems them when it leaves, so the
day-over-day change in shares outstanding times NAV *is* the net flow -- no
aggregator needed. That matters because the aggregators aren't usable here: Farside
sits behind a Cloudflare challenge (HTTP 403 to anything but a browser) and the rest
want an API key. yfinance reports no share count for these funds at all.

BlackRock publishes both numbers on each fund's public product page ("Shares
Outstanding 1,396,640,000 as of Sep 22, 2026" and a "NAV as of" value), and IBIT and
ETHA are the largest spot bitcoin and ether funds by a wide margin -- so they stand in
for the category. It is a proxy, and the alert says which funds it covers.

The page gives only today's figures, so flows exist from the second daily snapshot
on; snapshots are stored, and every flow is computed from two of them.
"""
from __future__ import annotations

import datetime as dt
import html
import re
from dataclasses import dataclass

import requests

# fund ticker -> (coin, product page)
FUNDS = {
    "IBIT": ("BTC", "https://www.ishares.com/us/products/333011/ishares-bitcoin-trust-etf"),
    "ETHA": ("ETH", "https://www.ishares.com/us/products/337614/ishares-ethereum-trust-etf"),
}
_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

# The page embeds its data as JSON as well as rendering it; the JSON is the sturdier
# of the two, the rendered sentence the fallback.
_SHARES_JSON_RE = re.compile(
    r'"sharesOutstanding"\s*:\s*\{[^{}]*?"formattedValue"\s*:\s*"([\d,]+)"'
    r'[^{}]*?"formattedAsOfDate"\s*:\s*"(\w{3}\s+\d{1,2},\s+\d{4})"')
_SHARES_RE = re.compile(r"Shares Outstanding\s+([\d,]+)\s+as of\s+(\w{3}\s+\d{1,2},\s+\d{4})")
_NAV_RE = re.compile(r'"name"\s*:\s*"NAV as of"\s*,\s*"value"\s*:\s*"([\d,.]+)"')


@dataclass
class Snapshot:
    fund: str
    coin: str
    as_of: str                 # ISO date the issuer stamped the figures with
    shares_outstanding: float
    nav_usd: float


def _text(raw: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw))


def parse_page(fund: str, raw_html: str) -> Snapshot | None:
    """Shares outstanding, its as-of date and NAV, or None if any is missing --
    a partial snapshot would compute a flow out of a stale or absent figure."""
    shares_m = _SHARES_JSON_RE.search(raw_html) or _SHARES_RE.search(_text(raw_html))
    nav_m = _NAV_RE.search(raw_html)
    if not shares_m or not nav_m:
        return None
    try:
        as_of = dt.datetime.strptime(re.sub(r"\s+", " ", shares_m.group(2)), "%b %d, %Y").date()
        shares = float(shares_m.group(1).replace(",", ""))
        nav = float(nav_m.group(1).replace(",", ""))
    except ValueError:
        return None
    if shares <= 0 or nav <= 0:
        return None
    return Snapshot(fund=fund, coin=FUNDS[fund][0], as_of=as_of.isoformat(),
                    shares_outstanding=shares, nav_usd=nav)


def fetch_snapshots(session: requests.Session | None = None) -> list[Snapshot]:
    session = session or requests.Session()
    out = []
    for fund, (_coin, url) in FUNDS.items():
        resp = session.get(url, headers=_HEADERS, timeout=40)
        resp.raise_for_status()
        snap = parse_page(fund, resp.text)
        if snap is None:
            raise ValueError(f"{fund}: shares outstanding / NAV not found on the product page "
                             f"-- the page layout has probably changed")
        out.append(snap)
    return out


def flows(snapshots: list[tuple[str, float, float]]) -> list[tuple[str, str, float]]:
    """(prev_as_of, as_of, flow_usd) for each consecutive pair of one fund's
    (as_of, shares, nav) snapshots, oldest first. Valued at the later NAV: shares
    are created or redeemed at that day's NAV."""
    rows = sorted(snapshots)
    return [(a[0], b[0], (b[1] - a[1]) * b[2]) for a, b in zip(rows, rows[1:])]


# Farside Investors publishes every US spot fund's daily net flow, in $ millions, in
# one table per coin -- all the funds, where the issuer pages above cover only
# BlackRock's two. Outflows are in parentheses, "-" is a fund that hasn't reported yet.
FARSIDE_URLS = {"BTC": "https://farside.co.uk/btc/", "ETH": "https://farside.co.uk/eth/"}
FARSIDE_ALL_URLS = {"BTC": "https://farside.co.uk/bitcoin-etf-flow-all-data/",
                    "ETH": "https://farside.co.uk/ethereum-etf-flow-all-data/"}
_TABLE_RE = re.compile(r"<table.*?</table>", re.S | re.I)
_ROW_RE = re.compile(r"<tr.*?</tr>", re.S | re.I)
_CELL_RE = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.S | re.I)
_FUND_RE = re.compile(r"[A-Z]{2,5}")


@dataclass(frozen=True)
class Flow:
    coin: str
    date: str          # ISO
    fund: str
    flow_usd: float    # net, negative for an outflow


def _cells(row: str) -> list[str]:
    return [html.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in _CELL_RE.findall(row)]


def _farside_amount(text: str) -> float | None:
    t = text.replace(",", "").strip()
    negative = t.startswith("(") and t.endswith(")")
    try:
        value = float(t.strip("()"))
    except ValueError:          # "-", "", anything else: no figure yet
        return None
    return (-value if negative else value) * 1e6


def parse_farside(coin: str, raw_html: str) -> list[Flow]:
    """Every (day, fund) figure in the page's flow table, the largest table on it. The
    fund row is the header row whose second cell is a ticker; fee and summary rows
    (Total, Average, Maximum, Minimum) carry no date and are skipped, as is the
    unnamed Total column."""
    tables = _TABLE_RE.findall(raw_html)
    if not tables:
        return []
    funds: list[str] | None = None
    out = []
    for row in _ROW_RE.findall(max(tables, key=len)):
        cells = _cells(row)
        if funds is None:
            if len(cells) > 2 and _FUND_RE.fullmatch(cells[1]):
                funds = cells
            continue
        try:
            day = dt.datetime.strptime(cells[0], "%d %b %Y").date().isoformat()
        except (ValueError, IndexError):
            continue
        for fund, text in zip(funds[1:], cells[1:]):
            if not _FUND_RE.fullmatch(fund):
                continue
            usd = _farside_amount(text)
            if usd is not None:
                out.append(Flow(coin, day, fund, usd))
    return out


def fetch_farside(coin: str, full_history: bool = False,
                  session: requests.Session | None = None) -> list[Flow]:
    url = (FARSIDE_ALL_URLS if full_history else FARSIDE_URLS)[coin]
    resp = (session or requests.Session()).get(url, headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    return parse_farside(coin, resp.text)
