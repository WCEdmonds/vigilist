# Timeline v2 — Trustworthy Dates, Significance, Editing, Polished Rail

**Date:** 2026-07-24
**Status:** Approved (decisions captured in conversation)
**Branch:** `feat/timeline-v2` (off origin/main). Migration parents on `e7f8a9b0c1d2`.

## Problem (grounded in prod data for SCHLEGEL, 557 events)

- **Too much:** 202 `communication` + 182 `other` (routine chatter) bury the 60 filings / 112 meetings / the verdict.
- **Wrong years:** 298 events got full day-precision dates; samples show the model guessing years on recurring calendar references ("MLK Jr. Birthday → 2023-01-16", "End of year celebration → 2022-06-22") that aren't dated case events. 220 events are undated.
- **UI is raw:** all events dumped, "Load more" button, no visual spine.

## Decisions

1. **Re-run with an improved extractor** (not band-aid existing data): never invent years, cite date provenance, skip calendar noise, rate significance.
2. **Edit scope:** correct dates/precision + delete spurious events.
3. **Redesign:** vertical timeline rail, infinite scroll, key-events default + show-all, prominent searchable entity filter.

## Schema (one migration, parent e7f8a9b0c1d2, import-safe)

Add to `ontology_events` (both nullable — re-run populates; no backfill):
- `significance` SmallInteger — 1 (routine) … 5 (pivotal); null = unrated. Indexed.
- `date_source_text` Text — the verbatim phrase from the document the date came from (provenance); null when undated/unsourced.

No separate "inferred" flag: an event with a date but null `date_source_text` is by definition unsourced. The new extractor won't produce that state (it leaves undated events null-dated), so it's a data-quality signal for any legacy rows.

## Tasks

### T1 — migration + model + reset-and-reextract
- Migration `wXXXX_add_event_significance_provenance.py` (parent `e7f8a9b0c1d2`): add the two columns + index on `(production_id, significance)`. Import-safe (alembic + sqlalchemy only). Model fields on `OntologyEvent`.
- `POST /api/productions/{id}/reset-entities` (manager+, audit-logged): delete all `entities` for the production (FK cascade removes mentions/events/participants/relationships/merge-suggestions), clear `documents.entities_extracted_at` for the production, then enqueue the pipeline (reuse the existing extract-entities trigger path). Returns `{reset: true}`. **Explicit destructive action** — the UI must warn it discards existing entities + any confirmed merges. Fake-session tests: scoping 404, manager gate, cascade/clear calls issued.

### T2 — extraction quality (entity_extraction.py + prompt)
- Prompt discipline: (a) assign a date ONLY when the **year is determinable from the document** (stated, or from an email/message header) — if only month/day with no year, leave the event undated; (b) **never guess or infer a year**; (c) quote the exact source phrase in a `date_source` field; (d) **skip recurring calendar references** (holidays, "end of year", birthdays) unless tied to a specific dated case event; (e) rate each event `significance` 1–5 with a rubric (filings, rulings, the verdict, key admissions, terminations = 4–5; substantive meetings/decisions = 3; routine logistics/greetings/scheduling = 1–2).
- Parse/persist: extend the event schema parse to carry `significance` (clamp 1–5, default 3) and `date_source` → `date_source_text`; keep `parse_event_date` but only accept a date when a 4-digit year is present. Tests: "March 18 (no year) → undated", "recurring holiday → skipped", significance clamp/default, date_source captured, year-required guard.

### T3 — timeline API + edit endpoints
- Timeline endpoint (`get_production_timeline`): include `significance` + `date_source_text` in `TimelineEventOut`; add `min_significance` query param (default surfaces key events, e.g. ≥3; `min_significance=1` shows all); keep pagination but the UI drives it via scroll.
- `PATCH /api/events/{event_id}` (manager+, scoped, audit): body `{event_date?: ISO|null, date_precision?}` — correct or clear a date. Validate precision enum; when clearing, null both. 
- `DELETE /api/events/{event_id}` (manager+, scoped, audit): remove a spurious event (cascade participants).
- Tests: filter correctness, edit validation, scoping 404, delete cascade.

### T4 — frontend redesign (use frontend-design skill)
`EntityTimelineView.tsx` rebuilt:
- **Vertical timeline rail** down one side (spine with node markers per event/group), events as cards to the side; significance drives emphasis (size/weight/color accent), routine events muted.
- **Infinite scroll** via IntersectionObserver sentinel — remove the "Load more" button; auto-fetch next page as the user nears the bottom; keep the undated section at the end.
- **Key-events default** (`min_significance≥3`) with a "Show all events" toggle; a significance legend.
- **Prominent, searchable entity filter** (type-ahead over the production's entities) replacing the plain dropdown; event-type filter retained.
- **Inline edit:** per-event date edit (date + precision) and delete, behind a small affordance (manager only); show `date_source_text` as a tooltip/subtext so a date is verifiable at a glance.
- Preserve deep-link (`entity` param), participant→panel, event→document.

### T5 — verify + PR
Backend suite green (known pre-existing failure only); frontend build + lint baseline; merge latest main. PR "feat(timeline): trustworthy dates, significance ranking, editing, vertical-rail redesign". PR body must tell the user to run **Reset & re-extract** on each matter to repopulate with the improved extractor.

## Notes

- Reset-and-reextract discards existing entities/merges for the matter — acceptable now (data is early/low-signal) but the UI warns clearly.
- Re-extraction cost ≈ one Haiku call/document (~550 for SCHLEGEL, few dollars); requires the funded Anthropic key (now set).
- Migration coordinates with parallel session: parent verified `e7f8a9b0c1d2` at build time; if main's head moved, re-parent before PR.
