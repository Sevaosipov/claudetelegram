"""Parse SEC Schedule 13D / 13G beneficial-ownership filings.

Anyone crossing 5% of a class of a public company's shares has to say so: 13D if
they may seek to influence control (an activist stake), 13G if they are passive
(index funds, most institutions). Amendments (/A) follow whenever the position
materially changes.

Why this project wants them: every other source here answers "who bought, and for
how much money". These answer "who owns how much OF THE COMPANY" -- percentOfClass,
straight from the filing. That is the number cluster.py's solo-whale threshold was
explicitly a stand-in for ("we don't have shares-outstanding data to measure '% of
the company', so this is a flat money bar instead"). A €500k purchase means one
thing in a €50m company and nothing at all in a €500bn one.

These arrive in the same daily index Form 4 already comes from, so they cost no
extra index requests -- see sec_edgar.fetch_daily_index_accessions.

The one real complication is that 13D and 13G, despite being the same disclosure
regime and living in the same XML namespace, do not share tag names:

    concept              13D                        13G
    ------------------   ------------------------   -------------------------------
    issuer CIK           issuerCIK                  issuerCik           (sic)
    event date           dateOfEvent                eventDateRequiresFilingThisStatement
    person block         reportingPersons/          coverPageHeaderReportingPersonDetails
                           reportingPersonInfo
    % of class           percentOfClass             classPercent
    shares held          aggregateAmountOwned       reportingPersonBeneficiallyOwnedAggregateNumberOfShares

Both spellings are tried for each field. Getting this wrong wouldn't raise -- it
would silently yield nothing, which is the failure this project is most prone to.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from xml.etree import ElementTree as ET


@dataclass
class StakeFiling:
    """One reporting person's position in one issuer, from one 13D/13G filing."""
    accession: str
    form_type: str          # "SCHEDULE 13D", "SCHEDULE 13G/A", ...
    issuer_name: str
    issuer_cik: str
    cusip: str | None
    ticker: str | None      # filled in by the caller via cik_map
    event_date: str         # ISO, or "" when the form doesn't carry one
    person_name: str
    person_type: str | None
    percent_of_class: float | None
    amount_owned: float | None
    source_url: str

    @property
    def is_activist(self) -> bool:
        """13D means the holder may seek to influence control; 13G means passive.
        Same 5% trigger, very different intent."""
        return self.form_type.startswith("SCHEDULE 13D")

    @property
    def is_amendment(self) -> bool:
        return self.form_type.endswith("/A")


def strip_namespaces(root: ET.Element) -> ET.Element:
    """These forms declare a default XML namespace (Form 4 does not), which would
    otherwise force every lookup to be written as '{http://...}tag'."""
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _first_text(el: ET.Element, *paths: str) -> str | None:
    """First non-empty match among alternative paths -- the 13D/13G spelling split."""
    for path in paths:
        found = el.findtext(path)
        if found and found.strip():
            return found.strip()
    return None


def _number(raw: str | None) -> float | None:
    """Values arrive as '17285425.04', '13.5' or '10,251,900'; percentages
    occasionally carry a trailing '%'."""
    if not raw:
        return None
    text = raw.strip().replace(",", "").rstrip("%").strip()
    try:
        return float(text)
    except ValueError:
        return None


def _iso_date(raw: str | None) -> str:
    """MM/DD/YYYY as these forms give it -> ISO, so it sorts and compares like
    every other date stored in this project."""
    if not raw:
        return ""
    try:
        return dt.datetime.strptime(raw.strip(), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return raw.strip()


def parse_13dg_xml(xml_bytes: bytes, accession: str, source_url: str) -> list[StakeFiling]:
    """One StakeFiling per reporting person named in the filing (a single 13D
    routinely covers several affiliated entities holding the same block)."""
    root = strip_namespaces(ET.fromstring(xml_bytes))

    form_type = (root.findtext("headerData/submissionType") or "").strip()
    cover = root.find("formData/coverPageHeader")
    if cover is None:
        return []

    issuer_name = _first_text(cover, "issuerInfo/issuerName") or ""
    issuer_cik = _first_text(cover, "issuerInfo/issuerCIK", "issuerInfo/issuerCik") or ""
    cusip = _first_text(cover, "issuerInfo/issuerCusips/issuerCusipNumber")
    event_date = _iso_date(_first_text(cover, "dateOfEvent",
                                        "eventDateRequiresFilingThisStatement"))

    # 13D nests its reporting persons; 13G puts each one directly under formData.
    person_blocks = root.findall("formData/reportingPersons/reportingPersonInfo")
    if not person_blocks:
        person_blocks = root.findall("formData/coverPageHeaderReportingPersonDetails")

    out = []
    for block in person_blocks:
        name = _first_text(block, "reportingPersonName")
        if not name:
            continue
        out.append(StakeFiling(
            accession=accession,
            form_type=form_type,
            issuer_name=issuer_name,
            issuer_cik=issuer_cik,
            cusip=cusip,
            ticker=None,
            event_date=event_date,
            person_name=name,
            person_type=_first_text(block, "typeOfReportingPerson"),
            percent_of_class=_number(_first_text(block, "percentOfClass", "classPercent")),
            amount_owned=_number(_first_text(
                block, "aggregateAmountOwned",
                "reportingPersonBeneficiallyOwnedAggregateNumberOfShares")),
            source_url=source_url,
        ))
    return out


def scan_daily_index(date, seen_accessions: set[str], session, cik_lookup=None):
    """Yield (accession, list[StakeFiling]) for each not-yet-seen 13D/G filed that
    day. Every accession examined is yielded exactly once even when it parses to
    nothing, so callers can mark it seen -- same contract as the Form 4 scanner.

    `cik_lookup` is a cik_map.CikMap; these forms carry no ticker.
    """
    import requests
    import sec_edgar

    for form_type, cik, accession, _filed in sec_edgar.fetch_daily_index_accessions(
            date, session=session, forms=sec_edgar.FORM_13DG):
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
            filings = parse_13dg_xml(xml_bytes, accession, doc_url)
        except ET.ParseError:
            yield accession, []
            continue
        if cik_lookup is not None:
            for f in filings:
                f.ticker = cik_lookup.ticker(f.issuer_cik)
        yield accession, filings
