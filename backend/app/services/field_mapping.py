"""Map load-file columns to canonical fields: a deterministic alias dictionary
plus (Task 4) an AI-assisted fallback for unrecognized columns."""

from __future__ import annotations

# Canonical fields that a source column can map to. "text_link"/"native_link"
# are structural (used to locate text/native files); the rest are typed columns.
CANONICAL_FIELDS: list[str] = [
    "bates_begin", "bates_end", "page_count", "text_link", "native_link",
    "custodian", "date_sent", "date_received", "date_created", "date_modified",
    "file_hash_md5", "file_hash_sha256", "file_type", "file_name", "source_path",
    "email_from", "email_to", "email_cc", "email_bcc", "email_subject",
]

ALIAS_DICT: dict[str, list[str]] = {
    "bates_begin": ["Begin Bates", "BegBates", "BEGDOC", "Bates Beg", "Bates Begin", "DocID", "Production::Begin Bates"],
    "bates_end": ["End Bates", "EndBates", "ENDDOC", "Bates End", "Production::End Bates"],
    "page_count": ["Page Count", "Pages", "PageCount", "Num Pages"],
    "text_link": ["Text Link", "Extracted Text", "OCR Path", "TextLink", "Text Path"],
    "native_link": ["Native Link", "NativeLink", "Native Path", "File Path", "Native File"],
    "custodian": ["Custodian", "Cust", "Custodian Name", "Source Custodian"],
    "date_sent": ["Date Sent", "Sent", "DateSent", "Sent Date", "Email Sent Date"],
    "date_received": ["Date Received", "Received", "DateReceived", "Received Date"],
    "date_created": ["Date Created", "Created", "DateCreated", "Creation Date", "File Created"],
    "date_modified": ["Date Modified", "Modified", "DateModified", "Last Modified", "File Modified"],
    "file_hash_md5": ["MD5", "MD5 Hash", "Hash", "MD5Hash", "MD5 Digest"],
    "file_hash_sha256": ["SHA256", "SHA-256", "SHA256 Hash", "SHA Hash"],
    "file_type": ["File Type", "FileType", "Type", "File Extension", "Extension", "Doc Type"],
    "file_name": ["File Name", "FileName", "Filename", "Original File Name", "Document Name"],
    "source_path": ["Source Path", "SourcePath", "Original Path", "Full Path", "Path"],
    "email_from": ["From", "Email From", "Author", "Sender"],
    "email_to": ["To", "Email To", "Recipients", "Recipient"],
    "email_cc": ["CC", "Email CC", "Copyee"],
    "email_bcc": ["BCC", "Email BCC", "Blind Copyee"],
    "email_subject": ["Subject", "Email Subject", "Title"],
}


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


# Precompute normalized alias → canonical (first canonical wins per alias).
_NORM_ALIAS: dict[str, str] = {}
for _canon, _aliases in ALIAS_DICT.items():
    for _a in _aliases:
        _NORM_ALIAS.setdefault(_norm(_a), _canon)


def match_aliases(headers: list[str]) -> dict[str, str]:
    """Return {canonical_field: source_header} for recognized headers.
    First header wins if two map to the same canonical field."""
    result: dict[str, str] = {}
    for h in headers:
        canon = _NORM_ALIAS.get(_norm(h))
        if canon and canon not in result:
            result[canon] = h
    return result
