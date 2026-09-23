"""Normalised buyer roles (cluster/roles.py) and the finders that attach them."""
from __future__ import annotations

import datetime as dt

import pytest

import cluster
from cluster.roles import bafin_role, sec_role, sweden_role
from conftest import add_bafin_txn, add_house_txn, add_sec_purchase, add_sweden_txn

TODAY = dt.date.today()
RECENT = (TODAY - dt.timedelta(days=2)).isoformat()


@pytest.mark.parametrize("title,officer,director,ten_pct,role", [
    ("Chief Executive Officer", 1, 0, 0, "ceo"),
    ("President and CEO", 1, 0, 0, "ceo"),
    ("Chairman and CEO", 1, 1, 0, "ceo"),          # CEO outranks chair
    ("Chief Financial Officer", 1, 0, 0, "cfo"),
    ("EVP & CFO", 1, 0, 0, "cfo"),
    ("Executive Chairman", 1, 1, 0, "chair"),
    ("See Remarks", 1, 0, 0, "officer"),
    (None, 0, 1, 0, "director"),
    (None, 0, 0, 1, "holder"),
    (None, 0, 0, 0, "other"),
])
def test_sec_role(title, officer, director, ten_pct, role):
    assert sec_role(title, officer, director, ten_pct) == role


@pytest.mark.parametrize("position,role", [
    ("Vorstand", "officer"), ("Vorsitzender des Vorstands", "ceo"),
    ("Aufsichtsrat", "director"), ("Vorsitzender des Aufsichtsrats", "chair"),
    ("in enger Beziehung", "associate"), ("Sonstige Führungsperson", "officer"),
    (None, "officer"),
])
def test_bafin_role(position, role):
    assert bafin_role(position) == role


@pytest.mark.parametrize("position,related,role", [
    ("Verkställande direktör (VD)", False, "ceo"), ("VD", False, "ceo"),
    ("Vice VD", False, "officer"),
    ("Ekonomichef/finanschef/finansdirektör", False, "cfo"),
    ("Styrelseordförande", False, "chair"), ("Styrelseledamot", False, "director"),
    ("Annan ledande befattningshavare", False, "officer"),
    ("Verkställande direktör (VD)", True, "associate"),
])
def test_sweden_role(position, related, role):
    assert sweden_role(position, related) == role


def test_sec_finder_attaches_roles_totals_and_increase(conn):
    add_sec_purchase(conn, "AAA", "Boss", 580_000, RECENT, officer=1, director=0,
                     title="Chief Executive Officer", shares=100, shares_owned_after=500)
    add_sec_purchase(conn, "AAA", "Board", 116_000, RECENT)
    [sig] = cluster.find_sec_clusters(conn)
    by_name = {b.name: b for b in sig.buyers}
    assert by_name["Boss"].role == "ceo" and by_name["Board"].role == "director"
    assert by_name["Boss"].total_eur == pytest.approx(500_000)      # USD at 1.16
    assert by_name["Boss"].increase_pct == pytest.approx(25.0)      # 100 on top of 400


def test_house_buyers_are_other(conn):
    date = (TODAY - dt.timedelta(days=3)).strftime("%m/%d/%Y")
    for m in ("Member One", "Member Two"):
        add_house_txn(conn, "AAA", m, "$250,001 - $500,000", date=date)
    [sig] = cluster.find_house_clusters(conn)
    assert {b.role for b in sig.buyers} == {"other"}


def test_bafin_and_sweden_finders_attach_roles(conn):
    d = (TODAY - dt.timedelta(days=2))
    add_bafin_txn(conn, "DE0000000001", "Chef", 600_000, date=d.strftime("%d.%m.%Y"),
                  position="Vorstand")
    add_sweden_txn(conn, "SE0000000001", "Vd Person", 7_000_000, date=d.isoformat())
    [bafin] = cluster.find_bafin_clusters(conn)
    [swe] = cluster.find_sweden_clusters(conn)
    assert bafin.buyers[0].role == "officer" and swe.buyers[0].role == "ceo"


def test_norway_buyers_are_insiders(conn):
    for i, person in enumerate(("A Person", "B Person")):
        conn.execute(
            "INSERT INTO norway_purchases (message_id, person, issuer_name, ticker, txn_type, "
            "txn_date, shares, price, currency, value, source_url) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (100 + i, person, "Test ASA", "TST", "P", RECENT, 1000, 100.0, "NOK", 2_000_000, "u"))
    conn.commit()
    [sig] = cluster.find_norway_clusters(conn)
    assert {b.role for b in sig.buyers} == {"insider"}
