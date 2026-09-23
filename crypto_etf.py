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
