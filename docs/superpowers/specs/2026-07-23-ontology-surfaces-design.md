# Ontology Surfaces — Timeline, Relationship Graph, Ambient Weaving — Design

**Date:** 2026-07-23
**Status:** Approved (brainstorming complete)
**Parent spec:** `2026-07-22-ontology-entity-intelligence-design.md` (Phase A shipped: PRs #48/#49/#56). This spec covers Phase B (timeline), Phase C (graph), and ambient weaving.

## Constraints inherited from Phase A

- **No schema changes.** Every feature reads existing tables (`ontology_events`, `event_participants`, `entities`, `entity_mentions`, `entity_relationships`, `Production.brief`). No migrations → no migration-head race exposure with the parallel session.
- All new endpoints production-scoped via existing dependencies; 404-never-403 on out-of-scope.
- Frontend: React 19, no router/React Query; React Compiler + `set-state-in-effect` lint rules; zero new lint errors over baseline.
- **One approved new frontend dependency: `d3-force` only** (layout math; we render SVG ourselves). No other new deps, backend or frontend.

## 1. Timeline (Phase B) — `view=timeline`

**Backend:** `GET /api/productions/{id}/timeline?entity_id=&event_type=&page=&per_page=`
- Events ordered `event_date ASC NULLS LAST`, then `id` (stable pagination).
- `entity_id` filter joins through `event_participants`.
- Response rows: `{event_id, description, event_type, event_date (ISO|null), date_precision, document_id, bates_begin, title, participants: [{entity_id, canonical_name, entity_type}]}` + `{total, undated_count}`.
- Participants loaded without N+1 explosion: one query for the page's events, one for all their participants (IN-clause), joined in Python.

**Frontend:** `EntityTimelineView` (`view=timeline` in `useUrlState`, nav button beside Entities):
- Vertical chronology grouped **year → month**; precision-aware labels: day → "Mar 15, 2019", month → "March 2019", year → "2019".
- Undated events under a collapsed "Undated (N)" section at the end.
- Filters: entity (searchable select fed by `listEntities`; deep-linkable `&entity=<uuid>`), event type dropdown.
- Event card: type badge, description, participant chips (click → EntityPanel), source doc link (click → viewer).
- Pagination: "Load more" appending pages.

## 2. Relationship graph (Phase C) — `view=graph`

**Backend:** `GET /api/productions/{id}/graph?max_nodes=&min_shared_docs=`
- Nodes: top `max_nodes` (default 75, clamp ≤150) entities by `mention_count`: `{id, canonical_name, entity_type, mention_count}`.
- Edges among included nodes only:
  - stated: distinct `(source, target, relationship_type)` from `entity_relationships` (evidence doc count as `weight`).
  - cooccurrence: pairs sharing ≥ `min_shared_docs` (default 2) documents via `entity_mentions` self-join GROUP BY; `weight` = shared-doc count. Pairs already stated are not duplicated as co-occurrence.
- Response: `{nodes, edges: [{source, target, kind: "stated"|"cooccurrence", relationship_type?, weight}], truncated: bool}`.

**Frontend:** `EntityGraphView`:
- `d3-force` (forceLink/forceManyBody/forceCollide/forceCenter) run to convergence **before first paint** (synchronous ticks; no jitter), then interactive: node drag (brief re-simulation with warm alpha), wheel zoom + background-drag pan via an SVG transform group.
- Nodes: circles sized `sqrt(mention_count)` (clamped), person/org fill matching the entity-mark palette (#4f7cff / #b4690e), label beside node (hide labels below a zoom threshold for large graphs).
- Edges: stated = solid, hover shows `relationship_type`; cooccurrence = dashed, opacity/width scaled by weight.
- Click node → EntityPanel (same component). `truncated` renders a "showing top N entities" note.
- Empty state (no entities) mirrors EntitiesView (offers extraction button).

## 3. Ambient weaving

**3a. Entity deep-link plumbing (serves everything):** `entity=<uuid>` URL param in `useUrlState`; `view=entities&entity=<uuid>` opens EntitiesView with EntityPanel open (seed `openEntityId` from the param; sync back). Timeline/graph views reuse the same param for their panel state.

**3b. Brief key players become real entities:** wherever the brief/intake-summary is served, the backend augments each `key_players` string with `entity_id|null` by matching through `normalize_name` against the production's entities (also alias match). No stored change — resolved at read time (brief stays a JSONB blob). Frontend renders matched players as clickable chips → navigates to `view=entities&entity=<id>`. Unmatched render as today.

**3c. Entity chips on document rows:** `GET /api/documents/entities-summary?ids=<uuid,csv≤100>` → `{doc_id: [{entity_id, canonical_name, entity_type}] (top 3 by per-doc mention count)}`. Document list and search results render up to 3 small chips per row (click → entity deep link). Fetched per rendered page of rows (one batch call per page render, non-blocking, chips appear when loaded; failures silent).

**3d. Chat entity links:** chat markdown already renders `[label](doc:<uuid>)`. Add `entity:<uuid>` scheme: renderer makes it navigate to the entity deep link; the chat system prompt (where `lookup_entity` is described / cite conventions listed) instructs citing entities as `[Name](entity:<uuid>)` using ids returned by `lookup_entity`.

## Error handling

- Timeline/graph endpoints: empty matter → empty arrays (UI empty states), not errors.
- Graph clamps: `max_nodes ≤ 150`, `min_shared_docs ≥ 1`; degenerate simulation inputs (0/1 node) skip d3 and render directly.
- Chips batch: cap 100 ids per call; unknown/foreign ids silently omitted (scoped query filters them).
- Brief augmentation failure (any exception) → serve brief unaugmented; never break the dashboard.

## Testing

- Backend: fake-session tests per endpoint (scoping 404s, filter correctness, pagination stability, chips cap, brief augmentation match/no-match) following Phase A patterns.
- Frontend: build + lint at baseline; d3-force layout logic isolated in a pure helper (`buildGraphLayout(nodes, edges) -> positioned nodes`) so it's unit-testable in principle; manual walkthrough post-deploy.

## Rollout

Single PR (frontend + backend, no migrations). Nav gains Timeline and Graph buttons beside Entities. Cost: zero new LLM spend — all surfaces render already-extracted data.
