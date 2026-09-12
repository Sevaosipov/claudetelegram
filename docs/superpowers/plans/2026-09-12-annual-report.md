# Annual-report dossier section Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a new dossier section showing a company's own annual-report (10-K/20-F) financial figures and three specific, standardized red-flag disclosures, sourced straight from the filing's own XBRL data and text.

**Architecture:** New standalone library module `annual_report.py` (no CLI, like `tradingview.py`), consumed only by `research.py`'s existing dossier `build()`/`format_report()`.

**Tech Stack:** `requests` (already a dependency), `re` (stdlib), SEC's public `data.sec.gov` submissions and XBRL companyfacts APIs (both already used elsewhere in this project).

**Spec:** `docs/superpowers/specs/2026-09-12-annual-report-design.md`

## Global Constraints

- **No verdict, no summarization, no interpretation.** Every red flag is a verbatim quote plus a link. Every financial figure is the filing's own number with its YoY delta, nothing else.
- **No AI/LLM calls.** Pure deterministic text search and XBRL lookups.
- **Not attached to the daily Telegram digest.** On-demand only, via `research.py`'s dossier.
- **SEC filers only.** No Norway/Sweden/Germany code in this plan.
- A missing red flag is never reported as a clean bill of health — the report always says which specific disclosures were checked and not found.
- `annual_report.py` has no `main()`/CLI — it is a library module, matching `tradingview.py`.
- Every network-touching function returns `None` (or `[]` for `find_red_flags`) on any failure — never raises out to its caller.

---

### Task 1: SEC filing lookup and document fetch

**Files:**
- Create: `annual_report.py`
- Test: `tests/test_annual_report.py`

**Interfaces:**
- Produces: `_ANNUAL_FORMS: tuple[str, ...]`, `find_latest_annual_filing(cik: str, session: requests.Session | None = None) -> dict | None`, `fetch_filing_text(cik: str, accession: str, primary_document: str, session: requests.Session | None = None) -> str | None`

- [ ] **Step 1: Write the failing tests**

```python
"""annual_report.py -- offline. requests calls monkeypatched throughout, no network."""
from __future__ import annotations

import pytest

import annual_report as ar


class _FakeResp:
    def __init__(self, json_data=None, text="", status=200):
        self._json = json_data
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._json


def _submissions(forms, filed=None, docs=None, accessions=None, fys=None):
    n = len(forms)
    return {"filings": {"recent": {
        "form": forms,
        "filingDate": filed or [f"2026-0{i+1}-01" for i in range(n)],
        "primaryDocument": docs or [f"doc{i}.htm" for i in range(n)],
        "accessionNumber": accessions or [f"0000000000-26-00000{i}" for i in range(n)],
        "fy": fys if fys is not None else [2025] * n,
    }}}


def test_find_latest_annual_filing_picks_the_first_annual_form(monkeypatch):
    data = _submissions(["8-K", "10-K", "10-Q"],
                         filed=["2026-01-01", "2026-02-01", "2026-03-01"],
                         accessions=["a0", "a1", "a2"], docs=["d0.htm", "d1.htm", "d2.htm"])

    class FakeSession:
        def get(self, url, headers=None, timeout=None):
            return _FakeResp(json_data=data)

    result = ar.find_latest_annual_filing("320193", session=FakeSession())
    assert result["form"] == "10-K"
    assert result["accession"] == "a1"
    assert result["primary_document"] == "d1.htm"
    assert result["filed"] == "2026-02-01"
    assert result["fiscal_year"] == 2025
    assert result["url"] == "https://www.sec.gov/Archives/edgar/data/320193/a1/d1.htm"


def test_find_latest_annual_filing_accepts_20f_and_amendments(monkeypatch):
    for form in ("10-K/A", "20-F", "20-F/A"):
        data = _submissions([form])

        class FakeSession:
            def get(self, url, headers=None, timeout=None):
                return _FakeResp(json_data=data)

        assert ar.find_latest_annual_filing("1", session=FakeSession())["form"] == form


def test_find_latest_annual_filing_none_when_no_annual_form_present():
    data = _submissions(["8-K", "10-Q", "4"])

    class FakeSession:
        def get(self, url, headers=None, timeout=None):
            return _FakeResp(json_data=data)

    assert ar.find_latest_annual_filing("1", session=FakeSession()) is None


def test_find_latest_annual_filing_none_on_request_failure():
    class FakeSession:
        def get(self, url, headers=None, timeout=None):
            return _FakeResp(status=404)

    assert ar.find_latest_annual_filing("1", session=FakeSession()) is None


def test_find_latest_annual_filing_survives_a_short_fy_array():
    """The 'fy' array can legitimately be shorter than 'form' for older filings."""
    data = _submissions(["10-K"], fys=[])
    data["filings"]["recent"]["fy"] = []

    class FakeSession:
        def get(self, url, headers=None, timeout=None):
            return _FakeResp(json_data=data)

    result = ar.find_latest_annual_filing("1", session=FakeSession())
    assert result["fiscal_year"] is None


def test_fetch_filing_text_strips_tags_and_entities():
    html = "<html><body><p>Item 1.&nbsp;Business.</p><p>Revenue&amp;growth.</p></body></html>"

    class FakeSession:
        def get(self, url, headers=None, timeout=None):
            return _FakeResp(text=html)

    text = ar.fetch_filing_text("1", "0000000000-26-000001", "doc.htm", session=FakeSession())
    assert "<" not in text and ">" not in text
    assert "Item 1." in text and "Business." in text
    assert "Revenue" in text and "growth." in text


def test_fetch_filing_text_none_on_request_failure():
    class FakeSession:
        def get(self, url, headers=None, timeout=None):
            return _FakeResp(status=500)

    assert ar.fetch_filing_text("1", "0000000000-26-000001", "doc.htm", session=FakeSession()) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Desktop/disclosure-bot && .venv/bin/python -m pytest tests/test_annual_report.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'annual_report'`

- [ ] **Step 3: Write `annual_report.py`**

```python
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
    except (requests.RequestException, ValueError):
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
    url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
           f"{accession.replace('-', '')}/{primary_document}")
    try:
        resp = session.get(url, headers=universe.sec_headers(), timeout=30)
        resp.raise_for_status()
        html = resp.text
    except requests.RequestException:
        return None
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;|&amp;|&lt;|&gt;|&#\d+;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_annual_report.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add annual_report.py tests/test_annual_report.py
git commit -m "feat(annual_report): SEC filing lookup and document fetch"
```

---

### Task 2: Red-flag detection

**Files:**
- Modify: `annual_report.py` (append)
- Test: `tests/test_annual_report.py` (append)
- Create: `tests/fixtures/annual_report_going_concern.htm`
- Create: `tests/fixtures/annual_report_clean.htm`

**Interfaces:**
- Consumes: nothing from Task 1 (pure text function)
- Produces: `_RED_FLAGS: tuple[tuple[str, tuple[str, ...], str], ...]`, `find_red_flags(text: str) -> list[dict]` (each dict: `{"key", "label", "quote"}`)

- [ ] **Step 1: Write the fixtures**

`tests/fixtures/annual_report_going_concern.htm` — synthetic, but shaped like a
real filing, deliberately including the two boilerplate sentences that false-
positived during design validation (a hypothetical risk-factor sentence about
material weakness, and the universal Rule 10D-1 restatement checkbox) alongside
one real going-concern disclosure:

```html
<html><body>
<p>Item 1. Business. The Company designs, develops, and sells products and
services worldwide.</p>
<p>Item 1A. Risk Factors. If we experience a material weakness in our internal
control over financial reporting, or otherwise fail to maintain effective
internal controls, our ability to accurately report our financial results could
be impaired, which could adversely affect investor confidence and the trading
price of our securities.</p>
<p>Indicate by check mark whether any of the registrant's error corrections
disclosed above are restatements that required a recovery analysis of
incentive-based compensation received by any of the registrant's executive
officers during the relevant recovery period pursuant to Rule 10D-1(b).</p>
<p>Item 7. Management's Discussion and Analysis. In connection with the
preparation of the accompanying consolidated financial statements, management
evaluated whether there are conditions and events, considered in aggregate,
that raise substantial doubt about the Company's ability to continue as a
going concern within one year after the date that these consolidated
financial statements are issued.</p>
</body></html>
```

`tests/fixtures/annual_report_clean.htm` — no red-flag language at all:

```html
<html><body>
<p>Item 1. Business. The Company designs, develops, and sells consumer
electronics products and services worldwide, including smartphones, personal
computers, tablets, and wearables.</p>
<p>Item 1A. Risk Factors. The Company's business is subject to global economic
conditions and financial market volatility, which could affect demand for the
Company's products.</p>
<p>Item 7. Management's Discussion and Analysis. Net sales increased during the
current fiscal year compared to the prior fiscal year, driven primarily by
higher sales of the Company's core products across all geographic segments.</p>
</body></html>
```

- [ ] **Step 2: Write the failing tests** (append to `tests/test_annual_report.py`)

```python
from conftest import fixture_text


def _strip(html: str) -> str:
    import re
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;|&amp;|&lt;|&gt;|&#\d+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def test_find_red_flags_catches_the_real_going_concern_disclosure():
    text = _strip(fixture_text("annual_report_going_concern.htm"))
    flags = ar.find_red_flags(text)
    keys = {f["key"] for f in flags}
    assert "going_concern" in keys
    gc = next(f for f in flags if f["key"] == "going_concern")
    assert "substantial doubt" in gc["quote"].lower()


def test_find_red_flags_ignores_hypothetical_material_weakness_boilerplate():
    """The fixture's Item 1A sentence is generic risk-factor language ('if we
    experience...'), not an actual disclosed weakness -- this is the exact false
    positive naive keyword search produced during design validation."""
    text = _strip(fixture_text("annual_report_going_concern.htm"))
    keys = {f["key"] for f in ar.find_red_flags(text)}
    assert "material_weakness" not in keys


def test_find_red_flags_ignores_the_universal_restatement_checkbox():
    """The fixture's checkbox sentence is the boilerplate every post-2023 10-K
    carries regardless of whether a restatement ever happened."""
    text = _strip(fixture_text("annual_report_going_concern.htm"))
    keys = {f["key"] for f in ar.find_red_flags(text)}
    assert "restatement" not in keys


def test_find_red_flags_empty_on_a_clean_filing():
    text = _strip(fixture_text("annual_report_clean.htm"))
    assert ar.find_red_flags(text) == []


def test_find_red_flags_quote_is_centered_on_the_match():
    text = "x" * 200 + "substantial doubt about the ability to continue" + "y" * 200
    flags = ar.find_red_flags(text)
    quote = flags[0]["quote"]
    assert "substantial doubt" in quote
    # roughly centered: some x's before, some y's after, neither the whole 200
    assert 0 < quote.count("x") < 200
    assert 0 < quote.count("y") < 200
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_annual_report.py -v -k red_flag`
Expected: FAIL with `AttributeError: module 'annual_report' has no attribute 'find_red_flags'`

- [ ] **Step 4: Append to `annual_report.py`**

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_annual_report.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add annual_report.py tests/test_annual_report.py tests/fixtures/annual_report_going_concern.htm tests/fixtures/annual_report_clean.htm
git commit -m "feat(annual_report): red-flag text detection"
```

---

### Task 3: Financial facts from XBRL

**Files:**
- Modify: `annual_report.py` (append)
- Test: `tests/test_annual_report.py` (append)

**Interfaces:**
- Consumes: `_ANNUAL_FORMS` (Task 1)
- Produces: `_FACT_CONCEPTS: tuple[tuple[str, tuple[str, ...], str], ...]`, `annual_financials(cik: str, form_filter: tuple[str, ...] = _ANNUAL_FORMS, session: requests.Session | None = None) -> dict | None` — each present key maps to `{"label": str, "value": float, "yoy_pct": float | None}`

- [ ] **Step 1: Write the failing tests** (append to `tests/test_annual_report.py`)

```python
def _facts_payload(points_by_concept: dict) -> dict:
    return {"facts": {"us-gaap": {
        concept: {"units": {"USD": points}} for concept, points in points_by_concept.items()
    }}}


def test_annual_financials_picks_the_most_recent_two_fy_points_and_computes_yoy():
    data = _facts_payload({
        "NetIncomeLoss": [
            {"end": "2023-12-31", "val": 100, "fp": "FY", "form": "10-K"},
            {"end": "2024-12-31", "val": 150, "fp": "FY", "form": "10-K"},
        ],
    })

    class FakeSession:
        def get(self, url, headers=None, timeout=None):
            return _FakeResp(json_data=data)

    result = ar.annual_financials("1", session=FakeSession())
    assert result["net_income"]["value"] == 150
    assert result["net_income"]["yoy_pct"] == pytest.approx(50.0)


def test_annual_financials_excludes_10q_and_non_fy_points():
    data = _facts_payload({
        "NetIncomeLoss": [
            {"end": "2024-03-31", "val": 999, "fp": "Q1", "form": "10-Q"},
            {"end": "2024-12-31", "val": 150, "fp": "FY", "form": "10-K"},
        ],
    })

    class FakeSession:
        def get(self, url, headers=None, timeout=None):
            return _FakeResp(json_data=data)

    result = ar.annual_financials("1", session=FakeSession())
    assert result["net_income"]["value"] == 150


def test_annual_financials_falls_back_to_the_older_revenue_tag():
    data = _facts_payload({
        "Revenues": [{"end": "2017-12-31", "val": 500, "fp": "FY", "form": "10-K"}],
    })

    class FakeSession:
        def get(self, url, headers=None, timeout=None):
            return _FakeResp(json_data=data)

    result = ar.annual_financials("1", session=FakeSession())
    assert result["revenue"]["value"] == 500


def test_annual_financials_omits_a_concept_with_no_tagged_data():
    data = _facts_payload({"NetIncomeLoss": [{"end": "2024-12-31", "val": 1, "fp": "FY", "form": "10-K"}]})

    class FakeSession:
        def get(self, url, headers=None, timeout=None):
            return _FakeResp(json_data=data)

    result = ar.annual_financials("1", session=FakeSession())
    assert "revenue" not in result
    assert "net_income" in result


def test_annual_financials_none_on_request_failure():
    class FakeSession:
        def get(self, url, headers=None, timeout=None):
            return _FakeResp(status=500)

    assert ar.annual_financials("1", session=FakeSession()) is None


def test_annual_financials_single_point_has_no_yoy():
    data = _facts_payload({"Assets": [{"end": "2024-12-31", "val": 100, "fp": "FY", "form": "10-K"}]})

    class FakeSession:
        def get(self, url, headers=None, timeout=None):
            return _FakeResp(json_data=data)

    result = ar.annual_financials("1", session=FakeSession())
    assert result["assets"]["yoy_pct"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_annual_report.py -v -k annual_financials`
Expected: FAIL with `AttributeError: module 'annual_report' has no attribute 'annual_financials'`

- [ ] **Step 3: Append to `annual_report.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_annual_report.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add annual_report.py tests/test_annual_report.py
git commit -m "feat(annual_report): financial facts from XBRL"
```

---

### Task 4: Orchestration and report formatting

**Files:**
- Modify: `annual_report.py` (append)
- Test: `tests/test_annual_report.py` (append)

**Interfaces:**
- Consumes: `find_latest_annual_filing`, `fetch_filing_text` (Task 1); `find_red_flags`, `_RED_FLAGS` (Task 2); `annual_financials`, `_FACT_CONCEPTS` (Task 3); `termstyle.section`, `termstyle.table` (existing module)
- Produces: `build(cik: str | None, session: requests.Session | None = None) -> dict | None` (keys: `"filing"`, `"financials"`, `"red_flags"`); `format_report(rep: dict | None) -> str`

- [ ] **Step 1: Write the failing tests** (append to `tests/test_annual_report.py`)

```python
def test_build_none_for_a_falsy_cik():
    assert ar.build(None) is None
    assert ar.build("") is None


def test_build_none_when_no_annual_filing_found(monkeypatch):
    monkeypatch.setattr(ar, "find_latest_annual_filing", lambda cik, session=None: None)
    assert ar.build("1") is None


def test_build_assembles_filing_financials_and_red_flags(monkeypatch):
    filing = {"form": "10-K", "filed": "2026-03-01", "fiscal_year": 2025,
              "accession": "a1", "primary_document": "d.htm", "url": "https://example.test/d.htm"}
    monkeypatch.setattr(ar, "find_latest_annual_filing", lambda cik, session=None: filing)
    monkeypatch.setattr(ar, "fetch_filing_text", lambda *a, **k: "substantial doubt about survival")
    monkeypatch.setattr(ar, "annual_financials", lambda *a, **k: {
        "net_income": {"label": "Чистая прибыль", "value": 150, "yoy_pct": 50.0}})

    rep = ar.build("1")
    assert rep["filing"] == filing
    assert rep["financials"]["net_income"]["value"] == 150
    assert rep["red_flags"][0]["key"] == "going_concern"


def test_build_survives_a_failed_text_fetch(monkeypatch):
    filing = {"form": "10-K", "filed": "2026-03-01", "fiscal_year": 2025,
              "accession": "a1", "primary_document": "d.htm", "url": "https://example.test/d.htm"}
    monkeypatch.setattr(ar, "find_latest_annual_filing", lambda cik, session=None: filing)
    monkeypatch.setattr(ar, "fetch_filing_text", lambda *a, **k: None)
    monkeypatch.setattr(ar, "annual_financials", lambda *a, **k: {})
    rep = ar.build("1")
    assert rep["red_flags"] == []


_FAKE_REP = {
    "filing": {"form": "10-K", "filed": "2026-03-01", "fiscal_year": 2025,
               "url": "https://example.test/filing.htm"},
    "financials": {"net_income": {"label": "Чистая прибыль", "value": 150.0, "yoy_pct": 50.0},
                    "assets": {"label": "Активы", "value": 900.0, "yoy_pct": None}},
    "red_flags": [{"key": "going_concern",
                    "label": "существенные сомнения в способности продолжать деятельность (going concern)",
                    "quote": "raise substantial doubt about the Company's ability to continue"}],
}


def test_format_report_empty_on_none():
    assert ar.format_report(None) == ""


def test_format_report_shows_the_filing_header_and_year():
    text = ar.format_report(_FAKE_REP)
    assert "10-K" in text and "2026-03-01" in text and "2025" in text


def test_format_report_shows_financial_figures_with_yoy():
    text = ar.format_report(_FAKE_REP)
    assert "Чистая прибыль" in text
    assert "150" in text and "+50.0%" in text


def test_format_report_shows_a_red_flag_quote_and_link_verbatim():
    text = ar.format_report(_FAKE_REP)
    assert "substantial doubt about the Company's ability to continue" in text
    assert "https://example.test/filing.htm" in text


def test_format_report_names_the_flags_not_found():
    text = ar.format_report(_FAKE_REP)
    assert "недостаток внутреннего контроля" in text  # material_weakness label, not found here
    assert "пересчёт" in text  # restatement label, not found here


def test_format_report_omits_the_caveat_when_no_flags_are_missing():
    all_found = dict(_FAKE_REP, red_flags=[
        {"key": key, "label": label, "quote": "x"} for key, _p, label in ar._RED_FLAGS
    ])
    text = ar.format_report(all_found)
    assert "не нашёл" not in text


def test_format_report_notes_missing_xbrl_data_without_crashing():
    rep = dict(_FAKE_REP, financials={})
    text = ar.format_report(rep)
    assert "IFRS" in text or "не найдены" in text


def test_format_report_never_renders_a_verdict():
    text = ar.format_report(_FAKE_REP).lower()
    for bad in ("buy ", "sell ", "рекомендуем", "стоит купить", "покупайте", "target price"):
        assert bad not in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_annual_report.py -v -k "build or format_report"`
Expected: FAIL with `AttributeError: module 'annual_report' has no attribute 'build'`

- [ ] **Step 3: Append to `annual_report.py`**

```python
def build(cik: str | None, session: requests.Session | None = None) -> dict | None:
    """Orchestrator: find the latest annual filing, then its financials and red
    flags. None if there's no CIK or no annual filing on file at all."""
    if not cik:
        return None
    session = session or requests.Session()
    filing = find_latest_annual_filing(cik, session)
    if filing is None:
        return None
    text = fetch_filing_text(cik, filing["accession"], filing["primary_document"], session)
    return {
        "filing": filing,
        "financials": annual_financials(cik, session=session) or {},
        "red_flags": find_red_flags(text) if text else [],
    }


def format_report(rep: dict | None) -> str:
    if not rep:
        return ""
    import termstyle

    filing = rep["filing"]
    year = f" (отчётный год: {filing['fiscal_year']})" if filing.get("fiscal_year") else ""
    L = [termstyle.section("Годовой отчёт (SEC 10-K/20-F)")]
    L.append(f"  Форма {filing['form']} от {filing['filed']}{year}")

    fin = rep["financials"]
    rows = []
    for key, _concepts, _label in _FACT_CONCEPTS:
        if key not in fin:
            continue
        f = fin[key]
        value = f"${f['value']:,.0f}"
        yoy = f"{f['yoy_pct']:+.1f}% г/г" if f["yoy_pct"] is not None else ""
        rows.append([f["label"], value, yoy])
    if rows:
        L.append("")
        L.append(termstyle.table(["", "", ""], rows))
    elif not fin:
        L.append("  Данные XBRL не найдены для этой компании (возможно, отчётность по IFRS).")

    for f in rep["red_flags"]:
        L.append("")
        L.append(f"  ⚠️ {f['label']}:")
        L.append(f"     «{f['quote']}»")
        L.append(f"     Полный документ: {filing['url']}")

    found_keys = {f["key"] for f in rep["red_flags"]}
    missing_labels = [label for key, _phrases, label in _RED_FLAGS if key not in found_keys]
    if missing_labels:
        L.append("")
        L.append("  Текстовый поиск не нашёл упоминаний о " + " или ".join(missing_labels)
                 + " — это не гарантия их отсутствия, только то, что стандартные"
                   " формулировки не встретились.")

    return "\n".join(L)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_annual_report.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add annual_report.py tests/test_annual_report.py
git commit -m "feat(annual_report): build orchestrator and report formatting"
```

---

### Task 5: Wire into the ticker dossier

**Files:**
- Modify: `research.py:1-20` (import), `research.py` `build()` (~line 682-758), `research.py` `format_report()` (~line 760-850)
- Modify: `tests/test_research.py` (append)
- Modify: `README.md` (~line 285, after the "финансы" bullet in the "Досье по тикеру" section)

**Interfaces:**
- Consumes: `annual_report.build(cik) -> dict | None`, `annual_report.format_report(rep) -> str` (Task 4)

- [ ] **Step 1: Write the failing tests** (append to `tests/test_research.py`)

```python
def test_build_includes_annual_report_for_a_us_ticker(conn, monkeypatch):
    monkeypatch.setattr(research.annual_report, "build", lambda cik, session=None: {"marker": True})
    rep = research.build(conn, "AAPL")
    assert rep["annual_report"] == {"marker": True}


def test_build_skips_annual_report_for_an_isin(conn, monkeypatch):
    """ISINs already skip every other CIK-dependent section for the same
    reason (no confirmed CIK to key the lookup on)."""
    add_bafin_txn(conn, "DE0007190001", "Someone", 1_000_000)
    rep = research.build(conn, "DE0007190001")
    assert rep["annual_report"] is None


def test_format_report_includes_the_annual_report_section_when_present(conn, monkeypatch):
    monkeypatch.setattr(research.annual_report, "build", lambda cik, session=None: {"marker": True})
    monkeypatch.setattr(research.annual_report, "format_report",
                        lambda rep: "ANNUAL-REPORT-MARKER-TEXT" if rep else "")
    text = research.format_report(research.build(conn, "AAPL"))
    assert "ANNUAL-REPORT-MARKER-TEXT" in text


def test_format_report_omits_the_annual_report_section_when_absent(conn, monkeypatch):
    monkeypatch.setattr(research.annual_report, "build", lambda cik, session=None: None)
    text = research.format_report(research.build(conn, "AAPL"))
    assert "ANNUAL-REPORT-MARKER-TEXT" not in text
    assert "Годовой отчёт" not in text
```

Add `add_bafin_txn` to the existing `from conftest import (...)` line at the top
of `tests/test_research.py` if it is not already imported there.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_research.py -v -k annual_report`
Expected: FAIL with `AttributeError: module 'research' has no attribute 'annual_report'`

- [ ] **Step 3: Wire `annual_report` into `research.py`**

Add the import alongside the other project-module imports near the top of the file:

```python
import annual_report
```

In `build()`, change:

```python
    if is_isin:
        fin = dilution = own = short = earnings = None
    else:
        info = _yf_info(ticker)
        fin = financials(ticker)
        dilution = share_count_history(cik)
        own = ownership(ticker, info)
        short = short_interest(info)
        earnings = earnings_calendar(ticker)
```

to:

```python
    if is_isin:
        fin = dilution = own = short = earnings = annual = None
    else:
        info = _yf_info(ticker)
        fin = financials(ticker)
        dilution = share_count_history(cik)
        own = ownership(ticker, info)
        short = short_interest(info)
        earnings = earnings_calendar(ticker)
        annual = annual_report.build(cik)
```

and in the returned dict, add the new key right after `"financials": fin,`:

```python
        "financials": fin,
        "annual_report": annual,
        "dilution": dilution,
```

In `format_report()`, change:

```python
    if rep.get("financials"):
        L.append(_format_financials(rep["financials"]))
    if rep.get("dilution"):
        L.append(_format_dilution(rep["dilution"]))
```

to:

```python
    if rep.get("financials"):
        L.append(_format_financials(rep["financials"]))
    if rep.get("annual_report"):
        L.append(annual_report.format_report(rep["annual_report"]))
    if rep.get("dilution"):
        L.append(_format_dilution(rep["dilution"]))
```

**Known, accepted inefficiency:** `recent_filings()` (called a few lines above in
`build()`) already fetches this same ticker's `data.sec.gov/submissions/CIK....json`
once; `annual_report.build()` fetches it again independently via
`find_latest_annual_filing`. Both payloads are small (~KB, not the multi-MB filing
document itself), and the dossier already makes a dozen-plus sequential network
calls per ticker, so this is not addressed in this plan — flagging it here so a
future reviewer sees it as a known tradeoff, not a miss.

- [ ] **Step 4: Add the README bullet**

In the "Досье по тикеру" section, insert a new bullet immediately after the
"финансы" bullet and before "акции в обращении":

```markdown
- **годовой отчёт (10-K/20-F)** — выручка / чистая прибыль / активы /
  обязательства / операционный денежный поток напрямую из XBRL самой компании
  (не смешанные по кварталам данные Yahoo, а именно то, что подано в годовом
  отчёте), с изменением год к году. Плюс текстовый поиск по самому документу на
  три конкретных, стандартизированных раскрытия — сомнения в способности
  продолжать деятельность (going concern), выявленный существенный недостаток
  внутреннего контроля, пересчёт отчётности — с цитатой дословно и ссылкой на
  документ. Ничего не суммируется и не оценивается: либо формулировка нашлась,
  либо нет;
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_research.py -v`
Expected: all PASS

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all PASS, no regressions

- [ ] **Step 7: Commit**

```bash
git add research.py README.md tests/test_research.py
git commit -m "feat(research): wire annual_report into the ticker dossier"
```

---

## Manual verification (after all tasks)

```bash
cd ~/Desktop/disclosure-bot && source .venv/bin/activate
pytest tests/ -q                                    # all green
python -c "
import db, research
conn = db.connect('data/disclosures.db')
print(research.format_report(research.build(conn, 'AAPL')))
"                                                     # real dossier, annual-report section present with real figures
python bot.py --once --no-telegram                   # unaffected -- annual_report.py has no bot.py caller
grep -rl 'annual_report' *.py                         # only annual_report.py and research.py
```

Success: pytest green; a real ticker's dossier shows the new section between
financials and dilution, with real XBRL figures and a YoY delta; a ticker with no
CIK or no annual filing shows no section and no error; nothing outside
`research.py` imports `annual_report`.
