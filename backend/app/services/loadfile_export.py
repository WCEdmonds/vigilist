"""Pure Concordance DAT / Opticon OPT writers + manifest helpers (P2-3).

These mirror the import conventions in app/utils/parsers.py exactly — the
import parsers are the round-trip oracle for this module's output.
"""

from __future__ import annotations

from datetime import datetime, timezone

FIELD_WRAPPER = "þ"   # thorn
FIELD_SEPARATOR = "\x14"   # DC4

DAT_COLUMNS = ["BEGBATES", "ENDBATES", "BEGATTACH", "ENDATTACH", "CUSTODIAN",
               "FROM", "TO", "CC", "DATESENT", "DATERECEIVED", "SUBJECT",
               "FILENAME", "FILETYPE", "MD5HASH", "SHA256HASH", "PAGECOUNT",
               "REDACTED", "WITHHELD", "CONFIDENTIALITY", "TEXTPATH",
               "NATIVELINK"]


def _clean(value) -> str:
    """Format-breaking characters never legitimately appear in metadata."""
    if value is None:
        return ""
    s = str(value)
    for ch in (FIELD_WRAPPER, FIELD_SEPARATOR, "\r", "\n"):
        s = s.replace(ch, " ")
    return s.strip()


def dat_bytes(rows: list[dict]) -> bytes:
    def fmt(values):
        return FIELD_SEPARATOR.join(
            f"{FIELD_WRAPPER}{v}{FIELD_WRAPPER}" for v in values)

    lines = [fmt(DAT_COLUMNS)]
    for row in rows:
        lines.append(fmt(_clean(row.get(c)) for c in DAT_COLUMNS))
    return ("\r\n".join(lines) + "\r\n").encode("utf-8-sig")


def opt_bytes(entries: list[tuple[str, str, str, int]]) -> bytes:
    lines = [f"{bates},{volume},{path},Y,,,{pages}"
             for bates, volume, path, pages in entries]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def opt_bytes_paged(docs: list[tuple[str, list[tuple[str, str]]]]) -> bytes:
    """Page-level Opticon for TIFF productions.

    docs = (volume, [(page_bates, path), ...]) per document, in sort order.
    First page row carries the doc break + page count; continuation rows
    leave both blank — exactly the shape parse_opt groups back into docs.
    """
    lines = []
    for volume, pages in docs:
        for i, (page_bates, path) in enumerate(pages):
            if i == 0:
                lines.append(f"{page_bates},{volume},{path},Y,,,{len(pages)}")
            else:
                lines.append(f"{page_bates},{volume},{path},,,,")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def check_continuity(items: list[tuple[str, str, int]], prefix: str,
                     start_number: int) -> list[str]:
    """items = (bates_begin, bates_end, pages) in sort order. [] = gap-free."""
    if not items:
        return ["production set has no members"]
    errors = []
    expected = start_number
    for begin, end, pages in items:
        b, e = int(begin[len(prefix):]), int(end[len(prefix):])
        if b != expected:
            errors.append(f"{begin}: expected to start at {prefix}{expected} (gap or overlap)")
        if e != b + pages - 1:
            errors.append(f"{begin}: end {end} does not match {pages} page(s)")
        expected = e + 1
    return errors


def manifest_dict(ps_info: dict, counts: dict, bates_range: dict,
                  continuity_errors: list[str], artifacts: list[dict]) -> dict:
    return {
        "production_set": ps_info,
        "counts": counts,
        "bates_range": bates_range,
        "continuity": {"ok": not continuity_errors, "errors": list(continuity_errors)},
        "artifacts": artifacts,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
