# Ontology / Entity Intelligence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the entity-intelligence foundation for Vigilist: LLM extraction of people/orgs/events/relationships per document into graph-shaped Postgres tables, deterministic tiered resolution with a human merge queue, and a mentions-and-profiles UI (clickable names in the text panel, entity drawer, matter-level key-players view).

**Architecture:** One Claude Haiku call per document returns verbatim surface strings (never offsets); the backend locates char offsets by string search into `documents.text_content`. Extraction runs as a new stage in the existing ambient pipeline (`services/pipeline.py`), riding the existing Cloud Tasks `run-pipeline` worker's retry/resume semantics with a per-document idempotency marker (`documents.entities_extracted_at`). Resolution tiers (auto-attach / suggest / create) are pure functions. All new tables live in Neon behind the existing production-access dependencies.

**Tech Stack:** FastAPI + SQLAlchemy async + Alembic; Anthropic SDK (lazy import, existing pattern); React 19 + Vite + TS, hand-rolled `request<T>()` client, no router/React Query.

**Spec:** `docs/superpowers/specs/2026-07-22-ontology-entity-intelligence-design.md`

**One deviation from spec, decided here:** the spec named a dedicated Cloud Tasks worker (`enqueue_extract_entities` → `/api/ingest/extract-entities-batch`). Instead, extraction is a pipeline *stage* inside the existing `run-pipeline` worker, exactly like the summaries stage: batched loop, per-doc idempotency marker, resumable via Cloud Tasks retry of the whole pipeline task. This preserves every spec goal (idempotent, resumable, Cloud-Tasks-driven, backfill = pipeline re-run) with one less worker endpoint and no new enqueue plumbing. Backfill/manual trigger is `POST /api/productions/{id}/extract-entities`, which re-enqueues the pipeline.

## Global Constraints

- Migrations must be import-safe under minimal CI deps: import only `alembic` and `sqlalchemy` (+ `sqlalchemy.dialects.postgresql`), never app modules.
- Migration `down_revision` is `"t2b3c4d5e6f7"` — **verify at implementation time** with `cd backend; alembic heads`. The parallel session's `feat/p2-3-loadfiles-packaging` branch adds its own migrations; if main's head has moved, use the new head (or add a merge migration) instead.
- No new Python or npm dependencies. Fuzzy matching uses stdlib `difflib`; no spaCy, no pg_trgm requirement.
- LLM: extraction + profiles use the Haiku model string already used by `services/ai.py` for summaries (check the constant there — expected `"claude-haiku-4-5"`); lazy `import anthropic` inside functions, never at module top.
- Frontend lint baseline is 41 errors on main — `npm run lint` must not add NEW errors; React Compiler rules ban setState/ref-reads during render.
- All entity API endpoints enforce production scoping via `get_accessible_production_ids` / `get_user_role_for_production`; write operations reject `readonly` role.
- Entity type values: `"person" | "org"`. Event types: `meeting | communication | payment | filing | agreement | other`. Relationship types: `employment | counsel | correspondent | party_to_agreement | family | other`. Date precision: `day | month | year | unknown`.
- Backend tests: fake-session pattern from `backend/tests/fakes.py` / `test_redaction_endpoints.py` — no DB, no network; run with `cd backend; python -m pytest tests/ -q`.
- Rollout: Tasks 1–8 = PR 1 (backend, data-only, safe alone). Tasks 9–13 = PR 2 (frontend). Branch: `feat/ontology-entity-intelligence`.

---

### Task 1: Ontology models + migration

**Files:**
- Modify: `backend/app/models.py` (append after `DocumentClusterAssignment`, ~line 484; also add one column to `Document`)
- Create: `backend/alembic/versions/u3c4d5e6f7g8_add_ontology_tables.py`
- Test: `backend/tests/test_ontology_models.py`

**Interfaces:**
- Consumes: `Base`, existing column/type imports in `models.py` (`JSONB`, `UUID`, `func`, `Index`, `UniqueConstraint`).
- Produces: models `Entity`, `EntityMention`, `OntologyEvent`, `EventParticipant`, `EntityRelationship`, `EntityMergeSuggestion`, `EntityMerge`; column `Document.entities_extracted_at` (DateTime, nullable). All later tasks import these from `app.models`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_ontology_models.py
"""Smoke tests: ontology models exist with the columns later tasks rely on."""


def test_ontology_models_importable():
    from app.models import (
        Entity, EntityMention, OntologyEvent, EventParticipant,
        EntityRelationship, EntityMergeSuggestion, EntityMerge,
    )
    assert Entity.__tablename__ == "entities"
    assert EntityMention.__tablename__ == "entity_mentions"
    assert OntologyEvent.__tablename__ == "ontology_events"
    assert EventParticipant.__tablename__ == "event_participants"
    assert EntityRelationship.__tablename__ == "entity_relationships"
    assert EntityMergeSuggestion.__tablename__ == "entity_merge_suggestions"
    assert EntityMerge.__tablename__ == "entity_merges"


def test_entity_columns():
    from app.models import Entity
    cols = {c.name for c in Entity.__table__.columns}
    assert {"id", "production_id", "entity_type", "canonical_name", "aliases",
            "attributes", "overview", "overview_generated_at",
            "overview_mention_count", "mention_count"} <= cols


def test_document_has_extraction_marker():
    from app.models import Document
    assert "entities_extracted_at" in {c.name for c in Document.__table__.columns}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend; python -m pytest tests/test_ontology_models.py -q`
Expected: FAIL — `ImportError: cannot import name 'Entity'`

- [ ] **Step 3: Add the models**

In `backend/app/models.py`, add to the `Document` class (after `privilege_description`, line ~145):

```python
    # Ontology — per-document extraction idempotency marker
    entities_extracted_at = Column(DateTime, nullable=True)
```

Append at end of file:

```python
# ── Ontology (entity intelligence) ──────────────────────────────────────────
# Graph-shaped: entities are nodes, entity_relationships are typed edges,
# entity_mentions are provenance. Co-occurrence edges are NOT stored — they
# are computed live from entity_mentions.


class Entity(Base):
    __tablename__ = "entities"
    __table_args__ = (
        Index("ix_entities_production_id", "production_id"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    production_id = Column(Integer, ForeignKey("productions.id", ondelete="CASCADE"), nullable=False)
    entity_type = Column(String(10), nullable=False)  # 'person' | 'org'
    canonical_name = Column(String(500), nullable=False)
    aliases = Column(JSONB, nullable=False, default=list)      # surface forms seen
    attributes = Column(JSONB, nullable=False, default=dict)   # {"role": ..., "emails": [...]}
    overview = Column(Text, nullable=True)                     # cached LLM profile
    overview_generated_at = Column(DateTime, nullable=True)
    overview_mention_count = Column(Integer, nullable=True)    # mention_count at generation time
    mention_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class EntityMention(Base):
    __tablename__ = "entity_mentions"
    __table_args__ = (
        UniqueConstraint("document_id", "entity_id", "start_offset", name="uq_mention_doc_entity_offset"),
        Index("ix_entity_mentions_entity_id", "entity_id"),
        Index("ix_entity_mentions_document_id", "document_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    production_id = Column(Integer, ForeignKey("productions.id", ondelete="CASCADE"), nullable=False)
    entity_id = Column(UUID(as_uuid=True), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False)
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    surface_text = Column(String(500), nullable=False)
    # Char offsets into documents.text_content; NULL when the surface form
    # couldn't be located verbatim (OCR drift) — still counts, just not clickable.
    start_offset = Column(Integer, nullable=True)
    end_offset = Column(Integer, nullable=True)
    context_snippet = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    entity = relationship("Entity")
    document = relationship("Document")


class OntologyEvent(Base):
    __tablename__ = "ontology_events"
    __table_args__ = (
        Index("ix_ontology_events_production_id", "production_id"),
        Index("ix_ontology_events_document_id", "document_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    production_id = Column(Integer, ForeignKey("productions.id", ondelete="CASCADE"), nullable=False)
    event_type = Column(String(20), nullable=False)
    description = Column(Text, nullable=False)
    event_date = Column(Date, nullable=True)
    date_precision = Column(String(10), nullable=False, default="unknown")  # day|month|year|unknown
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    participants = relationship("EventParticipant", back_populates="event", cascade="all, delete-orphan")


class EventParticipant(Base):
    __tablename__ = "event_participants"
    __table_args__ = (
        UniqueConstraint("event_id", "entity_id", name="uq_event_entity"),
        Index("ix_event_participants_entity_id", "entity_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("ontology_events.id", ondelete="CASCADE"), nullable=False)
    entity_id = Column(UUID(as_uuid=True), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False)
    role = Column(String(100), nullable=True)

    event = relationship("OntologyEvent", back_populates="participants")


class EntityRelationship(Base):
    __tablename__ = "entity_relationships"
    __table_args__ = (
        UniqueConstraint("source_entity_id", "target_entity_id", "relationship_type", "document_id",
                         name="uq_edge_pair_type_doc"),
        Index("ix_entity_relationships_source", "source_entity_id"),
        Index("ix_entity_relationships_target", "target_entity_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    production_id = Column(Integer, ForeignKey("productions.id", ondelete="CASCADE"), nullable=False)
    source_entity_id = Column(UUID(as_uuid=True), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False)
    target_entity_id = Column(UUID(as_uuid=True), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False)
    relationship_type = Column(String(30), nullable=False)
    description = Column(Text, nullable=True)  # short evidence phrase
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class EntityMergeSuggestion(Base):
    __tablename__ = "entity_merge_suggestions"
    __table_args__ = (
        UniqueConstraint("entity_a_id", "entity_b_id", name="uq_merge_suggestion_pair"),
        Index("ix_entity_merge_suggestions_production_id", "production_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    production_id = Column(Integer, ForeignKey("productions.id", ondelete="CASCADE"), nullable=False)
    entity_a_id = Column(UUID(as_uuid=True), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False)
    entity_b_id = Column(UUID(as_uuid=True), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False)
    score = Column(Float, nullable=False)
    rationale = Column(Text, nullable=False)
    status = Column(String(10), nullable=False, default="pending")  # pending|accepted|rejected
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    resolved_by = Column(String(128), nullable=True)
    resolved_at = Column(DateTime, nullable=True)


class EntityMerge(Base):
    __tablename__ = "entity_merges"
    __table_args__ = (
        Index("ix_entity_merges_production_id", "production_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    production_id = Column(Integer, ForeignKey("productions.id", ondelete="CASCADE"), nullable=False)
    winner_entity_id = Column(UUID(as_uuid=True), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False)
    loser_snapshot = Column(JSONB, nullable=False)        # full loser Entity row for undo
    winner_prior = Column(JSONB, nullable=False)          # {"aliases": [...], "mention_count": N} pre-merge
    moved_mention_ids = Column(JSONB, nullable=False, default=list)
    moved_relationship_ids = Column(JSONB, nullable=False, default=list)
    moved_participant_ids = Column(JSONB, nullable=False, default=list)
    undone = Column(Boolean, nullable=False, default=False)
    merged_by = Column(String(128), nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
```

Add `Date` to the `sqlalchemy` import list at the top of `models.py` (it is not currently imported).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend; python -m pytest tests/test_ontology_models.py -q`
Expected: 3 passed

- [ ] **Step 5: Write the migration**

Create `backend/alembic/versions/u3c4d5e6f7g8_add_ontology_tables.py`. **First run `cd backend; alembic heads`** — if the head is not `t2b3c4d5e6f7`, use the actual head as `down_revision`.

```python
"""add ontology tables (entities, mentions, events, edges, merges)

Revision ID: u3c4d5e6f7g8
Revises: t2b3c4d5e6f7
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "u3c4d5e6f7g8"
down_revision = "t2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("documents", sa.Column("entities_extracted_at", sa.DateTime(), nullable=True))

    op.create_table(
        "entities",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("production_id", sa.Integer(), sa.ForeignKey("productions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity_type", sa.String(length=10), nullable=False),
        sa.Column("canonical_name", sa.String(length=500), nullable=False),
        sa.Column("aliases", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("attributes", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("overview", sa.Text(), nullable=True),
        sa.Column("overview_generated_at", sa.DateTime(), nullable=True),
        sa.Column("overview_mention_count", sa.Integer(), nullable=True),
        sa.Column("mention_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_entities_production_id", "entities", ["production_id"])

    op.create_table(
        "entity_mentions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("production_id", sa.Integer(), sa.ForeignKey("productions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity_id", UUID(as_uuid=True), sa.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_id", UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("surface_text", sa.String(length=500), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=True),
        sa.Column("end_offset", sa.Integer(), nullable=True),
        sa.Column("context_snippet", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("document_id", "entity_id", "start_offset", name="uq_mention_doc_entity_offset"),
    )
    op.create_index("ix_entity_mentions_entity_id", "entity_mentions", ["entity_id"])
    op.create_index("ix_entity_mentions_document_id", "entity_mentions", ["document_id"])

    op.create_table(
        "ontology_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("production_id", sa.Integer(), sa.ForeignKey("productions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(length=20), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=True),
        sa.Column("date_precision", sa.String(length=10), nullable=False, server_default="unknown"),
        sa.Column("document_id", UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_ontology_events_production_id", "ontology_events", ["production_id"])
    op.create_index("ix_ontology_events_document_id", "ontology_events", ["document_id"])

    op.create_table(
        "event_participants",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("ontology_events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity_id", UUID(as_uuid=True), sa.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(length=100), nullable=True),
        sa.UniqueConstraint("event_id", "entity_id", name="uq_event_entity"),
    )
    op.create_index("ix_event_participants_entity_id", "event_participants", ["entity_id"])

    op.create_table(
        "entity_relationships",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("production_id", sa.Integer(), sa.ForeignKey("productions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_entity_id", UUID(as_uuid=True), sa.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_entity_id", UUID(as_uuid=True), sa.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("relationship_type", sa.String(length=30), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("document_id", UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("source_entity_id", "target_entity_id", "relationship_type", "document_id",
                            name="uq_edge_pair_type_doc"),
    )
    op.create_index("ix_entity_relationships_source", "entity_relationships", ["source_entity_id"])
    op.create_index("ix_entity_relationships_target", "entity_relationships", ["target_entity_id"])

    op.create_table(
        "entity_merge_suggestions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("production_id", sa.Integer(), sa.ForeignKey("productions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity_a_id", UUID(as_uuid=True), sa.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity_b_id", UUID(as_uuid=True), sa.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_by", sa.String(length=128), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("entity_a_id", "entity_b_id", name="uq_merge_suggestion_pair"),
    )
    op.create_index("ix_entity_merge_suggestions_production_id", "entity_merge_suggestions", ["production_id"])

    op.create_table(
        "entity_merges",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("production_id", sa.Integer(), sa.ForeignKey("productions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("winner_entity_id", UUID(as_uuid=True), sa.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("loser_snapshot", JSONB(), nullable=False),
        sa.Column("winner_prior", JSONB(), nullable=False),
        sa.Column("moved_mention_ids", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("moved_relationship_ids", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("moved_participant_ids", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("undone", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("merged_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_entity_merges_production_id", "entity_merges", ["production_id"])


def downgrade():
    for name in ("entity_merges", "entity_merge_suggestions", "entity_relationships",
                 "event_participants", "ontology_events", "entity_mentions", "entities"):
        op.drop_table(name)
    op.drop_column("documents", "entities_extracted_at")
```

- [ ] **Step 6: Verify migration imports cleanly and full suite passes**

Run: `cd backend; python -c "import runpy; runpy.run_path('alembic/versions/u3c4d5e6f7g8_add_ontology_tables.py'); print('ok')"`
Expected: `ok` (proves the migration imports with only alembic + sqlalchemy — the CI minimal-deps constraint)
Run: `cd backend; python -m pytest tests/ -q`
Expected: all pass (no regressions)

- [ ] **Step 7: Commit**

```bash
git add backend/app/models.py backend/alembic/versions/u3c4d5e6f7g8_add_ontology_tables.py backend/tests/test_ontology_models.py
git commit -m "feat(ontology): entity/mention/event/edge tables + migration"
```

---

### Task 2: Extraction pure functions (prompt, parser, slicing, offset location, email parsing)

**Files:**
- Create: `backend/app/services/entity_extraction.py`
- Test: `backend/tests/test_entity_extraction.py`

**Interfaces:**
- Consumes: nothing app-specific (pure module; `app.config.settings` only in Task 4's additions).
- Produces (used by Tasks 4–5):
  - `EXTRACTION_SYSTEM_PROMPT: str`, `build_extraction_prompt(document_text: str) -> str`
  - `parse_extraction_response(raw: str) -> dict` — always returns `{"entities": [...], "events": [...], "relationships": [...]}`
  - `slice_text(text: str, window: int = 140_000, overlap: int = 2_000) -> list[str]`
  - `locate_mentions(text: str, surface_forms: list[str], max_per_form: int = 200) -> list[dict]` — dicts `{"surface_text", "start_offset", "end_offset", "context_snippet"}`
  - `parse_event_date(raw: str | None) -> tuple[date | None, str]` — (date, precision)
  - `parse_email_addresses(raw: str | None) -> list[tuple[str, str]]` — [(display_name, email)]

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_entity_extraction.py
"""Pure-function tests for entity extraction: parsing, slicing, offsets, dates."""
from datetime import date

from app.services.entity_extraction import (
    build_extraction_prompt, locate_mentions, parse_email_addresses,
    parse_event_date, parse_extraction_response, slice_text,
)


def test_parse_valid_response():
    raw = '''```json
{"entities": [{"name": "Jorge Rivera", "type": "person", "surface_forms": ["Jorge Rivera", "J. Rivera"], "role": "CFO", "emails": ["jr@acme.com"]}],
 "events": [{"description": "Board meeting", "type": "meeting", "date": "2019-03-15", "participants": ["Jorge Rivera"]}],
 "relationships": [{"source": "Jorge Rivera", "target": "Acme Corp", "type": "employment", "evidence": "signature block"}]}
```'''
    out = parse_extraction_response(raw)
    assert out["entities"][0]["name"] == "Jorge Rivera"
    assert out["entities"][0]["type"] == "person"
    assert out["events"][0]["type"] == "meeting"
    assert out["relationships"][0]["type"] == "employment"


def test_parse_garbage_returns_empty_sentinel():
    out = parse_extraction_response("I could not process this document.")
    assert out == {"entities": [], "events": [], "relationships": []}


def test_parse_drops_invalid_enum_values():
    raw = '{"entities": [{"name": "X", "type": "alien", "surface_forms": ["X"]}], "events": [{"description": "y", "type": "party", "participants": []}], "relationships": []}'
    out = parse_extraction_response(raw)
    assert out["entities"] == []          # bad entity type dropped
    assert out["events"][0]["type"] == "other"  # bad event type coerced


def test_locate_mentions_finds_all_occurrences_with_offsets():
    text = "Jorge Rivera met the board. Later, Rivera signed. Jorge Rivera left."
    mentions = locate_mentions(text, ["Jorge Rivera", "Rivera"])
    spans = {(m["start_offset"], m["end_offset"]) for m in mentions}
    assert (0, 12) in spans and (50, 62) in spans      # both "Jorge Rivera"
    assert (35, 41) in spans                            # bare "Rivera"
    # longest-form-first: bare "Rivera" inside "Jorge Rivera" is NOT double-counted
    assert (6, 12) not in spans and (56, 62) not in spans
    for m in mentions:
        assert text[m["start_offset"]:m["end_offset"]] == m["surface_text"]


def test_locate_mentions_missing_form_returns_nothing_for_it():
    assert locate_mentions("nothing here", ["Jorge Rivera"]) == []


def test_slice_text_short_is_single_slice():
    assert slice_text("abc") == ["abc"]


def test_slice_text_long_overlaps():
    text = "x" * 300_000
    slices = slice_text(text, window=140_000, overlap=2_000)
    assert len(slices) == 3
    assert all(len(s) <= 140_000 for s in slices)


def test_parse_event_date_precisions():
    assert parse_event_date("2019-03-15") == (date(2019, 3, 15), "day")
    assert parse_event_date("2019-03") == (date(2019, 3, 1), "month")
    assert parse_event_date("2019") == (date(2019, 1, 1), "year")
    assert parse_event_date(None) == (None, "unknown")
    assert parse_event_date("sometime") == (None, "unknown")


def test_parse_email_addresses():
    assert parse_email_addresses('Jorge Rivera <jr@acme.com>') == [("Jorge Rivera", "jr@acme.com")]
    assert parse_email_addresses('jr@acme.com; Ana Cruz <ana@firm.law>') == [
        ("", "jr@acme.com"), ("Ana Cruz", "ana@firm.law")]
    assert parse_email_addresses(None) == []


def test_prompt_includes_document_text():
    assert "the quick brown" in build_extraction_prompt("the quick brown")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend; python -m pytest tests/test_entity_extraction.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.entity_extraction'`

- [ ] **Step 3: Implement**

```python
# backend/app/services/entity_extraction.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend; python -m pytest tests/test_entity_extraction.py -q`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/entity_extraction.py backend/tests/test_entity_extraction.py
git commit -m "feat(ontology): extraction prompt, parser, offset location, date/email helpers"
```

---

### Task 3: Resolution pure functions (normalize, match tiers)

**Files:**
- Create: `backend/app/services/entity_resolution.py`
- Test: `backend/tests/test_entity_resolution.py`

**Interfaces:**
- Consumes: nothing (pure; stdlib `difflib`).
- Produces (used by Task 4):
  - `normalize_name(name: str) -> str`
  - `match_entity(candidate: dict, existing: list) -> tuple` — candidate is `{"name", "type", "surface_forms", "emails"}`; existing items need attrs `.entity_type`, `.canonical_name`, `.aliases`, `.attributes`. Returns `("attach", entity)`, `("suggest", entity, score: float, rationale: str)`, or `("create", None)`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_entity_resolution.py
"""Pure resolution-tier tests. Wrong merges mislead reviewers — every tier
transition here is a behavioral contract, not an implementation detail."""

from app.services.entity_resolution import match_entity, normalize_name


class E:
    def __init__(self, name, etype="person", aliases=None, emails=None):
        self.canonical_name = name
        self.entity_type = etype
        self.aliases = aliases or []
        self.attributes = {"emails": emails or []}


def test_normalize_strips_honorifics_case_punctuation():
    assert normalize_name("Dr. Jorge  Rivera, Esq.") == "jorge rivera"
    assert normalize_name("RIVERA, Jorge") == "jorge rivera"  # comma form swapped


def test_attach_on_exact_normalized_name():
    e = E("Jorge Rivera")
    assert match_entity({"name": "jorge rivera", "type": "person", "surface_forms": [], "emails": []}, [e]) == ("attach", e)


def test_attach_on_alias():
    e = E("Jorge Rivera", aliases=["J. Rivera"])
    assert match_entity({"name": "J. Rivera", "type": "person", "surface_forms": [], "emails": []}, [e]) == ("attach", e)


def test_attach_on_email_even_when_name_differs():
    e = E("Jorge Rivera", emails=["jr@acme.com"])
    assert match_entity({"name": "J.R.", "type": "person", "surface_forms": [], "emails": ["jr@acme.com"]}, [e]) == ("attach", e)


def test_suggest_on_initial_pattern():
    e = E("Jorge Rivera")
    kind, ent, score, rationale = match_entity(
        {"name": "J. Rivera", "type": "person", "surface_forms": [], "emails": []}, [e])
    assert kind == "suggest" and ent is e and score >= 0.8 and "initial" in rationale


def test_suggest_on_high_similarity():
    e = E("Jonathan Smithers")
    kind, ent, score, rationale = match_entity(
        {"name": "Jonathon Smithers", "type": "person", "surface_forms": [], "emails": []}, [e])
    assert kind == "suggest" and ent is e


def test_create_when_no_match():
    kind, ent = match_entity({"name": "Ana Cruz", "type": "person", "surface_forms": [], "emails": []},
                             [E("Jorge Rivera")])
    assert kind == "create" and ent is None


def test_never_matches_across_entity_types():
    e = E("Rivera", etype="org")
    kind, *_ = match_entity({"name": "Rivera", "type": "person", "surface_forms": [], "emails": []}, [e])
    assert kind == "create"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend; python -m pytest tests/test_entity_resolution.py -q`
Expected: FAIL — module not found

- [ ] **Step 3: Implement**

```python
# backend/app/services/entity_resolution.py
"""Deterministic entity resolution tiers. No LLM in the loop.

Tier 1 (attach): normalized-name equality, known alias, or shared email.
Tier 2 (suggest): initial-pattern or high string similarity — creates a
merge suggestion for a human; nothing merges silently.
Tier 3 (create): everything else.
"""

import re
from difflib import SequenceMatcher

_HONORIFICS = {"mr", "mrs", "ms", "dr", "prof", "hon", "esq", "jr", "sr", "ii", "iii"}
_SIMILARITY_THRESHOLD = 0.85


def normalize_name(name: str) -> str:
    """Lowercase, strip punctuation/honorifics, swap 'Last, First' to 'first last'."""
    s = name.strip().lower()
    if s.count(",") == 1:
        last, first = s.split(",")
        s = f"{first.strip()} {last.strip()}"
    s = re.sub(r"[^\w\s]", " ", s)
    tokens = [t for t in s.split() if t not in _HONORIFICS]
    return " ".join(tokens)


def _initial_pattern(a: str, b: str) -> bool:
    """'j rivera' vs 'jorge rivera' — same last token, first tokens agree on initial."""
    ta, tb = a.split(), b.split()
    if len(ta) < 2 or len(tb) < 2 or ta[-1] != tb[-1]:
        return False
    fa, fb = ta[0], tb[0]
    if fa == fb:
        return False  # would already be an exact match
    return (len(fa) == 1 and fb.startswith(fa)) or (len(fb) == 1 and fa.startswith(fb))


def match_entity(candidate: dict, existing: list) -> tuple:
    """Match one extracted candidate against a production's existing entities.

    Returns ("attach", entity) | ("suggest", entity, score, rationale) | ("create", None).
    Only entities of the same type are considered.
    """
    cand_norm = normalize_name(candidate["name"])
    cand_emails = set(candidate.get("emails") or [])
    same_type = [e for e in existing if e.entity_type == candidate["type"]]

    for e in same_type:
        if normalize_name(e.canonical_name) == cand_norm and cand_norm:
            return ("attach", e)
        if cand_norm in {normalize_name(a) for a in (e.aliases or [])}:
            return ("attach", e)
        if cand_emails & {em.lower() for em in (e.attributes or {}).get("emails", [])}:
            return ("attach", e)

    best = None  # (score, entity, rationale)
    for e in same_type:
        e_norm = normalize_name(e.canonical_name)
        if not e_norm or not cand_norm:
            continue
        if _initial_pattern(cand_norm, e_norm):
            score, rationale = 0.9, f'initial pattern: "{candidate["name"]}" ~ "{e.canonical_name}"'
        else:
            ratio = SequenceMatcher(None, cand_norm, e_norm).ratio()
            if ratio < _SIMILARITY_THRESHOLD:
                continue
            score, rationale = ratio, f'name similarity {ratio:.2f}: "{candidate["name"]}" ~ "{e.canonical_name}"'
        if best is None or score > best[0]:
            best = (score, e, rationale)

    if best is not None:
        return ("suggest", best[1], round(best[0], 3), best[2])
    return ("create", None)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend; python -m pytest tests/test_entity_resolution.py -q`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/entity_resolution.py backend/tests/test_entity_resolution.py
git commit -m "feat(ontology): deterministic resolution tiers (attach/suggest/create)"
```

---

### Task 4: Persistence — apply one document's extraction to the ontology

**Files:**
- Modify: `backend/app/services/entity_extraction.py` (append persistence + LLM-call section)
- Test: `backend/tests/test_entity_persist.py`

**Interfaces:**
- Consumes: Task 1 models; Task 2 helpers; Task 3 `match_entity`; `app.config.settings.anthropic_api_key`; retry pattern from `services/ai_review.py`.
- Produces (used by Task 5):
  - `async persist_extraction(db, production_id: int, document_id, text: str, parsed: dict) -> dict` — writes entities/mentions/events/edges/suggestions; returns `{"entities": n, "mentions": n, "events": n, "relationships": n, "suggestions": n}`. Does NOT commit — caller owns the transaction.
  - `async extract_document_entities(text: str) -> dict | None` — Claude call over `slice_text` windows, merged parse; `None` when no API key or hard failure (callers must treat `None` as "retry later", distinct from `{}` = "genuinely nothing found").
  - `header_candidates(doc) -> list[dict]` — deterministic candidates from `email_from/to/cc/bcc`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_entity_persist.py
"""persist_extraction against a FakeSession: entity creation, attach,
suggestion creation, mention offsets, event + relationship wiring."""

import asyncio
import uuid

from app.models import Entity, EntityMention, EntityMergeSuggestion, EntityRelationship, OntologyEvent
from app.services.entity_extraction import header_candidates, persist_extraction
from tests.fakes import FakeResult, FakeSession

DOC_ID = uuid.uuid4()
TEXT = "Jorge Rivera emailed Ana Cruz about the Acme Corp merger. Rivera signed."


def _parsed(entities=None, events=None, relationships=None):
    return {"entities": entities or [], "events": events or [], "relationships": relationships or []}


def _session_with_existing(entities):
    # persist_extraction loads existing production entities with one SELECT on "entities"
    return FakeSession(responders=[("FROM entities", FakeResult(items=entities))])


def test_creates_entities_and_offset_mentions():
    db = _session_with_existing([])
    parsed = _parsed(entities=[
        {"name": "Jorge Rivera", "type": "person", "surface_forms": ["Jorge Rivera", "Rivera"], "role": None, "emails": []},
        {"name": "Acme Corp", "type": "org", "surface_forms": ["Acme Corp"], "role": None, "emails": []},
    ])
    stats = asyncio.run(persist_extraction(db, 1, DOC_ID, TEXT, parsed))
    ents = [o for o in db.added if isinstance(o, Entity)]
    mentions = [o for o in db.added if isinstance(o, EntityMention)]
    assert stats["entities"] == 2 and len(ents) == 2
    assert stats["mentions"] == len(mentions) == 3  # 2x Rivera-forms + 1x Acme
    for m in mentions:
        assert TEXT[m.start_offset:m.end_offset] == m.surface_text


def test_attaches_to_existing_and_appends_alias():
    existing = Entity(id=uuid.uuid4(), production_id=1, entity_type="person",
                      canonical_name="Jorge Rivera", aliases=[], attributes={}, mention_count=5)
    db = _session_with_existing([existing])
    parsed = _parsed(entities=[{"name": "jorge rivera", "type": "person",
                                "surface_forms": ["Rivera"], "role": None, "emails": []}])
    asyncio.run(persist_extraction(db, 1, DOC_ID, TEXT, parsed))
    assert not [o for o in db.added if isinstance(o, Entity)]  # no new entity
    assert "Rivera" in existing.aliases
    # "Rivera" occurs twice in TEXT (inside "Jorge Rivera" and standalone) — with
    # only the bare form supplied, both count: 5 + 2.
    assert existing.mention_count == 7


def test_borderline_creates_entity_plus_suggestion():
    existing = Entity(id=uuid.uuid4(), production_id=1, entity_type="person",
                      canonical_name="J. Rivera", aliases=[], attributes={}, mention_count=1)
    db = _session_with_existing([existing])
    parsed = _parsed(entities=[{"name": "Jorge Rivera", "type": "person",
                                "surface_forms": ["Jorge Rivera"], "role": None, "emails": []}])
    stats = asyncio.run(persist_extraction(db, 1, DOC_ID, TEXT, parsed))
    assert stats["entities"] == 1 and stats["suggestions"] == 1
    sugg = [o for o in db.added if isinstance(o, EntityMergeSuggestion)]
    assert len(sugg) == 1 and sugg[0].status == "pending"


def test_events_and_relationships_link_created_entities():
    db = _session_with_existing([])
    parsed = _parsed(
        entities=[
            {"name": "Jorge Rivera", "type": "person", "surface_forms": ["Jorge Rivera"], "role": None, "emails": []},
            {"name": "Acme Corp", "type": "org", "surface_forms": ["Acme Corp"], "role": None, "emails": []},
        ],
        events=[{"description": "Merger discussion", "type": "communication", "date": "2019-03",
                 "participants": ["Jorge Rivera", "Acme Corp", "Nobody Known"]}],
        relationships=[{"source": "Jorge Rivera", "target": "Acme Corp", "type": "employment", "evidence": "sig"}],
    )
    stats = asyncio.run(persist_extraction(db, 1, DOC_ID, TEXT, parsed))
    events = [o for o in db.added if isinstance(o, OntologyEvent)]
    edges = [o for o in db.added if isinstance(o, EntityRelationship)]
    assert stats["events"] == 1 and events[0].date_precision == "month"
    assert len(events[0].participants) == 2  # unknown participant skipped
    assert stats["relationships"] == 1 and edges[0].relationship_type == "employment"


def test_header_candidates_from_email_metadata():
    class Doc:
        email_from = "Jorge Rivera <jr@acme.com>"
        email_to = "ana@firm.law"
        email_cc = None
        email_bcc = None
    cands = header_candidates(Doc())
    names = {c["name"] for c in cands}
    assert "Jorge Rivera" in names and "ana" in names  # bare address falls back to local-part
    assert all(c["type"] == "person" for c in cands)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend; python -m pytest tests/test_entity_persist.py -q`
Expected: FAIL — `ImportError: cannot import name 'persist_extraction'`

- [ ] **Step 3: Implement — append to `backend/app/services/entity_extraction.py`**

```python
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
    union of surface forms/emails; events and relationships concatenate."""
    by_key: dict = {}
    events, relationships = [], []
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
        events.extend(r["events"])
        relationships.extend(r["relationships"])
    return {"entities": list(by_key.values()), "events": events, "relationships": relationships}


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
        for m in mentions:
            db.add(EntityMention(production_id=production_id, entity_id=entity.id,
                                 document_id=document_id, **m))
        entity.mention_count = (entity.mention_count or 0) + len(mentions)
        stats["mentions"] += len(mentions)

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

    for rel in parsed["relationships"]:
        src = name_to_entity.get(rel["source"].lower())
        tgt = name_to_entity.get(rel["target"].lower())
        if src is None or tgt is None or src.id == tgt.id:
            continue
        db.add(EntityRelationship(production_id=production_id, source_entity_id=src.id,
                                  target_entity_id=tgt.id, relationship_type=rel["type"],
                                  description=rel["evidence"], document_id=document_id))
        stats["relationships"] += 1

    return stats
```

Note for the `EventParticipant` construction: `event.participants = [...]` relies on the relationship cascade; participants get added with the event. The FakeSession `add` only tracks the event — the test asserts `events[0].participants` length directly, which works in-memory.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend; python -m pytest tests/test_entity_persist.py tests/test_entity_extraction.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/entity_extraction.py backend/tests/test_entity_persist.py
git commit -m "feat(ontology): persist extraction — entities, mentions, events, edges, suggestions"
```

---

### Task 5: Pipeline stage + backfill endpoint

**Files:**
- Modify: `backend/app/services/pipeline.py` (add `"entities"` stage)
- Modify: `backend/app/routers/ingest.py` (add `POST /api/productions/{production_id}/extract-entities`)
- Test: `backend/tests/test_entity_pipeline.py`; Modify: `backend/tests/test_pipeline.py` (STAGES change)

**Interfaces:**
- Consumes: `extract_document_entities`, `persist_extraction`, `header_candidates`, `merge_parsed` (Task 4); `stages_to_run`/`_STAGE_RUNNERS` structure in `pipeline.py`; `enqueue_pipeline` from `services/tasks.py`.
- Produces: pipeline stage name `"entities"` (between `"summaries"` and `"brief"`); endpoint `POST /api/productions/{id}/extract-entities` (manager+) returning `{"status": "enqueued"|"started"}`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_entity_pipeline.py
"""Entity extraction stage: batching, idempotency marker, per-doc failure
isolation, stop-on-fully-failed-batch (mirrors the summaries stage contract)."""

import asyncio
import uuid
from datetime import datetime

import app.services.pipeline as pipeline


class FakeDoc:
    def __init__(self, text="Jorge Rivera wrote this."):
        self.id = uuid.uuid4()
        self.production_id = 1
        self.text_content = text
        self.email_from = None
        self.email_to = None
        self.email_cc = None
        self.email_bcc = None
        self.entities_extracted_at = None


def test_entities_stage_registered_between_summaries_and_brief():
    assert pipeline.STAGES == ("clustering", "summaries", "entities", "brief")
    assert "entities" in pipeline._STAGE_RUNNERS


class FakeBatchSession:
    """Async-context session that serves docs by id (the runner re-fetches
    each doc inside a fresh session before extracting)."""

    def __init__(self, docs):
        self._docs = {d.id: d for d in docs}

    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, model, key): return self._docs.get(key)
    async def commit(self): pass


def test_run_entities_marks_docs_and_stops_when_none_left(monkeypatch):
    docs = [FakeDoc(), FakeDoc()]
    batches = [docs, []]  # first select returns docs, second returns none

    async def fake_pending(production_id, limit):
        return batches.pop(0)

    async def fake_extract_one(db, doc):
        doc.entities_extracted_at = datetime(2026, 7, 22)
        return True

    monkeypatch.setattr(pipeline, "_pending_extraction_docs", fake_pending)
    monkeypatch.setattr(pipeline, "_extract_one_document", fake_extract_one)
    monkeypatch.setattr(pipeline, "async_session", lambda: FakeBatchSession(docs))
    asyncio.run(pipeline._run_entities(1))
    assert all(d.entities_extracted_at for d in docs)


def test_run_entities_raises_after_fully_failed_batch(monkeypatch):
    import pytest
    calls = {"n": 0}
    doc = FakeDoc()

    async def fake_pending(production_id, limit):
        calls["n"] += 1
        return [doc]  # same doc forever — must not spin

    async def fake_extract_one(db, d):
        return False  # extraction failed; doc left unmarked

    monkeypatch.setattr(pipeline, "_pending_extraction_docs", fake_pending)
    monkeypatch.setattr(pipeline, "_extract_one_document", fake_extract_one)
    monkeypatch.setattr(pipeline, "async_session", lambda: FakeBatchSession([doc]))
    with pytest.raises(RuntimeError):
        asyncio.run(pipeline._run_entities(1))
    assert calls["n"] == 1  # gave up after one all-failed batch (stage marked failed by the pipeline wrapper)
```

Also update `backend/tests/test_pipeline.py`: any assertion pinning `STAGES` or stage counts must now include `"entities"` (read the file; adjust the expected tuple/list to `("clustering", "summaries", "entities", "brief")`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend; python -m pytest tests/test_entity_pipeline.py -q`
Expected: FAIL — STAGES mismatch / missing `_run_entities`

- [ ] **Step 3: Implement the stage in `backend/app/services/pipeline.py`**

Change the constant:

```python
STAGES = ("clustering", "summaries", "entities", "brief")
ENTITY_BATCH_SIZE = 10
```

Add after `_run_summaries`:

```python
async def _pending_extraction_docs(production_id: int, limit: int):
    async with async_session() as db:
        return (
            await db.execute(
                select(Document)
                .where(
                    Document.production_id == production_id,
                    Document.entities_extracted_at.is_(None),
                    Document.text_content.isnot(None),
                )
                .order_by(Document.bates_begin)
                .limit(limit)
            )
        ).scalars().all()


async def _extract_one_document(db, doc) -> bool:
    """Extract + persist one document. True = marked done. False = left
    unmarked for a later pipeline run (LLM failure). Never raises."""
    from datetime import datetime, timezone
    from app.services.entity_extraction import (
        extract_document_entities, header_candidates, merge_parsed, persist_extraction,
    )
    try:
        parsed = await extract_document_entities(doc.text_content or "")
        if parsed is None:
            return False
        headers = header_candidates(doc)
        if headers:
            parsed = merge_parsed([parsed, {"entities": headers, "events": [], "relationships": []}])
        await persist_extraction(db, doc.production_id, doc.id, doc.text_content or "", parsed)
        doc.entities_extracted_at = datetime.now(timezone.utc).replace(tzinfo=None)
        return True
    except Exception:
        logger.exception("Entity extraction failed for document %s", doc.id)
        return False


async def _run_entities(production_id: int) -> None:
    """Extract entities for documents not yet processed, in small batches,
    one commit per document. A batch where every document fails aborts the
    stage (rather than spinning); unmarked docs retry on the next run."""
    while True:
        pending = await _pending_extraction_docs(production_id, ENTITY_BATCH_SIZE)
        if not pending:
            return
        any_ok = False
        for doc in pending:
            async with async_session() as db:
                live = await db.get(Document, doc.id)
                if live is None or live.entities_extracted_at is not None:
                    continue
                if await _extract_one_document(db, live):
                    any_ok = True
                    await db.commit()
        if not any_ok:
            raise RuntimeError("entity extraction: entire batch failed (no API key or persistent errors)")
```

Register it:

```python
_STAGE_RUNNERS = {
    "clustering": _run_clustering,
    "summaries": _run_summaries,
    "entities": _run_entities,
    "brief": _run_brief,
}
```

(Raising from `_run_entities` marks the stage `"failed"` in `ai_pipeline_status` via the existing wrapper — visible, retryable, non-poisoning.)

- [ ] **Step 4: Add the backfill endpoint in `backend/app/routers/ingest.py`**

Add after `run_pipeline_handler`:

```python
@router.post("/productions/{production_id}/extract-entities")
async def trigger_entity_extraction(
    production_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Backfill/manual trigger: run the ambient pipeline (which now includes
    the entities stage) for this production. Manager or admin only."""
    from app.dependencies import ROLE_RANK, get_user_role_for_production
    role = await get_user_role_for_production(db, user, production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or admin role required")

    from app.services import tasks as task_service
    if task_service.is_configured():
        task_service.enqueue_pipeline(production_id)
        return {"status": "enqueued"}

    from app.services.pipeline import run_ambient_pipeline
    background_tasks.add_task(run_ambient_pipeline, production_id)
    return {"status": "started"}
```

- [ ] **Step 5: Run tests**

Run: `cd backend; python -m pytest tests/test_entity_pipeline.py tests/test_pipeline.py tests/test_pipeline_staleness.py -q`
Expected: all pass (after the `test_pipeline.py` STAGES fixup)

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/pipeline.py backend/app/routers/ingest.py backend/tests/test_entity_pipeline.py backend/tests/test_pipeline.py
git commit -m "feat(ontology): entities pipeline stage + backfill trigger endpoint"
```

---

### Task 6: Read API — document entities, profile, mentions, connections, matter list

**Files:**
- Create: `backend/app/routers/entities.py`
- Create: `backend/app/services/entity_profile.py`
- Modify: `backend/app/schemas.py` (append ontology schemas)
- Modify: `backend/app/main.py` (mount router — follow how existing routers are included there)
- Test: `backend/tests/test_entity_endpoints.py`, `backend/tests/test_entity_profile.py`

**Interfaces:**
- Consumes: Task 1 models; `get_current_user`, `get_accessible_production_ids`, `get_user_role_for_production`; Haiku client pattern from `services/ai.py`.
- Produces:
  - `GET /api/documents/{doc_id}/entities` → `{"entities": [DocEntityOut]}` where `DocEntityOut = {id, entity_type, canonical_name, mention_count, mentions: [{surface_text, start_offset, end_offset}]}`
  - `GET /api/entities/{entity_id}` → `EntityProfileOut = {id, production_id, entity_type, canonical_name, aliases, attributes, overview, mention_count, document_count}`
  - `GET /api/entities/{entity_id}/mentions?page=&per_page=` → `{"documents": [{document_id, bates_begin, title, mentions: [{surface_text, context_snippet, start_offset}]}], "total": n}`
  - `GET /api/entities/{entity_id}/connections` → `{"stated": [...], "cooccurrence": [...], "shared_events": [...]}`
  - `GET /api/productions/{production_id}/entities?search=&entity_type=&page=&per_page=` → `{"entities": [...], "total": n}`
  - `services/entity_profile.py`: `is_overview_stale(entity) -> bool` (pure), `async generate_entity_overview(db, entity) -> str | None`

- [ ] **Step 1: Write failing tests for the pure/staleness parts and key endpoints**

```python
# backend/tests/test_entity_profile.py
from app.services.entity_profile import is_overview_stale


class E:
    def __init__(self, overview=None, gen_count=None, count=0):
        self.overview = overview
        self.overview_mention_count = gen_count
        self.mention_count = count


def test_no_overview_is_stale():
    assert is_overview_stale(E(overview=None, count=3))


def test_fresh_overview_not_stale():
    assert not is_overview_stale(E(overview="x", gen_count=10, count=12))


def test_growth_by_ratio_is_stale():
    assert is_overview_stale(E(overview="x", gen_count=10, count=15))  # 1.5x


def test_growth_by_absolute_is_stale():
    assert is_overview_stale(E(overview="x", gen_count=100, count=110))  # +10
```

```python
# backend/tests/test_entity_endpoints.py
"""Fake-session tests for the entities read API: scoping + shapes."""

import asyncio
import uuid

import pytest
from fastapi import HTTPException

import app.routers.entities as er
from app.models import Entity
from tests.fakes import FakeResult, FakeSession, FakeUser


ENT_ID = uuid.uuid4()


def _entity(production_id=1):
    return Entity(id=ENT_ID, production_id=production_id, entity_type="person",
                  canonical_name="Jorge Rivera", aliases=["J. Rivera"], attributes={},
                  overview="Existing overview", overview_mention_count=100, mention_count=100)


def _patch(monkeypatch, accessible=(1,)):
    async def fake_accessible(db, user):
        return list(accessible)
    monkeypatch.setattr(er, "get_accessible_production_ids", fake_accessible)


def test_get_entity_denies_out_of_scope(monkeypatch):
    _patch(monkeypatch, accessible=(2,))  # entity is in production 1
    db = FakeSession(get_objects={("Entity", ENT_ID): _entity(production_id=1)})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.get_entity(entity_id=ENT_ID, db=db, user=FakeUser()))
    assert exc.value.status_code == 404


def test_get_entity_returns_profile_without_regenerating_fresh_overview(monkeypatch):
    _patch(monkeypatch)
    called = {"gen": False}

    async def fake_generate(db, entity):
        called["gen"] = True
        return "new overview"
    monkeypatch.setattr(er, "generate_entity_overview", fake_generate)
    db = FakeSession(
        get_objects={("Entity", ENT_ID): _entity()},
        responders=[("count", FakeResult(scalar=7))],
    )
    out = asyncio.run(er.get_entity(entity_id=ENT_ID, db=db, user=FakeUser()))
    assert out.canonical_name == "Jorge Rivera"
    assert out.overview == "Existing overview"
    assert called["gen"] is False  # fresh — no regeneration


def test_document_entities_denies_unknown_document(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession()  # no document
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.get_document_entities(doc_id=uuid.uuid4(), db=db, user=FakeUser()))
    assert exc.value.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend; python -m pytest tests/test_entity_profile.py tests/test_entity_endpoints.py -q`
Expected: FAIL — modules not found

- [ ] **Step 3: Implement `backend/app/services/entity_profile.py`**

```python
"""Lazy, cached entity overview generation (Haiku)."""

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import settings

logger = logging.getLogger(__name__)

PROFILE_MODEL = "claude-haiku-4-5"
_STALE_RATIO = 1.5
_STALE_ABSOLUTE = 10
_MAX_SNIPPETS = 20


def is_overview_stale(entity) -> bool:
    """Spec rule: regenerate when there is no overview, or mention_count has
    reached 1.5x the count at generation time, or grown by >= 10 since."""
    if not entity.overview or entity.overview_mention_count is None:
        return True
    grown = (entity.mention_count or 0) - entity.overview_mention_count
    return (entity.mention_count or 0) >= _STALE_RATIO * entity.overview_mention_count or grown >= _STALE_ABSOLUTE


async def generate_entity_overview(db, entity) -> str | None:
    """Synthesize a short 'who is this' overview from mentions + edges.
    Returns None on failure — the caller renders the profile without one."""
    if not settings.anthropic_api_key:
        return None
    from app.models import Entity, EntityMention, EntityRelationship

    snippets = (await db.execute(
        select(EntityMention.context_snippet)
        .where(EntityMention.entity_id == entity.id, EntityMention.context_snippet.isnot(None))
        .limit(_MAX_SNIPPETS)
    )).scalars().all()

    edge_rows = (await db.execute(
        select(EntityRelationship, Entity.canonical_name)
        .join(Entity, Entity.id == EntityRelationship.target_entity_id)
        .where(EntityRelationship.source_entity_id == entity.id)
        .limit(10)
    )).all()
    edges = [f"{rel.relationship_type} -> {name}: {rel.description or ''}" for rel, name in edge_rows]

    role = (entity.attributes or {}).get("role")
    prompt = f"""Based ONLY on the excerpts below from a legal document collection, write a 2-4 sentence factual overview of {entity.canonical_name} ({entity.entity_type}): who they are, their role, and how they figure in these documents. No speculation; if the excerpts are thin, say what little is known.

Known role: {role or "unknown"}
Known relationships: {"; ".join(edges) or "none recorded"}

Excerpts:
{chr(10).join("- " + (s or "")[:300] for s in snippets)}

Respond with ONLY the overview text."""
    try:
        import anthropic  # lazy
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        response = await client.messages.create(
            model=PROFILE_MODEL, max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        text = next((b.text for b in response.content if b.type == "text"), "").strip()
        if not text:
            return None
        entity.overview = text
        entity.overview_generated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        entity.overview_mention_count = entity.mention_count
        return text
    except Exception as e:
        logger.warning("Overview generation failed for entity %s: %s", entity.id, e)
        return None
```

- [ ] **Step 4: Append ontology schemas to `backend/app/schemas.py`**

```python
# ── Ontology ──

class MentionSpanOut(BaseModel):
    surface_text: str
    start_offset: int | None
    end_offset: int | None


class DocEntityOut(BaseModel):
    id: UUID4
    entity_type: str
    canonical_name: str
    mention_count: int
    mentions: list[MentionSpanOut]


class DocumentEntitiesOut(BaseModel):
    entities: list[DocEntityOut]


class EntityProfileOut(BaseModel):
    id: UUID4
    production_id: int
    entity_type: str
    canonical_name: str
    aliases: list[str]
    attributes: dict
    overview: str | None
    mention_count: int
    document_count: int


class EntityDocMentionOut(BaseModel):
    surface_text: str
    context_snippet: str | None
    start_offset: int | None


class EntityDocumentMentionsOut(BaseModel):
    document_id: UUID4
    bates_begin: str
    title: str | None
    mentions: list[EntityDocMentionOut]


class EntityMentionsPageOut(BaseModel):
    documents: list[EntityDocumentMentionsOut]
    total: int


class EntityConnectionOut(BaseModel):
    entity_id: UUID4
    canonical_name: str
    entity_type: str
    relationship_type: str | None = None
    description: str | None = None
    document_id: UUID4 | None = None
    shared_doc_count: int | None = None


class SharedEventOut(BaseModel):
    event_id: int
    description: str
    event_type: str
    event_date: str | None
    document_id: UUID4


class EntityConnectionsOut(BaseModel):
    stated: list[EntityConnectionOut]
    cooccurrence: list[EntityConnectionOut]
    shared_events: list[SharedEventOut]


class EntityListItemOut(BaseModel):
    id: UUID4
    entity_type: str
    canonical_name: str
    mention_count: int
    document_count: int


class EntityListPageOut(BaseModel):
    entities: list[EntityListItemOut]
    total: int
```

(Match the file's existing import style — it already imports `BaseModel`; add `UUID4` to the pydantic import if not present.)

- [ ] **Step 5: Implement `backend/app/routers/entities.py`**

```python
"""Ontology read API: document entities, profiles, mentions, connections."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_accessible_production_ids
from app.models import (
    Document, Entity, EntityMention, EntityRelationship,
    EventParticipant, OntologyEvent, User,
)
from app.routers.auth import get_current_user
from app.schemas import (
    DocEntityOut, DocumentEntitiesOut, EntityConnectionOut, EntityConnectionsOut,
    EntityDocMentionOut, EntityDocumentMentionsOut, EntityListItemOut,
    EntityListPageOut, EntityMentionsPageOut, EntityProfileOut, MentionSpanOut,
    SharedEventOut,
)
from app.services.entity_profile import generate_entity_overview, is_overview_stale

router = APIRouter(prefix="/api", tags=["entities"])


async def _get_scoped_entity(db: AsyncSession, user: User, entity_id: UUID) -> Entity:
    accessible = await get_accessible_production_ids(db, user)
    entity = await db.get(Entity, entity_id)
    if entity is None or entity.production_id not in accessible:
        raise HTTPException(status_code=404, detail="Entity not found")
    return entity


@router.get("/documents/{doc_id}/entities", response_model=DocumentEntitiesOut)
async def get_document_entities(
    doc_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    doc = await db.get(Document, doc_id)
    if not doc or doc.production_id not in accessible:
        raise HTTPException(status_code=404, detail="Document not found")

    rows = (await db.execute(
        select(EntityMention, Entity)
        .join(Entity, EntityMention.entity_id == Entity.id)
        .where(EntityMention.document_id == doc_id)
        .order_by(EntityMention.start_offset)
    )).all()

    by_entity: dict = {}
    for mention, entity in rows:
        item = by_entity.setdefault(entity.id, DocEntityOut(
            id=entity.id, entity_type=entity.entity_type,
            canonical_name=entity.canonical_name,
            mention_count=entity.mention_count, mentions=[],
        ))
        item.mentions.append(MentionSpanOut(
            surface_text=mention.surface_text,
            start_offset=mention.start_offset, end_offset=mention.end_offset,
        ))
    return DocumentEntitiesOut(entities=list(by_entity.values()))


@router.get("/entities/{entity_id}", response_model=EntityProfileOut)
async def get_entity(
    entity_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    entity = await _get_scoped_entity(db, user, entity_id)

    doc_count = (await db.execute(
        select(func.count(func.distinct(EntityMention.document_id)))
        .where(EntityMention.entity_id == entity_id)
    )).scalar() or 0

    overview = entity.overview
    if is_overview_stale(entity):
        generated = await generate_entity_overview(db, entity)
        if generated is not None:
            overview = generated
            await db.commit()

    return EntityProfileOut(
        id=entity.id, production_id=entity.production_id,
        entity_type=entity.entity_type, canonical_name=entity.canonical_name,
        aliases=list(entity.aliases or []), attributes=dict(entity.attributes or {}),
        overview=overview, mention_count=entity.mention_count, document_count=doc_count,
    )


@router.get("/entities/{entity_id}/mentions", response_model=EntityMentionsPageOut)
async def get_entity_mentions(
    entity_id: UUID,
    page: int = 1,
    per_page: int = 20,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _get_scoped_entity(db, user, entity_id)
    per_page = max(1, min(50, per_page))

    total = (await db.execute(
        select(func.count(func.distinct(EntityMention.document_id)))
        .where(EntityMention.entity_id == entity_id)
    )).scalar() or 0

    doc_ids = (await db.execute(
        select(EntityMention.document_id, Document.bates_begin)
        .join(Document, EntityMention.document_id == Document.id)
        .where(EntityMention.entity_id == entity_id)
        .group_by(EntityMention.document_id, Document.bates_begin)
        .order_by(Document.bates_begin)
        .offset((max(1, page) - 1) * per_page)
        .limit(per_page)
    )).all()

    documents = []
    for document_id, _bates in doc_ids:
        doc = await db.get(Document, document_id)
        mention_rows = (await db.execute(
            select(EntityMention)
            .where(EntityMention.entity_id == entity_id, EntityMention.document_id == document_id)
            .order_by(EntityMention.start_offset)
            .limit(20)
        )).scalars().all()
        documents.append(EntityDocumentMentionsOut(
            document_id=document_id, bates_begin=doc.bates_begin, title=doc.title,
            mentions=[EntityDocMentionOut(surface_text=m.surface_text,
                                          context_snippet=m.context_snippet,
                                          start_offset=m.start_offset) for m in mention_rows],
        ))
    return EntityMentionsPageOut(documents=documents, total=total)


@router.get("/entities/{entity_id}/connections", response_model=EntityConnectionsOut)
async def get_entity_connections(
    entity_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _get_scoped_entity(db, user, entity_id)

    stated = []
    for direction_col, other_col in (
        (EntityRelationship.source_entity_id, EntityRelationship.target_entity_id),
        (EntityRelationship.target_entity_id, EntityRelationship.source_entity_id),
    ):
        rows = (await db.execute(
            select(EntityRelationship, Entity)
            .join(Entity, Entity.id == other_col)
            .where(direction_col == entity_id)
            .limit(50)
        )).all()
        for rel, other in rows:
            stated.append(EntityConnectionOut(
                entity_id=other.id, canonical_name=other.canonical_name,
                entity_type=other.entity_type, relationship_type=rel.relationship_type,
                description=rel.description, document_id=rel.document_id,
            ))

    em_self = EntityMention.__table__.alias("em_self")
    em_other = EntityMention.__table__.alias("em_other")
    cooc_rows = (await db.execute(
        select(em_other.c.entity_id, func.count(func.distinct(em_other.c.document_id)).label("shared"))
        .select_from(em_self.join(em_other, em_self.c.document_id == em_other.c.document_id))
        .where(em_self.c.entity_id == entity_id, em_other.c.entity_id != entity_id)
        .group_by(em_other.c.entity_id)
        .order_by(func.count(func.distinct(em_other.c.document_id)).desc())
        .limit(10)
    )).all()
    cooccurrence = []
    for other_id, shared in cooc_rows:
        other = await db.get(Entity, other_id)
        if other is not None:
            cooccurrence.append(EntityConnectionOut(
                entity_id=other.id, canonical_name=other.canonical_name,
                entity_type=other.entity_type, shared_doc_count=shared,
            ))

    event_rows = (await db.execute(
        select(OntologyEvent)
        .join(EventParticipant, EventParticipant.event_id == OntologyEvent.id)
        .where(EventParticipant.entity_id == entity_id)
        .order_by(OntologyEvent.event_date.desc().nullslast())
        .limit(20)
    )).scalars().all()
    shared_events = [SharedEventOut(
        event_id=e.id, description=e.description, event_type=e.event_type,
        event_date=e.event_date.isoformat() if e.event_date else None,
        document_id=e.document_id,
    ) for e in event_rows]

    return EntityConnectionsOut(stated=stated, cooccurrence=cooccurrence, shared_events=shared_events)


@router.get("/productions/{production_id}/entities", response_model=EntityListPageOut)
async def list_production_entities(
    production_id: int,
    search: str | None = None,
    entity_type: str | None = None,
    page: int = 1,
    per_page: int = 50,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=404, detail="Production not found")
    per_page = max(1, min(100, per_page))

    query = select(Entity).where(Entity.production_id == production_id)
    count_query = select(func.count(Entity.id)).where(Entity.production_id == production_id)
    if entity_type in ("person", "org"):
        query = query.where(Entity.entity_type == entity_type)
        count_query = count_query.where(Entity.entity_type == entity_type)
    if search:
        pattern = f"%{search}%"
        query = query.where(Entity.canonical_name.ilike(pattern))
        count_query = count_query.where(Entity.canonical_name.ilike(pattern))

    total = (await db.execute(count_query)).scalar() or 0
    rows = (await db.execute(
        query.order_by(Entity.mention_count.desc())
        .offset((max(1, page) - 1) * per_page).limit(per_page)
    )).scalars().all()

    out = []
    for e in rows:
        doc_count = (await db.execute(
            select(func.count(func.distinct(EntityMention.document_id)))
            .where(EntityMention.entity_id == e.id)
        )).scalar() or 0
        out.append(EntityListItemOut(id=e.id, entity_type=e.entity_type,
                                     canonical_name=e.canonical_name,
                                     mention_count=e.mention_count, document_count=doc_count))
    return EntityListPageOut(entities=out, total=total)
```

Mount in `backend/app/main.py` alongside the other routers (find where `intelligence.router` is included and mirror it):

```python
from app.routers import entities as entities_router
app.include_router(entities_router.router)
```

- [ ] **Step 6: Run tests**

Run: `cd backend; python -m pytest tests/test_entity_profile.py tests/test_entity_endpoints.py -q`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add backend/app/routers/entities.py backend/app/services/entity_profile.py backend/app/schemas.py backend/app/main.py backend/tests/test_entity_profile.py backend/tests/test_entity_endpoints.py
git commit -m "feat(ontology): entities read API — doc entities, profile, mentions, connections, matter list"
```

---

### Task 7: Merge workflow — suggestions, manual merge, undo

**Files:**
- Create: `backend/app/services/entity_merge.py`
- Modify: `backend/app/routers/entities.py` (append endpoints)
- Modify: `backend/app/schemas.py` (append merge schemas)
- Test: `backend/tests/test_entity_merge.py`

**Interfaces:**
- Consumes: Task 1 models; Task 6 router/scoping helpers; `log_action` from `services/audit.py`.
- Produces:
  - `async merge_entities(db, winner: Entity, loser: Entity, user_id: str) -> EntityMerge` — re-points mentions/edges/participants, folds aliases + counts, snapshots loser, deletes loser, resolves pending suggestions between the pair. No commit.
  - `async undo_merge(db, merge: EntityMerge) -> Entity` — restores loser from snapshot, re-points moved rows back, restores winner's prior aliases/count; raises `ValueError` if not undoable. No commit.
  - Endpoints: `GET /api/productions/{id}/merge-suggestions?status=`, `POST /api/merge-suggestions/{id}/accept`, `POST /api/merge-suggestions/{id}/reject`, `POST /api/entities/merge` (body `{winner_id, loser_id}`), `POST /api/entity-merges/{merge_id}/undo`. All reject `readonly`.
  - Schemas: `MergeSuggestionOut = {id, score, rationale, status, entity_a: EntityListItemOut, entity_b: EntityListItemOut}`, `MergeRequest = {winner_id: UUID4, loser_id: UUID4}`, `MergeResultOut = {merge_id: int, winner_id: UUID4}`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_entity_merge.py
"""Merge/undo round-trip against a FakeSession."""

import asyncio
import uuid

import pytest

from app.models import Entity, EntityMention, EntityMerge
from app.services.entity_merge import merge_entities, undo_merge
from tests.fakes import FakeResult, FakeSession


def _pair():
    winner = Entity(id=uuid.uuid4(), production_id=1, entity_type="person",
                    canonical_name="Jorge Rivera", aliases=["J. Rivera"], attributes={}, mention_count=10)
    loser = Entity(id=uuid.uuid4(), production_id=1, entity_type="person",
                   canonical_name="J Rivera", aliases=["JR"], attributes={}, mention_count=4)
    return winner, loser


def _mentions_for(loser, n=2):
    return [EntityMention(id=100 + i, production_id=1, entity_id=loser.id,
                          document_id=uuid.uuid4(), surface_text="J Rivera",
                          start_offset=i, end_offset=i + 8) for i in range(n)]


def _db(loser_mentions):
    return FakeSession(responders=[
        ("entity_mentions", FakeResult(items=loser_mentions)),
        ("entity_relationships", FakeResult(items=[])),
        ("event_participants", FakeResult(items=[])),
        ("entity_merge_suggestions", FakeResult(items=[])),
    ])


def test_merge_repoints_mentions_and_folds_counts():
    winner, loser = _pair()
    mentions = _mentions_for(loser)
    db = _db(mentions)
    merge = asyncio.run(merge_entities(db, winner, loser, "u1"))
    assert all(m.entity_id == winner.id for m in mentions)
    assert winner.mention_count == 14
    assert "J Rivera" in winner.aliases and "JR" in winner.aliases
    assert merge.loser_snapshot["canonical_name"] == "J Rivera"
    assert merge.moved_mention_ids == [100, 101]
    assert loser in db.deleted


def test_merge_rejects_cross_production():
    winner, loser = _pair()
    loser.production_id = 2
    with pytest.raises(ValueError):
        asyncio.run(merge_entities(_db([]), winner, loser, "u1"))


def test_undo_restores_loser_and_repoints():
    winner, loser = _pair()
    mentions = _mentions_for(loser)
    db = _db(mentions)
    merge = asyncio.run(merge_entities(db, winner, loser, "u1"))
    # undo: mentions are looked up by moved ids
    db2 = FakeSession(
        get_objects={("Entity", winner.id): winner},
        responders=[("entity_mentions", FakeResult(items=mentions)),
                    ("entity_relationships", FakeResult(items=[])),
                    ("event_participants", FakeResult(items=[]))],
    )
    restored = asyncio.run(undo_merge(db2, merge))
    assert restored.canonical_name == "J Rivera"
    assert all(m.entity_id == restored.id for m in mentions)
    assert winner.mention_count == 10
    assert winner.aliases == ["J. Rivera"]
    assert merge.undone is True


def test_undo_twice_raises():
    winner, loser = _pair()
    db = _db(_mentions_for(loser))
    merge = asyncio.run(merge_entities(db, winner, loser, "u1"))
    merge.undone = True
    with pytest.raises(ValueError):
        asyncio.run(undo_merge(FakeSession(get_objects={("Entity", winner.id): winner}), merge))
```

**Note:** `FakeSession` needs a `delete` tracker — `tests/fakes.py`'s `FakeSession.delete` currently passes silently; change it to append to a `self.deleted` list initialized in `__init__` (mirror `self.added`). That is a small additive change to `tests/fakes.py`; existing tests don't assert on it.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend; python -m pytest tests/test_entity_merge.py -q`
Expected: FAIL — module not found

- [ ] **Step 3: Implement `backend/app/services/entity_merge.py`**

```python
"""Reversible entity merge: re-point provenance to the winner, snapshot the
loser, log everything needed for a mechanical undo."""

import uuid as _uuid

from sqlalchemy import select

from app.models import (
    Entity, EntityMention, EntityMerge, EntityMergeSuggestion,
    EntityRelationship, EventParticipant,
)


def _snapshot(entity: Entity) -> dict:
    return {
        "id": str(entity.id), "production_id": entity.production_id,
        "entity_type": entity.entity_type, "canonical_name": entity.canonical_name,
        "aliases": list(entity.aliases or []), "attributes": dict(entity.attributes or {}),
        "overview": entity.overview, "mention_count": entity.mention_count,
    }


async def merge_entities(db, winner: Entity, loser: Entity, user_id: str) -> EntityMerge:
    if winner.id == loser.id:
        raise ValueError("Cannot merge an entity into itself")
    if winner.production_id != loser.production_id:
        raise ValueError("Entities belong to different productions")
    if winner.entity_type != loser.entity_type:
        raise ValueError("Entities are of different types")

    merge = EntityMerge(
        production_id=winner.production_id, winner_entity_id=winner.id,
        loser_snapshot=_snapshot(loser),
        winner_prior={"aliases": list(winner.aliases or []), "mention_count": winner.mention_count},
        moved_mention_ids=[], moved_relationship_ids=[], moved_participant_ids=[],
        merged_by=user_id,
    )

    mentions = (await db.execute(
        select(EntityMention).where(EntityMention.entity_id == loser.id)
    )).scalars().all()
    for m in mentions:
        m.entity_id = winner.id
        merge.moved_mention_ids.append(m.id)

    edges = (await db.execute(
        select(EntityRelationship).where(
            (EntityRelationship.source_entity_id == loser.id)
            | (EntityRelationship.target_entity_id == loser.id))
    )).scalars().all()
    for e in edges:
        if e.source_entity_id == loser.id:
            e.source_entity_id = winner.id
        if e.target_entity_id == loser.id:
            e.target_entity_id = winner.id
        if e.source_entity_id == e.target_entity_id:
            await db.delete(e)  # became a self-edge; drop (not restored on undo)
            continue
        merge.moved_relationship_ids.append(e.id)

    participants = (await db.execute(
        select(EventParticipant).where(EventParticipant.entity_id == loser.id)
    )).scalars().all()
    for p in participants:
        p.entity_id = winner.id
        merge.moved_participant_ids.append(p.id)

    suggestions = (await db.execute(
        select(EntityMergeSuggestion).where(
            ((EntityMergeSuggestion.entity_a_id == loser.id) | (EntityMergeSuggestion.entity_b_id == loser.id))
            & (EntityMergeSuggestion.status == "pending"))
    )).scalars().all()
    for s in suggestions:
        s.status = "accepted" if {s.entity_a_id, s.entity_b_id} == {winner.id, loser.id} else "rejected"
        s.resolved_by = user_id

    new_aliases = [loser.canonical_name] + list(loser.aliases or [])
    winner.aliases = list(winner.aliases or []) + [a for a in new_aliases if a not in (winner.aliases or [])]
    winner.mention_count = (winner.mention_count or 0) + (loser.mention_count or 0)

    await db.delete(loser)
    db.add(merge)
    return merge


async def undo_merge(db, merge: EntityMerge) -> Entity:
    if merge.undone:
        raise ValueError("Merge already undone")
    winner = await db.get(Entity, merge.winner_entity_id)
    if winner is None:
        raise ValueError("Winner entity no longer exists (merged again?) — cannot undo")

    snap = merge.loser_snapshot
    restored = Entity(
        id=_uuid.UUID(snap["id"]), production_id=snap["production_id"],
        entity_type=snap["entity_type"], canonical_name=snap["canonical_name"],
        aliases=snap["aliases"], attributes=snap["attributes"],
        overview=snap["overview"], mention_count=snap["mention_count"],
    )
    db.add(restored)

    if merge.moved_mention_ids:
        for m in (await db.execute(
            select(EntityMention).where(EntityMention.id.in_(merge.moved_mention_ids))
        )).scalars().all():
            m.entity_id = restored.id
    if merge.moved_relationship_ids:
        for e in (await db.execute(
            select(EntityRelationship).where(EntityRelationship.id.in_(merge.moved_relationship_ids))
        )).scalars().all():
            # restore whichever side(s) pointed at the loser originally is not
            # recorded per-edge; winner-side occurrences of winner.id that came
            # from this merge are exactly the moved ids, so flip those back.
            if e.source_entity_id == winner.id:
                e.source_entity_id = restored.id
            elif e.target_entity_id == winner.id:
                e.target_entity_id = restored.id
    if merge.moved_participant_ids:
        for p in (await db.execute(
            select(EventParticipant).where(EventParticipant.id.in_(merge.moved_participant_ids))
        )).scalars().all():
            p.entity_id = restored.id

    winner.aliases = merge.winner_prior["aliases"]
    winner.mention_count = merge.winner_prior["mention_count"]
    merge.undone = True
    return restored
```

- [ ] **Step 4: Append merge endpoints to `backend/app/routers/entities.py`**

Add schemas to `backend/app/schemas.py`:

```python
class MergeSuggestionOut(BaseModel):
    id: int
    score: float
    rationale: str
    status: str
    entity_a: EntityListItemOut
    entity_b: EntityListItemOut


class MergeRequest(BaseModel):
    winner_id: UUID4
    loser_id: UUID4


class MergeResultOut(BaseModel):
    merge_id: int
    winner_id: UUID4
```

Endpoints (append to `entities.py`; add imports `EntityMerge, EntityMergeSuggestion` from models, `MergeRequest, MergeResultOut, MergeSuggestionOut` from schemas, `get_user_role_for_production` from dependencies, `log_action` from `app.services.audit`, and `merge_entities, undo_merge` from `app.services.entity_merge`):

```python
async def _require_writer(db: AsyncSession, user: User, production_id: int) -> None:
    role = await get_user_role_for_production(db, user, production_id)
    if role == "readonly":
        raise HTTPException(status_code=403, detail="Read-only access")


async def _entity_list_item(db: AsyncSession, entity_id) -> EntityListItemOut | None:
    e = await db.get(Entity, entity_id)
    if e is None:
        return None
    doc_count = (await db.execute(
        select(func.count(func.distinct(EntityMention.document_id)))
        .where(EntityMention.entity_id == e.id)
    )).scalar() or 0
    return EntityListItemOut(id=e.id, entity_type=e.entity_type,
                             canonical_name=e.canonical_name,
                             mention_count=e.mention_count, document_count=doc_count)


@router.get("/productions/{production_id}/merge-suggestions", response_model=list[MergeSuggestionOut])
async def list_merge_suggestions(
    production_id: int,
    status: str = "pending",
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=404, detail="Production not found")
    rows = (await db.execute(
        select(EntityMergeSuggestion)
        .where(EntityMergeSuggestion.production_id == production_id,
               EntityMergeSuggestion.status == status)
        .order_by(EntityMergeSuggestion.score.desc())
        .limit(100)
    )).scalars().all()
    out = []
    for s in rows:
        a = await _entity_list_item(db, s.entity_a_id)
        b = await _entity_list_item(db, s.entity_b_id)
        if a and b:
            out.append(MergeSuggestionOut(id=s.id, score=s.score, rationale=s.rationale,
                                          status=s.status, entity_a=a, entity_b=b))
    return out


@router.post("/merge-suggestions/{suggestion_id}/accept", response_model=MergeResultOut)
async def accept_merge_suggestion(
    suggestion_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    sugg = await db.get(EntityMergeSuggestion, suggestion_id)
    accessible = await get_accessible_production_ids(db, user)
    if sugg is None or sugg.production_id not in accessible:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    if sugg.status != "pending":
        raise HTTPException(status_code=409, detail="Suggestion already resolved")
    await _require_writer(db, user, sugg.production_id)

    a = await db.get(Entity, sugg.entity_a_id)
    b = await db.get(Entity, sugg.entity_b_id)
    if a is None or b is None:
        raise HTTPException(status_code=409, detail="An entity in this pair no longer exists")
    # keep the one with more mentions as the winner
    winner, loser = (a, b) if (a.mention_count or 0) >= (b.mention_count or 0) else (b, a)
    try:
        merge = await merge_entities(db, winner, loser, user.id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    await db.flush()
    await log_action(db, user, "entity_merged", "entity", str(winner.id),
                     production_id=sugg.production_id,
                     details={"suggestion_id": suggestion_id, "loser": str(loser.id)})
    await db.commit()
    return MergeResultOut(merge_id=merge.id, winner_id=winner.id)


@router.post("/merge-suggestions/{suggestion_id}/reject")
async def reject_merge_suggestion(
    suggestion_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    sugg = await db.get(EntityMergeSuggestion, suggestion_id)
    accessible = await get_accessible_production_ids(db, user)
    if sugg is None or sugg.production_id not in accessible:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    if sugg.status != "pending":
        raise HTTPException(status_code=409, detail="Suggestion already resolved")
    await _require_writer(db, user, sugg.production_id)
    sugg.status = "rejected"
    sugg.resolved_by = user.id
    await db.commit()
    return {"ok": True}


@router.post("/entities/merge", response_model=MergeResultOut)
async def manual_merge(
    body: MergeRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    winner = await _get_scoped_entity(db, user, body.winner_id)
    loser = await _get_scoped_entity(db, user, body.loser_id)
    await _require_writer(db, user, winner.production_id)
    try:
        merge = await merge_entities(db, winner, loser, user.id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    await db.flush()
    await log_action(db, user, "entity_merged", "entity", str(winner.id),
                     production_id=winner.production_id, details={"loser": str(loser.id), "manual": True})
    await db.commit()
    return MergeResultOut(merge_id=merge.id, winner_id=winner.id)


@router.post("/entity-merges/{merge_id}/undo")
async def undo_entity_merge(
    merge_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    merge = await db.get(EntityMerge, merge_id)
    accessible = await get_accessible_production_ids(db, user)
    if merge is None or merge.production_id not in accessible:
        raise HTTPException(status_code=404, detail="Merge not found")
    await _require_writer(db, user, merge.production_id)
    try:
        restored = await undo_merge(db, merge)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    await log_action(db, user, "entity_merge_undone", "entity", str(restored.id),
                     production_id=merge.production_id, details={"merge_id": merge_id})
    await db.commit()
    return {"ok": True, "restored_entity_id": str(restored.id)}
```

- [ ] **Step 5: Run tests**

Run: `cd backend; python -m pytest tests/test_entity_merge.py tests/ -q`
Expected: all pass (full suite — the `fakes.py` change must not break other tests)

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/entity_merge.py backend/app/routers/entities.py backend/app/schemas.py backend/tests/test_entity_merge.py backend/tests/fakes.py
git commit -m "feat(ontology): merge workflow — suggestions accept/reject, manual merge, reversible undo"
```

---

### Task 8: Chat agent `lookup_entity` tool + PR 1

**Files:**
- Modify: `backend/app/services/ai_tools.py`
- Test: `backend/tests/test_ai_tools.py` (append)

**Interfaces:**
- Consumes: `TOOLS` list / `_DISPATCH` dict / `ToolRun` dataclass / `tool_use_summary` in `ai_tools.py`; `Entity`, `EntityMention`, `EntityRelationship` models.
- Produces: chat tool `lookup_entity` with input `{"name": str, "production_id"?: int}`.

- [ ] **Step 1: Write the failing test** (append to `backend/tests/test_ai_tools.py`, following that file's existing fake patterns — read it first and mirror how another tool is tested)

```python
def test_lookup_entity_registered():
    from app.services.ai_tools import TOOLS, _DISPATCH, tool_use_summary
    assert any(t["name"] == "lookup_entity" for t in TOOLS)
    assert "lookup_entity" in _DISPATCH
    assert "Jorge" in tool_use_summary("lookup_entity", {"name": "Jorge"})
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend; python -m pytest tests/test_ai_tools.py -q`
Expected: FAIL on the new test

- [ ] **Step 3: Implement in `backend/app/services/ai_tools.py`**

Append to `TOOLS`:

```python
    {
        "name": "lookup_entity",
        "description": (
            "Look up a person or organization in the case ontology by name. Returns "
            "their profile overview, aliases, how many documents mention them, their "
            "stated relationships, and sample mention snippets. Use this to answer "
            "'who is X' or 'how is X connected to Y'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Person or organization name (partial ok)."},
                "production_id": {"type": "integer", "description": "Optional: restrict to one production id."},
            },
            "required": ["name"],
        },
    },
```

Implementation function (place near the other tool impls; add `Entity`, `EntityMention`, `EntityRelationship` to the models import):

```python
async def _lookup_entity(db: AsyncSession, user: User, accessible_ids: list[int], tool_input: dict) -> ToolRun:
    name = (tool_input.get("name") or "").strip()
    if not name:
        return ToolRun(result="Missing name", result_summary="missing name", ok=False)
    scope = accessible_ids
    prod_id = tool_input.get("production_id")
    if prod_id is not None:
        if prod_id not in accessible_ids:
            return ToolRun(result=json.dumps({"matches": []}), result_summary="0 entities found")
        scope = [prod_id]

    rows = (await db.execute(
        select(Entity)
        .where(Entity.production_id.in_(scope), Entity.canonical_name.ilike(f"%{name}%"))
        .order_by(Entity.mention_count.desc())
        .limit(5)
    )).scalars().all()

    matches = []
    for e in rows:
        doc_count = (await db.execute(
            select(func.count(func.distinct(EntityMention.document_id)))
            .where(EntityMention.entity_id == e.id)
        )).scalar() or 0
        edges = (await db.execute(
            select(EntityRelationship.relationship_type, Entity.canonical_name)
            .join(Entity, Entity.id == EntityRelationship.target_entity_id)
            .where(EntityRelationship.source_entity_id == e.id)
            .limit(10)
        )).all()
        snippets = (await db.execute(
            select(EntityMention.context_snippet)
            .where(EntityMention.entity_id == e.id, EntityMention.context_snippet.isnot(None))
            .limit(3)
        )).scalars().all()
        matches.append({
            "entity_id": str(e.id), "production_id": e.production_id,
            "type": e.entity_type, "name": e.canonical_name,
            "aliases": list(e.aliases or []), "overview": e.overview,
            "mention_count": e.mention_count, "document_count": doc_count,
            "relationships": [f"{rt} -> {n}" for rt, n in edges],
            "sample_mentions": [s[:200] for s in snippets],
        })
    return ToolRun(
        result=json.dumps({"matches": matches}),
        result_summary=f"{len(matches)} entit{'y' if len(matches) == 1 else 'ies'} found",
    )
```

Register in `_DISPATCH` (`"lookup_entity": _lookup_entity`) and add to `tool_use_summary`:

```python
    if name == "lookup_entity":
        return f'Looking up "{tool_input.get("name", "")}" in the case ontology'
```

- [ ] **Step 4: Run tests**

Run: `cd backend; python -m pytest tests/test_ai_tools.py tests/ -q`
Expected: all pass

- [ ] **Step 5: Commit and open PR 1**

```bash
git add backend/app/services/ai_tools.py backend/tests/test_ai_tools.py
git commit -m "feat(ontology): lookup_entity chat tool"
git push -u origin feat/ontology-entity-intelligence
```

Open PR 1 (`gh pr create`) titled "feat(ontology): entity intelligence backend — schema, extraction pipeline, API" targeting `main`. Body: summary of spec + note that it is data-only (no UI change) and that backfill is `POST /api/productions/{id}/extract-entities`. **Check for drift with the parallel session first** (`git fetch origin main` and rebase if main moved; re-check `alembic heads`).

---

### Task 9: Frontend API client + types

**Files:**
- Modify: `frontend/src/types.ts` (append)
- Modify: `frontend/src/api/client.ts` (append section)

**Interfaces:**
- Consumes: Task 6/7 endpoint shapes; `request<T>()` helper.
- Produces (used by Tasks 10–13): types `EntityMentionSpan`, `DocEntity`, `EntityProfile`, `EntityDocMentions`, `EntityMentionsPage`, `EntityConnection`, `SharedEvent`, `EntityConnections`, `EntityListItem`, `EntityListPage`, `MergeSuggestion`; functions `getDocumentEntities`, `getEntity`, `getEntityMentions`, `getEntityConnections`, `listEntities`, `listMergeSuggestions`, `acceptMergeSuggestion`, `rejectMergeSuggestion`, `mergeEntities`, `triggerEntityExtraction`.

- [ ] **Step 1: Append to `frontend/src/types.ts`**

```ts
// ── Ontology ──

export interface EntityMentionSpan {
  surface_text: string;
  start_offset: number | null;
  end_offset: number | null;
}

export interface DocEntity {
  id: string;
  entity_type: 'person' | 'org';
  canonical_name: string;
  mention_count: number;
  mentions: EntityMentionSpan[];
}

export interface EntityProfile {
  id: string;
  production_id: number;
  entity_type: 'person' | 'org';
  canonical_name: string;
  aliases: string[];
  attributes: { role?: string; emails?: string[] };
  overview: string | null;
  mention_count: number;
  document_count: number;
}

export interface EntityDocMentions {
  document_id: string;
  bates_begin: string;
  title: string | null;
  mentions: { surface_text: string; context_snippet: string | null; start_offset: number | null }[];
}

export interface EntityMentionsPage {
  documents: EntityDocMentions[];
  total: number;
}

export interface EntityConnection {
  entity_id: string;
  canonical_name: string;
  entity_type: 'person' | 'org';
  relationship_type?: string | null;
  description?: string | null;
  document_id?: string | null;
  shared_doc_count?: number | null;
}

export interface SharedEvent {
  event_id: number;
  description: string;
  event_type: string;
  event_date: string | null;
  document_id: string;
}

export interface EntityConnections {
  stated: EntityConnection[];
  cooccurrence: EntityConnection[];
  shared_events: SharedEvent[];
}

export interface EntityListItem {
  id: string;
  entity_type: 'person' | 'org';
  canonical_name: string;
  mention_count: number;
  document_count: number;
}

export interface EntityListPage {
  entities: EntityListItem[];
  total: number;
}

export interface MergeSuggestion {
  id: number;
  score: number;
  rationale: string;
  status: string;
  entity_a: EntityListItem;
  entity_b: EntityListItem;
}
```

- [ ] **Step 2: Append to `frontend/src/api/client.ts`** (import the new types in the type-import block at top)

```ts
// ── Ontology / entities ──

export const getDocumentEntities = (docId: string) =>
  request<{ entities: DocEntity[] }>(`/api/documents/${docId}/entities`);

export const getEntity = (entityId: string) =>
  request<EntityProfile>(`/api/entities/${entityId}`);

export const getEntityMentions = (entityId: string, page = 1, perPage = 20) =>
  request<EntityMentionsPage>(`/api/entities/${entityId}/mentions?page=${page}&per_page=${perPage}`);

export const getEntityConnections = (entityId: string) =>
  request<EntityConnections>(`/api/entities/${entityId}/connections`);

export function listEntities(productionId: number, search?: string, entityType?: string, page = 1, perPage = 50) {
  const params = new URLSearchParams({ page: String(page), per_page: String(perPage) });
  if (search) params.set('search', search);
  if (entityType) params.set('entity_type', entityType);
  return request<EntityListPage>(`/api/productions/${productionId}/entities?${params}`);
}

export const listMergeSuggestions = (productionId: number, status = 'pending') =>
  request<MergeSuggestion[]>(`/api/productions/${productionId}/merge-suggestions?status=${status}`);

export const acceptMergeSuggestion = (suggestionId: number) =>
  request<{ merge_id: number; winner_id: string }>(`/api/merge-suggestions/${suggestionId}/accept`, { method: 'POST' });

export const rejectMergeSuggestion = (suggestionId: number) =>
  request<{ ok: boolean }>(`/api/merge-suggestions/${suggestionId}/reject`, { method: 'POST' });

export const mergeEntities = (winnerId: string, loserId: string) =>
  request<{ merge_id: number; winner_id: string }>(`/api/entities/merge`,
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ winner_id: winnerId, loser_id: loserId }) });

export const triggerEntityExtraction = (productionId: number) =>
  request<{ status: string }>(`/api/productions/${productionId}/extract-entities`, { method: 'POST' });
```

- [ ] **Step 3: Verify build + lint**

Run: `cd frontend; npm run build; npm run lint`
Expected: build succeeds; lint shows no NEW errors beyond the 41-error baseline.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/types.ts frontend/src/api/client.ts
git commit -m "feat(ontology): frontend types + API client for entities"
```

---

### Task 10: Clickable entity mentions in TextPanel

**Files:**
- Modify: `frontend/src/components/TextPanel.tsx`

**Interfaces:**
- Consumes: `DocEntity`, `EntityMentionSpan` types.
- Produces: new optional props on `TextPanel`: `entities?: DocEntity[]`, `onEntityClick?: (entityId: string) => void`, `focusEntityId?: string | null`. Entity spans render as `<mark class="entity-mark entity-person|entity-org">`, clickable; when `focusEntityId` changes, the panel scrolls to that entity's first mention.

- [ ] **Step 1: Rewrite `TextPanel.tsx`**

```tsx
import { Fragment, useCallback, useEffect, useMemo, useRef, type ReactNode } from 'react';
import type { DocEntity } from '../types';

interface Props {
  text: string | null;
  searchQuery?: string;
  entities?: DocEntity[];
  onEntityClick?: (entityId: string) => void;
  focusEntityId?: string | null;
  onTitleChanged?: (title: string) => void;
}

function highlightTerms(text: string, searchQuery?: string): ReactNode {
  if (!searchQuery) return text;
  const terms = searchQuery
    .replace(/["()]/g, '')
    .split(/\s+/)
    .filter(t => t && !['AND', 'OR', 'NOT'].includes(t.toUpperCase()));
  if (terms.length === 0) return text;

  const escaped = terms.map(t =>
    t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/\\\*$/, '\\w*'),
  );
  const regex = new RegExp(`(${escaped.join('|')})`, 'gi');
  const parts = text.split(regex);
  return parts.map((part, i) => {
    if (i % 2 === 1) return <mark key={i}>{part}</mark>;
    return <Fragment key={i}>{part}</Fragment>;
  });
}

interface EntitySpan {
  start: number;
  end: number;
  entityId: string;
  entityType: string;
  name: string;
}

/** Flatten entity mentions into a non-overlapping, offset-sorted span list. */
function buildSpans(entities: DocEntity[]): EntitySpan[] {
  const spans: EntitySpan[] = [];
  for (const e of entities) {
    for (const m of e.mentions) {
      if (m.start_offset == null || m.end_offset == null) continue;
      spans.push({ start: m.start_offset, end: m.end_offset, entityId: e.id, entityType: e.entity_type, name: e.canonical_name });
    }
  }
  spans.sort((a, b) => a.start - b.start);
  const out: EntitySpan[] = [];
  let lastEnd = -1;
  for (const s of spans) {
    if (s.start < lastEnd) continue; // drop overlaps
    out.push(s);
    lastEnd = s.end;
  }
  return out;
}

function renderWithEntities(
  text: string,
  spans: EntitySpan[],
  searchQuery: string | undefined,
  onEntityClick?: (entityId: string) => void,
): ReactNode {
  if (spans.length === 0) return highlightTerms(text, searchQuery);
  const parts: ReactNode[] = [];
  let cursor = 0;
  spans.forEach((s, i) => {
    if (s.start > cursor) {
      parts.push(<Fragment key={`t${i}`}>{highlightTerms(text.slice(cursor, s.start), searchQuery)}</Fragment>);
    }
    parts.push(
      <mark
        key={`e${i}`}
        className={`entity-mark entity-${s.entityType}`}
        data-entity-id={s.entityId}
        role="button"
        tabIndex={0}
        title={s.name}
        style={{ cursor: 'pointer' }}
        onClick={() => onEntityClick?.(s.entityId)}
        onKeyDown={ev => { if (ev.key === 'Enter') onEntityClick?.(s.entityId); }}
      >
        {text.slice(s.start, s.end)}
      </mark>,
    );
    cursor = s.end;
  });
  if (cursor < text.length) {
    parts.push(<Fragment key="tail">{highlightTerms(text.slice(cursor), searchQuery)}</Fragment>);
  }
  return parts;
}

export default function TextPanel({ text, searchQuery, entities, onEntityClick, focusEntityId }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);

  const copyToClipboard = useCallback(() => {
    if (text) navigator.clipboard.writeText(text);
  }, [text]);

  const rendered = useMemo(() => {
    if (!text) return null;
    const spans = entities?.length ? buildSpans(entities) : [];
    return renderWithEntities(text, spans, searchQuery, onEntityClick);
  }, [text, searchQuery, entities, onEntityClick]);

  useEffect(() => {
    if (!focusEntityId || !containerRef.current) return;
    const el = containerRef.current.querySelector(`[data-entity-id="${focusEntityId}"]`);
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }, [focusEntityId]);

  if (!text) {
    return <div className="empty-state">No extracted text available</div>;
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div className="panel-header">
        <span>Extracted Text</span>
        <button onClick={copyToClipboard} className="btn btn-ghost btn-xs" style={{ marginLeft: 'auto' }} aria-label="Copy extracted text to clipboard">
          Copy
        </button>
      </div>
      <div
        ref={containerRef}
        style={{ flex: 1, overflow: 'auto', padding: 'var(--space-4)', fontSize: 'var(--text-sm)', lineHeight: 1.65, whiteSpace: 'pre-wrap', fontFamily: 'var(--font-mono)' }}
      >
        {rendered}
      </div>
    </div>
  );
}
```

Add to the global stylesheet (find where `.panel-header` or `mark` styles live — likely `frontend/src/index.css` or `App.css`):

```css
.entity-mark { border-radius: 3px; padding: 0 1px; }
.entity-mark.entity-person { background: color-mix(in srgb, #4f7cff 22%, transparent); border-bottom: 1px solid #4f7cff; }
.entity-mark.entity-org { background: color-mix(in srgb, #b4690e 22%, transparent); border-bottom: 1px solid #b4690e; }
.entity-mark:hover { filter: brightness(1.15); }
```

- [ ] **Step 2: Verify build + lint**

Run: `cd frontend; npm run build; npm run lint`
Expected: build passes; no new lint errors (note: search-highlighting inside/around entity spans stays intact because `highlightTerms` runs on the non-entity segments).

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/TextPanel.tsx frontend/src/index.css
git commit -m "feat(ontology): clickable entity mentions in TextPanel"
```

---

### Task 11: EntityPanel drawer component

**Files:**
- Create: `frontend/src/components/EntityPanel.tsx`

**Interfaces:**
- Consumes: `getEntity`, `getEntityMentions`, `getEntityConnections` (Task 9).
- Produces: `<EntityPanel entityId onClose onOpenEntity(id) onOpenDocument(docId, entityId) />` — overview, aliases/role, connections (stated + co-occurrence chips), mentions grouped by document with snippets.

- [ ] **Step 1: Implement**

```tsx
import { useEffect, useState } from 'react';
import { getEntity, getEntityConnections, getEntityMentions } from '../api/client';
import type { EntityConnections, EntityMentionsPage, EntityProfile } from '../types';

interface Props {
  entityId: string;
  onClose: () => void;
  onOpenEntity: (entityId: string) => void;
  onOpenDocument: (docId: string, entityId: string) => void;
}

export default function EntityPanel({ entityId, onClose, onOpenEntity, onOpenDocument }: Props) {
  const [profile, setProfile] = useState<EntityProfile | null>(null);
  const [mentions, setMentions] = useState<EntityMentionsPage | null>(null);
  const [connections, setConnections] = useState<EntityConnections | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setProfile(null); setMentions(null); setConnections(null); setError(null);
    getEntity(entityId).then(p => { if (!cancelled) setProfile(p); })
      .catch(e => { if (!cancelled) setError(String(e.message || e)); });
    getEntityMentions(entityId).then(m => { if (!cancelled) setMentions(m); }).catch(() => {});
    getEntityConnections(entityId).then(c => { if (!cancelled) setConnections(c); }).catch(() => {});
    return () => { cancelled = true; };
  }, [entityId]);

  return (
    <div className="entity-panel" style={{
      position: 'absolute', top: 0, right: 0, bottom: 0, width: 380, zIndex: 30,
      background: 'var(--color-bg, #fff)', borderLeft: '1px solid var(--color-border, #ddd)',
      display: 'flex', flexDirection: 'column', boxShadow: '-4px 0 16px rgba(0,0,0,0.12)',
    }}>
      <div className="panel-header" style={{ display: 'flex', alignItems: 'center' }}>
        <span style={{ fontWeight: 600 }}>
          {profile ? profile.canonical_name : 'Loading…'}
          {profile && (
            <span className="badge" style={{ marginLeft: 8, fontSize: 'var(--text-xs)' }}>
              {profile.entity_type === 'person' ? 'Person' : 'Organization'}
            </span>
          )}
        </span>
        <button onClick={onClose} className="btn btn-ghost btn-xs" style={{ marginLeft: 'auto' }} aria-label="Close entity panel">✕</button>
      </div>
      <div style={{ flex: 1, overflow: 'auto', padding: 'var(--space-4)', fontSize: 'var(--text-sm)' }}>
        {error && <div className="empty-state">{error}</div>}
        {profile && (
          <>
            {profile.attributes.role && <div style={{ marginBottom: 8, opacity: 0.85 }}>{profile.attributes.role}</div>}
            {profile.overview
              ? <p style={{ marginBottom: 12 }}>{profile.overview}</p>
              : <p style={{ marginBottom: 12, opacity: 0.6 }}>No overview yet.</p>}
            <div style={{ marginBottom: 12, opacity: 0.75 }}>
              Mentioned {profile.mention_count} times across {profile.document_count} documents.
            </div>
            {profile.aliases.length > 0 && (
              <div style={{ marginBottom: 16 }}>
                <div className="panel-header" style={{ padding: 0 }}>Also appears as</div>
                <div>{profile.aliases.join(' · ')}</div>
              </div>
            )}
          </>
        )}

        {connections && (connections.stated.length > 0 || connections.cooccurrence.length > 0) && (
          <div style={{ marginBottom: 16 }}>
            <div className="panel-header" style={{ padding: 0 }}>Connections</div>
            {connections.stated.map((c, i) => (
              <div key={`s${i}`} style={{ padding: '4px 0' }}>
                <button className="btn btn-ghost btn-xs" onClick={() => onOpenEntity(c.entity_id)}>
                  {c.canonical_name}
                </button>
                <span style={{ opacity: 0.7 }}> — {c.relationship_type?.replace(/_/g, ' ')}</span>
                {c.description && <div style={{ opacity: 0.6, fontSize: 'var(--text-xs)' }}>{c.description}</div>}
              </div>
            ))}
            {connections.cooccurrence.map((c, i) => (
              <div key={`c${i}`} style={{ padding: '4px 0' }}>
                <button className="btn btn-ghost btn-xs" onClick={() => onOpenEntity(c.entity_id)}>
                  {c.canonical_name}
                </button>
                <span style={{ opacity: 0.7 }}> — appear together in {c.shared_doc_count} docs</span>
              </div>
            ))}
          </div>
        )}

        {mentions && mentions.documents.length > 0 && (
          <div>
            <div className="panel-header" style={{ padding: 0 }}>Mentions ({mentions.total} documents)</div>
            {mentions.documents.map(d => (
              <div key={d.document_id} style={{ margin: '8px 0' }}>
                <button className="btn btn-ghost btn-xs" style={{ fontWeight: 600 }}
                        onClick={() => onOpenDocument(d.document_id, entityId)}>
                  {d.bates_begin}{d.title ? ` — ${d.title}` : ''}
                </button>
                {d.mentions.slice(0, 3).map((m, i) => (
                  <div key={i} style={{ opacity: 0.7, fontSize: 'var(--text-xs)', padding: '2px 0 2px 12px' }}>
                    …{m.context_snippet || m.surface_text}…
                  </div>
                ))}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Verify build + lint**

Run: `cd frontend; npm run build; npm run lint`
Expected: clean build, no new lint errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/EntityPanel.tsx
git commit -m "feat(ontology): EntityPanel drawer — profile, connections, cross-doc mentions"
```

---

### Task 12: DocumentViewer wiring — sidebar section + panel + text props

**Files:**
- Modify: `frontend/src/components/DocumentViewer.tsx`

**Interfaces:**
- Consumes: `getDocumentEntities` (Task 9), `TextPanel` new props (Task 10), `EntityPanel` (Task 11).
- Produces: entities loaded per document; "People & Orgs" left-sidebar section; entity panel opens on click from text or sidebar; navigating to another document from the panel keeps the panel open and focuses the entity.

- [ ] **Step 1: Wire state + fetch**

In `DocumentViewer.tsx`:

1. Imports: add `getDocumentEntities` to the `../api/client` import; `import EntityPanel from './EntityPanel';`; `import type { DocEntity } from '../types';`.
2. State (near the `duplicates` state, ~line 38):

```tsx
  const [entities, setEntities] = useState<DocEntity[]>([]);
  const [openEntityId, setOpenEntityId] = useState<string | null>(null);
  const [focusEntityId, setFocusEntityId] = useState<string | null>(null);
```

3. Fetch alongside the duplicates fetch (~line 65) and reset alongside the duplicates reset (~line 57):

```tsx
    setEntities([]);
```

```tsx
    getDocumentEntities(docId).then(r => setEntities(r.entities)).catch(e => console.warn('getDocumentEntities failed:', e));
```

4. Both `<TextPanel …>` usages (~lines 346 and 461) gain:

```tsx
    entities={entities}
    onEntityClick={id => setOpenEntityId(id)}
    focusEntityId={focusEntityId}
```

5. Left sidebar — add a section after the Duplicates section (~line 404's enclosing block), matching its markup style:

```tsx
          {entities.length > 0 && (
            <div className="sidebar-section">
              <div className="panel-header">People &amp; Orgs ({entities.length})</div>
              {entities
                .slice()
                .sort((a, b) => b.mentions.length - a.mentions.length)
                .map(e => (
                  <button
                    key={e.id}
                    className="btn btn-ghost btn-xs"
                    style={{ display: 'block', width: '100%', textAlign: 'left' }}
                    onClick={() => setOpenEntityId(e.id)}
                  >
                    <span className={`entity-dot entity-${e.entity_type}`} style={{ marginRight: 6 }}>●</span>
                    {e.canonical_name}
                    <span style={{ opacity: 0.6 }}> ({e.mentions.length})</span>
                  </button>
                ))}
            </div>
          )}
```

6. Render the panel inside the viewer's root container (it is `position: relative` or make it so):

```tsx
      {openEntityId && (
        <EntityPanel
          entityId={openEntityId}
          onClose={() => setOpenEntityId(null)}
          onOpenEntity={id => setOpenEntityId(id)}
          onOpenDocument={(targetDocId, entityId) => {
            setFocusEntityId(entityId);
            setRightTab('text');
            if (targetDocId !== docId) onNavigate(targetDocId);
          }}
        />
      )}
```

(`onNavigate` is the existing prop `DocumentViewer` already receives — confirm its name at the top of the component; `App.tsx` passes `onNavigate={setViewDocId}`.)

Add the sidebar dot colors to the stylesheet next to the Task 10 rules:

```css
.entity-dot.entity-person { color: #4f7cff; }
.entity-dot.entity-org { color: #b4690e; }
```

- [ ] **Step 2: Verify build + lint**

Run: `cd frontend; npm run build; npm run lint`
Expected: clean; no new errors. React Compiler note: all state updates happen in handlers/effects, none in render.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/DocumentViewer.tsx frontend/src/index.css
git commit -m "feat(ontology): entity sidebar section + panel wiring in DocumentViewer"
```

---

### Task 13: Matter-level Entities view + merge queue + final verification

**Files:**
- Create: `frontend/src/components/EntitiesView.tsx`
- Modify: `frontend/src/App.tsx`, `frontend/src/hooks/useUrlState.ts`

**Interfaces:**
- Consumes: `listEntities`, `listMergeSuggestions`, `acceptMergeSuggestion`, `rejectMergeSuggestion` (Task 9); `EntityPanel` (Task 11); `showReview`/`view` wiring pattern in `App.tsx`.
- Produces: `view=entities` URL state; `<EntitiesView productionId onViewDocument />` with key-players list + merge-suggestion queue.

- [ ] **Step 1: Implement `EntitiesView.tsx`**

```tsx
import { useCallback, useEffect, useState } from 'react';
import { acceptMergeSuggestion, listEntities, listMergeSuggestions, rejectMergeSuggestion } from '../api/client';
import type { EntityListItem, MergeSuggestion } from '../types';
import EntityPanel from './EntityPanel';

interface Props {
  productionId: number;
  onViewDocument: (docId: string) => void;
  onBack: () => void;
}

export default function EntitiesView({ productionId, onViewDocument, onBack }: Props) {
  const [entities, setEntities] = useState<EntityListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [search, setSearch] = useState('');
  const [typeFilter, setTypeFilter] = useState<string>('');
  const [suggestions, setSuggestions] = useState<MergeSuggestion[]>([]);
  const [openEntityId, setOpenEntityId] = useState<string | null>(null);
  const [busy, setBusy] = useState<number | null>(null);

  const refresh = useCallback(() => {
    listEntities(productionId, search || undefined, typeFilter || undefined)
      .then(r => { setEntities(r.entities); setTotal(r.total); })
      .catch(e => console.warn('listEntities failed:', e));
    listMergeSuggestions(productionId)
      .then(setSuggestions)
      .catch(e => console.warn('listMergeSuggestions failed:', e));
  }, [productionId, search, typeFilter]);

  useEffect(() => { refresh(); }, [refresh]);

  const resolve = async (id: number, accept: boolean) => {
    setBusy(id);
    try {
      if (accept) await acceptMergeSuggestion(id);
      else await rejectMergeSuggestion(id);
      refresh();
    } catch (e) {
      console.warn('merge suggestion resolution failed:', e);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div style={{ position: 'relative', height: '100%', display: 'flex', flexDirection: 'column' }}>
      <div className="panel-header" style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <button className="btn btn-ghost btn-xs" onClick={onBack}>← Back</button>
        <span style={{ fontWeight: 600 }}>People &amp; Organizations ({total})</span>
        <input
          className="input"
          placeholder="Search entities…"
          value={search}
          onChange={e => setSearch(e.target.value)}
          style={{ marginLeft: 'auto', maxWidth: 240 }}
        />
        <select className="input" value={typeFilter} onChange={e => setTypeFilter(e.target.value)} style={{ maxWidth: 140 }}>
          <option value="">All types</option>
          <option value="person">People</option>
          <option value="org">Organizations</option>
        </select>
      </div>

      <div style={{ flex: 1, overflow: 'auto', padding: 'var(--space-4)' }}>
        {suggestions.length > 0 && (
          <div className="card" style={{ marginBottom: 16, padding: 'var(--space-4)' }}>
            <div className="panel-header" style={{ padding: 0 }}>
              Possible duplicates — same person? ({suggestions.length})
            </div>
            {suggestions.map(s => (
              <div key={s.id} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 0' }}>
                <span>
                  <b>{s.entity_a.canonical_name}</b> ({s.entity_a.mention_count})
                  {' ↔ '}
                  <b>{s.entity_b.canonical_name}</b> ({s.entity_b.mention_count})
                </span>
                <span style={{ opacity: 0.6, fontSize: 'var(--text-xs)' }}>{s.rationale}</span>
                <span style={{ marginLeft: 'auto' }}>
                  <button className="btn btn-xs" disabled={busy === s.id} onClick={() => resolve(s.id, true)}>Same — merge</button>
                  <button className="btn btn-ghost btn-xs" disabled={busy === s.id} onClick={() => resolve(s.id, false)}>Different</button>
                </span>
              </div>
            ))}
          </div>
        )}

        <table className="table" style={{ width: '100%' }}>
          <thead>
            <tr><th>Name</th><th>Type</th><th>Mentions</th><th>Documents</th></tr>
          </thead>
          <tbody>
            {entities.map(e => (
              <tr key={e.id} style={{ cursor: 'pointer' }} onClick={() => setOpenEntityId(e.id)}>
                <td>{e.canonical_name}</td>
                <td>{e.entity_type === 'person' ? 'Person' : 'Org'}</td>
                <td>{e.mention_count}</td>
                <td>{e.document_count}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {entities.length === 0 && <div className="empty-state">No entities extracted yet.</div>}
      </div>

      {openEntityId && (
        <EntityPanel
          entityId={openEntityId}
          onClose={() => setOpenEntityId(null)}
          onOpenEntity={setOpenEntityId}
          onOpenDocument={docId => { setOpenEntityId(null); onViewDocument(docId); }}
        />
      )}
    </div>
  );
}
```

- [ ] **Step 2: Wire into `App.tsx` + URL state**

`useUrlState.ts` needs no structural change (`view` is already a free string) — update only the comment: `view?: string; // 'review' | 'ai' (legacy) | 'entities' | etc.`

In `App.tsx`, mirror the `showReview` pattern exactly (state seeded from `initialUrl.view === 'entities'`, reflected back into the `useSyncUrl` state object, rendered ahead of the document list):

```tsx
  const [showEntities, setShowEntities] = useState(initialUrl.view === 'entities');
```

- Include in the URL-state object where `view` is currently derived from `showReview` — extend that expression so `view` is `'entities'` when `showEntities` is true (read the current derivation around line ~119 and add the case).
- Render: next to where `showReview` renders its view, add:

```tsx
  if (showEntities && selectedProductionId) {
    return (
      <EntitiesView
        productionId={selectedProductionId}
        onViewDocument={(id) => { setShowEntities(false); setViewDocId(id); }}
        onBack={() => setShowEntities(false)}
      />
    );
  }
```

(Adapt the surrounding layout wrapper to match how the review view is embedded — read those lines and mirror them. `selectedProductionId` is whatever variable `App.tsx` uses for the active production — confirm its actual name where `prod` URL state is handled.)

- Nav entry: find the button/tab that opens the review view (search for `setShowReview(true)`) and add a sibling "Entities" button that calls `setShowEntities(true)`.

- [ ] **Step 3: Full verification**

Backend:
Run: `cd backend; python -m pytest tests/ -q`
Expected: all pass.

Frontend:
Run: `cd frontend; npm run build; npm run lint`
Expected: build clean; lint no new errors vs baseline.

Manual walkthrough (local dev per `vigilist-local-dev-env` memory: pgvector compose, `.env`, sign-in via Playwright flow):
1. Run migration: `cd backend; alembic upgrade head`.
2. Start backend + frontend; trigger `POST /api/productions/{id}/extract-entities` on a small test production (or via the UI once the button exists).
3. Verify: names highlighted in Text panel → click → EntityPanel shows overview/mentions/connections → click a mention in another doc → navigates and scrolls → Entities view lists key players → merge queue accept/reject works.

- [ ] **Step 4: Commit and open PR 2**

```bash
git add frontend/src/components/EntitiesView.tsx frontend/src/App.tsx frontend/src/hooks/useUrlState.ts
git commit -m "feat(ontology): matter-level entities view + merge suggestion queue"
git push
```

Open PR 2 titled "feat(ontology): entity intelligence UI — clickable mentions, profiles, key players" (or fold into PR 1 if it hasn't merged yet and the user prefers one PR). After PR 1 deploys: run the backfill trigger per existing matter and verify `ai_pipeline_status.entities` reaches `"done"`.
