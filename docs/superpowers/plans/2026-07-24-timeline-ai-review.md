# Timeline AI Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A whole-timeline Claude Opus 5 review pass per production that merges cross-document duplicate events, deletes irrelevant entries, and fixes extraction errors — auto-applied with full audit snapshots.

**Architecture:** New service `timeline_review.py` (serialize timeline → one structured-output model call → apply verdicts with guardrails). Wired in as a new ambient-pipeline stage `timeline_review` between `entities` and `brief`, plus a manager-gated trigger endpoint and status endpoint. Minimal frontend: an "AI review" button on the timeline bar with polling.

**Tech Stack:** FastAPI + SQLAlchemy async (existing), `anthropic` Python SDK ≥0.116 (`claude-opus-5`, adaptive thinking, streaming, `output_config.format` structured outputs), React frontend, pytest fake-session tests.

**Spec:** `docs/superpowers/specs/2026-07-24-timeline-review-design.md`

## Global Constraints

- **No migrations.** Everything uses existing tables (`ontology_events`, `event_participants`, `audit_logs`, `productions.ai_pipeline_status`).
- **Model:** `REVIEW_MODEL = "claude-opus-5"` — Claude Opus 5, released 2026-07-24, user-confirmed; verified against live model docs ($5/$25 per MTok, 1M context, adaptive thinking on by default, same request shape as Opus 4.8). The exact string is `claude-opus-5` — do not "correct" it to a 4.x id or add a date suffix. Never Fable/Sonnet/Haiku here.
- **Lazy `anthropic` import** inside functions only — CI runs alembic under minimal deps; module import must succeed without the SDK (mirror `entity_extraction.py`).
- **Confidence gate:** verdicts with `confidence < 0.8` are skipped and counted, never applied.
- **Human-edit guardrail:** event ids present in `audit_logs` rows with `action == "event_edited"` are never deleted, merged away, or edited; they may be a merge *keeper* but keeper corrections are skipped for them.
- **No partial application:** a truncated (`stop_reason == "max_tokens"`) or unparseable response fails the run; nothing is applied.
- **Refusal → Opus 4.8 fallback (user decision):** the review call opts into server-side fallbacks (beta header `server-side-fallback-2026-07-01`, `fallbacks=[{"model": "claude-opus-4-8"}]`), so an Opus 5 safety-classifier decline re-runs automatically on Opus 4.8 and those verdicts are used; the serving model is recorded in the run audit row. A final `stop_reason == "refusal"` (whole chain refused) fails the run with nothing applied.
- Backend tests: fake-session style (`tests/fakes.py`), run with `python -m pytest` from `backend/`. Frontend: `npm run build` must pass; lint is red on main (known baseline — don't try to fix unrelated lint).
- Work happens on branch `feat/timeline-ai-review` in worktree `F:/Users/WCEdm/Documents/Developer/descubre/.claude/worktrees/timeline-t4-look`.

---

### Task 1: Serialization, prompt, schema, and response parsing

**Files:**
- Create: `backend/app/services/timeline_review.py`
- Create: `backend/tests/test_timeline_review_service.py`
- Modify: `backend/requirements.txt` (line 9: `anthropic>=0.52` → `anthropic>=0.116`)

**Interfaces:**
- Produces: `serialize_timeline(events, bates, participants) -> str`, `build_review_user_content(serialized: str, event_count: int) -> str`, `REVIEW_SYSTEM_PROMPT: str`, `REVIEW_SCHEMA: dict`, `parse_review_response(raw: str) -> list[dict]`, `ReviewError(Exception)`, `REVIEW_MODEL = "claude-opus-4-8"`, `REVIEW_MIN_CONFIDENCE = 0.8`. (`events`: list of `OntologyEvent`; `bates`: `dict[int, str | None]` by event id; `participants`: `dict[int, list[str]]` by event id.)

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_timeline_review_service.py
"""Fake-session tests for the timeline AI review service."""

import json
import uuid
from datetime import date

import pytest

from app.models import OntologyEvent
from app.services import timeline_review as tr


def _event(eid, d=None, precision="unknown", etype="meeting", desc=None,
           quote=None, significance=3):
    ev = OntologyEvent(production_id=1, event_type=etype,
                       description=desc or f"Event {eid}",
                       event_date=d, date_precision=precision,
                       document_id=uuid.uuid4(), significance=significance,
                       date_source_text=quote)
    ev.id = eid
    return ev


def test_serialize_orders_chronologically_undated_last():
    evs = [_event(3), _event(1, d=date(2020, 5, 1), precision="day"),
           _event(2, d=date(2019, 1, 1), precision="year")]
    out = tr.serialize_timeline(evs, bates={}, participants={})
    ids = [json.loads(line)["id"] for line in out.splitlines()]
    assert ids == [2, 1, 3]


def test_serialize_line_shape():
    ev = _event(7, d=date(2020, 5, 1), precision="day", etype="payment",
                desc="Wire sent", quote="wired on May 1, 2020")
    out = tr.serialize_timeline([ev], bates={7: "ABC-0042"},
                                participants={7: ["Jorge Rivera"]})
    row = json.loads(out)
    assert row == {"id": 7, "date": "2020-05-01", "precision": "day",
                   "type": "payment", "desc": "Wire sent",
                   "quote": "wired on May 1, 2020", "bates": "ABC-0042",
                   "who": ["Jorge Rivera"]}


def test_user_content_mentions_count_and_embeds_events():
    ev = _event(1, d=date(2020, 1, 2), precision="day")
    serialized = tr.serialize_timeline([ev], {}, {})
    content = tr.build_review_user_content(serialized, 1)
    assert serialized in content
    assert "1 event" in content


def test_parse_valid_response():
    raw = json.dumps({"verdicts": [
        {"kind": "delete", "event_id": 4, "event_ids": None, "keep_id": None,
         "date": None, "precision": None, "event_type": None,
         "description": None, "reason": "court reporter logistics",
         "confidence": 0.95},
    ]})
    verdicts = tr.parse_review_response(raw)
    assert len(verdicts) == 1 and verdicts[0]["kind"] == "delete"


def test_parse_rejects_malformed_json():
    with pytest.raises(tr.ReviewError):
        tr.parse_review_response("{not json")


def test_parse_rejects_missing_verdicts_key():
    with pytest.raises(tr.ReviewError):
        tr.parse_review_response(json.dumps({"nope": []}))


def test_schema_is_strict():
    # Structured-output constraints: every object closed, everything required,
    # no numeric range constraints (unsupported by the API).
    s = json.dumps(tr.REVIEW_SCHEMA)
    assert "minimum" not in s and "maximum" not in s
    item = tr.REVIEW_SCHEMA["properties"]["verdicts"]["items"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == set(item["properties"].keys())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_timeline_review_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.timeline_review'`

- [ ] **Step 3: Write the implementation**

```python
# backend/app/services/timeline_review.py
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
```

(`EVENT_TYPES` import is used by Task 2's `apply_verdicts`; keeping it in the module header now avoids touching the import block twice.)

Also edit `backend/requirements.txt` line 9: change `anthropic>=0.52` to `anthropic>=0.116`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_timeline_review_service.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/timeline_review.py backend/tests/test_timeline_review_service.py backend/requirements.txt
git commit -m "feat(timeline-review): serialization, prompt, schema, parse"
```

---

### Task 2: apply_verdicts — merge/delete/edit with guardrails

**Files:**
- Modify: `backend/app/services/timeline_review.py` (append)
- Modify: `backend/tests/test_timeline_review_service.py` (append)

**Interfaces:**
- Consumes: `REVIEW_MIN_CONFIDENCE`, `REVIEW_MODEL` from Task 1; `log_action(db, user, action, resource_type, resource_id, production_id, details)` from `app.services.audit`.
- Produces: `apply_verdicts(db, production_id: int, verdicts: list[dict], actor) -> dict` returning `{"merged": int, "deleted": int, "edited": int, "skipped": int, "skip_reasons": list[str]}`. Audit actions written: `event_merged_by_review`, `event_deleted_by_review`, `event_edited_by_review` (all with `details["actor"] == "ai_timeline_review"`).

- [ ] **Step 1: Write the failing tests** (append to `backend/tests/test_timeline_review_service.py`)

```python
import asyncio

from app.models import AuditLog, EventParticipant
from tests.fakes import FakeResult, FakeSession, FakeUser


def _dbase(events, participant_rows=(), edited_ids=()):
    """FakeSession serving the three loads apply_verdicts performs."""
    return FakeSession(responders=[
        ("FROM audit_logs", FakeResult(rows=[(str(i),) for i in edited_ids])),
        ("FROM event_participants", FakeResult(rows=list(participant_rows))),
        ("FROM ontology_events", FakeResult(items=list(events))),
    ])


def _verdict(**kw):
    base = {"kind": None, "event_id": None, "event_ids": None, "keep_id": None,
            "date": None, "precision": None, "event_type": None,
            "description": None, "reason": "r", "confidence": 0.95}
    base.update(kw)
    return base


def _run(db, verdicts):
    return asyncio.run(tr.apply_verdicts(db, 1, verdicts, FakeUser()))


def test_delete_applies_with_snapshot_audit():
    ev = _event(4)
    db = _dbase([ev])
    summary = _run(db, [_verdict(kind="delete", event_id=4)])
    assert summary["deleted"] == 1 and db.deleted == [ev]
    audits = [a for a in db.added if isinstance(a, AuditLog)]
    assert audits[0].action == "event_deleted_by_review"
    assert audits[0].details["snapshot"]["description"] == "Event 4"
    assert audits[0].details["actor"] == "ai_timeline_review"


def test_confidence_gate_skips():
    ev = _event(4)
    db = _dbase([ev])
    summary = _run(db, [_verdict(kind="delete", event_id=4, confidence=0.5)])
    assert summary["deleted"] == 0 and summary["skipped"] == 1
    assert db.deleted == []


def test_human_edited_event_never_deleted():
    ev = _event(4)
    db = _dbase([ev], edited_ids=[4])
    summary = _run(db, [_verdict(kind="delete", event_id=4)])
    assert summary["deleted"] == 0 and summary["skipped"] == 1 and db.deleted == []


def test_merge_unions_participants_and_deletes_absorbed():
    keeper, dupe = _event(1, d=date(2020, 1, 2), precision="day"), _event(2)
    ent_a, ent_b = uuid.uuid4(), uuid.uuid4()
    # keeper already has ent_a; dupe has ent_a (overlap) and ent_b (new)
    db = _dbase([keeper, dupe],
                participant_rows=[(1, ent_a), (2, ent_a), (2, ent_b)])
    summary = _run(db, [_verdict(kind="merge", event_ids=[1, 2], keep_id=1,
                                 description="Board approved the wire")])
    assert summary["merged"] == 1
    assert db.deleted == [dupe]
    new_parts = [p for p in db.added if isinstance(p, EventParticipant)]
    assert [(p.event_id, p.entity_id) for p in new_parts] == [(1, ent_b)]
    assert keeper.description == "Board approved the wire"
    actions = [a.action for a in db.added if isinstance(a, AuditLog)]
    assert "event_merged_by_review" in actions and "event_deleted_by_review" in actions


def test_merge_skips_keeper_corrections_when_keeper_human_edited():
    keeper, dupe = _event(1, desc="Human wording"), _event(2)
    db = _dbase([keeper, dupe], edited_ids=[1])
    summary = _run(db, [_verdict(kind="merge", event_ids=[1, 2], keep_id=1,
                                 description="AI wording")])
    assert summary["merged"] == 1 and db.deleted == [dupe]
    assert keeper.description == "Human wording"  # correction skipped


def test_merge_rejected_when_absorbed_event_human_edited():
    keeper, dupe = _event(1), _event(2)
    db = _dbase([keeper, dupe], edited_ids=[2])
    summary = _run(db, [_verdict(kind="merge", event_ids=[1, 2], keep_id=1)])
    assert summary["merged"] == 0 and summary["skipped"] == 1 and db.deleted == []


def test_edit_applies_date_and_type():
    ev = _event(5, d=date(2020, 1, 1), precision="year", etype="meeting")
    db = _dbase([ev])
    summary = _run(db, [_verdict(kind="edit", event_id=5, date="2020-03-15",
                                 precision="day", event_type="payment")])
    assert summary["edited"] == 1
    assert ev.event_date == date(2020, 3, 15) and ev.date_precision == "day"
    assert ev.event_type == "payment"
    audits = [a for a in db.added if isinstance(a, AuditLog)]
    assert audits[0].action == "event_edited_by_review"
    assert audits[0].details["before"]["date_precision"] == "year"


def test_edit_rejects_invalid_event_type():
    ev = _event(5)
    db = _dbase([ev])
    summary = _run(db, [_verdict(kind="edit", event_id=5, event_type="nonsense")])
    assert summary["edited"] == 0 and summary["skipped"] == 1


def test_unknown_and_foreign_ids_skip_not_crash():
    ev = _event(1)
    db = _dbase([ev])
    summary = _run(db, [
        _verdict(kind="delete", event_id=999),
        _verdict(kind="merge", event_ids=[1, 999], keep_id=1),
        _verdict(kind="merge", event_ids=[1], keep_id=1),        # group too small
        _verdict(kind="merge", event_ids=[1, 999], keep_id=999),  # keeper unknown
        _verdict(kind="edit", event_id=1, date="not-a-date", precision="day"),
        _verdict(kind="frobnicate", event_id=1),
    ])
    assert summary["skipped"] == 6
    assert summary["merged"] == summary["deleted"] == summary["edited"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_timeline_review_service.py -v`
Expected: new tests FAIL with `AttributeError: ... has no attribute 'apply_verdicts'`; Task 1 tests still pass.

- [ ] **Step 3: Write the implementation** (append to `backend/app/services/timeline_review.py`)

```python
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
                        db.add(EventParticipant(event_id=keep_id, entity_id=ent_id,
                                                production_id=production_id))
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
```

**Note:** check `EventParticipant`'s columns before finishing — if the model has no `production_id` column, construct it without that kwarg (`EventParticipant(event_id=..., entity_id=...)`) and adjust the test's expectation accordingly. `EVENT_TYPES` must exist in `entity_extraction.py` (it is already imported by `routers/entities.py`, so it does).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_timeline_review_service.py -v`
Expected: all pass (16 total)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/timeline_review.py backend/tests/test_timeline_review_service.py
git commit -m "feat(timeline-review): apply_verdicts with confidence gate and human-edit guardrail"
```

---

### Task 3: run_timeline_review orchestration

**Files:**
- Modify: `backend/app/services/timeline_review.py` (append)
- Modify: `backend/tests/test_timeline_review_service.py` (append)

**Interfaces:**
- Consumes: everything from Tasks 1–2.
- Produces: `run_timeline_review(db, production_id: int, actor) -> dict` (summary dict from `apply_verdicts` plus `"status": "done" | "empty"`); module-level `_call_review_model(user_content: str) -> tuple[str, str, dict]` returning `(raw_text, stop_reason, usage)` — the seam tests monkeypatch. Writes a `timeline_review_completed` audit row on success.

- [ ] **Step 1: Write the failing tests** (append to `backend/tests/test_timeline_review_service.py`)

```python
def _run_db(events, bates_rows=None, name_rows=(), edited_ids=()):
    """FakeSession for run_timeline_review: event+bates load, participant
    names load, then apply_verdicts' three loads (order matters: first
    matching substring wins, so the joined load precedes the bare one)."""
    bates_rows = bates_rows if bates_rows is not None else [(e, None) for e in events]
    return FakeSession(responders=[
        ("JOIN documents", FakeResult(rows=bates_rows)),
        ("JOIN entities", FakeResult(rows=list(name_rows))),
        ("FROM audit_logs", FakeResult(rows=[(str(i),) for i in edited_ids])),
        ("FROM event_participants", FakeResult(rows=[])),
        ("FROM ontology_events", FakeResult(items=list(events))),
    ])


def test_run_empty_timeline_skips_model(monkeypatch):
    called = []
    async def fake_call(content):
        called.append(content)
        return "{}", "end_turn", {}
    monkeypatch.setattr(tr, "_call_review_model", fake_call)
    db = _run_db([])
    out = asyncio.run(tr.run_timeline_review(db, 1, FakeUser()))
    assert out["status"] == "empty" and called == []


def test_run_happy_path_applies_and_audits(monkeypatch):
    ev = _event(4)
    raw = json.dumps({"verdicts": [_verdict(kind="delete", event_id=4)]})
    async def fake_call(content):
        assert '"id":4' in content
        return raw, "end_turn", {"input_tokens": 10, "output_tokens": 5}
    monkeypatch.setattr(tr, "_call_review_model", fake_call)
    db = _run_db([ev])
    out = asyncio.run(tr.run_timeline_review(db, 1, FakeUser()))
    assert out["status"] == "done" and out["deleted"] == 1
    run_rows = [a for a in db.added if isinstance(a, AuditLog)
                and a.action == "timeline_review_completed"]
    assert len(run_rows) == 1
    assert run_rows[0].details["model"] == tr.REVIEW_MODEL
    assert run_rows[0].details["usage"] == {"input_tokens": 10, "output_tokens": 5}


def test_run_truncated_response_fails_applies_nothing(monkeypatch):
    ev = _event(4)
    async def fake_call(content):
        return "{\"verdicts\": [", "max_tokens", {}
    monkeypatch.setattr(tr, "_call_review_model", fake_call)
    db = _run_db([ev])
    with pytest.raises(tr.ReviewError):
        asyncio.run(tr.run_timeline_review(db, 1, FakeUser()))
    assert db.deleted == []


def test_run_refusal_fails_applies_nothing(monkeypatch):
    # The call carries a server-side fallback to Opus 4.8, so a final
    # stop_reason of "refusal" means the WHOLE chain declined; the guard
    # must hold: fail the run, apply nothing.
    ev = _event(4)
    async def fake_call(content):
        return "", "refusal", {}
    monkeypatch.setattr(tr, "_call_review_model", fake_call)
    db = _run_db([ev])
    with pytest.raises(tr.ReviewError):
        asyncio.run(tr.run_timeline_review(db, 1, FakeUser()))
    assert db.deleted == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_timeline_review_service.py -v`
Expected: new tests FAIL with `AttributeError: ... no attribute '_call_review_model'` / `'run_timeline_review'`

- [ ] **Step 3: Write the implementation** (append to `backend/app/services/timeline_review.py`)

```python
# ── Model call + orchestration ──

async def _call_review_model(user_content: str) -> tuple[str, str, dict]:
    """One structured-output review call. Returns (raw_json, stop_reason,
    usage). Streaming because 64k max_tokens exceeds non-streaming SDK
    limits. Raises ReviewError when attempts are exhausted."""
    if not settings.anthropic_api_key:
        raise ReviewError("No Anthropic API key configured")
    import anthropic  # lazy: keep the SDK off the startup/alembic path

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    retryable = _retryable_errors()
    last_err: Exception | None = None
    for attempt in range(_REVIEW_MAX_ATTEMPTS):
        try:
            # Refusal fallback (user decision): an Opus 5 safety-classifier
            # decline re-runs server-side on Opus 4.8 in the same call.
            # `fallbacks` + its beta are newer than the SDK's typed surface,
            # so they ride extra_body/extra_headers — harmless once typed.
            async with client.messages.stream(
                model=REVIEW_MODEL,
                max_tokens=64000,
                thinking={"type": "adaptive"},
                system=REVIEW_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
                output_config={"format": {"type": "json_schema",
                                          "schema": REVIEW_SCHEMA}},
                extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
                extra_body={"fallbacks": [{"model": "claude-opus-4-8"}]},
            ) as stream:
                response = await stream.get_final_message()
            raw = next((b.text for b in response.content if b.type == "text"), "")
            usage = {"input_tokens": response.usage.input_tokens,
                     "output_tokens": response.usage.output_tokens,
                     "served_by": response.model}
            return raw, response.stop_reason, usage
        except retryable as e:
            last_err = e
            status = getattr(e, "status_code", None)
            if status is not None and status not in (408, 429) and status < 500:
                raise ReviewError(f"Review call failed with status {status}") from e
            logger.warning("Timeline review attempt %d/%d failed: %s",
                           attempt + 1, _REVIEW_MAX_ATTEMPTS, e)
            if attempt < _REVIEW_MAX_ATTEMPTS - 1:
                await _asyncio.sleep(2 * (attempt + 1))
    raise ReviewError(f"Review call failed after {_REVIEW_MAX_ATTEMPTS} attempts: {last_err}")


async def run_timeline_review(db, production_id: int, actor) -> dict:
    """Load the production's timeline, review it in one model call, apply
    the verdicts, and audit the run. Caller owns the transaction; raises
    ReviewError on model failure/truncation (nothing applied)."""
    rows = (await db.execute(
        select(OntologyEvent, Document.bates_begin)
        .join(Document, OntologyEvent.document_id == Document.id)
        .where(OntologyEvent.production_id == production_id))).all()
    events = [r[0] for r in rows]
    if not events:
        return {"status": "empty", "merged": 0, "deleted": 0, "edited": 0,
                "skipped": 0, "skip_reasons": []}
    bates = {r[0].id: r[1] for r in rows}

    name_rows = (await db.execute(
        select(EventParticipant.event_id, Entity.canonical_name)
        .join(Entity, EventParticipant.entity_id == Entity.id)
        .where(EventParticipant.event_id.in_([e.id for e in events])))).all()
    participants: dict[int, list[str]] = {}
    for ev_id, name in name_rows:
        participants.setdefault(ev_id, []).append(name)

    serialized = serialize_timeline(events, bates, participants)
    raw, stop_reason, usage = await _call_review_model(
        build_review_user_content(serialized, len(events)))
    if stop_reason == "max_tokens":
        raise ReviewError("Review response truncated (max_tokens) — nothing applied")
    if stop_reason == "refusal":
        # Whole fallback chain (Opus 5 → Opus 4.8) declined — nothing applied.
        raise ReviewError("Review declined by model safety classifiers — nothing applied")

    verdicts = parse_review_response(raw)
    summary = await apply_verdicts(db, production_id, verdicts, actor)
    summary["status"] = "done"
    await log_action(db, actor, "timeline_review_completed", "production",
                     str(production_id), production_id=production_id,
                     details={"model": REVIEW_MODEL, "usage": usage,
                              "event_count": len(events),
                              "verdict_count": len(verdicts),
                              **{k: summary[k] for k in
                                 ("merged", "deleted", "edited", "skipped")}})
    return summary
```

**Note:** verify the compiled SQL of the two loads actually contains the substrings the fake responders match on (`JOIN documents`, `JOIN entities`) — print `str(select(...))` in a scratch if unsure, and adjust the responder substrings to match reality rather than changing the query.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_timeline_review_service.py -v`
Expected: all pass (20 total)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/timeline_review.py backend/tests/test_timeline_review_service.py
git commit -m "feat(timeline-review): orchestration — one Opus call, apply, audit run row"
```

---

### Task 4: Pipeline stage + rebuild integration

**Files:**
- Modify: `backend/app/services/pipeline.py` (STAGES tuple, stage runner, `_STAGE_RUNNERS`, new public `run_timeline_review_stage`)
- Modify: `backend/app/routers/ingest.py` (rebuild path: clear the `timeline_review` stage key too)
- Create: `backend/tests/test_timeline_review_stage.py`

**Interfaces:**
- Consumes: `run_timeline_review(db, production_id, actor)` from Task 3; `resolve_audit_actor(db, production)` from `app.services.audit`; existing `_set_stage`, `stages_to_run`, `_STAGE_RUNNERS`, `async_session` in `pipeline.py`.
- Produces: `STAGES == ("clustering", "summaries", "entities", "timeline_review", "brief")`; `run_timeline_review_stage(production_id: int) -> None` (sets `timeline_review` stage running/done/failed around the run — used by Task 5's endpoint).

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_timeline_review_stage.py
"""Pipeline wiring for the timeline_review stage."""

import asyncio

import pytest

import app.services.pipeline as pl


def test_stage_registered_between_entities_and_brief():
    assert pl.STAGES == ("clustering", "summaries", "entities",
                         "timeline_review", "brief")
    assert "timeline_review" in pl._STAGE_RUNNERS


def test_stages_to_run_includes_new_stage_for_existing_productions():
    # A production that ran before this feature has no timeline_review key.
    status = {"clustering": "done", "summaries": "done", "entities": "done",
              "brief": "done"}
    assert pl.stages_to_run(status, force=False) == ["timeline_review"]


def test_standalone_stage_runner_marks_failed_without_raising(monkeypatch):
    calls = []

    async def boom(production_id):
        raise RuntimeError("model exploded")

    async def spy_set_stage(production_id, stage, state, error=None):
        calls.append((stage, state, error))

    monkeypatch.setattr(pl, "_run_timeline_review", boom)
    monkeypatch.setattr(pl, "_set_stage", spy_set_stage)
    asyncio.run(pl.run_timeline_review_stage(7))  # must not raise
    assert calls[0] == ("timeline_review", "running", None)
    assert calls[1][0:2] == ("timeline_review", "failed")
    assert "model exploded" in calls[1][2]


def test_standalone_stage_runner_marks_done(monkeypatch):
    calls = []

    async def ok(production_id):
        pass

    async def spy_set_stage(production_id, stage, state, error=None):
        calls.append((stage, state))

    monkeypatch.setattr(pl, "_run_timeline_review", ok)
    monkeypatch.setattr(pl, "_set_stage", spy_set_stage)
    asyncio.run(pl.run_timeline_review_stage(7))
    assert calls == [("timeline_review", "running"), ("timeline_review", "done")]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_timeline_review_stage.py -v`
Expected: FAIL — STAGES tuple mismatch / missing attributes

- [ ] **Step 3: Implement**

In `backend/app/services/pipeline.py`:

1. Change line 24 to:

```python
STAGES = ("clustering", "summaries", "entities", "timeline_review", "brief")
```

2. Add the stage runner next to `_run_entities` / `_run_brief`:

```python
async def _run_timeline_review(production_id: int) -> None:
    """AI review of the extracted timeline (spec:
    docs/superpowers/specs/2026-07-24-timeline-review-design.md). Runs after
    entities so the review sees the full extraction, before brief so the
    brief regenerates from the cleaned chronology."""
    from app.services.audit import resolve_audit_actor
    from app.services.timeline_review import run_timeline_review
    async with async_session() as db:
        prod = await db.get(Production, production_id)
        if prod is None:
            return
        actor = await resolve_audit_actor(db, prod)
        if actor is None:
            logger.warning("Timeline review skipped for production %s: "
                           "no owner to attribute audit rows to", production_id)
            return
        await run_timeline_review(db, production_id, actor)
        await db.commit()
```

(If `Production` isn't already imported at pipeline.py's top, import it inside the function like the other lazy imports there.)

3. Register it in `_STAGE_RUNNERS` (currently ends with `"entities": _run_entities, "brief": _run_brief`):

```python
_STAGE_RUNNERS = {
    "clustering": _run_clustering,
    "summaries": _run_summaries,
    "entities": _run_entities,
    "timeline_review": _run_timeline_review,
    "brief": _run_brief,
}
```

(Match the actual existing dict contents — add the one key in stage order.)

4. Add the standalone runner (used by the Task 5 endpoint) at module bottom:

```python
async def run_timeline_review_stage(production_id: int) -> None:
    """Standalone timeline-review run for the manager endpoint — same
    stage-status writes as the pipeline loop, so one status surface serves
    both triggers. Never raises."""
    await _set_stage(production_id, "timeline_review", "running")
    try:
        await _run_timeline_review(production_id)
    except Exception as exc:
        logger.exception("Timeline review failed for production %s", production_id)
        await _set_stage(production_id, "timeline_review", "failed",
                         error=str(exc)[:300])
    else:
        await _set_stage(production_id, "timeline_review", "done")
```

In `backend/app/routers/ingest.py`, in `trigger_entity_extraction`'s rebuild block, change the stage-clearing loop:

```python
            for stage in ("entities", "timeline_review", "brief"):
                status.pop(stage, None)
                errors.pop(stage, None)
```

(Also update the comment above it: entities + review + brief re-run; clustering/summaries stay done.)

- [ ] **Step 4: Run tests to verify they pass, plus the existing pipeline/ingest tests**

Run: `cd backend && python -m pytest tests/test_timeline_review_stage.py -v && python -m pytest -q -k "pipeline or ingest or extract"`
Expected: new tests pass; no regressions (some existing tests may pin `STAGES` — update any that enumerate the old tuple to include `timeline_review`, keeping their intent).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/pipeline.py backend/app/routers/ingest.py backend/tests/test_timeline_review_stage.py
git commit -m "feat(timeline-review): pipeline stage between entities and brief; rebuild clears it"
```

---

### Task 5: Trigger + status endpoints

**Files:**
- Modify: `backend/app/routers/entities.py` (two new routes near the timeline routes)
- Create: `backend/tests/test_timeline_review_endpoint.py`

**Interfaces:**
- Consumes: `run_timeline_review_stage(production_id)` from Task 4; existing `get_accessible_production_ids`, `log_action` already imported in `entities.py`; `ROLE_RANK` / `get_user_role_for_production` from `app.dependencies` (import locally inside the handler, mirroring `ingest.py`).
- Produces: `POST /api/productions/{production_id}/timeline-review` → `{"status": "started"}` (404 out-of-scope, 403 below manager); `GET /api/productions/{production_id}/timeline-review/status` → `{"state": str | None, "error": str | None, "summary": dict | None}`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_timeline_review_endpoint.py
"""Fake-session tests for the timeline-review trigger + status endpoints."""

import asyncio

import pytest
from fastapi import HTTPException

import app.routers.entities as er
from app.models import AuditLog, Production
from tests.fakes import FakeResult, FakeSession, FakeUser


class FakeBackgroundTasks:
    def __init__(self):
        self.tasks = []

    def add_task(self, fn, *args):
        self.tasks.append((fn, args))


def _patch(monkeypatch, accessible=(1,), role="manager"):
    async def fake_accessible(db, user):
        return list(accessible)
    monkeypatch.setattr(er, "get_accessible_production_ids", fake_accessible)
    import app.dependencies as deps
    async def fake_role(db, user, pid):
        return role
    monkeypatch.setattr(deps, "get_user_role_for_production", fake_role)


def test_trigger_denies_out_of_scope(monkeypatch):
    _patch(monkeypatch, accessible=(2,))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.trigger_timeline_review(
            production_id=1, background_tasks=FakeBackgroundTasks(),
            db=FakeSession(), user=FakeUser()))
    assert exc.value.status_code == 404


def test_trigger_denies_below_manager(monkeypatch):
    _patch(monkeypatch, role="member")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.trigger_timeline_review(
            production_id=1, background_tasks=FakeBackgroundTasks(),
            db=FakeSession(), user=FakeUser()))
    assert exc.value.status_code == 403


def test_trigger_kicks_off_background_run(monkeypatch):
    _patch(monkeypatch)
    bg = FakeBackgroundTasks()
    db = FakeSession()
    out = asyncio.run(er.trigger_timeline_review(
        production_id=1, background_tasks=bg, db=db, user=FakeUser()))
    assert out == {"status": "started"}
    from app.services.pipeline import run_timeline_review_stage
    assert bg.tasks == [(run_timeline_review_stage, (1,))]
    audits = [a for a in db.added if isinstance(a, AuditLog)]
    assert audits and audits[0].action == "timeline_review_triggered"


def test_status_reports_stage_and_latest_summary(monkeypatch):
    _patch(monkeypatch)
    prod = Production(name="M")
    prod.id = 1
    prod.ai_pipeline_status = {"timeline_review": "failed",
                               "errors": {"timeline_review": "boom"}}
    row = AuditLog(user_id="u1", user_email="u1@thirulaw.com",
                   action="timeline_review_completed", resource_type="production",
                   resource_id="1", production_id=1,
                   details={"merged": 2, "deleted": 1, "edited": 0, "skipped": 3})
    db = FakeSession(get_objects={("Production", 1): prod},
                     responders=[("FROM audit_logs", FakeResult(items=[row]))])
    out = asyncio.run(er.get_timeline_review_status(
        production_id=1, db=db, user=FakeUser()))
    assert out["state"] == "failed" and out["error"] == "boom"
    assert out["summary"]["merged"] == 2


def test_status_denies_out_of_scope(monkeypatch):
    _patch(monkeypatch, accessible=(2,))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.get_timeline_review_status(
            production_id=1, db=FakeSession(), user=FakeUser()))
    assert exc.value.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_timeline_review_endpoint.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'trigger_timeline_review'`

- [ ] **Step 3: Implement** (add to `backend/app/routers/entities.py`, near the existing timeline routes; `BackgroundTasks` needs adding to the `fastapi` import if absent, and `Production` / `AuditLog` to the `app.models` import if absent)

```python
@router.post("/productions/{production_id}/timeline-review")
async def trigger_timeline_review(
    production_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Run the AI timeline review for this production in the background.
    Manager or admin only; 404 for productions outside the caller's scope.
    Same run as the automatic post-extraction pipeline stage — this is the
    on-demand/backfill trigger. Status via GET .../timeline-review/status."""
    import app.dependencies as deps
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=404, detail="Production not found")
    role = await deps.get_user_role_for_production(db, user, production_id)
    if deps.ROLE_RANK.get(role, 0) < deps.ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or admin role required")

    await log_action(db, user, "timeline_review_triggered", "production",
                     str(production_id), production_id=production_id)
    # Commit before handing off: the background run opens its own sessions.
    await db.commit()
    from app.services.pipeline import run_timeline_review_stage
    background_tasks.add_task(run_timeline_review_stage, production_id)
    return {"status": "started"}


@router.get("/productions/{production_id}/timeline-review/status")
async def get_timeline_review_status(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Stage state from ai_pipeline_status + the latest run summary from the
    audit log (the run row is the durable summary store — no new table)."""
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=404, detail="Production not found")
    prod = await db.get(Production, production_id)
    status = dict((prod.ai_pipeline_status if prod else None) or {})
    row = (await db.execute(
        select(AuditLog)
        .where(AuditLog.production_id == production_id,
               AuditLog.action == "timeline_review_completed")
        .order_by(AuditLog.created_at.desc())
        .limit(1))).scalars().first()
    return {"state": status.get("timeline_review"),
            "error": (status.get("errors") or {}).get("timeline_review"),
            "summary": row.details if row else None}
```

**Route-shadowing check:** `entities.py` already serves `/productions/{production_id}/timeline`, so the `/productions/{production_id}/...` namespace is proven reachable from this router — no shadowing risk for the two new literal-suffix paths.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_timeline_review_endpoint.py -v`
Expected: 5 passed. If `test_trigger_denies_below_manager` fails because the monkeypatch misses (handler imported `deps` locally), the patch target `app.dependencies.get_user_role_for_production` is correct for the `import app.dependencies as deps` form shown above — keep that import form.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/entities.py backend/tests/test_timeline_review_endpoint.py
git commit -m "feat(timeline-review): manager trigger + status endpoints"
```

---

### Task 6: Frontend — AI review action on the timeline bar

**Files:**
- Modify: `frontend/src/api/client.ts` (two functions, next to `triggerEntityExtraction` at ~line 879)
- Modify: `frontend/src/types.ts` (status type)
- Modify: `frontend/src/components/EntityTimelineView.tsx` (button, polling, reload)
- Modify: `frontend/src/styles/timeline.css` (button styling)

**Interfaces:**
- Consumes: Task 5's endpoints.
- Produces: `triggerTimelineReview(productionId: number)`, `getTimelineReviewStatus(productionId: number)` in `client.ts`; `TimelineReviewStatus` in `types.ts`.

- [ ] **Step 1: Add API client + types**

In `frontend/src/types.ts`:

```ts
export interface TimelineReviewStatus {
  state: 'running' | 'done' | 'failed' | null;
  error?: string | null;
  summary: { merged?: number; deleted?: number; edited?: number; skipped?: number } | null;
}
```

In `frontend/src/api/client.ts` (import `TimelineReviewStatus` in the existing types import; place next to `triggerEntityExtraction`):

```ts
export const triggerTimelineReview = (productionId: number) =>
  request<{ status: string }>(`/api/productions/${productionId}/timeline-review`, { method: 'POST' });

export const getTimelineReviewStatus = (productionId: number) =>
  request<TimelineReviewStatus>(`/api/productions/${productionId}/timeline-review/status`);
```

- [ ] **Step 2: Wire the button into EntityTimelineView**

Add to the component's imports: `triggerTimelineReview, getTimelineReviewStatus` from `../api/client`.

Add state (next to the existing per-event editing state):

```tsx
  // ── AI review ──
  const [reviewing, setReviewing] = useState(false);
  const [reviewMsg, setReviewMsg] = useState<string | null>(null);
```

Add handlers (next to the other handlers; `errText` already exists in the file):

```tsx
  const startReview = async () => {
    if (!window.confirm(
      'Run an AI review of this chronology? Cross-document duplicates will be '
      + 'merged, irrelevant entries removed, and clear date errors corrected. '
      + 'Every change is recorded in the audit log.')) return;
    setReviewMsg(null);
    try {
      await triggerTimelineReview(productionId);
      setReviewing(true);
    } catch (e) {
      setReviewMsg(errText(e));
    }
  };

  // Poll while the background review runs; on completion reload page 1 the
  // same way the retry path does, so merged/removed events disappear.
  useEffect(() => {
    if (!reviewing) return;
    const timer = setInterval(async () => {
      try {
        const s = await getTimelineReviewStatus(productionId);
        if (s.state === 'done') {
          setReviewing(false);
          const m = s.summary;
          setReviewMsg(m
            ? `Review complete — ${m.merged ?? 0} merged, ${m.deleted ?? 0} removed, ${m.edited ?? 0} corrected.`
            : 'Review complete.');
          setEvents([]);
          setPage(1);
          setSettledPage(0);
          setRetryTick(t => t + 1);
        } else if (s.state === 'failed') {
          setReviewing(false);
          setReviewMsg(s.error ? `Review failed: ${s.error}` : 'Review failed.');
        }
      } catch {
        // transient poll failure — keep polling
      }
    }, 10000);
    return () => clearInterval(timer);
  }, [reviewing, productionId]);
```

**Check before using:** the reload must match how the existing retry button resets the list — find `retryTick` usages in this file and mirror them exactly (if the retry path resets different state, copy that reset instead of the four lines above). The main fetch effect must depend on `retryTick` for this to re-fetch.

Add the button to the control bar, immediately after the `.chrono-seg` segmented control's closing tag in the bar row (manager gating is server-side, like the Extract-entities button — non-managers get a 403 message):

```tsx
          <button className="chrono-review" onClick={startReview} disabled={reviewing}
                  title="AI review: merge duplicate events, drop irrelevant entries, fix clear date errors (manager only)">
            {reviewing ? 'Reviewing…' : 'AI review'}
          </button>
          {reviewMsg && <span className="chrono-review-msg">{reviewMsg}</span>}
```

- [ ] **Step 3: Style it** (append near `.chrono-seg` rules in `frontend/src/styles/timeline.css`; keep the T4 palette)

```css
/* AI review action — bar-level, quiet; square like the seg control. */
.chrono-review {
  border: 1px solid rgba(44, 62, 107, 0.16);
  background: rgba(255, 255, 255, 0.6);
  border-radius: var(--radius-md);
  padding: 5px 13px;
  font-family: var(--font-sans);
  font-size: var(--text-xs);
  font-weight: var(--font-semibold);
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--color-neutral-500);
  cursor: pointer;
}

.chrono-review:hover:not(:disabled) { color: var(--color-ink); }
.chrono-review:disabled { opacity: 0.6; cursor: default; }

.chrono-review-msg {
  font-size: var(--text-xs);
  color: var(--color-neutral-500);
}
```

Also add `.chrono-review:focus-visible,` to the existing focus-visible selector list at the bottom of `timeline.css`.

- [ ] **Step 4: Build**

Run: `cd frontend && npm run build`
Expected: clean build (tsc + vite). Don't fix unrelated lint — it's red on main by baseline.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/client.ts frontend/src/types.ts frontend/src/components/EntityTimelineView.tsx frontend/src/styles/timeline.css
git commit -m "feat(timeline-review): AI review action on the chronology bar"
```

---

### Task 7: Full verification + PR

**Files:** none new.

- [ ] **Step 1: Full backend suite**

Run: `cd backend && python -m pytest -q`
Expected: everything green (baseline was 714 pass + 1 known allowed failure; the known failure is acceptable, new failures are not).

- [ ] **Step 2: Frontend build again from clean**

Run: `cd frontend && npm run build`
Expected: clean.

- [ ] **Step 3: Push and open PR**

```bash
git push -u origin feat/timeline-ai-review
gh pr create --title "feat: AI timeline review — merge duplicates, drop noise, fix errors (Opus 5)" --body "..."
```

PR body must cover: spec link, the Opus 4.8 whole-timeline design, auto-apply + audit trail, confidence gate + human-edit guardrail, pipeline stage + rebuild integration, endpoints, frontend button, test coverage, **no migrations**, and the post-deploy step: press "AI review" on each matter's timeline to clean existing prod data.

- [ ] **Step 4: Check CI and hand back for merge decision**
