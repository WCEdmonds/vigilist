# Timeline AI Review — Design

**Date:** 2026-07-24
**Status:** Approved by user (conversation), pending spec review
**Scope:** Backend service + endpoint + extraction hook. No migrations. Minimal frontend.

## Problem

Timeline events are extracted per-document by Haiku (`entity_extraction.py`,
`EXTRACTION_MODEL = "claude-haiku-4-5"`). Dedup exists only *within* a document
(slice merging in `merge_parsed` + belt-and-braces in `persist_extraction`), so
the same real-world event described in ten documents produces ten timeline
entries. Extraction also produces occasional errors (wrong dates, garbled
descriptions) and entries irrelevant to the dispute. The user wants an
overarching review pass by a smarter model that cleans all three up.

## Decisions (made with user)

1. **Disposition: auto-apply, log everything.** The reviewer merges duplicates,
   deletes irrelevant entries, and fixes clear errors directly. Every change is
   audit-logged with a snapshot; uncertain calls are left alone.
2. **Trigger: automatic after extraction**, plus a manager-gated endpoint so it
   can be run against existing prod data now and re-run on demand.
3. **Architecture: one whole-timeline pass.** A single Opus 4.8 call per
   production sees every event at once — that is what catches cross-document
   duplicates and chronology-inconsistent dates. Rejected alternatives:
   two-tier screen/adjudicate (extra plumbing, screen can't see cross-doc
   duplicates anyway, savings are pennies at current scale) and
   blocking/pairwise dedup (most code, loses whole-timeline perspective,
   worst at "irrelevant to the case" judgments).

## Components

### 1. `backend/app/services/timeline_review.py` (new)

- `REVIEW_MODEL = "claude-opus-4-8"` — $5/$25 per MTok, 1M context. Adaptive
  thinking (`thinking={"type": "adaptive"}`), streaming (the SDK requires it
  for large `max_tokens`; use `get_final_message()`), `max_tokens=64000`.
- `serialize_timeline(events) -> str`: compact JSON lines, chronological order
  (undated last), one line per event:
  `{"id", "date", "precision", "type", "desc", "quote", "bates", "who": [...]}`.
- `build_review_prompt(...)`: instructs the model to return verdicts for
  duplicates (same real-world event, not merely similar), errors (date/type/
  description contradicted by the quote or the surrounding chronology), and
  irrelevant entries (litigation-process machinery, extraction garbage,
  events with no bearing on the dispute). Explicitly: when uncertain, return
  no verdict — silence means keep.
- Structured output via `output_config={"format": {"type": "json_schema", ...}}`
  so the response is guaranteed parseable. Schema:

```json
{
  "verdicts": [
    {
      "kind": "merge",
      "event_ids": [1, 2, 3],
      "keep_id": 1,
      "description": "optional corrected description for keeper",
      "date": "optional YYYY-MM-DD", "precision": "day|month|year",
      "reason": "...", "confidence": 0.0
    },
    {
      "kind": "delete", "event_id": 4,
      "reason": "...", "confidence": 0.0
    },
    {
      "kind": "edit", "event_id": 5,
      "date": "YYYY-MM-DD or null", "precision": "...", "event_type": "...",
      "description": "...",
      "reason": "...", "confidence": 0.0
    }
  ]
}
```

  (Exact JSON-schema spelling in the plan; every verdict requires `reason` and
  `confidence`; unsupported constraint types avoided per structured-outputs
  limits — no `minimum`/`maximum` on confidence, validated in code instead.)

- `apply_verdicts(db, production_id, verdicts, actor) -> summary`:
  - **Confidence gate:** verdicts with `confidence < 0.8` are recorded in the
    run summary as skipped and not applied.
  - **Human-edit guardrail:** event ids that appear in audit rows with action
    `event_edited` (human path) are never deleted, merged away, or edited by
    the reviewer. They may still be the *keeper* of a merge — duplicates are
    absorbed onto them — but keeper corrections (description/date) are
    skipped for human-edited keepers.
  - **merge:** union `EventParticipant` rows onto the keeper (respecting the
    `uq_event_entity` constraint), apply optional description/date correction
    to the keeper, hard-delete the other events. Audit:
    `event_merged_by_review` on the keeper (details: absorbed ids + snapshots),
    one `event_deleted_by_review` per absorbed event with full snapshot.
  - **delete:** hard-delete with snapshot in `event_deleted_by_review` audit
    row (mirrors the human `event_deleted` shape + `reason`).
  - **edit:** apply date/precision/type/description changes; audit
    `event_edited_by_review` with before/after + `reason`.
  - Validation: unknown event ids, ids outside the production, keeper not in
    `event_ids`, malformed dates → that verdict is skipped and counted, never
    fatal to the run.
- `run_timeline_review(db, production_id, actor) -> summary`: load events →
  serialize → call model → parse → apply → audit a run-level row
  (`timeline_review_completed`, details: counts of merged/deleted/edited/
  skipped, model, token usage) → return summary.
- Retry/backoff on API errors reusing the `_retryable_errors()` pattern from
  `entity_extraction.py`. Timeline with zero events: no-op summary, no API call.

### 2. Endpoint (in `backend/app/routers/entities.py`)

- `POST /api/productions/{production_id}/timeline-review` — manager-gated
  (same gate as `extract-entities`), kicks off the review as a background job
  using the same job pattern as extraction; returns job status; frontend polls
  the same way the Extract-entities button does.
- Response/status includes the run summary when complete.

### 3. Extraction hook

At the successful end of the extraction/rebuild job (the single consolidated
rebuild path), the same background task runs `run_timeline_review` for that
production before marking the job done. A review failure does not fail the
extraction — it is logged and surfaced in the job status as
`review_failed`; extraction results stand.

### 4. Frontend (minimal)

The existing timeline UI needs no structural change. The Extract-entities flow
already refreshes; the review runs inside that job. For the standalone
endpoint, a small "Review timeline" action next to Extract entities (manager
role only, same polling UX) — reuse the existing button/polling component
pattern. No new views.

### 5. Running against current prod data

After deploy, the user (manager role) presses "Review timeline" per matter —
same self-serve pattern as the entity-extraction backfill (PR #56). No
one-off script needed.

## Error handling

- Model/API failure → job status `review_failed`, no partial application
  (verdicts apply only after a fully parsed response).
- Individual bad verdicts skip-and-count, never abort the run.
- All applications happen in one DB transaction per run; failure rolls back
  cleanly.
- `stop_reason == "max_tokens"` (truncated verdict list) → treat as failure,
  do not apply a partial parse.

## Cost

~20k–160k input tokens and ~5–20k output tokens per production →
**$0.25–$1.50 per run** at Opus 4.8 rates. No chunking needed (1M context).
Automatic post-extraction runs add this to each rebuild, which is acceptable
per the user's trigger choice.

## Testing

- Pytest, model call mocked:
  - `apply_verdicts`: merge semantics (participant union incl. duplicate
    participant, keeper corrections, absorbed-event deletion + audit rows),
    delete with snapshot, edit with before/after, confidence gate,
    human-edit guardrail, unknown-id/foreign-production/keeper-not-in-set
    skips, transaction rollback on forced failure.
  - `serialize_timeline`: ordering (chronological, undated last), field shape.
  - Parse round-trip: a canned model response parses into verdicts; truncated
    response (max_tokens stop) treated as failure.
  - Endpoint: manager gate, job kickoff, status surface; extraction hook runs
    review and `review_failed` does not fail extraction.
- Frontend has no test baseline (lint red on main is a known state); build
  must stay clean.

## Non-goals

- No review docket UI (auto-apply was chosen).
- No embedding/blocking dedup infrastructure.
- No changes to the extraction prompt or model.
- No migrations.
