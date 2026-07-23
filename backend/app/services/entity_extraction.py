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
- Never include character offsets or positions; return only verbatim strings.
- Respond with ONLY the JSON object, no other text."""


def build_extraction_prompt(document_text: str) -> str:
    return f"## Document Text\n\n{document_text}\n\nExtract entities, events, and relationships. Respond with JSON only."


def _clean_str(v, limit: int = 500) -> str:
    return str(v).strip()[:limit] if isinstance(v, (str, int, float)) else ""


def _as_list(v, limit: int | None = None) -> list:
    """Validate-by-isinstance: anything that isn't a list (dict, int, bool,
    str, None, ...) becomes an empty list instead of raising downstream when
    sliced/iterated. Optionally truncates to `limit` items."""
    if not isinstance(v, list):
        return []
    return v[:limit] if limit is not None else v


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
    for ent in _as_list(data.get("entities"), MAX_ENTITIES):
        if not isinstance(ent, dict):
            continue
        name = _clean_str(ent.get("name"))
        etype = _clean_str(ent.get("type"), 10)
        if not name or etype not in ENTITY_TYPES:
            continue
        forms = [_clean_str(f) for f in _as_list(ent.get("surface_forms")) if _clean_str(f)]
        entities.append({
            "name": name,
            "type": etype,
            "surface_forms": (forms or [name])[:MAX_SURFACE_FORMS],
            "role": _clean_str(ent.get("role")) or None,
            "emails": [_clean_str(e).lower() for e in _as_list(ent.get("emails")) if "@" in _clean_str(e)],
        })

    events = []
    for ev in _as_list(data.get("events"), MAX_EVENTS):
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
            "participants": [_clean_str(p) for p in _as_list(ev.get("participants")) if _clean_str(p)],
        })

    relationships = []
    for rel in _as_list(data.get("relationships"), MAX_RELATIONSHIPS):
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
    window = max(1, window)
    overlap = max(0, min(overlap, window - 1))
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


# ── Persistence + LLM call (imports kept local to preserve the pure top half) ──

import asyncio as _asyncio
import uuid as _uuid

from app.config import settings

EXTRACTION_MODEL = "claude-haiku-4-5"   # keep in sync with services/ai.py haiku usage
_EXTRACT_MAX_ATTEMPTS = 3
_RETRYABLE_ERRORS: tuple[type[BaseException], ...] | None = None


def _retryable_errors() -> tuple[type[BaseException], ...]:
    # Same lazy-resolve pattern as services/ai_review.py — see rationale there.
    global _RETRYABLE_ERRORS
    if _RETRYABLE_ERRORS is None:
        try:
            import anthropic
            _RETRYABLE_ERRORS = (anthropic.RateLimitError, anthropic.APIStatusError, anthropic.APIConnectionError)
        except Exception:
            _RETRYABLE_ERRORS = ()
    return _RETRYABLE_ERRORS


def merge_parsed(results: list[dict]) -> dict:
    """Merge per-slice parses; entities dedupe by (type, name), keeping the
    union of surface forms/emails. Events and relationships also dedupe
    (slices overlap by design, so the same event/relationship can be
    re-extracted from two adjacent slices) — first occurrence wins, order
    preserved via dict insertion order."""
    by_key: dict = {}
    events_by_key: dict = {}
    relationships_by_key: dict = {}
    for r in results:
        for ent in r["entities"]:
            key = (ent["type"], ent["name"].lower())
            if key in by_key:
                have = by_key[key]
                have["surface_forms"] = list(dict.fromkeys(have["surface_forms"] + ent["surface_forms"]))
                have["emails"] = list(dict.fromkeys(have["emails"] + ent["emails"]))
                have["role"] = have["role"] or ent["role"]
            else:
                by_key[key] = dict(ent)
        for ev in r["events"]:
            ev_key = (ev["description"].lower(), ev["type"], ev["date"])
            events_by_key.setdefault(ev_key, ev)
        for rel in r["relationships"]:
            rel_key = (rel["source"].lower(), rel["target"].lower(), rel["type"])
            relationships_by_key.setdefault(rel_key, rel)
    return {
        "entities": list(by_key.values()),
        "events": list(events_by_key.values()),
        "relationships": list(relationships_by_key.values()),
    }


async def extract_document_entities(text: str) -> dict | None:
    """Run LLM extraction over the document (sliced if long). None = hard
    failure or missing key (retry later); a dict with empty lists is a real
    'nothing found' result."""
    if not settings.anthropic_api_key:
        return None
    import anthropic  # lazy: keep the SDK off the startup path

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    retryable = _retryable_errors()
    parsed_slices: list[dict] = []
    for chunk in slice_text(text):
        raw = None
        for attempt in range(_EXTRACT_MAX_ATTEMPTS):
            try:
                response = await client.messages.create(
                    model=EXTRACTION_MODEL,
                    max_tokens=4000,
                    system=EXTRACTION_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": build_extraction_prompt(chunk)}],
                )
                raw = next((b.text for b in response.content if b.type == "text"), "")
                break
            except retryable as e:
                status = getattr(e, "status_code", None)
                if status is not None and status not in (408, 429) and status < 500:
                    logger.error("Extraction failed with non-retryable status %s: %s", status, e)
                    return None
                logger.warning("Extraction attempt %d/%d failed: %s", attempt + 1, _EXTRACT_MAX_ATTEMPTS, e)
                if attempt < _EXTRACT_MAX_ATTEMPTS - 1:
                    await _asyncio.sleep(2 * (attempt + 1))
            except Exception as e:
                logger.error("Extraction failed: %s", e)
                return None
        if raw is None:
            return None
        parsed_slices.append(parse_extraction_response(raw))
    return merge_parsed(parsed_slices)


def header_candidates(doc) -> list[dict]:
    """Deterministic person candidates from parsed email header columns."""
    out, seen = [], set()
    for field in (doc.email_from, doc.email_to, doc.email_cc, doc.email_bcc):
        for name, addr in parse_email_addresses(field):
            if addr in seen:
                continue
            seen.add(addr)
            display = name or addr.split("@", 1)[0]
            out.append({"name": display, "type": "person",
                        "surface_forms": [f for f in (name, addr) if f], "role": None, "emails": [addr]})
    return out


async def persist_extraction(db, production_id: int, document_id, text: str, parsed: dict) -> dict:
    """Write one document's extraction into the ontology. Caller commits."""
    from sqlalchemy import select
    from app.models import (Entity, EntityMention, EntityMergeSuggestion,
                             EntityRelationship, EventParticipant, OntologyEvent)
    from app.services.entity_resolution import match_entity, normalize_name

    existing = list((await db.execute(
        select(Entity).where(Entity.production_id == production_id)
    )).scalars().all())

    stats = {"entities": 0, "mentions": 0, "events": 0, "relationships": 0, "suggestions": 0}
    name_to_entity: dict[str, Entity] = {}
    # Two candidates can resolve to the same entity (e.g. one attaches via
    # alias) with overlapping surface forms, so locate_mentions can rediscover
    # the same offset twice across candidates — track what's already been
    # added this call so we never emit a duplicate (entity_id, start_offset)
    # row (would violate uq_mention_doc_entity_offset at commit). Offset-less
    # (OCR-drift) mentions key on (entity.id, None) — at most one per entity.
    seen_mentions: set[tuple] = set()

    for cand in parsed["entities"]:
        decision = match_entity(cand, existing)
        if decision[0] == "attach":
            entity = decision[1]
            new_aliases = [f for f in cand["surface_forms"]
                           if normalize_name(f) != normalize_name(entity.canonical_name)
                           and f not in (entity.aliases or [])]
            if new_aliases:
                entity.aliases = list(entity.aliases or []) + new_aliases
            if cand["emails"]:
                attrs = dict(entity.attributes or {})
                attrs["emails"] = list(dict.fromkeys((attrs.get("emails") or []) + cand["emails"]))
                entity.attributes = attrs
        else:
            entity = Entity(
                id=_uuid.uuid4(), production_id=production_id, entity_type=cand["type"],
                canonical_name=cand["name"], aliases=list(cand["surface_forms"]),
                attributes={k: v for k, v in (("role", cand["role"]), ("emails", cand["emails"])) if v},
                mention_count=0,
            )
            db.add(entity)
            existing.append(entity)
            stats["entities"] += 1
            if decision[0] == "suggest":
                _, other, score, rationale = decision
                db.add(EntityMergeSuggestion(
                    production_id=production_id, entity_a_id=entity.id, entity_b_id=other.id,
                    score=score, rationale=rationale, status="pending",
                ))
                stats["suggestions"] += 1

        name_to_entity[cand["name"].lower()] = entity
        mentions = locate_mentions(text or "", cand["surface_forms"])
        if not mentions and cand["surface_forms"]:
            # OCR drift: not locatable verbatim — record one offset-less mention
            mentions = [{"surface_text": cand["surface_forms"][0], "start_offset": None,
                         "end_offset": None, "context_snippet": None}]
        inserted = 0
        for m in mentions:
            mention_key = (entity.id, m["start_offset"])
            if mention_key in seen_mentions:
                continue
            seen_mentions.add(mention_key)
            db.add(EntityMention(production_id=production_id, entity_id=entity.id,
                                  document_id=document_id, **m))
            inserted += 1
        entity.mention_count = (entity.mention_count or 0) + inserted
        stats["mentions"] += inserted

    for ev in parsed["events"]:
        event_date, precision = parse_event_date(ev["date"])
        event = OntologyEvent(production_id=production_id, event_type=ev["type"],
                               description=ev["description"], event_date=event_date,
                               date_precision=precision, document_id=document_id)
        event.participants = [
            EventParticipant(entity_id=name_to_entity[p.lower()].id)
            for p in dict.fromkeys(ev["participants"]) if p.lower() in name_to_entity
        ]
        db.add(event)
        stats["events"] += 1

    # Belt-and-braces: slices can overlap (merge_parsed already dedupes across
    # slices), but also guard within this call so two candidates that resolve
    # to the same pair of entities never emit a duplicate edge (would violate
    # uq_edge_pair_type_doc at commit).
    seen_relationships: set[tuple] = set()
    for rel in parsed["relationships"]:
        src = name_to_entity.get(rel["source"].lower())
        tgt = name_to_entity.get(rel["target"].lower())
        if src is None or tgt is None or src.id == tgt.id:
            continue
        rel_key = (src.id, tgt.id, rel["type"])
        if rel_key in seen_relationships:
            continue
        seen_relationships.add(rel_key)
        db.add(EntityRelationship(production_id=production_id, source_entity_id=src.id,
                                   target_entity_id=tgt.id, relationship_type=rel["type"],
                                   description=rel["evidence"], document_id=document_id))
        stats["relationships"] += 1

    return stats
