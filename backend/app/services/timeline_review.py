"""Whole-timeline AI review: one Opus call per production that merges
cross-document duplicate events, deletes irrelevant entries, and corrects
extraction errors. Verdicts auto-apply with full audit snapshots; a
confidence gate and a human-edit guardrail bound the blast radius.

Spec: docs/superpowers/specs/2026-07-24-timeline-review-design.md
"""

import asyncio as _asyncio
import json
import logging
from datetime import date

from sqlalchemy import select

from app.config import settings
from app.models import (AuditLog, Document, Entity, EventParticipant,
                        OntologyEvent)
from app.services.audit import log_action
from app.services.entity_extraction import EVENT_TYPES

logger = logging.getLogger(__name__)

REVIEW_MODEL = "claude-opus-5"   # Opus 5 (2026-07-24) — same price/surface as 4.8
REVIEW_MIN_CONFIDENCE = 0.8
_REVIEW_MAX_ATTEMPTS = 3
_RETRYABLE_ERRORS: tuple[type[BaseException], ...] | None = None


class ReviewError(Exception):
    """Model call failed, was truncated, or returned an unusable response."""


def _retryable_errors() -> tuple[type[BaseException], ...]:
    # Same lazy-resolve pattern as services/entity_extraction.py.
    global _RETRYABLE_ERRORS
    if _RETRYABLE_ERRORS is None:
        try:
            import anthropic
            _RETRYABLE_ERRORS = (anthropic.RateLimitError, anthropic.APIStatusError,
                                 anthropic.APIConnectionError)
        except Exception:
            _RETRYABLE_ERRORS = ()
    return _RETRYABLE_ERRORS


# ── Serialization ──

def serialize_timeline(events: list, bates: dict, participants: dict) -> str:
    """One compact JSON line per event, chronological (undated last) so
    duplicates sit next to each other in the model's view."""
    ordered = sorted(events, key=lambda e: (e.event_date is None,
                                            e.event_date or date.min, e.id))
    lines = []
    for e in ordered:
        lines.append(json.dumps({
            "id": e.id,
            "date": e.event_date.isoformat() if e.event_date else None,
            "precision": e.date_precision,
            "type": e.event_type,
            "desc": e.description,
            "quote": e.date_source_text,
            "bates": bates.get(e.id),
            "who": participants.get(e.id, []),
        }, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines)


REVIEW_SYSTEM_PROMPT = """You are a senior litigator reviewing an AI-extracted case chronology for quality. The timeline was extracted per-document, so the same real-world event often appears multiple times, and individual entries can carry wrong dates or be irrelevant to the dispute.

Return verdicts ONLY where you are confident. Silence means keep: any event you do not name is left untouched. When uncertain, return no verdict for that event.

Verdict kinds:
- "merge": two or more entries describe the SAME real-world occurrence (same happening, same or compatible dates — one may be less precise). Recurring similar events (e.g. weekly meetings) are NOT duplicates. Set event_ids to the full group, keep_id to the best-evidenced entry (prefer day-precision dates and the richest description). Optionally supply a corrected description/date/precision for the keeper.
- "delete": the entry is irrelevant to the dispute (litigation-process machinery like court reporting or filing logistics, pure pleasantries) or is extraction garbage (garbled, meaningless).
- "edit": the entry's date, precision, type, or description is contradicted by its own quote or is impossible within the surrounding chronology. Only with clear evidence; cite it in reason.

Every verdict carries a one-sentence reason and a confidence from 0 to 1. Confidence below 0.8 will not be applied, so do not pad the list with low-confidence guesses."""


def build_review_user_content(serialized: str, event_count: int) -> str:
    plural = "event" if event_count == 1 else "events"
    return (f"Case chronology: {event_count} {plural}, one JSON object per line, "
            f"in chronological order (undated entries last).\n\n"
            f"{serialized}\n\n"
            f"Review the whole chronology and return your verdicts.")


# ── Structured-output schema ──
# Flat verdict object: `kind` discriminates, unused fields are null. All
# fields required + additionalProperties false (structured-output rules);
# numeric range constraints are unsupported by the API, so the confidence
# gate is enforced in apply_verdicts, not here.

_NULL_STR = {"anyOf": [{"type": "string"}, {"type": "null"}]}
_NULL_INT = {"anyOf": [{"type": "integer"}, {"type": "null"}]}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["merge", "delete", "edit"]},
                    "event_id": _NULL_INT,
                    "event_ids": {"anyOf": [{"type": "array", "items": {"type": "integer"}},
                                            {"type": "null"}]},
                    "keep_id": _NULL_INT,
                    "date": _NULL_STR,
                    "precision": {"anyOf": [{"type": "string",
                                             "enum": ["day", "month", "year", "unknown"]},
                                            {"type": "null"}]},
                    "event_type": _NULL_STR,
                    "description": _NULL_STR,
                    "reason": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["kind", "event_id", "event_ids", "keep_id", "date",
                             "precision", "event_type", "description", "reason",
                             "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}


def parse_review_response(raw: str) -> list[dict]:
    """Parse the model's JSON into a verdict list. Structured outputs make
    malformed responses unlikely, but the guard stays: a run either parses
    fully or applies nothing."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        raise ReviewError(f"Unparseable review response: {e}") from e
    verdicts = data.get("verdicts") if isinstance(data, dict) else None
    if not isinstance(verdicts, list):
        raise ReviewError("Review response missing 'verdicts' list")
    return [v for v in verdicts if isinstance(v, dict)]


# ── Applying verdicts ──

def _snapshot(ev) -> dict:
    return {"event_type": ev.event_type, "description": ev.description,
            "event_date": ev.event_date.isoformat() if ev.event_date else None,
            "date_precision": ev.date_precision, "significance": ev.significance,
            "date_source_text": ev.date_source_text,
            "document_id": str(ev.document_id)}


def _parse_iso_date(raw: str | None):
    """Returns (ok, date|None)."""
    if raw is None:
        return True, None
    try:
        return True, date.fromisoformat(raw)
    except ValueError:
        return False, None


async def apply_verdicts(db, production_id: int, verdicts: list[dict], actor) -> dict:
    """Apply parsed verdicts. Per-verdict validation failures skip-and-count,
    never abort; the caller owns the transaction."""
    events = {e.id: e for e in (await db.execute(
        select(OntologyEvent).where(OntologyEvent.production_id == production_id)
    )).scalars().all()}

    rows = (await db.execute(
        select(AuditLog.resource_id).where(
            AuditLog.production_id == production_id,
            AuditLog.action == "event_edited"))).all()
    human_edited = {int(r[0]) for r in rows if r[0] and str(r[0]).isdigit()}

    part_rows = (await db.execute(
        select(EventParticipant.event_id, EventParticipant.entity_id).where(
            EventParticipant.event_id.in_(list(events.keys()) or [0])))).all()
    parts_by_event: dict[int, set] = {}
    for ev_id, ent_id in part_rows:
        parts_by_event.setdefault(ev_id, set()).add(ent_id)

    summary = {"merged": 0, "deleted": 0, "edited": 0, "skipped": 0,
               "skip_reasons": []}

    def skip(msg: str) -> None:
        summary["skipped"] += 1
        if len(summary["skip_reasons"]) < 50:
            summary["skip_reasons"].append(msg)

    def details_for(v: dict) -> dict:
        return {"actor": "ai_timeline_review", "model": REVIEW_MODEL,
                "reason": v.get("reason"), "confidence": v.get("confidence")}

    for v in verdicts:
        kind = v.get("kind")
        conf = v.get("confidence")
        if not isinstance(conf, (int, float)) or conf < REVIEW_MIN_CONFIDENCE:
            skip(f"{kind}: confidence below threshold")
            continue

        if kind == "delete":
            ev = events.get(v.get("event_id"))
            if ev is None:
                skip(f"delete: unknown event {v.get('event_id')}")
                continue
            if ev.id in human_edited:
                skip(f"delete: event {ev.id} was human-edited")
                continue
            await log_action(db, actor, "event_deleted_by_review", "ontology_event",
                            str(ev.id), production_id=production_id,
                            details={**details_for(v), "snapshot": _snapshot(ev)})
            await db.delete(ev)
            events.pop(ev.id, None)
            summary["deleted"] += 1

        elif kind == "merge":
            ids = v.get("event_ids") or []
            keep_id = v.get("keep_id")
            if len(ids) < 2 or keep_id not in ids:
                skip(f"merge: bad group {ids} keep={keep_id}")
                continue
            if any(i not in events for i in ids):
                skip(f"merge: unknown event in group {ids}")
                continue
            absorbed = [i for i in ids if i != keep_id]
            if any(i in human_edited for i in absorbed):
                skip(f"merge: group {ids} contains human-edited event")
                continue
            keeper = events[keep_id]
            ok, new_date = _parse_iso_date(v.get("date"))
            if not ok:
                skip(f"merge: bad date {v.get('date')!r}")
                continue
            keeper_parts = parts_by_event.setdefault(keep_id, set())
            for aid in absorbed:
                for ent_id in parts_by_event.get(aid, set()):
                    if ent_id not in keeper_parts:
                        db.add(EventParticipant(event_id=keep_id, entity_id=ent_id))
                        keeper_parts.add(ent_id)
            # Keeper corrections — skipped when a human already edited it.
            if keep_id not in human_edited:
                if v.get("description"):
                    keeper.description = v["description"]
                if new_date is not None and v.get("precision"):
                    keeper.event_date = new_date
                    keeper.date_precision = v["precision"]
            await log_action(db, actor, "event_merged_by_review", "ontology_event",
                            str(keep_id), production_id=production_id,
                            details={**details_for(v), "absorbed": absorbed})
            for aid in absorbed:
                ev = events.pop(aid)
                await log_action(db, actor, "event_deleted_by_review", "ontology_event",
                                str(aid), production_id=production_id,
                                details={**details_for(v), "merged_into": keep_id,
                                         "snapshot": _snapshot(ev)})
                await db.delete(ev)
            summary["merged"] += 1

        elif kind == "edit":
            ev = events.get(v.get("event_id"))
            if ev is None:
                skip(f"edit: unknown event {v.get('event_id')}")
                continue
            if ev.id in human_edited:
                skip(f"edit: event {ev.id} was human-edited")
                continue
            ok, new_date = _parse_iso_date(v.get("date"))
            if not ok:
                skip(f"edit: bad date {v.get('date')!r}")
                continue
            new_type = v.get("event_type")
            if new_type is not None and new_type not in EVENT_TYPES:
                skip(f"edit: bad event_type {new_type!r}")
                continue
            before = _snapshot(ev)
            if new_date is not None and v.get("precision"):
                ev.event_date = new_date
                ev.date_precision = v["precision"]
            if new_type is not None:
                ev.event_type = new_type
            if v.get("description"):
                ev.description = v["description"]
            await log_action(db, actor, "event_edited_by_review", "ontology_event",
                            str(ev.id), production_id=production_id,
                            details={**details_for(v), "before": before,
                                     "after": _snapshot(ev)})
            summary["edited"] += 1

        else:
            skip(f"unknown verdict kind {kind!r}")

    return summary
