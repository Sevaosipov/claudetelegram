"""Look up a member of Congress's full Periodic Transaction Report history --
every buy AND sell (unlike bot.py/cluster.py, which only track purchases within
S&P100+Nasdaq100 for signal alerting) -- grouped by filing, printed to the
terminal. Some PTRs are filed on paper and scanned to image by the Clerk; those
can't be parsed at all, so they're reported as such with a link to the PDF and to
capitoltrades.com as a human-readable alternative.

Usage:
    python politician_report.py "Gottheimer"
    python politician_report.py "Pelosi" --years 2025,2026
    python politician_report.py "Khanna" --years 2026
"""
from __future__ import annotations

import argparse
import datetime as dt

import datefmt
import house_ptr

TXN_DISPLAY = {
    "P": ("🟢", "купил"),
    "S": ("🔴", "продал"),
    "S (partial)": ("🔴", "продал"),
    "E": ("🔄", "обменял"),
}

CAPITOL_TRADES_URL = "https://www.capitoltrades.com"
CURRENT_YEAR = dt.date.today().year


def find_filers(name_query: str, years: list[int], session=None) -> list[house_ptr.Filer]:
    session = session or house_ptr.new_session()
    query = name_query.strip().lower()
    matches = []
    for year in years:
        for f in house_ptr.fetch_year_index(year, session=session):
            if f.filing_type != "P":
                continue
            full_name = f"{f.first} {f.last}".lower()
            if query in full_name or query in f.last.lower():
                matches.append(f)
    matches.sort(key=lambda f: dt.datetime.strptime(f.filing_date, "%m/%d/%Y"))
    return matches


def _display_ticker(t: house_ptr.Transaction) -> str:
    label = t.ticker or t.asset
    if "[OP]" in t.asset:
        label += " (опционы)"
    return label


def format_report(filer: house_ptr.Filer, filer_info: dict, txns: list[house_ptr.Transaction],
                   scanned: bool, source_url: str) -> str:
    name = filer_info.get("name") or f"{filer.first} {filer.last}"
    header = f"{name} ({filer.state_dst}) — отчёт от {datefmt.fmt(filer.filing_date)}"
    lines = [header]

    if scanned:
        lines.append(f"  📄 подан на бумаге — сканы, автопарсинг невозможен. PDF: {source_url}")
        lines.append(f"     в удобном виде: {CAPITOL_TRADES_URL}")
        return "\n".join(lines)

    if not txns:
        lines.append("  (нет строк с покупкой/продажей акций в этом отчёте)")
        return "\n".join(lines)

    for t in txns:
        icon, verb = TXN_DISPLAY.get(t.txn_type, ("⚪", t.txn_type))
        lines.append(f"  {icon} {verb} {_display_ticker(t)} {datefmt.fmt(t.txn_date)} {t.amount_range}")
    lines.append(f"  {source_url}")
    return "\n".join(lines)


def run(name_query: str, years: list[int]) -> None:
    session = house_ptr.new_session()
    filers = find_filers(name_query, years, session=session)
    if not filers:
        print(f"Не нашёл PTR-отчётов для «{name_query}» за {years}.")
        return

    for filer in filers:
        try:
            pdf_bytes = house_ptr.fetch_ptr_pdf_bytes(int(filer.year), filer.doc_id, session=session)
        except Exception as e:
            print(f"{filer.first} {filer.last} ({filer.state_dst}) — отчёт от {datefmt.fmt(filer.filing_date)}")
            print(f"  ⚠️ не удалось скачать PDF: {e}")
            print()
            continue

        source_url = house_ptr.PTR_PDF_URL.format(year=filer.year, doc_id=filer.doc_id)
        scanned = house_ptr.is_scanned_pdf(pdf_bytes)
        filer_info, txns = ({}, []) if scanned else house_ptr.parse_ptr_pdf(pdf_bytes, filer.doc_id, source_url)

        print(format_report(filer, filer_info, txns, scanned, source_url))
        print()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", help="last name (or 'First Last') to search for, e.g. \"Pelosi\"")
    ap.add_argument("--years", type=str, default=str(CURRENT_YEAR),
                     help=f"comma-separated years to search (default: {CURRENT_YEAR})")
    args = ap.parse_args()
    years = [int(y) for y in args.years.split(",")]
    run(args.name, years)


if __name__ == "__main__":
    main()
