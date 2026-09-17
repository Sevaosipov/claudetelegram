"""sec_edgar.scan_daily_index's amendment filtering. Network calls
(fetch_daily_index_accessions, fetch_primary_xml) are monkeypatched."""
from __future__ import annotations

import datetime as dt

import sec_edgar


def test_scan_daily_index_skips_4a_without_fetching_its_xml(monkeypatch):
    """A Form 4/A amendment must be skipped before the (network) XML fetch,
    not just have its transactions discarded afterward -- fetching it at all
    would be pure waste for something we're going to throw away anyway."""
    fetched = []
    monkeypatch.setattr(sec_edgar, "fetch_daily_index_accessions",
                        lambda date, session=None, forms=None: [
                            ("4", "cik1", "acc-original", "2026-09-16"),
                            ("4/A", "cik2", "acc-amendment", "2026-09-16"),
                        ])
    def fake_fetch_primary_xml(cik, accession, session):
        fetched.append(accession)
        return ("http://u", b"<xml/>")
    monkeypatch.setattr(sec_edgar, "fetch_primary_xml", fake_fetch_primary_xml)
    monkeypatch.setattr(sec_edgar, "parse_form4_xml", lambda *a, **k: ["fake-txn"])

    results = list(sec_edgar.scan_daily_index(dt.date(2026, 9, 16), seen_accessions=set()))

    assert fetched == ["acc-original"]  # the amendment's XML was never requested
    by_accession = dict(results)
    assert by_accession["acc-original"] == ["fake-txn"]
    # Still yielded (empty, not omitted): bot.py's _handle_sec_filing calls
    # db.mark_sec_accession_seen() unconditionally after iterating an
    # accession's transactions, so this is what keeps a skipped 4/A from
    # being re-fetched from the daily index on every future run.
    assert by_accession["acc-amendment"] == []
