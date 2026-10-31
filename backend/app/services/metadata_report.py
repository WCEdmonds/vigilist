"""Metadata report rows: the columns a reviewer hands to a partner or opposing
counsel (Bates, family, date, from, to, cc, subject). Pure — the router loads
the documents and their tags.
"""

from __future__ import annotations

from app.services.families import is_attachment
from app.services.search import resolve_tz

METADATA_HEADERS = [
    "Bates Begin", "Bates End", "Family", "Parent Bates", "Date Sent",
    "From", "To", "CC", "BCC", "Subject", "Title", "Custodian",
    "File Type", "Pages", "Tags",
]


def _tz(name: str | None):
    try:
        return resolve_tz(name)
    except ValueError:
        return resolve_tz(None)


def metadata_row(doc, tz: str | None = None) -> list:
    """One report row; doc needs its tags (DocumentTag with .tag) loaded."""
    when = doc.date_sent or doc.date_received
    family = doc.family_id or ""
    return [
        doc.bates_begin,
        doc.bates_end,
        family,
        family if is_attachment(doc.bates_begin, doc.family_id) else "",
        when.astimezone(_tz(tz)).strftime("%Y-%m-%d %H:%M") if when else "",
        doc.email_from or "",
        doc.email_to or "",
        doc.email_cc or "",
        doc.email_bcc or "",
        doc.email_subject or "",
        doc.title or "",
        doc.custodian or "",
        doc.file_type or "",
        doc.page_count,
        "; ".join(sorted(dt.tag.name for dt in doc.tags)),
    ]
