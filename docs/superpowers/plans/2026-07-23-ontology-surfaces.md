# Ontology Surfaces Implementation Plan — Timeline, Graph, Ambient Weaving

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship Phase B (matter timeline over already-extracted events), Phase C (interactive relationship graph), and ambient weaving (entity deep links, linkified brief key players, entity chips on document rows, chat entity links).

**Architecture:** Read-only over Phase A's tables — NO schema changes, NO migrations. Three new backend read endpoints + brief augmentation, all in existing routers/services. Frontend: two new full-screen views following the EntitiesView/App.tsx wiring pattern, one new dep (`d3-force`, layout math only), chips and links woven into existing components.

**Tech Stack:** FastAPI + SQLAlchemy async; React 19 + TS, hand-rolled client; d3-force (sole new dependency).

**Spec:** `docs/superpowers/specs/2026-07-23-ontology-surfaces-design.md`

## Global Constraints

- NO new migrations or model changes; backend adds only endpoints/schemas/helpers.
- Only new dependency (frontend): `d3-force` (+ `@types/d3-force` devDep). Nothing else, no full d3.
- All endpoints scoped via `get_accessible_production_ids`; missing AND out-of-scope → 404 (never 403).
- Event types: meeting|communication|payment|filing|agreement|other. Date precision: day|month|year|unknown; `event_date` nullable.
- Pagination convention (mirror `routers/entities.py:list_production_entities`): `page`/`per_page` params, in-body clamp, offset `(max(1,page)-1)*per_page`, stable secondary sort key.
- Frontend lint: zero new errors over baseline (4 errors/6 warnings); React Compiler + `react-hooks/set-state-in-effect` rules (setState only in handlers/.then).
- Backend tests: fake-session pattern (`tests/fakes.py`); run `cd backend && F:/Users/WCEdm/Documents/Developer/descubre/backend/venv/Scripts/python.exe -m pytest tests/ -q`; known pre-existing failure `test_ai_review.py::test_build_classification_prompt` is NOT ours.
- Frontend verify: `cd frontend && npm run build && npm run lint`.
- Branch `feat/ontology-surfaces`; single PR at the end.
- Entity colors: person `#4f7cff`, org `#b4690e` (match `.entity-mark`/`.entity-dot`).

---

### Task 1: Timeline API

**Files:**
- Modify: `backend/app/routers/entities.py` (append endpoint)
- Modify: `backend/app/schemas.py` (append schemas)
- Test: `backend/tests/test_timeline_endpoint.py`

**Interfaces:**
- Consumes: `OntologyEvent`, `EventParticipant`, `Entity`, `Document` models; `get_accessible_production_ids`.
- Produces: `GET /api/productions/{production_id}/timeline?entity_id=&event_type=&page=&per_page=` → `TimelinePageOut = {events: [TimelineEventOut], total: int, undated_count: int}`; `TimelineEventOut = {event_id, description, event_type, event_date: str|None, date_precision, document_id, bates_begin, title, participants: [{entity_id, canonical_name, entity_type}]}`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_timeline_endpoint.py
"""Fake-session tests for the production timeline endpoint."""

import asyncio
import uuid
from datetime import date

import pytest
from fastapi import HTTPException

import app.routers.entities as er
from app.models import Entity, OntologyEvent
from tests.fakes import FakeResult, FakeSession, FakeUser


def _event(eid, d=None, precision="unknown", etype="meeting"):
    ev = OntologyEvent(production_id=1, event_type=etype, description=f"Event {eid}",
                       event_date=d, date_precision=precision, document_id=uuid.uuid4())
    ev.id = eid
    return ev


def _patch(monkeypatch, accessible=(1,)):
    async def fake_accessible(db, user):
        return list(accessible)
    monkeypatch.setattr(er, "get_accessible_production_ids", fake_accessible)


def test_timeline_denies_out_of_scope(monkeypatch):
    _patch(monkeypatch, accessible=(2,))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.get_production_timeline(production_id=1, db=FakeSession(), user=FakeUser()))
    assert exc.value.status_code == 404


def test_timeline_returns_events_with_participants_and_doc(monkeypatch):
    _patch(monkeypatch)
    ent = Entity(id=uuid.uuid4(), production_id=1, entity_type="person",
                 canonical_name="Jorge Rivera", aliases=[], attributes={}, mention_count=5)
    ev = _event(10, d=date(2019, 3, 15), precision="day")
    db = FakeSession(responders=[
        ("count", FakeResult(scalar=1)),
        # page query returns (event, bates, title) rows
        ("FROM ontology_events", FakeResult(rows=[(ev, "ABC-0001", "Board deck")])),
        # participants query returns (event_id, entity) rows
        ("FROM event_participants", FakeResult(rows=[(10, ent)])),
    ])
    out = asyncio.run(er.get_production_timeline(production_id=1, db=db, user=FakeUser()))
    assert out.total == 1
    e = out.events[0]
    assert e.event_date == "2019-03-15" and e.date_precision == "day"
    assert e.bates_begin == "ABC-0001"
    assert e.participants[0].canonical_name == "Jorge Rivera"


def test_timeline_clamps_per_page(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(responders=[("count", FakeResult(scalar=0))])
    out = asyncio.run(er.get_production_timeline(
        production_id=1, per_page=5000, db=db, user=FakeUser()))
    assert out.events == [] and out.total == 0


def test_timeline_null_date_serializes_none(monkeypatch):
    _patch(monkeypatch)
    ev = _event(11, d=None, precision="unknown")
    db = FakeSession(responders=[
        ("count", FakeResult(scalar=1)),
        ("FROM ontology_events", FakeResult(rows=[(ev, "ABC-0002", None)])),
        ("FROM event_participants", FakeResult(rows=[])),
    ])
    out = asyncio.run(er.get_production_timeline(production_id=1, db=db, user=FakeUser()))
    assert out.events[0].event_date is None
```

Note on the count responder: the endpoint issues two counts (total + undated). Both SQL strings contain "count"; the same `FakeResult(scalar=N)` serves both — assert only on `total` where they'd differ, or register a more specific substring first if a test needs distinct values (fakes dispatch is first-match).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend; F:/Users/WCEdm/Documents/Developer/descubre/backend/venv/Scripts/python.exe -m pytest tests/test_timeline_endpoint.py -q`
Expected: FAIL — `AttributeError: ... has no attribute 'get_production_timeline'`

- [ ] **Step 3: Add schemas to `backend/app/schemas.py`** (after the ontology block)

```python
class TimelineParticipantOut(BaseModel):
    entity_id: UUID4
    canonical_name: str
    entity_type: str


class TimelineEventOut(BaseModel):
    event_id: int
    description: str
    event_type: str
    event_date: str | None
    date_precision: str
    document_id: UUID4
    bates_begin: str
    title: str | None
    participants: list[TimelineParticipantOut]


class TimelinePageOut(BaseModel):
    events: list[TimelineEventOut]
    total: int
    undated_count: int
```

- [ ] **Step 4: Implement the endpoint** (append to `backend/app/routers/entities.py`; add `OntologyEvent` already imported, add `Document` — check imports)

```python
@router.get("/productions/{production_id}/timeline", response_model=TimelinePageOut)
async def get_production_timeline(
    production_id: int,
    entity_id: UUID | None = None,
    event_type: str | None = None,
    page: int = 1,
    per_page: int = 50,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=404, detail="Production not found")
    per_page = max(1, min(100, per_page))

    base = select(OntologyEvent).where(OntologyEvent.production_id == production_id)
    count_base = select(func.count(OntologyEvent.id)).where(OntologyEvent.production_id == production_id)
    if event_type in ("meeting", "communication", "payment", "filing", "agreement", "other"):
        base = base.where(OntologyEvent.event_type == event_type)
        count_base = count_base.where(OntologyEvent.event_type == event_type)
    if entity_id is not None:
        base = base.join(EventParticipant, EventParticipant.event_id == OntologyEvent.id).where(
            EventParticipant.entity_id == entity_id)
        count_base = count_base.join(EventParticipant, EventParticipant.event_id == OntologyEvent.id).where(
            EventParticipant.entity_id == entity_id)

    total = (await db.execute(count_base)).scalar() or 0
    undated_count = (await db.execute(
        count_base.where(OntologyEvent.event_date.is_(None)))).scalar() or 0

    rows = (await db.execute(
        base.add_columns(Document.bates_begin, Document.title)
        .join(Document, OntologyEvent.document_id == Document.id)
        .order_by(OntologyEvent.event_date.asc().nullslast(), OntologyEvent.id)
        .offset((max(1, page) - 1) * per_page)
        .limit(per_page)
    )).all()

    event_ids = [ev.id for ev, _b, _t in rows]
    participants_by_event: dict[int, list[TimelineParticipantOut]] = {}
    if event_ids:
        prows = (await db.execute(
            select(EventParticipant.event_id, Entity)
            .join(Entity, EventParticipant.entity_id == Entity.id)
            .where(EventParticipant.event_id.in_(event_ids))
        )).all()
        for eid, ent in prows:
            participants_by_event.setdefault(eid, []).append(TimelineParticipantOut(
                entity_id=ent.id, canonical_name=ent.canonical_name, entity_type=ent.entity_type))

    return TimelinePageOut(
        events=[TimelineEventOut(
            event_id=ev.id, description=ev.description, event_type=ev.event_type,
            event_date=ev.event_date.isoformat() if ev.event_date else None,
            date_precision=ev.date_precision, document_id=ev.document_id,
            bates_begin=bates, title=title,
            participants=participants_by_event.get(ev.id, []),
        ) for ev, bates, title in rows],
        total=total, undated_count=undated_count,
    )
```

Add `TimelineEventOut, TimelinePageOut, TimelineParticipantOut` to the schemas import in `entities.py`, and `Document` to the models import if absent.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend; F:/Users/WCEdm/Documents/Developer/descubre/backend/venv/Scripts/python.exe -m pytest tests/test_timeline_endpoint.py -q`
Expected: 4 passed. Then full suite: no regressions.

- [ ] **Step 6: Commit**

```bash
git add backend/app/routers/entities.py backend/app/schemas.py backend/tests/test_timeline_endpoint.py
git commit -m "feat(ontology): production timeline endpoint"
```

---

### Task 2: Graph API

**Files:**
- Modify: `backend/app/routers/entities.py`, `backend/app/schemas.py`
- Test: `backend/tests/test_graph_endpoint.py`

**Interfaces:**
- Produces: `GET /api/productions/{production_id}/graph?max_nodes=&min_shared_docs=` → `GraphOut = {nodes: [GraphNodeOut], edges: [GraphEdgeOut], truncated: bool}`; `GraphNodeOut = {id, canonical_name, entity_type, mention_count}`; `GraphEdgeOut = {source: UUID4, target: UUID4, kind: "stated"|"cooccurrence", relationship_type: str|None, weight: int}`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_graph_endpoint.py
"""Fake-session tests for the relationship-graph endpoint."""

import asyncio
import uuid

import pytest
from fastapi import HTTPException

import app.routers.entities as er
from app.models import Entity
from tests.fakes import FakeResult, FakeSession, FakeUser

A, B, C = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def _ent(eid, name, count=10):
    return Entity(id=eid, production_id=1, entity_type="person",
                  canonical_name=name, aliases=[], attributes={}, mention_count=count)


def _patch(monkeypatch, accessible=(1,)):
    async def fake_accessible(db, user):
        return list(accessible)
    monkeypatch.setattr(er, "get_accessible_production_ids", fake_accessible)


def test_graph_denies_out_of_scope(monkeypatch):
    _patch(monkeypatch, accessible=(2,))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.get_production_graph(production_id=1, db=FakeSession(), user=FakeUser()))
    assert exc.value.status_code == 404


def test_graph_nodes_edges_and_cooccurrence_dedup(monkeypatch):
    _patch(monkeypatch)
    nodes = [_ent(A, "Jorge Rivera", 30), _ent(B, "Acme Corp", 20), _ent(C, "Ana Cruz", 10)]
    db = FakeSession(responders=[
        ("count", FakeResult(scalar=3)),
        ("FROM entities", FakeResult(items=nodes)),
        # stated edges: (source, target, relationship_type, weight)
        ("FROM entity_relationships", FakeResult(rows=[(A, B, "employment", 2)])),
        # co-occurrence pairs: (a, b, shared) — includes the A-B pair which must be deduped
        ("em_a", FakeResult(rows=[(A, B, 5), (A, C, 3)])),
    ])
    out = asyncio.run(er.get_production_graph(production_id=1, db=db, user=FakeUser()))
    assert {n.id for n in out.nodes} == {A, B, C}
    kinds = {(e.source, e.target): e.kind for e in out.edges}
    assert kinds[(A, B)] == "stated"          # stated wins; no duplicate cooccurrence edge
    assert kinds[(A, C)] == "cooccurrence"
    assert out.truncated is False


def test_graph_truncation_flag(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(responders=[
        ("count", FakeResult(scalar=500)),
        ("FROM entities", FakeResult(items=[_ent(A, "X", 1)])),
        ("FROM entity_relationships", FakeResult(rows=[])),
        ("em_a", FakeResult(rows=[])),
    ])
    out = asyncio.run(er.get_production_graph(production_id=1, max_nodes=1, db=db, user=FakeUser()))
    assert out.truncated is True


def test_graph_clamps_params(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(responders=[
        ("count", FakeResult(scalar=0)),
        ("FROM entities", FakeResult(items=[])),
    ])
    out = asyncio.run(er.get_production_graph(
        production_id=1, max_nodes=10_000, min_shared_docs=0, db=db, user=FakeUser()))
    assert out.nodes == [] and out.edges == []
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_graph_endpoint.py -q` → AttributeError.

- [ ] **Step 3: Schemas** (append to `backend/app/schemas.py`)

```python
class GraphNodeOut(BaseModel):
    id: UUID4
    canonical_name: str
    entity_type: str
    mention_count: int


class GraphEdgeOut(BaseModel):
    source: UUID4
    target: UUID4
    kind: str  # 'stated' | 'cooccurrence'
    relationship_type: str | None = None
    weight: int


class GraphOut(BaseModel):
    nodes: list[GraphNodeOut]
    edges: list[GraphEdgeOut]
    truncated: bool
```

- [ ] **Step 4: Implement** (append to `entities.py`)

```python
@router.get("/productions/{production_id}/graph", response_model=GraphOut)
async def get_production_graph(
    production_id: int,
    max_nodes: int = 75,
    min_shared_docs: int = 2,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=404, detail="Production not found")
    max_nodes = max(1, min(150, max_nodes))
    min_shared_docs = max(1, min_shared_docs)

    total_entities = (await db.execute(
        select(func.count(Entity.id)).where(Entity.production_id == production_id)
    )).scalar() or 0

    node_rows = (await db.execute(
        select(Entity).where(Entity.production_id == production_id)
        .order_by(Entity.mention_count.desc(), Entity.id).limit(max_nodes)
    )).scalars().all()
    node_ids = {e.id for e in node_rows}
    nodes = [GraphNodeOut(id=e.id, canonical_name=e.canonical_name,
                          entity_type=e.entity_type, mention_count=e.mention_count)
             for e in node_rows]

    edges: list[GraphEdgeOut] = []
    stated_pairs: set[frozenset] = set()
    if node_ids:
        srows = (await db.execute(
            select(EntityRelationship.source_entity_id, EntityRelationship.target_entity_id,
                   EntityRelationship.relationship_type,
                   func.count(EntityRelationship.id).label("weight"))
            .where(EntityRelationship.source_entity_id.in_(node_ids),
                   EntityRelationship.target_entity_id.in_(node_ids))
            .group_by(EntityRelationship.source_entity_id, EntityRelationship.target_entity_id,
                      EntityRelationship.relationship_type)
        )).all()
        for src, tgt, rtype, weight in srows:
            stated_pairs.add(frozenset((src, tgt)))
            edges.append(GraphEdgeOut(source=src, target=tgt, kind="stated",
                                      relationship_type=rtype, weight=weight))

        # Co-occurrence among included nodes; a < b ordering avoids duplicate pairs.
        # Same production-invariant note as get_entity_connections: rows for one
        # document always share its production_id (enforced at persist time).
        em_a = EntityMention.__table__.alias("em_a")
        em_b = EntityMention.__table__.alias("em_b")
        crows = (await db.execute(
            select(em_a.c.entity_id, em_b.c.entity_id,
                   func.count(func.distinct(em_a.c.document_id)).label("shared"))
            .select_from(em_a.join(em_b, em_a.c.document_id == em_b.c.document_id))
            .where(em_a.c.entity_id.in_(node_ids), em_b.c.entity_id.in_(node_ids),
                   em_a.c.entity_id < em_b.c.entity_id)
            .group_by(em_a.c.entity_id, em_b.c.entity_id)
            .having(func.count(func.distinct(em_a.c.document_id)) >= min_shared_docs)
        )).all()
        for a, b, shared in crows:
            if frozenset((a, b)) in stated_pairs:
                continue
            edges.append(GraphEdgeOut(source=a, target=b, kind="cooccurrence", weight=shared))

    return GraphOut(nodes=nodes, edges=edges, truncated=total_entities > max_nodes)
```

- [ ] **Step 5: Run tests** → 4 passed; full suite no regressions.
- [ ] **Step 6: Commit** — `feat(ontology): relationship graph endpoint`

---

### Task 3: Entities-summary batch API (chips)

**Files:**
- Modify: `backend/app/routers/entities.py`, `backend/app/schemas.py`
- Test: `backend/tests/test_entities_summary.py`

**Interfaces:**
- Produces: `GET /api/documents/entities-summary?ids=<uuid,uuid,...>` (≤100 ids) → `EntitiesSummaryOut = {summaries: dict[str, list[ChipEntityOut]]}` where `ChipEntityOut = {entity_id, canonical_name, entity_type}` — top 3 entities per doc by per-doc mention count. Unknown/out-of-scope ids silently omitted.

- [ ] **Step 1: Failing tests**

```python
# backend/tests/test_entities_summary.py
import asyncio
import uuid

import pytest
from fastapi import HTTPException

import app.routers.entities as er
from app.models import Entity
from tests.fakes import FakeResult, FakeSession, FakeUser

D1, D2 = uuid.uuid4(), uuid.uuid4()


def _patch(monkeypatch, accessible=(1,)):
    async def fake_accessible(db, user):
        return list(accessible)
    monkeypatch.setattr(er, "get_accessible_production_ids", fake_accessible)


def _ent(name, etype="person"):
    return Entity(id=uuid.uuid4(), production_id=1, entity_type=etype,
                  canonical_name=name, aliases=[], attributes={}, mention_count=1)


def test_summary_groups_top3_per_doc(monkeypatch):
    _patch(monkeypatch)
    ents = [_ent(f"P{i}") for i in range(4)]
    rows = [(D1, ents[i], 10 - i) for i in range(4)] + [(D2, ents[0], 2)]
    db = FakeSession(responders=[("FROM entity_mentions", FakeResult(rows=rows))])
    out = asyncio.run(er.get_entities_summary(ids=f"{D1},{D2}", db=db, user=FakeUser()))
    assert len(out.summaries[str(D1)]) == 3          # top 3 only
    assert out.summaries[str(D1)][0].canonical_name == "P0"
    assert len(out.summaries[str(D2)]) == 1


def test_summary_caps_ids_and_rejects_garbage(monkeypatch):
    _patch(monkeypatch)
    too_many = ",".join(str(uuid.uuid4()) for _ in range(101))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(er.get_entities_summary(ids=too_many, db=FakeSession(), user=FakeUser()))
    assert exc.value.status_code == 422
    out = asyncio.run(er.get_entities_summary(ids="not-a-uuid,,", db=FakeSession(), user=FakeUser()))
    assert out.summaries == {}
```

- [ ] **Step 2: Verify failure.**
- [ ] **Step 3: Schemas**

```python
class ChipEntityOut(BaseModel):
    entity_id: UUID4
    canonical_name: str
    entity_type: str


class EntitiesSummaryOut(BaseModel):
    summaries: dict[str, list[ChipEntityOut]]
```

- [ ] **Step 4: Implement** (append to `entities.py`; route path must not collide with `/documents/{doc_id}/entities` — a literal segment registered on the same router is fine in FastAPI as long as this handler is defined BEFORE the parameterized one matches; FastAPI matches literal paths first regardless of order, but keep the name distinct)

```python
@router.get("/documents/entities-summary", response_model=EntitiesSummaryOut)
async def get_entities_summary(
    ids: str = "",
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Batch: top-3 entities per document for row chips. Scoped; bad ids skipped."""
    raw = [s.strip() for s in ids.split(",") if s.strip()]
    if len(raw) > 100:
        raise HTTPException(status_code=422, detail="Too many ids (max 100)")
    doc_ids = []
    for s in raw:
        try:
            doc_ids.append(UUID(s))
        except ValueError:
            continue
    if not doc_ids:
        return EntitiesSummaryOut(summaries={})

    accessible = await get_accessible_production_ids(db, user)
    rows = (await db.execute(
        select(EntityMention.document_id, Entity,
               func.count(EntityMention.id).label("cnt"))
        .join(Entity, EntityMention.entity_id == Entity.id)
        .where(EntityMention.document_id.in_(doc_ids),
               EntityMention.production_id.in_(accessible))
        .group_by(EntityMention.document_id, Entity.id)
        .order_by(EntityMention.document_id, func.count(EntityMention.id).desc())
    )).all()

    summaries: dict[str, list[ChipEntityOut]] = {}
    for doc_id, ent, _cnt in rows:
        bucket = summaries.setdefault(str(doc_id), [])
        if len(bucket) < 3:
            bucket.append(ChipEntityOut(entity_id=ent.id, canonical_name=ent.canonical_name,
                                        entity_type=ent.entity_type))
    return EntitiesSummaryOut(summaries=summaries)
```

- [ ] **Step 5: Run tests; full suite.**
- [ ] **Step 6: Commit** — `feat(ontology): batch entities-summary endpoint for row chips`

---

### Task 4: Brief key-player resolution

**Files:**
- Modify: `backend/app/services/brief.py` (append helper), `backend/app/routers/productions.py` (`get_pipeline`, ~line 148), `backend/app/schemas.py` (`PipelineStatusOut` gains a field)
- Test: `backend/tests/test_brief_key_players.py`

**Interfaces:**
- Produces: `async resolve_key_players(db, production_id: int, names: list[str]) -> list[dict]` in `services/brief.py` — `[{"name": str, "entity_id": str|None}]`, matching via `entity_resolution.normalize_name` against canonical names AND aliases. `PipelineStatusOut` gains `key_players_resolved: list[KeyPlayerOut] | None = None` (`KeyPlayerOut = {name, entity_id: UUID4|None}`). `get_pipeline` populates it when a brief with key_players exists; ANY exception → field stays None (brief must never break).

- [ ] **Step 1: Failing tests**

```python
# backend/tests/test_brief_key_players.py
import asyncio
import uuid

from app.models import Entity
from app.services.brief import resolve_key_players
from tests.fakes import FakeResult, FakeSession


def _ent(name, aliases=None):
    return Entity(id=uuid.uuid4(), production_id=1, entity_type="person",
                  canonical_name=name, aliases=aliases or [], attributes={}, mention_count=1)


def test_resolves_by_normalized_name_and_alias():
    jorge = _ent("Jorge Rivera")
    acme = _ent("Acme Corp Inc", aliases=["Acme"])
    db = FakeSession(responders=[("FROM entities", FakeResult(items=[jorge, acme]))])
    out = asyncio.run(resolve_key_players(db, 1, ["jorge rivera", "Acme", "Nobody Known"]))
    assert out[0] == {"name": "jorge rivera", "entity_id": str(jorge.id)}
    assert out[1] == {"name": "Acme", "entity_id": str(acme.id)}
    assert out[2] == {"name": "Nobody Known", "entity_id": None}


def test_empty_names_short_circuits():
    assert asyncio.run(resolve_key_players(FakeSession(), 1, [])) == []
```

- [ ] **Step 2: Verify failure.**
- [ ] **Step 3: Implement helper** (append to `backend/app/services/brief.py`)

```python
async def resolve_key_players(db, production_id: int, names: list[str]) -> list[dict]:
    """Match brief key_players strings to ontology entities at read time.

    Read-only enrichment: normalized-name or alias equality, first match wins.
    Never raises past the caller's guard — the brief must render regardless.
    """
    if not names:
        return []
    from sqlalchemy import select
    from app.models import Entity
    from app.services.entity_resolution import normalize_name

    entities = (await db.execute(
        select(Entity).where(Entity.production_id == production_id)
    )).scalars().all()
    by_norm: dict[str, Entity] = {}
    for e in entities:
        for form in [e.canonical_name, *(e.aliases or [])]:
            norm = normalize_name(form)
            if norm:
                by_norm.setdefault(norm, e)

    out = []
    for name in names:
        match = by_norm.get(normalize_name(name))
        out.append({"name": name, "entity_id": str(match.id) if match else None})
    return out
```

- [ ] **Step 4: Wire into `get_pipeline`** (`backend/app/routers/productions.py` ~line 148; read the current function first). Add to `PipelineStatusOut` in `schemas.py`:

```python
class KeyPlayerOut(BaseModel):
    name: str
    entity_id: UUID4 | None = None
```
and on `PipelineStatusOut`: `key_players_resolved: list[KeyPlayerOut] | None = None`

In `get_pipeline`, after the existing response fields are gathered:

```python
    key_players_resolved = None
    if prod.brief and prod.brief.get("key_players"):
        try:
            from app.services.brief import resolve_key_players
            key_players_resolved = await resolve_key_players(
                db, production_id, list(prod.brief["key_players"]))
        except Exception:
            logger.exception("key player resolution failed for production %s", production_id)
```
and pass `key_players_resolved=key_players_resolved` into the `PipelineStatusOut(...)` construction. (Add `logger = logging.getLogger(__name__)` if the module lacks one.)

- [ ] **Step 5: Run tests; full suite.**
- [ ] **Step 6: Commit** — `feat(ontology): resolve brief key players to entities at read time`

---

### Task 5: Frontend client/types + entity deep-link plumbing

**Files:**
- Modify: `frontend/src/types/index.ts`, `frontend/src/api/client.ts`, `frontend/src/hooks/useUrlState.ts`, `frontend/src/App.tsx`, `frontend/src/components/EntitiesView.tsx`

**Interfaces:**
- Types: `TimelineParticipant`, `TimelineEvent`, `TimelinePage`, `GraphNode`, `GraphEdge`, `GraphData`, `ChipEntity`, plus `PipelineInfo` gains `key_players_resolved?: {name: string; entity_id: string | null}[] | null`.
- Client: `getTimeline(productionId, entityId?, eventType?, page?, perPage?)`, `getGraph(productionId, maxNodes?, minSharedDocs?)`, `getEntitiesSummary(ids: string[])`.
- URL: `entity` key joins `VigilistUrlState` (add to the keys array and the sync-deps list in `useUrlState.ts`).
- App: `navigateToEntity(id: string)` — sets `showEntities(true)` (closing other views/doc) and seeds the panel; `EntitiesView` gains props `initialEntityId?: string | null` and `onOpenEntityChange?: (id: string | null) => void`; App mirrors panel state into the URL-sync object (`entity: showEntities ? entityPanelId ?? undefined : undefined`).
- Produces for Tasks 6–9: `navigateToEntity` passed down; later views reuse the same mirror pattern.

- [ ] **Step 1: Types** (append to `frontend/src/types/index.ts`)

```ts
// ── Ontology surfaces ──

export interface TimelineParticipant {
  entity_id: string;
  canonical_name: string;
  entity_type: 'person' | 'org';
}

export interface TimelineEvent {
  event_id: number;
  description: string;
  event_type: string;
  event_date: string | null;
  date_precision: 'day' | 'month' | 'year' | 'unknown';
  document_id: string;
  bates_begin: string;
  title: string | null;
  participants: TimelineParticipant[];
}

export interface TimelinePage {
  events: TimelineEvent[];
  total: number;
  undated_count: number;
}

export interface GraphNode {
  id: string;
  canonical_name: string;
  entity_type: 'person' | 'org';
  mention_count: number;
}

export interface GraphEdge {
  source: string;
  target: string;
  kind: 'stated' | 'cooccurrence';
  relationship_type?: string | null;
  weight: number;
}

export interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
  truncated: boolean;
}

export interface ChipEntity {
  entity_id: string;
  canonical_name: string;
  entity_type: 'person' | 'org';
}
```
Also extend `PipelineInfo` (find it in this file) with `key_players_resolved?: { name: string; entity_id: string | null }[] | null;`

- [ ] **Step 2: Client functions** (append to `frontend/src/api/client.ts`; import the new types)

```ts
export function getTimeline(productionId: number, entityId?: string, eventType?: string, page = 1, perPage = 50) {
  const params = new URLSearchParams({ page: String(page), per_page: String(perPage) });
  if (entityId) params.set('entity_id', entityId);
  if (eventType) params.set('event_type', eventType);
  return request<TimelinePage>(`/api/productions/${productionId}/timeline?${params}`);
}

export const getGraph = (productionId: number, maxNodes = 75, minSharedDocs = 2) =>
  request<GraphData>(`/api/productions/${productionId}/graph?max_nodes=${maxNodes}&min_shared_docs=${minSharedDocs}`);

export const getEntitiesSummary = (ids: string[]) =>
  request<{ summaries: Record<string, ChipEntity[]> }>(`/api/documents/entities-summary?ids=${ids.join(',')}`);
```

- [ ] **Step 3: URL param.** In `useUrlState.ts`: add `entity?: string;` to `VigilistUrlState`, `'entity'` to the keys array in `getInitialUrlState`, and `state.entity` to the `useSyncUrl` dep list.

- [ ] **Step 4: App plumbing.** In `App.tsx` (anchors per current file — state block ~line 90, URL-sync object ~line 136, EntitiesView branch ~line 390, AppHeader wiring ~line 455):

```tsx
const [entityPanelId, setEntityPanelId] = useState<string | null>(initialUrl.entity ?? null);

const navigateToEntity = (id: string) => {
  setShowReview(false);
  setViewDocId(null);
  setEntityPanelId(id);
  setShowEntities(true);
};
```
URL-sync object gains: `entity: showEntities && entityPanelId ? entityPanelId : undefined,`
EntitiesView branch gains props: `initialEntityId={entityPanelId} onOpenEntityChange={setEntityPanelId}`.

- [ ] **Step 5: EntitiesView.** Seed and mirror: `useState<string | null>(initialEntityId ?? null)` for `openEntityId`; wherever `setOpenEntityId(x)` is called, also call `onOpenEntityChange?.(x)` (wrap in a helper `openEntity(id: string | null)` used everywhere).

- [ ] **Step 6: Verify** — `npm run build && npm run lint` (baseline, zero new). **Commit** — `feat(ontology): entity deep-link plumbing + surfaces client/types`

---

### Task 6: Timeline UI

**Files:**
- Create: `frontend/src/components/EntityTimelineView.tsx`
- Modify: `frontend/src/App.tsx`, `frontend/src/components/AppHeader.tsx`

**Interfaces:**
- Consumes: `getTimeline`, `listEntities` (filter dropdown), `EntityPanel`, `navigateToEntity` pattern.
- Produces: `<EntityTimelineView productionId initialEntityId onViewDocument onBack onOpenEntityChange />`; `view=timeline` wiring (state `showTimeline`, URL derivation extends the ternary chain, render branch, `onOpenTimeline` AppHeader prop + "Timeline" button after Entities).

- [ ] **Step 1: Component**

```tsx
import { useCallback, useEffect, useState } from 'react';
import { getTimeline, listEntities } from '../api/client';
import type { EntityListItem, TimelineEvent } from '../types';
import EntityPanel from './EntityPanel';

interface Props {
  productionId: number;
  initialEntityId?: string | null;
  onViewDocument: (docId: string) => void;
  onBack: () => void;
  onOpenEntityChange?: (id: string | null) => void;
}

const TYPE_BADGES: Record<string, string> = {
  meeting: 'Meeting', communication: 'Communication', payment: 'Payment',
  filing: 'Filing', agreement: 'Agreement', other: 'Event',
};

function dateLabel(e: TimelineEvent): string {
  if (!e.event_date) return 'Undated';
  const d = new Date(e.event_date + 'T00:00:00');
  if (e.date_precision === 'year') return String(d.getFullYear());
  if (e.date_precision === 'month') return d.toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

function groupKey(e: TimelineEvent): string {
  if (!e.event_date) return 'Undated';
  const d = new Date(e.event_date + 'T00:00:00');
  if (e.date_precision === 'year') return String(d.getFullYear());
  return d.toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
}

export default function EntityTimelineView({ productionId, initialEntityId, onViewDocument, onBack, onOpenEntityChange }: Props) {
  const [events, setEvents] = useState<TimelineEvent[]>([]);
  const [total, setTotal] = useState(0);
  const [undatedCount, setUndatedCount] = useState(0);
  const [page, setPage] = useState(1);
  const [entityFilter, setEntityFilter] = useState<string>(initialEntityId ?? '');
  const [typeFilter, setTypeFilter] = useState('');
  const [filterOptions, setFilterOptions] = useState<EntityListItem[]>([]);
  const [openEntityId, setOpenEntityId] = useState<string | null>(null);
  const [showUndated, setShowUndated] = useState(false);

  const openEntity = (id: string | null) => { setOpenEntityId(id); onOpenEntityChange?.(id); };

  useEffect(() => {
    listEntities(productionId, undefined, undefined, 1, 100)
      .then(r => setFilterOptions(r.entities))
      .catch(e => console.warn('listEntities failed:', e));
  }, [productionId]);

  const load = useCallback((pageNum: number, append: boolean) => {
    getTimeline(productionId, entityFilter || undefined, typeFilter || undefined, pageNum)
      .then(r => {
        setEvents(prev => (append ? [...prev, ...r.events] : r.events));
        setTotal(r.total);
        setUndatedCount(r.undated_count);
        setPage(pageNum);
      })
      .catch(e => console.warn('getTimeline failed:', e));
  }, [productionId, entityFilter, typeFilter]);

  useEffect(() => { load(1, false); }, [load]);

  const dated = events.filter(e => e.event_date);
  const undated = events.filter(e => !e.event_date);
  const groups: { key: string; items: TimelineEvent[] }[] = [];
  for (const e of dated) {
    const key = groupKey(e);
    const last = groups[groups.length - 1];
    if (last && last.key === key) last.items.push(e);
    else groups.push({ key, items: [e] });
  }

  const renderEvent = (e: TimelineEvent) => (
    <div key={e.event_id} className="card" style={{ padding: 'var(--space-3)', marginBottom: 8 }}>
      <div style={{ display: 'flex', gap: 8, alignItems: 'baseline', flexWrap: 'wrap' }}>
        <span className="badge badge-gray">{TYPE_BADGES[e.event_type] || e.event_type}</span>
        <span style={{ fontSize: 'var(--text-xs)', opacity: 0.7 }}>{dateLabel(e)}</span>
        <button className="btn btn-ghost btn-xs" style={{ marginLeft: 'auto' }}
                onClick={() => onViewDocument(e.document_id)}>
          {e.bates_begin}{e.title ? ` — ${e.title}` : ''}
        </button>
      </div>
      <div style={{ margin: '4px 0' }}>{e.description}</div>
      {e.participants.length > 0 && (
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {e.participants.map(p => (
            <button key={p.entity_id} className="btn btn-ghost btn-xs" onClick={() => openEntity(p.entity_id)}>
              <span className={`entity-dot entity-${p.entity_type}`} style={{ marginRight: 4 }}>●</span>
              {p.canonical_name}
            </button>
          ))}
        </div>
      )}
    </div>
  );

  return (
    <div style={{ position: 'relative', height: '100%', display: 'flex', flexDirection: 'column' }}>
      <div className="panel-header" style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <button className="btn btn-ghost btn-xs" onClick={onBack}>← Back</button>
        <span style={{ fontWeight: 600 }}>Timeline ({total} events)</span>
        <select className="input" value={entityFilter} onChange={e => setEntityFilter(e.target.value)}
                style={{ marginLeft: 'auto', maxWidth: 240 }}>
          <option value="">All people & orgs</option>
          {filterOptions.map(o => <option key={o.id} value={o.id}>{o.canonical_name}</option>)}
        </select>
        <select className="input" value={typeFilter} onChange={e => setTypeFilter(e.target.value)} style={{ maxWidth: 150 }}>
          <option value="">All types</option>
          {Object.entries(TYPE_BADGES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </div>

      <div style={{ flex: 1, overflow: 'auto', padding: 'var(--space-4)' }}>
        {groups.map(g => (
          <div key={g.key}>
            <div className="panel-header" style={{ padding: '8px 0' }}>{g.key}</div>
            {g.items.map(renderEvent)}
          </div>
        ))}
        {events.length < total - undatedCount + undated.length && (
          <button className="btn btn-xs" onClick={() => load(page + 1, true)}>Load more</button>
        )}
        {undatedCount > 0 && (
          <div style={{ marginTop: 16 }}>
            <button className="btn btn-ghost btn-xs" onClick={() => setShowUndated(v => !v)}>
              {showUndated ? '▾' : '▸'} Undated ({undatedCount})
            </button>
            {showUndated && undated.map(renderEvent)}
          </div>
        )}
        {total === 0 && <div className="empty-state">No events extracted yet — run entity extraction from the Entities view.</div>}
      </div>

      {openEntityId && (
        <EntityPanel entityId={openEntityId} onClose={() => openEntity(null)}
                     onOpenEntity={openEntity}
                     onOpenDocument={docId => { openEntity(null); onViewDocument(docId); }} />
      )}
    </div>
  );
}
```

- [ ] **Step 2: App + header wiring.** Mirror the four `showEntities` sites for `showTimeline` / `view === 'timeline'` / `onOpenTimeline` / render branch (`<EntityTimelineView productionId={production.id} initialEntityId={entityPanelId} onOpenEntityChange={setEntityPanelId} onViewDocument={...} onBack={...} />`). AppHeader gains `onOpenTimeline?: () => void` + a "Timeline" button after Entities. URL `view` ternary now: review → entities → timeline → undefined; only one show* flag true at a time (each setter clears the others via the nav callbacks in App: `onOpenTimeline={() => { setShowEntities(false); setShowTimeline(true); }}` etc.).

- [ ] **Step 3: Verify** build+lint baseline. **Commit** — `feat(ontology): timeline view (Phase B)`

---

### Task 7: Graph UI

**Files:**
- Modify: `frontend/package.json` (add deps: `"d3-force": "^3.0.0"`, devDep `"@types/d3-force": "^3.0.10"` — run `npm install d3-force && npm install -D @types/d3-force`)
- Create: `frontend/src/utils/graphLayout.ts` (pure layout helper)
- Create: `frontend/src/components/EntityGraphView.tsx`
- Modify: `frontend/src/App.tsx`, `frontend/src/components/AppHeader.tsx`

**Interfaces:**
- Produces: `computeGraphLayout(nodes, edges, width, height) -> PositionedNode[]` (pure, sync, 300 ticks); `<EntityGraphView productionId initialEntityId onViewDocument onBack onOpenEntityChange />`; `view=graph` wiring + "Graph" nav button.

- [ ] **Step 1: Layout helper**

```ts
// frontend/src/utils/graphLayout.ts
import { forceCenter, forceCollide, forceLink, forceManyBody, forceSimulation } from 'd3-force';
import type { GraphEdge, GraphNode } from '../types';

export interface PositionedNode extends GraphNode {
  x: number;
  y: number;
  r: number;
}

export function nodeRadius(mentionCount: number): number {
  return Math.max(8, Math.min(28, 6 + Math.sqrt(mentionCount) * 2));
}

/** Run the force simulation to convergence synchronously — calm first paint. */
export function computeGraphLayout(nodes: GraphNode[], edges: GraphEdge[], width: number, height: number): PositionedNode[] {
  if (nodes.length === 0) return [];
  const simNodes = nodes.map(n => ({ ...n, r: nodeRadius(n.mention_count), x: 0, y: 0 }));
  if (simNodes.length === 1) return [{ ...simNodes[0], x: width / 2, y: height / 2 }];
  const simLinks = edges.map(e => ({ source: e.source, target: e.target, weight: e.weight }));
  const sim = forceSimulation(simNodes as never[])
    .force('charge', forceManyBody().strength(-180))
    .force('link', forceLink(simLinks as never[]).id((d: { id?: string }) => d.id ?? '').distance(90))
    .force('collide', forceCollide().radius((d: { r?: number }) => (d.r ?? 10) + 6))
    .force('center', forceCenter(width / 2, height / 2))
    .stop();
  for (let i = 0; i < 300; i++) sim.tick();
  return simNodes as unknown as PositionedNode[];
}
```

- [ ] **Step 2: Graph view** — SVG rendering over the settled layout; state: `layout: PositionedNode[]`, `transform {x,y,k}`, `openEntityId`, `hoverEdge`. Fetch `getGraph` on mount (in effect, setState in `.then`), compute layout in the `.then` (measuring the container via a ref read inside the callback — allowed, not render). Interactions all in pointer handlers: node drag mutates that node's x/y in state (no re-simulation); wheel zoom multiplies `k` (clamp 0.3–3) around the pointer; background drag pans. Edges: `<line>`, stated solid `stroke: var(--color-border, #999)` opacity .7, cooccurrence dashed `strokeDasharray 4 3` opacity .25 + width `Math.min(4, weight/2)`; hover title via `<title>{relationship_type}</title>`. Nodes: `<circle>` fill by type (#4f7cff/#b4690e), white 1.5px stroke, `<text>` label at `x + r + 4` hidden when `k < 0.7 && nodes > 40`. Click node → `openEntity(id)` (same helper/mirror pattern as Timeline). `truncated` → note line "Showing top {nodes.length} entities by mentions." Empty state mirrors Timeline's. EntityPanel mounted the same way.

Full component code is the implementer's to assemble from these exact behaviors + the Timeline component's structure (header row with Back + title + note, `position: relative` root, panel mount); everything unusual (layout call, transform math) is specified here:

```tsx
// zoom (wheel handler on the svg):
const onWheel = (ev: React.WheelEvent) => {
  const factor = ev.deltaY < 0 ? 1.1 : 1 / 1.1;
  setTransform(t => ({ ...t, k: Math.max(0.3, Math.min(3, t.k * factor)) }));
};
// render group: <g transform={`translate(${transform.x},${transform.y}) scale(${transform.k})`}>
// drag: onPointerDown captures target (node id or background), onPointerMove updates
// either that node's x/y or transform x/y by movementX/movementY / k; onPointerUp clears.
```

- [ ] **Step 3: App + header wiring** — fourth view `showGraph` / `view === 'graph'` / "Graph" button, same mirror as Task 6.
- [ ] **Step 4: Verify** build+lint (new dep installs cleanly; lockfile updated and committed). **Commit** — `feat(ontology): relationship graph view (Phase C, d3-force)`

---

### Task 8: Ambient chips — document rows, search results, brief key players

**Files:**
- Modify: `frontend/src/App.tsx` (doc table ~751-814 + grid ~821-852 + summaries state), `frontend/src/components/SearchResults.tsx`, `frontend/src/components/ProductionBrief.tsx`

**Interfaces:**
- Consumes: `getEntitiesSummary`, `ChipEntity`, `navigateToEntity`, `PipelineInfo.key_players_resolved`.
- Produces: chips (≤3/row) on document-list rows (both views) and search results; brief key players render as clickable chips when resolved.

- [ ] **Step 1: App summaries cache.** Mirror the `ProductionBrief.tsx:205-227` batch pattern: `const [entityChips, setEntityChips] = useState<Record<string, ChipEntity[]>>({})` + `const chipsFetched = useRef<Set<string>>(new Set())`. Effect on the current `displayDocs` (and search results list): collect ids not in `chipsFetched`, mark them, one `getEntitiesSummary(ids)` call in the effect (async, setState in `.then`, failures silent). Chip renderer helper:

```tsx
const entityChip = (c: ChipEntity) => (
  <button key={c.entity_id} className="badge badge-gray" style={{ cursor: 'pointer' }}
          onClick={ev => { ev.stopPropagation(); navigateToEntity(c.entity_id); }}>
    <span className={`entity-dot entity-${c.entity_type}`} style={{ marginRight: 3 }}>●</span>
    {c.canonical_name}
  </button>
);
```
Table rows: new `<td className="meta-cell">{(entityChips[d.id] || []).slice(0, 3).map(entityChip)}</td>` beside the theme/AI cells (plus a header `<th>`); grid cards: append into `.doc-grid-meta`.

- [ ] **Step 2: SearchResults.** New optional props `entityChips?: Record<string, ChipEntity[]>` and `onOpenEntity?: (id: string) => void`; render up to 3 chips in `.result-header` after the tag badges (stopPropagation on click). App passes both.

- [ ] **Step 3: Brief.** In `ProductionBrief.tsx` key-players row (~354-360): when `pipeline.key_players_resolved` is available, render each player as a chip — matched (`entity_id`) → clickable via new optional prop `onOpenEntity?: (id: string) => void`; unmatched → plain span. Fallback to the current comma-join when the field is absent. App passes `onOpenEntity={navigateToEntity}` at the `ProductionBrief` mount (~line 507).

- [ ] **Step 4: Verify** build+lint. **Commit** — `feat(ontology): entity chips on rows, results, and brief key players`

---

### Task 9: Chat entity links

**Files:**
- Modify: `backend/app/services/ai.py` (`CHAT_SYSTEM_PROMPT`, cite bullet ~line 50)
- Modify: `frontend/src/utils/chatMarkdown.tsx`, `frontend/src/components/ChatPanel.tsx`, `frontend/src/App.tsx` (pass `onOpenEntity` to the chat mount(s) — find both ChatPanel/ContextRail usages)
- Test: `backend/tests/test_ai_chat.py` or a new `test_chat_prompt_entity_cite.py` (assert the prompt mentions the entity: scheme); frontend verified by build/lint

**Interfaces:**
- Produces: chat renders `[Name](entity:<uuid>)` as `.chat-entity-link` buttons; clicking navigates to the entity profile; system prompt instructs the model to cite entities that came from `lookup_entity`.

- [ ] **Step 1: Backend prompt.** In `CHAT_SYSTEM_PROMPT` (services/ai.py), after the doc-cite bullet add:

```
- When you identify a person or organization using the lookup_entity tool, cite them as a markdown link using the entity: scheme — [<Name>](entity:<entity_id>) with the exact entity_id from the tool result. Only use ids returned by lookup_entity; never invent them.
```
Test:

```python
# append to backend/tests/test_ai_chat.py (or new file, matching its style)
def test_chat_system_prompt_includes_entity_cite_scheme():
    from app.services.ai import CHAT_SYSTEM_PROMPT
    assert "entity:" in CHAT_SYSTEM_PROMPT and "lookup_entity" in CHAT_SYSTEM_PROMPT
```

- [ ] **Step 2: Renderer.** In `chatMarkdown.tsx`: extend `INLINE_PATTERN`'s first alternative to also match entity links (add `\[[^\]]+\]\(entity:[^)]+\)` as an alternative before the generic link), add `const ENTITY_LINK = /^\[([^\]]+)\]\(entity:([^)]+)\)$/;` and a branch mirroring the DOC_LINK one:

```tsx
const entityLink = part.match(ENTITY_LINK);
if (entityLink) {
  out.push(
    <button key={key} type="button" className="chat-entity-link" data-entity-target={entityLink[2].trim()}>
      {entityLink[1]}
    </button>,
  );
  continue;
}
```
(Ensure the bold-recursion path is preserved; add the same branch there if DOC_LINK has one.) Style: add `.chat-entity-link` next to `.chat-doc-link`'s CSS rules with the same look.

- [ ] **Step 3: Click handling.** In `ChatPanel.tsx`'s `handleBodyClick` (~line 59), before the doc-link branch:

```tsx
const entityLink = (e.target as HTMLElement).closest<HTMLElement>('.chat-entity-link');
if (entityLink && onOpenEntity) {
  const target = entityLink.dataset.entityTarget || '';
  if (UUID_RE.test(target)) onOpenEntity(target);
  return;
}
```
New optional prop `onOpenEntity?: (id: string) => void` threaded from App (`navigateToEntity`) through every ChatPanel mount (check ContextRail too — pass through if it wraps ChatPanel).

- [ ] **Step 4: Verify** backend test + full suite; frontend build+lint. **Commit** — `feat(ontology): chat entity links (entity: scheme)`

---

### Task 10: Final verification + PR

- [ ] Backend: full suite — expect prior baseline + new tests, 2 skipped, 1 pre-existing failure only.
- [ ] Frontend: `npm run build` clean; `npm run lint` at baseline (4 errors/6 warnings, zero new).
- [ ] `git fetch origin main` — if main moved, merge it in (NO migration concerns this time — verify with `ls backend/alembic/versions` that we added nothing) and re-run both suites.
- [ ] Push `feat/ontology-surfaces`; open PR titled "feat(ontology): timeline, relationship graph, ambient weaving" summarizing the three surfaces, the single new dep (d3-force), and zero-migration/zero-extraction-cost nature. End body with the Claude Code footer.

## Self-Review Notes

- Spec coverage: timeline (T1+T6), graph (T2+T7), deep links (T5), brief linkification (T4+T8), chips (T3+T8), chat links (T9) — all spec sections mapped.
- Type consistency: `TimelinePageOut/TimelinePage`, `GraphOut/GraphData`, `ChipEntityOut/ChipEntity`, `resolve_key_players` return shape vs `KeyPlayerOut` — names verified consistent across tasks.
- The graph co-occurrence `em_a.c.entity_id < em_b.c.entity_id` comparison works on UUID columns in Postgres (byte ordering) — fine for pair-dedup purposes.
