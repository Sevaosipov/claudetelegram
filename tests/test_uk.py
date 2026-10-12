"""UK director dealings (uk.py): the announcement's form is read by label, only a plain purchase is a 'P', and
the finder groups purchases by ISIN. No network: the announcements are fixtures."""
from __future__ import annotations

import datetime as dt

import pytest

import cluster
import db
import uk
from cluster.roles import uk_role
from conftest import fixture_text

META = {"seq_id": "f1", "submitted_date": "2026-10-12T17:12:00Z", "company": "X PLC", "lei": "L",
        "download_link": "NSM/RNS/f1.html"}


def test_a_plain_purchase_is_read_from_the_form():
    [t] = uk.parse_filing(fixture_text("uk_dsh_purchase.html"), META)
    assert (t["person"], t["position"], t["txn_type"]) == ("Juliette Stacey", "Non-executive Director", "P")
    assert (t["isin"], t["txn_date"], t["shares"], t["currency"]) == ("GB0003061511", "2026-10-12", 13089.0, "GBP")
    assert t["price"] == pytest.approx(0.764) and t["value"] == pytest.approx(13089 * 0.764)
    assert t["issuer_name"].lower().startswith("sanderson") and t["source_url"].endswith("NSM/RNS/f1.html")


def test_a_purchase_under_a_company_arrangement_is_not_a_signal():
    [t] = uk.parse_filing(fixture_text("uk_dsh_arrangement.html"), META)
    assert (t["person"], t["txn_type"], t["price"], t["shares"]) == ("Sir John Manzoni", "O", 16.4, 400.0)


def test_split_names_and_pence_with_a_decimal_comma_are_read():
    txns = uk.parse_filing(fixture_text("uk_dsh_names_pence.html"), META)
    assert txns and txns[0]["person"] == "Naomi Climer" and txns[0]["position"] == "Non-Executive Director"
    assert txns[0]["price"] == pytest.approx(0.3253) and txns[0]["shares"] == 10000.0


@pytest.mark.parametrize("cell, pounds", [
    ("£16.40", 16.40), ("GBP 2.164", 2.164), ("1,640p", 16.40), ("1640 pence", 16.40), ("GBX 86.6", 0.866),
    ("32,53 pence", 0.3253), ("Nil", None), ("16.40", None), ("C$2.59", None)])
def test_a_price_needs_a_unit(cell, pounds):
    assert uk.price_gbp(cell) == (pytest.approx(pounds) if pounds is not None else None)


@pytest.mark.parametrize("nature, kind", [
    ("Purchase of Ordinary Shares", "P"), ("Acquisition", "P"), ("Sale of shares", "S"), ("Disposal", "S"),
    ("Purchase of Shares under the Partnership Share scheme", "O"), ("Dividend Reinvestment", "O"),
    ("Vesting of award granted on 29 April 2024", "O"), ("Exercise of LTIP 2023 options", "O"),
    ("Sale of shares to cover tax and purchase", "O"), ("Subscription for New Ordinary Shares", "O"), ("", "O")])
def test_only_a_plain_purchase_or_sale_counts(nature, kind):
    assert uk.nature_type(nature) == kind


@pytest.mark.parametrize("text, iso", [("2026-10-12", "2026-10-12"), ("9 October 2026", "2026-10-09"),
                                       ("9th October 2026", "2026-10-09"), ("12/10/2026", "2026-10-12"),
                                       ("9 and 10 October", None)])
def test_dates(text, iso):
    assert uk.parse_date(text) == iso


@pytest.mark.parametrize("position, related, role", [
    ("Chief Executive Officer", False, "ceo"), ("Chief Financial Officer", False, "cfo"), ("Chair", False, "chair"),
    ("Non-executive Director", False, "director"), ("Company Secretary", False, "officer"),
    ("Person closely associated with the CEO", True, "associate")])
def test_roles(position, related, role):
    assert uk_role(position, related) == role


def _row(conn, isin, person, value, kind="P", position="Director"):
    today = dt.date.today().isoformat()
    db.save_uk_purchase(conn, {
        "txn_key": f"{isin}{person}{value}{kind}", "filing_id": "f", "published": today, "person": person,
        "position": position, "issuer_name": "Acme PLC", "lei": "L", "isin": isin, "nature": "Purchase",
        "txn_type": kind, "txn_date": today, "shares": 1.0, "price": value, "currency": "GBP", "value": value,
        "related_party": False, "place": "XLON", "source_url": "u"})


def test_two_buyers_make_a_cluster_and_plans_do_not(conn):
    _row(conn, "GB0000000001", "A Buyer", 60_000)
    _row(conn, "GB0000000001", "B Buyer", 80_000, position="Chief Executive Officer")
    _row(conn, "GB0000000002", "C Buyer", 90_000)                          # one buyer, not large
    _row(conn, "GB0000000003", "D One", 70_000, kind="O")
    _row(conn, "GB0000000003", "E Two", 70_000, kind="O")                  # share-plan rows: never counted
    assert not db.save_uk_purchase(conn, {**{
        "txn_key": "GB0000000001A Buyer60000P", "filing_id": "f", "published": "", "person": "A Buyer",
        "position": "", "issuer_name": "", "lei": "", "isin": "GB0000000001", "nature": "", "txn_type": "P",
        "txn_date": "", "shares": 1.0, "price": 1.0, "currency": "GBP", "value": 1.0, "related_party": False,
        "place": "", "source_url": ""}})                                   # the same transaction again
    [sig] = cluster.find_uk_clusters(conn, min_value=1.0, ignore_alert_state=True)
    assert (sig.source, sig.ticker, sig.buyer_count, sig.reason) == ("UK", "GB0000000001", 2, "cluster")
    assert sig.total_value > 140_000                                        # pounds became euros
    assert {b.role for b in sig.buyers} == {"director", "ceo"}
