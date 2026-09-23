"""Parser tests against saved real payloads.

These exist because every one of these sources is read out of a format nobody
promised to keep stable -- XML schemas that differ between two forms of the same
filing, a PDF parsed by word coordinates, an HTML table read positionally, free
prose matched by regex, and a semicolon CSV in UTF-16. When one of them changes
shape the parser doesn't raise; it returns nothing, which is indistinguishable
from a quiet week. Each test below asserts on real content, so a layout change
fails loudly here instead of silently zeroing a source in production.
"""
from __future__ import annotations

import json

import pytest

import bafin
import cluster
import house_ptr
import norway
import sec_13dg
import sec_144
import sec_edgar
import sweden
from conftest import fixture_bytes, fixture_text


# --------------------------------------------------------------- SEC Form 4
def test_form4_parses_transactions():
    txns = sec_edgar.parse_form4_xml(fixture_bytes("form4.xml"), "acc-1", "http://u")
    assert txns, "Form 4 fixture produced no transactions"
    t = txns[0]
    assert t.issuer_name and t.owner_name
    assert t.txn_code in ("P", "S")
    assert t.transaction_date.count("-") == 2, "expected an ISO transaction date"
    assert t.accession == "acc-1" and t.source_url == "http://u"


@pytest.mark.parametrize("raw,expected", [
    ("NONE", None), ("N/A", None), ("NA", None), ("-", None), ("", None), (None, None),
    ("  aapl ", "AAPL"), ("BRK.B", "BRK.B"),
    # Every share class listed in one field: the first is the one to use.
    ("LEN, LEN.B", "LEN"), ("GOOGL;GOOG", "GOOGL"),
])
def test_clean_ticker(raw, expected):
    """A non-traded fund files issuerTradingSymbol as the literal string "NONE";
    left alone it becomes a cluster key and merges unrelated issuers."""
    assert sec_edgar.clean_ticker(raw) == expected


# ------------------------------------------------------------- SEC Form 144
def test_form144_parses_notice():
    sales = sec_144.parse_form144_xml(fixture_bytes("form144.xml"), "acc-2", "http://u")
    assert sales, "Form 144 fixture produced no proposed sales"
    s = sales[0]
    assert s.person_name and s.issuer_name
    assert s.market_value and s.market_value > 0
    assert s.units_to_sell and s.units_outstanding
    assert s.approx_sale_date.count("-") == 2, "approxSaleDate should be normalised to ISO"
    assert s.relationship, "relationshipToIssuer should be captured"


def test_form144_percent_of_class_is_computable():
    """The reason this form is worth collecting: it carries the share count for the
    class, so a sale can be expressed as a fraction of the company rather than only
    as an amount of money."""
    s = sec_144.parse_form144_xml(fixture_bytes("form144.xml"), "a", "u")[0]
    assert s.percent_of_class == pytest.approx(
        s.units_to_sell / s.units_outstanding * 100)
    assert 0 < s.percent_of_class < 100


# ------------------------------------------------------ SEC Schedule 13D/13G
def test_sc13d_parses_stake():
    filings = sec_13dg.parse_13dg_xml(fixture_bytes("sc13d.xml"), "acc-3", "http://u")
    assert filings, "13D fixture produced no reporting persons"
    f = filings[0]
    assert f.form_type.startswith("SCHEDULE 13D")
    assert f.is_activist and not f.is_amendment
    assert f.issuer_name and f.issuer_cik
    assert f.percent_of_class and f.percent_of_class > 0
    assert f.amount_owned and f.amount_owned > 0
    assert f.event_date.count("-") == 2, "dateOfEvent should be normalised to ISO"


def test_sc13g_parses_stake_despite_different_tag_names():
    """13D and 13G share a namespace and a filing regime but not their tag names:
    issuerCIK/issuerCik, percentOfClass/classPercent, and different container
    elements for the reporting person. Reading only the 13D spelling yields an empty
    list rather than an error, so this asserts the 13G path specifically."""
    filings = sec_13dg.parse_13dg_xml(fixture_bytes("sc13g.xml"), "acc-4", "http://u")
    assert filings, "13G fixture produced no reporting persons (tag-name drift?)"
    f = filings[0]
    assert f.form_type.startswith("SCHEDULE 13G")
    assert not f.is_activist
    assert f.issuer_cik, "issuerCik (lower-case spelling) not read"
    assert f.percent_of_class and f.percent_of_class > 0, "classPercent not read"
    assert f.amount_owned and f.amount_owned > 0


def test_13d_covers_every_reporting_person():
    """One 13D routinely covers several affiliated entities holding the same block;
    dropping all but the first would understate who is involved."""
    filings = sec_13dg.parse_13dg_xml(fixture_bytes("sc13d.xml"), "a", "u")
    assert len(filings) > 1
    assert len({f.person_name for f in filings}) == len(filings)


# ------------------------------------------------------------- House PTR PDF
def test_house_ptr_pdf_parses():
    """Parsed by word bounding boxes, because asset names and amounts wrap across
    physical lines within one logical row."""
    info, txns = house_ptr.parse_ptr_pdf(fixture_bytes("house_ptr.pdf"), "doc-1", "http://u")
    assert info["name"], "filer name not extracted from the PDF header"
    assert len(txns) > 5, f"expected many transactions, got {len(txns)}"
    assert {t.txn_type for t in txns} <= {"P", "S", "S (partial)", "E"}
    assert any(t.ticker for t in txns), "no ticker extracted from any asset name"
    assert all("/" in t.txn_date for t in txns if t.txn_date)


@pytest.mark.parametrize("asset,code,worthy", [
    ("Apple Inc. (AAPL) [ST]", "ST", True),
    ("SPDR S&P 500 ETF (SPY) [EF]", "EF", True),
    ("U.S. Treasury Bill [GS]", "GS", False),
    ("Smash Capital Fund II LP [OT]", "OT", False),
    ("UNIV CA PUB EDUC [GS]", "GS", False),
    ("Some Asset With No Code", None, True),   # unknown is not the same as a bond
])
def test_house_asset_type(asset, code, worthy):
    assert house_ptr.asset_type(asset) == code
    assert house_ptr.is_feed_worthy(asset) is worthy


def test_house_amount_bracket_lower_bound():
    assert cluster.parse_amount_low("$1,001 - $15,000") == 1001.0
    assert cluster.parse_amount_low("$50,001 - $100,000") == 50001.0
    assert cluster.parse_amount_low("") == 0.0


# -------------------------------------------------------------------- BaFin
def test_bafin_notifier_page_parses():
    """The level-2 table is read positionally by column index, so a redesign that
    inserts or reorders a column silently changes what every field means."""
    filings = bafin.parse_notifier_page(fixture_text("bafin_notifier.html"), "32274")
    assert filings, "BaFin notifier page produced no filings (table layout changed?)"
    f = filings[0]
    assert f.notifier_name and f.issuer_name
    assert f.isin.startswith(("DE", "LU", "NL", "AT", "FR", "IE", "GB"))
    assert f.txn_type in ("P", "S") or f.txn_type, "transaction type column empty"
    assert f.txn_date.count(".") == 2, "expected BaFin's DD.MM.YYYY date"


def test_bafin_detail_page_yields_price_and_volume():
    """Level 3 pulls 'Preis:' and 'Aggregiertes Volumen:' out of free German text
    with a comma decimal separator (1.234,56)."""
    detail = bafin.parse_transaction_detail(fixture_text("bafin_detail.html"))
    assert detail.volume_eur and detail.volume_eur > 0, "aggregated volume not parsed"
    assert detail.price_eur and detail.price_eur > 0, "price not parsed"


# -------------------------------------------------------------------- Norway
def test_norway_bodies_parse():
    """Amounts live in prose ('purchased 5,000 shares at an average price of NOK
    80'), so these are regex-extracted and deliberately skipped when unrecognised."""
    bodies = json.loads(fixture_text("norway_bodies.json"))
    assert bodies
    parsed = [norway.parse_transaction(b) for b in bodies.values()]
    ok = [p for p in parsed if p]
    assert ok, "no Newsweb body parsed at all"
    for p in ok:
        assert p["txn_type"] in ("P", "S")
        assert p["shares"] and p["shares"] > 0
        assert p["price"] and p["price"] > 0
        assert p["person"]


# -------------------------------------------------------------------- Sweden
def _sweden_rows():
    import csv, io
    return list(csv.DictReader(io.StringIO(fixture_text("sweden_export.csv")), delimiter=";"))


def test_sweden_csv_parses():
    rows = _sweden_rows()
    assert rows
    parsed = [sweden.parse_row(r) for r in rows]
    ok = [p for p in parsed if p]
    assert ok, "no Swedish row parsed"
    for p in ok:
        assert p["isin"]
        assert p["txn_date"].count("-") == 2
        assert p["value"] == pytest.approx(p["shares"] * p["price"])
        assert p["currency"]


def test_sweden_decimal_comma():
    """Swedish numbers use a decimal comma; read as a thousands separator instead,
    a price of 1,196794 becomes 1196794."""
    assert sweden._parse_number("560183,0") == 560183.0
    assert sweden._parse_number("1,196794") == pytest.approx(1.196794)
    assert sweden._parse_number("") is None
    assert sweden._parse_number("not a number") is None


def test_sweden_maps_transaction_nature():
    rows = _sweden_rows()
    natures = {r[sweden.C_NATURE] for r in rows}
    parsed = [sweden.parse_row(r) for r in rows]
    types = {p["txn_type"] for p in parsed if p}
    if "Förvärv" in natures:
        assert "P" in types
    if "Avyttring" in natures:
        assert "S" in types
    # Anything unmapped keeps its Swedish label rather than being guessed into P/S.
    assert not (types - {"P", "S"}) or all(t not in ("", None) for t in types)


def test_sweden_txn_key_ignores_publication_time():
    """A corrected filing is republished in full, and both versions can come back
    marked "Aktuell" -- so identity must exclude the publication timestamp, or the
    same transaction is stored (and summed) more than once."""
    rows = _sweden_rows()
    row = dict(rows[0])
    later = dict(row)
    later[sweden.C_PUBLISHED] = "2099-01-01 00:00:00"
    assert sweden.txn_key(row) == sweden.txn_key(later)
    assert sweden.row_id(row) != sweden.row_id(later)


def test_sweden_share_program_flag_is_read():
    """The one thing no other source here discloses: whether the transaction came
    out of a share-incentive plan rather than being a decision to buy."""
    rows = _sweden_rows()
    parsed = [sweden.parse_row(r) for r in rows]
    flags = {p["share_program"] for p in parsed if p}
    assert flags <= {True, False}
    for r, p in zip(rows, parsed):
        if p and r[sweden.C_SHARE_PROGRAM].strip().lower() == "ja":
            assert p["share_program"] is True


def test_sweden_rejects_implausible_notional_values():
    """Swaps are filed with the notional in both the volume and the price column,
    so volume x price comes out around 1e16 -- a quadrillion-krona 'purchase'."""
    rows = _sweden_rows()
    row = dict(rows[0])
    row[sweden.C_VOLUME] = "100000000,0"
    row[sweden.C_PRICE] = "100000000,0"
    assert sweden.parse_row(row) is None


def test_sweden_accepts_a_large_but_real_value():
    """The bar has to clear a genuine control block changing hands (~3.4bn SEK)."""
    rows = _sweden_rows()
    row = dict(rows[0])
    row[sweden.C_VOLUME] = "88959647,0"
    row[sweden.C_PRICE] = "38,5"
    parsed = sweden.parse_row(row)
    assert parsed is not None and parsed["value"] > 3e9
