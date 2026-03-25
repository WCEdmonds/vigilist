"""Parsers for Concordance DAT and Opticon OPT file formats."""

import csv
import io
from pathlib import PurePosixPath


FIELD_WRAPPER = "\u00fe"  # þ (thorn)
FIELD_SEPARATOR = "\x14"  # DC4 control character


def parse_dat(file_path: str) -> list[dict[str, str]]:
    """Parse a Concordance DAT file.

    Format: UTF-8 with BOM, þ-wrapped fields, DC4 field separator, CRLF rows.
    First row is headers. Returns list of dicts keyed by header names.
    Path fields are normalized to forward slashes.
    """
    with open(file_path, "r", encoding="utf-8-sig") as f:
        content = f.read()

    rows = content.strip().split("\r\n")
    if not rows:
        return []

    # If file used just \n, try that
    if len(rows) == 1 and "\n" in rows[0]:
        rows = content.strip().split("\n")

    def parse_row(row: str) -> list[str]:
        fields = row.split(FIELD_SEPARATOR)
        return [f.strip(FIELD_WRAPPER).strip() for f in fields]

    headers = parse_row(rows[0])
    documents = []
    for row in rows[1:]:
        if not row.strip():
            continue
        values = parse_row(row)
        record = {}
        for i, header in enumerate(headers):
            val = values[i] if i < len(values) else ""
            # Normalize backslash paths to forward slashes
            if val and "\\" in val:
                val = val.replace("\\", "/")
            record[header] = val
        documents.append(record)

    return documents


def parse_opt(file_path: str) -> dict[str, list[str]]:
    """Parse an Opticon OPT file.

    Format: comma-delimited, no header, 7 fields per row.
    Returns dict mapping bates_begin -> ordered list of image paths.
    Groups pages by Doc Break = 'Y'.
    """
    with open(file_path, "r", encoding="utf-8-sig") as f:
        content = f.read()

    documents: dict[str, list[str]] = {}
    current_bates: str | None = None
    current_pages: list[str] = []

    for line in content.strip().splitlines():
        if not line.strip():
            continue

        # OPT is comma-delimited; fields may contain spaces (Bates numbers do)
        reader = csv.reader(io.StringIO(line))
        fields = next(reader)

        if len(fields) < 4:
            continue

        bates = fields[0].strip()
        image_path = fields[2].strip().replace("\\", "/")
        doc_break = fields[3].strip().upper()

        if doc_break == "Y":
            # Save previous document
            if current_bates and current_pages:
                documents[current_bates] = current_pages
            current_bates = bates
            current_pages = [image_path]
        else:
            current_pages.append(image_path)

    # Save last document
    if current_bates and current_pages:
        documents[current_bates] = current_pages

    return documents
