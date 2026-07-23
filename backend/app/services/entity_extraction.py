"""LLM entity/event/relationship extraction — pure helpers.

The model returns verbatim surface strings, never offsets; offsets are
computed here by string search so they are always exact. The Claude call
itself is added in the worker (extract_document_entities) — this module's
top half stays pure and unit-testable.
"""

import json
import logging
import re
from datetime import date
from email.utils import getaddresses

logger = logging.getLogger(__name__)

ENTITY_TYPES = {"person", "org"}
EVENT_TYPES = {"meeting", "communication", "payment", "filing", "agreement", "other"}
RELATIONSHIP_TYPES = {"employment", "counsel", "correspondent", "party_to_agreement", "family", "other"}

MAX_ENTITIES = 50
MAX_EVENTS = 30
MAX_RELATIONSHIPS = 30
MAX_SURFACE_FORMS = 10
SNIPPET_RADIUS = 80

EXTRACTION_SYSTEM_PROMPT = """You are an information-extraction engine for legal document review. Extract every person, organization, event, and stated relationship from the document.

You MUST respond with ONLY a JSON object of this exact shape:
{
  "entities": [
    {
      "name": "Jorge Rivera",
      "type": "person",
      "surface_forms": ["Jorge Rivera", "J. Rivera", "Rivera"],
      "role": "CFO of Acme Corp",
      "emails": ["jrivera@acme.com"]
    }
  ],
  "events": [
    {
      "description": "Board approved the Series B financing",
      "type": "meeting",
      "date": "2019-03-15",
      "participants": ["Jorge Rivera", "Acme Corp"]
    }
  ],
  "relationships": [
    {
      "source": "Jorge Rivera",
      "target": "Acme Corp",
      "type": "employment",
      "evidence": "signature block: 'Jorge Rivera, CFO, Acme Corp'"
    }
  ]
}

Rules:
- "type" for entities is "person" or "org".
- "type" for events is one of: meeting, communication, payment, filing, agreement, other.
- "type" for relationships is one of: employment, counsel, correspondent, party_to_agreement, family, other.
- "date" is "YYYY-MM-DD", "YYYY-MM", "YYYY", or null if undated.
- surface_forms MUST be verbatim substrings of the document text — never normalize, expand, or correct spelling. Include the name itself if it appears verbatim.
- "participants", "source" and "target" must use the "name" of an entity in "entities".
- Skip generic references ("the plaintiff", "opposing counsel") that are never tied to a name.
- Only include relationships the document itself states or clearly shows; never infer from mere co-occurrence.
- Respond with ONLY the JSON object, no other text."""


def build_extraction_prompt(document_text: str) -> str:
    return f"## Document Text\n\n{document_text}\n\nExtract entities, events, and relationships. Respond with JSON only."


def _clean_str(v, limit: int = 500) -> str:
    return str(v).strip()[:limit] if isinstance(v, (str, int, float)) else ""


def parse_extraction_response(raw: str) -> dict:
    """Defensive parse — always returns the full sentinel shape."""
    empty = {"entities": [], "events": [], "relationships": []}
    try:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError, TypeError, IndexError) as e:
        logger.warning("Failed to parse extraction response: %s", e)
        return empty
    if not isinstance(data, dict):
        return empty

    entities = []
    for ent in (data.get("entities") or [])[:MAX_ENTITIES]:
        if not isinstance(ent, dict):
            continue
        name = _clean_str(ent.get("name"))
        etype = _clean_str(ent.get("type"), 10)
        if not name or etype not in ENTITY_TYPES:
            continue
        forms = [_clean_str(f) for f in (ent.get("surface_forms") or []) if _clean_str(f)]
        entities.append({
            "name": name,
            "type": etype,
            "surface_forms": (forms or [name])[:MAX_SURFACE_FORMS],
            "role": _clean_str(ent.get("role")) or None,
            "emails": [_clean_str(e).lower() for e in (ent.get("emails") or []) if "@" in _clean_str(e)],
        })

    events = []
    for ev in (data.get("events") or [])[:MAX_EVENTS]:
        if not isinstance(ev, dict):
            continue
        desc = _clean_str(ev.get("description"), 2000)
        if not desc:
            continue
        etype = _clean_str(ev.get("type"), 20)
        events.append({
            "description": desc,
            "type": etype if etype in EVENT_TYPES else "other",
            "date": _clean_str(ev.get("date"), 10) or None,
            "participants": [_clean_str(p) for p in (ev.get("participants") or []) if _clean_str(p)],
        })

    relationships = []
    for rel in (data.get("relationships") or [])[:MAX_RELATIONSHIPS]:
        if not isinstance(rel, dict):
            continue
        src, tgt = _clean_str(rel.get("source")), _clean_str(rel.get("target"))
        rtype = _clean_str(rel.get("type"), 30)
        if not src or not tgt or src == tgt:
            continue
        relationships.append({
            "source": src,
            "target": tgt,
            "type": rtype if rtype in RELATIONSHIP_TYPES else "other",
            "evidence": _clean_str(rel.get("evidence"), 2000) or None,
        })

    return {"entities": entities, "events": events, "relationships": relationships}


def slice_text(text: str, window: int = 140_000, overlap: int = 2_000) -> list[str]:
    """Split long text into overlapping windows for per-slice extraction."""
    if len(text) <= window:
        return [text]
    slices, start = [], 0
    while start < len(text):
        slices.append(text[start:start + window])
        if start + window >= len(text):
            break
        start += window - overlap
    return slices


def locate_mentions(text: str, surface_forms: list[str], max_per_form: int = 200) -> list[dict]:
    """Find every occurrence of each surface form, longest-first so a short
    form ('Rivera') never double-claims the middle of a long one
    ('Jorge Rivera'). Returns offset-sorted mention dicts."""
    claimed: list[tuple[int, int]] = []
    out: list[dict] = []
    for form in sorted(set(f for f in surface_forms if f), key=len, reverse=True):
        pos, found = 0, 0
        while found < max_per_form:
            idx = text.find(form, pos)
            if idx == -1:
                break
            end = idx + len(form)
            pos = idx + 1
            if any(s < end and idx < e for s, e in claimed):
                continue
            claimed.append((idx, end))
            out.append({
                "surface_text": form,
                "start_offset": idx,
                "end_offset": end,
                "context_snippet": text[max(0, idx - SNIPPET_RADIUS):end + SNIPPET_RADIUS],
            })
            found += 1
    out.sort(key=lambda m: m["start_offset"])
    return out


def parse_event_date(raw: str | None) -> tuple[date | None, str]:
    if not raw:
        return None, "unknown"
    raw = raw.strip()
    m = re.fullmatch(r"(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?", raw)
    if not m:
        return None, "unknown"
    y, mo, d = m.group(1), m.group(2), m.group(3)
    try:
        if d:
            return date(int(y), int(mo), int(d)), "day"
        if mo:
            return date(int(y), int(mo), 1), "month"
        return date(int(y), 1, 1), "year"
    except ValueError:
        return None, "unknown"


def parse_email_addresses(raw: str | None) -> list[tuple[str, str]]:
    """Parse an email header value into (display_name, email) pairs."""
    if not raw:
        return []
    pairs = getaddresses([raw.replace(";", ",")])
    return [(name.strip(), addr.strip().lower()) for name, addr in pairs if "@" in addr]
