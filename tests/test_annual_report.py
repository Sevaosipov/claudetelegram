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
