"""Display dates as dd.mm.yyyy everywhere the user sees them (terminal, Telegram),
regardless of the format they're stored/parsed in internally -- SEC EDGAR gives
ISO "YYYY-MM-DD", the House Clerk gives "M/D/YYYY" (PDFs and the filer index
alike), BaFin already gives "DD.MM.YYYY" (a no-op, but listed for robustness and
consistency of handling). Internal storage/sorting is untouched; this is a
display-only concern.
"""
from __future__ import annotations

import datetime as dt

_INPUT_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d.%m.%Y")


def fmt(value) -> str:
    """Accepts a date string (ISO or US M/D/Y, zero-padded or not), a
    datetime.date, or a datetime.datetime, and returns dd.mm.yyyy. Falls back to
    the original string unchanged if it doesn't match a known format."""
    if isinstance(value, (dt.date, dt.datetime)):
        return value.strftime("%d.%m.%Y")
    if isinstance(value, str):
        for f in _INPUT_FORMATS:
            try:
                return dt.datetime.strptime(value, f).strftime("%d.%m.%Y")
            except ValueError:
                continue
    return str(value)
