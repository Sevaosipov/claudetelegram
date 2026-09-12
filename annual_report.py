"""Annual-report content for a company: financial figures pulled straight from
the filing's own XBRL data, and a small set of specific, standardized-language
red flags (going-concern doubt, an actually-identified material weakness, a real
restatement) found by text search and quoted verbatim.

SEC filers only (10-K / 10-K-A / 20-F / 20-F-A) -- see the design doc,
docs/superpowers/specs/2026-09-12-annual-report-design.md, for why the European
sources aren't here yet. Nothing here summarizes or characterizes anything:
every red flag is the filing's own sentence, quoted, with a link to it; every
financial figure is the filing's own number, shown with its YoY delta and
nothing else. This module has no CLI -- research.py is its only consumer,
matching tradingview.py's shape.
"""
from __future__ import annotations

import re

import requests

_ANNUAL_FORMS = ("10-K", "10-K/A", "20-F", "20-F/A")


def find_latest_annual_filing(cik: str, session: requests.Session | None = None) -> dict | None:
    """The most recently filed annual report on record for this CIK, or None.

    Only looks at the submissions feed's "recent" window -- the same limitation
    research.recent_filings already has; a company whose only annual filing has
    aged out of that window returns None here too.
    """
    import universe
    session = session or requests.Session()
    try:
        resp = session.get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json",
                            headers=universe.sec_headers(), timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError, TypeError):
        return None

    recent = data.get("filings", {}).get("recent", {})
    fiscal_years = recent.get("fy", [])
    for i, (form, filed, doc, acc) in enumerate(zip(
            recent.get("form", []), recent.get("filingDate", []),
            recent.get("primaryDocument", []), recent.get("accessionNumber", []))):
        if form not in _ANNUAL_FORMS:
            continue
        return {
            "form": form,
            "filed": filed,
            "fiscal_year": fiscal_years[i] if i < len(fiscal_years) else None,
            "accession": acc,
            "primary_document": doc,
            "url": (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                     f"{acc.replace('-', '')}/{doc}"),
        }
    return None


def fetch_filing_text(cik: str, accession: str, primary_document: str,
                       session: requests.Session | None = None) -> str | None:
    """The filing's primary document, HTML tags and entities stripped to plain
    text. None on any fetch failure."""
    import universe
    session = session or requests.Session()
    try:
        url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
               f"{accession.replace('-', '')}/{primary_document}")
        resp = session.get(url, headers=universe.sec_headers(), timeout=30)
        resp.raise_for_status()
        html = resp.text
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        return None
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;|&amp;|&lt;|&gt;|&#\d+;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()
