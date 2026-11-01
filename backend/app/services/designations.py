"""Confidentiality designations (Confidential, Attorneys' Eyes Only) from tags.

A tag with a designation stamps that legend on the documents it is applied to
when a production set is locked. A document with several designation tags gets
them joined in tag-creation order, so the usual pair reads
"CONFIDENTIAL – ATTORNEYS' EYES ONLY". Pure: the router does the query.
"""

from __future__ import annotations

# ProductionSetItem.designation is String(100).
MAX_DESIGNATION = 100


def combine_designations(tag_designations) -> str | None:
    """tag_designations: (tag_id, designation) pairs for one document."""
    parts: list[str] = []
    for _tag_id, text in sorted(tag_designations, key=lambda p: p[0]):
        text = (text or "").strip()
        if text and text not in parts:
            parts.append(text)
    return " – ".join(parts)[:MAX_DESIGNATION] or None
