"""Parse SEC Form 144 -- notice of a *proposed* sale of restricted or control stock.

An insider intending to sell files this before selling; the Form 4 recording the
sale follows afterwards. That ordering is the whole point of collecting it: the
exit signals this project already computes are driven by Form 4 sales, so they see
an insider leaving only after it has happened. A Form 144 says it is coming.

It also carries something the other US filings don't: noOfUnitsOutstanding, the
share count for the class. Together with Schedule 13D/G's percentOfClass (see
sec_13dg.py) that closes the gap cluster.py documents in its solo-whale threshold
-- "we don't have shares-outstanding data to measure '% of the company', so this is
a flat money bar instead".

Form 144 shares Form 4's XML namespace but, unlike Form 4, actually declares it, so
the tags have to be namespace-stripped before lookup. Amounts are already USD in
aggregateMarketValue, so no price lookup is needed.

Two honest caveats about what a 144 means:
  - it is an *intention*. Filers routinely file and then sell less than stated, or
    nothing at all.
  - much of the volume is routine: scheduled 10b5-1 plan sales and shares being
    sold straight out of a vesting event. natureOfAcquisitionTransaction and
    natureOfPayment often say which, and are kept for that reason.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from sec_13dg import strip_namespaces


@dataclass
class ProposedSale:
    accession: str
    issuer_name: str
    issuer_cik: str
    ticker: str | None          # filled in by the caller via cik_map
    person_name: str
    relationship: str | None    # "Officer", "Director", "10% Owner", ...
    security_class: str | None
    units_to_sell: float | None
    market_value: float | None  # USD, as filed
    units_outstanding: float | None
    approx_sale_date: str       # ISO
    exchange: str | None
    acquisition_nature: str | None
    payment_nature: str | None
    source_url: str

    @property
    def percent_of_class(self) -> float | None:
        """How much of the class this sale represents. The only US filing here that
        makes this computable without an external share-count lookup."""
        if not self.units_to_sell or not self.units_outstanding:
            return None
        return self.units_to_sell / self.units_outstanding * 100


def _number(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        return float(raw.strip().replace(",", ""))
    except ValueError:
        return None


def _iso_date(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return dt.datetime.strptime(raw.strip(), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return raw.strip()


def parse_form144_xml(xml_bytes: bytes, accession: str, source_url: str) -> list[ProposedSale]:
    """One ProposedSale per securitiesInformation block (a filing may cover more
    than one class of security)."""
    root = strip_namespaces(ET.fromstring(xml_bytes))
    issuer = root.find("formData/issuerInfo")
    if issuer is None:
        return []

    issuer_name = (issuer.findtext("issuerName") or "").strip()
    issuer_cik = (issuer.findtext("issuerCik") or "").strip()
    person = (issuer.findtext("nameOfPersonForWhoseAccountTheSecuritiesAreToBeSold") or "").strip()
    relationships = [
        (r.text or "").strip()
        for r in issuer.findall("relationshipsToIssuer/relationshipToIssuer")
        if (r.text or "").strip()
    ]
    relationship = ", ".join(relationships) or None

    # natureOfAcquisitionTransaction / natureOfPayment live in a sibling block and
    # say whether the stock being sold came from a comp event (vesting, option
    # exercise) or was bought outright. Only the first is read: a filing listing
    # several acquisition lots is still one intended sale.
    to_be_sold = root.find("formData/securitiesToBeSold")
    acquisition_nature = payment_nature = None
    if to_be_sold is not None:
        acquisition_nature = (to_be_sold.findtext("natureOfAcquisitionTransaction") or "").strip() or None
        payment_nature = (to_be_sold.findtext("natureOfPayment") or "").strip() or None

    out = []
    for info in root.findall("formData/securitiesInformation"):
        out.append(ProposedSale(
            accession=accession,
            issuer_name=issuer_name,
            issuer_cik=issuer_cik,
            ticker=None,
            person_name=person,
            relationship=relationship,
            security_class=(info.findtext("securitiesClassTitle") or "").strip() or None,
            units_to_sell=_number(info.findtext("noOfUnitsSold")),
            market_value=_number(info.findtext("aggregateMarketValue")),
            units_outstanding=_number(info.findtext("noOfUnitsOutstanding")),
            approx_sale_date=_iso_date(info.findtext("approxSaleDate")),
            exchange=(info.findtext("securitiesExchangeName") or "").strip() or None,
            acquisition_nature=acquisition_nature,
            payment_nature=payment_nature,
            source_url=source_url,
        ))
    return out


def scan_daily_index(date, seen_accessions: set[str], session, cik_lookup=None):
    """Yield (accession, list[ProposedSale]) for each not-yet-seen Form 144 filed
    that day. Same contract as the other scanners: every accession examined is
    yielded exactly once, even when it parses to nothing."""
    import requests
    import sec_edgar

    for _form, cik, accession, _filed in sec_edgar.fetch_daily_index_accessions(
            date, session=session, forms=sec_edgar.FORM_144):
        if accession in seen_accessions or not cik:
            continue
        try:
            fetched = sec_edgar.fetch_primary_xml(cik, accession, session)
        except requests.RequestException:
            continue  # transient failure: don't mark seen, retry next run
        if fetched is None:
            yield accession, []
            continue
        doc_url, xml_bytes = fetched
        try:
            sales = parse_form144_xml(xml_bytes, accession, doc_url)
        except ET.ParseError:
            yield accession, []
            continue
        if cik_lookup is not None:
            for s in sales:
                s.ticker = cik_lookup.ticker(s.issuer_cik)
        yield accession, sales
