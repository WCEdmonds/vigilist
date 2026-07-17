"""Normalize load-file values and promote them to typed Document fields."""

from __future__ import annotations

import os
from datetime import datetime, timezone

# Typed metadata fields promote_record emits (structural fields excluded).
_METADATA_TARGETS = {
    "custodian", "date_sent", "date_received", "date_created", "date_modified",
    "file_hash_md5", "file_hash_sha256", "file_type", "file_name", "source_path",
    "email_from", "email_to", "email_cc", "email_bcc", "email_subject",
}
_STRUCTURAL_TARGETS = {"bates_begin", "bates_end", "page_count", "text_link", "native_link"}
_DATE_TARGETS = {"date_sent", "date_received", "date_created", "date_modified"}

_DATE_FORMATS = [
    "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
    "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M", "%m/%d/%Y",
]


def normalize_date(value: str) -> datetime | None:
    if not isinstance(value, str):
        return None
    if not value.strip():
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+0000"
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(v, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def derive_file_type(name_or_path: str | None) -> str | None:
    if not name_or_path:
        return None
    ext = os.path.splitext(name_or_path)[1].lstrip(".").lower()
    return ext or None


def promote_record(record: dict, field_mapping: dict[str, str]) -> tuple[dict, dict]:
    """Return (typed_fields, leftover_metadata).

    typed_fields: canonical metadata field -> normalized value (dates parsed).
    leftover_metadata: ALL non-empty source columns EXCEPT those consumed by a
    structural target — mapped-metadata originals are still kept here so nothing
    is lost.
    """
    typed: dict = {}
    for canon, source_col in field_mapping.items():
        if canon not in _METADATA_TARGETS:
            continue
        raw = (record.get(source_col) or "").strip()
        if not raw:
            continue
        if canon in _DATE_TARGETS:
            dt = normalize_date(raw)
            if dt is not None:
                typed[canon] = dt
        elif canon == "file_type":
            typed[canon] = raw.lstrip(".").lower()[:50]
        else:
            typed[canon] = raw

    structural_cols = {
        source_col for canon, source_col in field_mapping.items()
        if canon in _STRUCTURAL_TARGETS
    }

    def _is_empty(v) -> bool:
        return v is None or (isinstance(v, str) and not v.strip())

    leftover = {
        k: v for k, v in record.items()
        if k not in structural_cols and not _is_empty(v)
    }
    return typed, leftover
