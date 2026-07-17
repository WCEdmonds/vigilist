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


import json
import logging

logger = logging.getLogger(__name__)

_AI_MODEL = "claude-opus-4-8"
_MAX_SAMPLES = 3


def _default_client():
    """Return an Anthropic client if a key is configured, else None."""
    from app.config import settings
    if not settings.anthropic_api_key:
        return None
    import anthropic
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def propose_ai_mapping(columns: list[dict], client=None) -> dict[str, str]:
    """Ask the model to map unrecognized columns to canonical fields.
    Returns {source_name: canonical_field}. Never raises — returns {} on any
    failure or when no client/key is available (ingest must not block on AI)."""
    if not columns:
        return {}
    if client is None:
        client = _default_client()
    if client is None:
        return {}

    lines = []
    for c in columns:
        samples = ", ".join(str(s) for s in c.get("samples", [])[:_MAX_SAMPLES] if s)
        lines.append(f'- "{c["name"]}" (examples: {samples})')
    prompt = (
        "Map each e-discovery load-file column to ONE canonical field, or null if none fits.\n"
        f"Canonical fields: {', '.join(CANONICAL_FIELDS)}\n\n"
        "Columns:\n" + "\n".join(lines) + "\n\n"
        'Respond with ONLY a JSON object {"<column name>": "<canonical field or null>"}.'
    )
    try:
        msg = client.messages.create(
            model=_AI_MODEL, max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
        parsed = json.loads(raw)
    except Exception:
        logger.warning("AI column mapping failed; leaving columns unmapped", exc_info=True)
        return {}

    valid = set(CANONICAL_FIELDS)
    return {
        name: target for name, target in parsed.items()
        if isinstance(target, str) and target in valid
    }


def build_proposed_mapping(headers: list[str], sample_rows: list[dict], use_ai: bool = True) -> list[dict]:
    """Combine alias matching + AI fallback into a per-column proposal list."""
    alias_map = match_aliases(headers)                     # canonical -> source
    source_to_canon = {src: canon for canon, src in alias_map.items()}

    def samples_for(name: str) -> list[str]:
        vals = []
        for r in sample_rows:
            v = r.get(name)
            if v:
                vals.append(v)
            if len(vals) >= _MAX_SAMPLES:
                break
        return vals

    unmapped = [h for h in headers if h not in source_to_canon]
    ai_map: dict[str, str] = {}
    if use_ai and unmapped:
        ai_map = propose_ai_mapping(
            [{"name": h, "samples": samples_for(h)} for h in unmapped]
        )

    proposed = []
    for h in headers:
        if h in source_to_canon:
            proposed.append({"source_name": h, "samples": samples_for(h),
                             "target": source_to_canon[h], "confidence": 1.0, "source": "alias"})
        elif h in ai_map:
            proposed.append({"source_name": h, "samples": samples_for(h),
                             "target": ai_map[h], "confidence": 0.6, "source": "ai"})
        else:
            proposed.append({"source_name": h, "samples": samples_for(h),
                             "target": None, "confidence": 0.0, "source": "unmapped"})
    return proposed
