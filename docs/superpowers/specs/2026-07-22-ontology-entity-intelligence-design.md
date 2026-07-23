# Ontology / Entity Intelligence — Design

**Date:** 2026-07-22
**Status:** Approved (brainstorming complete)
**Scope:** Foundation + first surface of a Palantir-style ontology for Vigilist: entities (people, orgs), events, and documents form typed, provenance-backed relationships. V1 ships the extraction/resolution pipeline plus the "mentions & profiles" experience: click a name in a document (or sidebar) → see who they are, everywhere else they appear, and who they're connected to.

## Vision & phasing

The end state is a **case intelligence layer**: entities + events on a timeline, relationship graph visualization, key-players dashboards per matter. All of it rides one foundation — an extraction + entity-resolution pipeline and a graph-shaped ontology schema.

- **Phase A (this spec):** pipeline + schema + mentions/profiles UI (clickable names, entity panel, matter entity list, merge-suggestion queue).
- **Phase B (later):** timeline UI over already-extracted events.
- **Phase C (later):** interactive relationship graph visualization; key-players dashboard.
- **Future (noted, not designed):** clickable entity boxes on page images — requires capturing Google Vision word bounding boxes at ingest (currently discarded in `services/ocr.py`); cross-matter intelligence.

**Events are extracted from day one** even though the timeline UI is Phase B, so the corpus never needs re-processing.

## Architecture decision

**Chosen: LLM extraction + graph-shaped Postgres tables in Neon** (over spaCy NER hybrid and over a dedicated graph database).

Rationale: extraction is identical in all options (Claude must read each doc regardless). At matter scale (hundreds of entities, low-thousands of edges) every query the UI needs — mentions of X, entities in doc Y, 1-hop connections, events by date — is a single index scan or join in Postgres. A graph DB (Neo4j Aura; Neon does not support the AGE extension) would mean a second managed store, duplicated access control, no shared transactions, and separate ops, while buying nothing until deep-traversal analytics at a scale Vigilist doesn't have. Hedge: the schema is deliberately graph-shaped (nodes/edges/provenance), so exporting to a graph DB later is a projection, not a rewrite. Graph analytics (centrality, communities) can meanwhile be computed in-process by loading a matter's node/edge set into memory.

## Data model

All tables scoped by `production_id` (matter), UUID PKs, enforced via existing access-control dependencies (`dependencies.py`). One migration for schema (must be import-safe under minimal CI deps); backfill happens by re-running the pipeline, not by data migration.

### `entities` — nodes
- `id`, `production_id`
- `entity_type`: `person` | `org`
- `canonical_name` (display form)
- `aliases` JSONB — list of surface forms seen ("J. Rivera", "Rivera, Jorge")
- `attributes` JSONB — title/role, email addresses (emails are high-signal for resolution)
- `overview` Text nullable — cached LLM-written profile; `overview_generated_at`, `overview_mention_count` (mention count at generation time, for staleness)
- `mention_count` int — denormalized for key-players sorting
- `created_at`, `updated_at`

### `entity_mentions` — provenance; what makes names clickable
- `id`, `production_id`, `entity_id` FK, `document_id` FK
- `surface_text` — verbatim string as it appears in the document
- `start_offset`, `end_offset` — char offsets into `documents.text_content` (same convention as `ai_review_results.key_excerpts`)
- `context_snippet` — surrounding text for display
- Indexes: `(entity_id)`, `(document_id)`; unique `(document_id, entity_id, start_offset)`

### `ontology_events` + `event_participants` — events as first-class nodes
- `ontology_events`: `id`, `production_id`, `event_type` (`meeting` | `communication` | `payment` | `filing` | `agreement` | `other`), `description`, `event_date` Date nullable, `date_precision` (`day` | `month` | `year` | `unknown`), `document_id` (source), `created_at`
- `event_participants`: `event_id`, `entity_id`, `role` Text nullable

### `entity_relationships` — typed, directed edges (stated only)
- `id`, `production_id`, `source_entity_id`, `target_entity_id`
- `relationship_type`: `employment` | `counsel` | `correspondent` | `party_to_agreement` | `family` | `other`
- `description` — short evidence phrase from the document
- `document_id` — where observed
- **Co-occurrence edges are NOT stored** — computed live from `entity_mentions` (GROUP BY shared documents). Storing them would cache a cheap query and go stale on merges.

### `entity_merge_suggestions` — human review queue
- `id`, `production_id`, `entity_a_id`, `entity_b_id`, `score`, `rationale` (one line, e.g. "same surname + first initial, both cc'd on Acme thread"), `status` (`pending` | `accepted` | `rejected`), `created_at`, `resolved_by`, `resolved_at`

### `entity_merges` — reversibility log
- On merge: loser's mentions/edges/event-participations re-pointed to winner; log row records loser's full snapshot + moved row IDs. Undo = mechanical restore. (Chosen over a `merged_into` pointer chain so that queries stay simple and the rare undo pays the cost, not every read.)

### `documents.entities_extracted_at`
- Nullable timestamp; per-item idempotency marker, same pattern as packaging state columns.

## Extraction pipeline

**Orchestration:** new stage in the ambient pipeline (`services/pipeline.py`) after summaries. Cloud Tasks worker following the established pattern: `enqueue_extract_entities` in `services/tasks.py` → `POST /api/ingest/extract-entities-batch`; bounded batches; skip documents with `entities_extracted_at` set; idempotent/resumable; progress in `Production.ai_pipeline_status`. Backfill for existing corpora = re-run pipeline per matter (admin-triggered).

**Per-document extraction:** one Claude Haiku 4.5 call per document using the `ai_review.py` structured-output pattern (schema-forced JSON prompt, defensive fence-stripping parser, retry with backoff on transient errors). Output:
- entities: name, type, all surface forms used in this doc, role/title/email if stated
- events: description, type, date + precision, participant names
- stated relationships: pair of names, type, evidence phrase

**The LLM returns verbatim surface strings, never offsets** (models are unreliable at character arithmetic). The backend locates all occurrences of each surface form in `text_content` via string search and writes mention rows with real offsets. Documents > ~150k chars are sliced and results merged. Failure of one document must not poison its batch (per-doc try/except; doc left unmarked for retry).

**Email-metadata assist:** `email_from/to/cc/bcc` are already parsed columns — sender/recipients become entities + mentions deterministically, no LLM trust required.

## Entity resolution (per matter, incremental, deterministic)

When a document's extracted entities arrive, match each against the matter's existing entities in tiers:

1. **Auto-attach:** normalized exact name match, known-alias match, or matching email address. New surface forms appended to `aliases`.
2. **Suggest:** surname + first-initial patterns ("J. Rivera" ↔ "Jorge Rivera"), high trigram similarity, nickname pairs. Creates a separate entity **plus** a pending `entity_merge_suggestions` row with rationale. Nothing merges silently.
3. **Create:** no plausible match → new entity.

Tiers 1–2 are pure functions over strings/attributes — no LLM in the loop; fully unit-testable. Accepting a suggestion merges (re-point + log, reversible). Rejecting marks the pair so it is never re-suggested.

## Profiles

Generated lazily on first view of an entity panel: Haiku call synthesizing an overview from top mention snippets, roles, attributes, and stated relationships; cached in `entities.overview`; regenerated only when `mention_count` ≥ 1.5 × `overview_mention_count` or has grown by ≥ 10 since generation. No cost for profiles nobody opens.

## API surface

New router `routers/entities.py`, all behind existing production-access dependencies:

- `GET /api/documents/{id}/entities` — entities in a document + mention offsets (powers clickable text)
- `GET /api/entities/{id}` — profile: overview (lazily generated here), aliases, attributes, stats
- `GET /api/entities/{id}/mentions` — paginated, grouped by document, with snippets
- `GET /api/entities/{id}/connections` — stated edges + top live co-occurrences + shared events
- `GET /api/productions/{id}/entities` — matter-wide searchable list, sorted by mention_count (key players)
- Merge workflow: `GET .../merge-suggestions`, `POST .../merge-suggestions/{id}/accept|reject`, `POST /api/entities/merge` (manual), `POST /api/entity-merges/{id}/undo`
- Chat agent: add `lookup_entity` tool to `services/ai_tools.py` (read-only, production-scoped) so chat answers "who is X?" from the ontology.

## Frontend

Stack constraints respected: no React Query/router; hand-rolled `request<T>()` in `src/api/client.ts`; state-driven routing in `App.tsx` + `useUrlState.ts`; React Compiler lint rules.

1. **Clickable names in `TextPanel.tsx`:** fetch `GET /documents/{id}/entities`; render entity mentions as tinted clickable spans (color by type), same `<mark>`-wrapping approach as existing search highlighting. Click → entity panel. Page images stay non-interactive in v1 (no OCR boxes yet).
2. **Entity panel** (drawer in `DocumentViewer.tsx`): overview, role, aliases; connections as clickable chips (navigate to that entity); mentions grouped by document — each deep-links via the existing `doc:<uuid>` convention and scrolls the text panel to the mention offset. Left sidebar gains a "People & organizations" section beside Duplicates listing this document's entities.
3. **Matter-level Entities view** (`view=entities` in `useUrlState`): searchable key-players list; merge-suggestion review queue with side-by-side candidate comparison (each entity's top mentions), accept/reject.

## Error handling

- Extraction: per-document isolation; parse failures → doc unmarked, logged, retried on next pipeline run; hard cap on retries per run.
- Offset location: if a surface form can't be found verbatim in `text_content` (OCR drift), record the mention with null offsets (still counts for the entity; just not clickable) rather than dropping it.
- Merge/undo: transactional; undo fails gracefully if subsequent merges touched the same entity (surface a clear message).
- Profile generation failure: panel renders without overview; retry on next open.

## Testing

- **Backend unit tests:** offset-location logic, resolution tiers (pure functions), JSON parser defense, merge/undo round-trip.
- **Router tests:** access-control scoping, pagination, merge workflow — following existing router test patterns.
- **Migration:** import-safe under minimal CI deps (known constraint — no pydantic/app imports in the migration).
- **Frontend:** no test suite exists; verify via local Playwright walkthrough (sign-in flow per established local-dev process). Lint must not add to the 41-error baseline; respect React Compiler rules.

## Rollout

1. PR 1: schema migration + extraction/resolution pipeline + API. Safe to merge alone — populates data, changes no UI.
2. PR 2: frontend surfaces (clickable text, entity panel, entities view, merge queue).
3. Backfill: trigger pipeline re-run per existing matter after PR 1 deploys.

Cost: ~one Haiku call per document (same order as the existing summary pass); a 1,000-doc matter is low single-digit dollars. Ship via PR to `main`; coordinate with the parallel session (check for drift before merging).
