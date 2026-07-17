"""Smart load-file parsing: detect encoding + delimiter and parse to records.

Tolerates Concordance (þ-wrapped, DC4-separated), CSV, TAB, and pipe formats,
with UTF-8/UTF-16/Windows-1252 encodings, so ingest is not tied to one layout.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

THORN = "þ"        # þ  Concordance field wrapper / quote
DC4 = "\x14"            # Concordance field separator
_DELIMS = [DC4, "\t", ",", "|"]


@dataclass
class LoadFileParse:
    encoding: str
    delimiter: str
    headers: list[str]
    sample_rows: list[dict]
    total_rows: int


def detect_encoding(raw: bytes) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return "utf-16"
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "cp1252"


def detect_delimiter(text_line: str) -> str:
    # Concordance DC4 wins if present; else the most frequent candidate.
    if DC4 in text_line:
        return DC4
    best, best_count = ",", 0
    for d in ["\t", ",", "|"]:
        c = text_line.count(d)
        if c > best_count:
            best, best_count = d, c
    return best


def _split(row: str, delimiter: str) -> list[str]:
    if delimiter == DC4:
        return [f.strip(THORN).strip() for f in row.split(DC4)]
    reader = csv.reader(io.StringIO(row), delimiter=delimiter)
    fields = next(reader, [])
    return [f.strip() for f in fields]


def parse_loadfile(path: str, sample_size: int = 20) -> LoadFileParse:
    with open(path, "rb") as f:
        raw = f.read()
    encoding = detect_encoding(raw)
    text = raw.decode(encoding, errors="replace").replace("\x00", "")

    lines = text.strip().split("\r\n")
    if len(lines) == 1 and "\n" in lines[0]:
        lines = text.strip().split("\n")
    if not lines or not lines[0].strip():
        return LoadFileParse(encoding, ",", [], [], 0)

    delimiter = detect_delimiter(lines[0])
    headers = _split(lines[0], delimiter)

    rows: list[dict] = []
    total = 0
    for line in lines[1:]:
        if not line.strip():
            continue
        total += 1
        if len(rows) < sample_size:
            values = _split(line, delimiter)
            record = {}
            for i, h in enumerate(headers):
                v = values[i] if i < len(values) else ""
                if v and "\\" in v:
                    v = v.replace("\\", "/")
                record[h] = v
            rows.append(record)
    return LoadFileParse(encoding, delimiter, headers, rows, total)
