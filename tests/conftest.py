"""Shared test fixtures.

Everything here is offline. The parser tests read saved real payloads from
tests/fixtures/ (captured from the live sources), and the signal tests run against
an in-memory SQLite seeded row by row. Nothing in the suite touches the network, so
it stays fast and can't fail because a source is down.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import db as db_module  # noqa: E402
import fx as fx_module  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def conn():
    """An empty in-memory database with FX rates pre-seeded.

    Seeding the rates matters: fx.py would otherwise try to fetch them, and a test
    suite that reaches the network to decide whether €500,000 has been exceeded is
    both slow and non-deterministic. fx's in-process memo is cleared too, so one
    test's cached rate can't leak into the next.
    """
    fx_module._MEMO.clear()
    c = db_module.connect(":memory:")
    for currency, rate in (("USD", 1.16), ("SEK", 11.14), ("NOK", 10.74), ("CAD", 1.60)):
        db_module.save_cached_value(c, f"fx_per_eur_{currency}", rate)
    yield c
    c.close()
    fx_module._MEMO.clear()


def add_sec_purchase(conn, ticker, owner, value, date="2026-09-01", *, derivative=0,
                     officer=0, director=1, ten_pct=0, title=None, issuer="Test Corp",
                     accession=None, security="Common Stock", is_10b5_1=0,
                     shares=100, shares_owned_after=None, filed_date=None):
    """Insert one Form 4 purchase. Defaults describe the ordinary case (a director
    buying common stock) so each test only states what it is actually about."""
    conn.execute(
        """INSERT INTO sec_purchases
           (accession, issuer_name, issuer_cik, ticker, owner_name, owner_cik, is_officer,
            is_director, is_ten_pct_owner, officer_title, transaction_date, shares, price,
            value, security_title, derivative, source_url, is_10b5_1, shares_owned_after,
            ownership_type, filed_date)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (accession or f"acc-{ticker}-{owner}-{date}-{value}", issuer, "1", ticker, owner, "2",
         officer, director, ten_pct, title, date, shares, value / max(shares, 1), value,
         security, derivative, f"https://example.test/{ticker}", is_10b5_1,
         # None means "filed the same day"; "" means genuinely absent, which real
         # rows collected before filed_date was captured actually have.
         shares_owned_after, "D", date if filed_date is None else filed_date),
    )
    conn.commit()


def add_sec_sale(conn, ticker, owner, value, date="2026-10-01", issuer="Test Corp"):
    conn.execute(
        """INSERT INTO sec_sales
           (accession, issuer_name, issuer_cik, ticker, owner_name, owner_cik, is_director,
            officer_title, transaction_date, shares, price, value, source_url)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"sale-{ticker}-{owner}-{date}", issuer, "1", ticker, owner, "2", 1, None, date,
         100, value / 100, value, f"https://example.test/sale/{ticker}"),
    )
    conn.commit()


def add_form_144(conn, ticker, person, value, date="2026-10-01", issuer="Test Corp"):
    conn.execute(
        """INSERT INTO sec_proposed_sales
           (accession, issuer_name, issuer_cik, ticker, person_name, relationship,
            security_class, units_to_sell, market_value, units_outstanding,
            approx_sale_date, exchange, acquisition_nature, payment_nature, source_url)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"144-{ticker}-{person}-{date}", issuer, "1", ticker, person, "Officer",
         "Common", 1000, value, 1_000_000, date, "NYSE", "Purchases of shares", None,
         f"https://example.test/144/{ticker}"),
    )
    conn.commit()


def add_stake(conn, ticker, person, percent, form_type="SCHEDULE 13D",
              event_date="2026-09-01", accession=None, issuer="Test Corp"):
    conn.execute(
        """INSERT INTO sec_stakes
           (accession, form_type, issuer_name, issuer_cik, cusip, ticker, event_date,
            person_name, person_type, percent_of_class, amount_owned, source_url)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (accession or f"stake-{ticker}-{person}-{event_date}-{percent}", form_type, issuer,
         "1", "CUSIP", ticker, event_date, person, "CO", percent, 1_000_000,
         f"https://example.test/13d/{ticker}"),
    )
    conn.commit()


def add_senate_txn(conn, ticker, member, amount_range, date="2026-09-01",
                   txn_type="P", asset=None, report_id=None):
    """Senate PTR row. Dates are ISO here, unlike the House table's M/D/YYYY."""
    conn.execute(
        """INSERT INTO senate_purchases
           (report_id, member_name, office, owner, asset, ticker, txn_type, txn_date,
            amount_range, source_url)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (report_id or f"rep-{ticker}-{member}-{date}-{amount_range}", member,
         "Senator, State", "Self", asset or f"{ticker} Inc", ticker, txn_type, date,
         amount_range, f"https://example.test/senate/{ticker}"),
    )
    conn.commit()


def add_bafin_txn(conn, isin, notifier, volume_eur, date="25.08.2026", txn_type="P",
                  issuer="Test AG", position="Vorstand"):
    """BaFin row. Dates are DD.MM.YYYY here, unlike every other table."""
    conn.execute(
        """INSERT INTO bafin_purchases
           (meldepflichtiger_id, notifier_name, position, issuer_name, issuer_bafin_id,
            isin, instrument_type, txn_type, txn_date, venue, price_eur, volume_eur, source_url)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"m-{notifier}-{date}", notifier, position, issuer, "b1", isin, "Aktie",
         txn_type, date, "XETRA", 10.0, volume_eur, "https://example.test/bafin"),
    )
    conn.commit()


def add_sweden_txn(conn, isin, person, value, date="2026-09-01", txn_type="P",
                   issuer="Test AB", status="Aktuell"):
    conn.execute(
        """INSERT INTO sweden_purchases
           (txn_key, row_id, published, person, pdmr, position, issuer_name, isin,
            instrument_type, txn_type, txn_date, shares, price, currency, value,
            share_program, related_party, status, source_url)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"k-{person}-{date}-{value}", f"r-{person}-{date}", f"{date} 12:00:00", person,
         person, "VD", issuer, isin, "Aktie", txn_type, date, 100, value / 100, "SEK",
         value, 0, 0, status, "https://example.test/fi"),
    )
    conn.commit()
