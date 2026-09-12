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


def test_find_latest_annual_filing_none_on_invalid_cik():
    """find_latest_annual_filing returns None when cik is None, not raises."""
    class FakeSession:
        def get(self, url, headers=None, timeout=None):
            return _FakeResp(json_data=_submissions(["10-K"]))

    assert ar.find_latest_annual_filing(None, session=FakeSession()) is None


def test_fetch_filing_text_none_on_non_numeric_cik():
    """fetch_filing_text returns None for non-numeric cik string, not raises."""
    class FakeSession:
        def get(self, url, headers=None, timeout=None):
            return _FakeResp(text="<p>Text</p>")

    assert ar.fetch_filing_text("not-a-number", "0000000000-26-000001", "doc.htm", session=FakeSession()) is None


def test_fetch_filing_text_none_on_none_cik():
    """fetch_filing_text returns None when cik is None, not raises."""
    class FakeSession:
        def get(self, url, headers=None, timeout=None):
            return _FakeResp(text="<p>Text</p>")

    assert ar.fetch_filing_text(None, "0000000000-26-000001", "doc.htm", session=FakeSession()) is None


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
    text = "#" * 300 + "substantial doubt about the ability to continue" + "@" * 300
    flags = ar.find_red_flags(text)
    quote = flags[0]["quote"]
    assert "substantial doubt" in quote
    # genuinely partial on both sides: 150 chars before (start clamped at idx-150),
    # 226 chars after (end clamped at the phrase + 250, well short of the full 300)
    assert 0 < quote.count("#") < 300
    assert 0 < quote.count("@") < 300


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


def test_annual_financials_none_on_invalid_cik():
    """annual_financials returns None when cik is None, not raises."""
    class FakeSession:
        def get(self, url, headers=None, timeout=None):
            return _FakeResp(json_data=_facts_payload({"NetIncomeLoss": []}))

    assert ar.annual_financials(None, session=FakeSession()) is None


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
