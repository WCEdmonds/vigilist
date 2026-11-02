"""Email header fields covered by redactions.

Imaging records the page-1 box of each header line (email_pages). A field
counts as redacted when any redaction on that page overlaps its line with
positive area; the load file then blanks the matching column, and a covered
"Attachments:" line raises a conflict for the attachments (whose own rows
still carry their filenames). Pure.
"""

from __future__ import annotations

# Header field -> DAT column it blanks (attachments: none on the parent row).
FIELD_COLUMNS = {"from": "FROM", "to": "TO", "cc": "CC", "bcc": "BCC",
                 "date": "DATESENT", "subject": "SUBJECT", "attachments": None}


def _overlaps(region: dict, red) -> bool:
    x0, y0 = max(region["x"], red.x_pct), max(region["y"], red.y_pct)
    x1 = min(region["x"] + region["w"], red.x_pct + red.w_pct)
    y1 = min(region["y"] + region["h"], red.y_pct + red.h_pct)
    return x1 > x0 and y1 > y0


def blanked_fields(header_regions: dict | None, redactions) -> set[str]:
    if not header_regions:
        return set()
    return {
        field for field, region in header_regions.items()
        if any(r.page_num == region.get("page", 1) and _overlaps(region, r) for r in redactions)
    }
