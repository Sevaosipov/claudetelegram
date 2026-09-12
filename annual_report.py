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


_RED_FLAGS = (
    ("going_concern",
     ("substantial doubt about", "substantial doubt regarding"),
     "существенные сомнения в способности продолжать деятельность (going concern)"),
    ("material_weakness",
     ("identified a material weakness", "identified one or more material weaknesses",
      "disclosure controls and procedures were not effective",
      "internal control over financial reporting was not effective"),
     "выявленный существенный недостаток внутреннего контроля"),
    ("restatement",
     ("restated its previously issued financial statements",
      "restatement of our previously issued financial statements",
      "we determined to restate"),
     "пересчёт (restatement) ранее опубликованной отчётности"),
)


def _quote_around(text: str, idx: int, phrase_len: int) -> str:
    start = max(0, idx - 150)
    end = idx + phrase_len + 250
    return re.sub(r"\s+", " ", text[start:end]).strip()


def find_red_flags(text: str) -> list[dict]:
    """Which of _RED_FLAGS' specific, standardized disclosures appear in `text`
    (case-insensitive search, original-case quote in the result). [] if none --
    checked, found nothing, which is different from "could not check"."""
    lowered = text.lower()
    found = []
    for key, phrases, label in _RED_FLAGS:
        for phrase in phrases:
            idx = lowered.find(phrase)
            if idx >= 0:
                found.append({"key": key, "label": label,
                              "quote": _quote_around(text, idx, len(phrase))})
                break
    return found


_FACT_CONCEPTS = (
    ("revenue", ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"), "Выручка"),
    ("net_income", ("NetIncomeLoss",), "Чистая прибыль"),
    ("assets", ("Assets",), "Активы"),
    ("liabilities", ("Liabilities",), "Обязательства"),
    ("operating_cf", ("NetCashProvidedByUsedInOperatingActivities",), "Операционный денежный поток"),
)


def _annual_points(facts: dict, concept: str, form_filter: tuple[str, ...]) -> list[dict]:
    """Annual (fp == "FY", form in form_filter) data points for one us-gaap
    concept, sorted by period end, most recent last."""
    units = facts.get("us-gaap", {}).get(concept, {}).get("units", {}).get("USD", [])
    annual = [u for u in units if u.get("fp") == "FY" and u.get("form") in form_filter]
    return sorted(annual, key=lambda u: u.get("end", ""))


def annual_financials(cik: str, form_filter: tuple[str, ...] = _ANNUAL_FORMS,
                       session: requests.Session | None = None) -> dict | None:
    """{key: {"label", "value", "yoy_pct"}} straight from the company's own XBRL
    facts, scoped to an actual annual filing. None only if the companyfacts fetch
    itself fails; a concept with no tagged data (common for IFRS-taxonomy 20-F
    filers, which this pass does not map) is simply absent from the result.
    """
    import universe
    session = session or requests.Session()
    try:
        resp = session.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json",
                            headers=universe.sec_headers(), timeout=30)
        resp.raise_for_status()
        facts = resp.json().get("facts", {})
    except (requests.RequestException, ValueError):
        return None

    out = {}
    for key, concepts, label in _FACT_CONCEPTS:
        points = []
        for concept in concepts:
            points = _annual_points(facts, concept, form_filter)
            if points:
                break
        if not points:
            continue
        values = [p["val"] for p in points[-2:]]
        yoy = None
        if len(values) == 2 and values[0]:
            yoy = (values[1] - values[0]) / abs(values[0]) * 100
        out[key] = {"label": label, "value": values[-1], "yoy_pct": yoy}
    return out
