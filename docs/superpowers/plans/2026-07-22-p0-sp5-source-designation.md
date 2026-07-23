# P0-SP5 Source Designation + Workspace Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-document source designation (`source_party` label + `collection`/`received` type) set at ingest, filterable in search/browse, excluded from outbound sets by default, flagged as a validation conflict — plus an Incoming/Outgoing/All workspace toggle.

**Architecture:** Two Document columns mirroring `custodian`; values ride `IngestJob.field_mapping` and are stamped centrally in the three batch processors (no constructor threading). Typed-column filters in search/documents; a distinct-values endpoint; `exclude_received` + `received_document` conflict in the production-set layer; frontend wizard block, dropdowns, and localStorage-persisted mode toggle.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, React 19 + TS, pytest fake-session tests.

**Spec:** `docs/superpowers/specs/2026-07-22-p0-sp5-source-designation-design.md`

## Global Constraints

- Branch `feat/p0-sp5-source-designation` (stacked on P2-3.5; PR base = `feat/p2-3-5-production-validation`).
- Migration `e1f2a3b4c5d6`, `down_revision = "d0e1f2a3b4c5"`, no `app.*` imports.
- `source_type` values (exact): `collection`, `received`. NULL = undesignated and is NEVER treated as received.
- `source_party` may also arrive per-document from a mapped load-file column; the job-level value is a fallback only (`_stamp_source` never overwrites).
- Tuple-shape changes ripple: `add_documents` info query grows to 4 columns, `compute_conflicts` doc query to 6 — existing tests' responder rows must be updated in the same task.
- Tests 0 warnings; frontend gate = `npm run build` passes; no AI-attribution trailers.

---

### Task 1: Migration + model columns

**Files:**
- Create: `backend/alembic/versions/e1f2a3b4c5d6_add_source_designation.py`
- Modify: `backend/app/models.py` (`Document`, after `custodian` ~line 125)

- [ ] **Step 1: Migration**

```python
"""add document source designation

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa

revision = "e1f2a3b4c5d6"
down_revision = "d0e1f2a3b4c5"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("documents", sa.Column("source_party", sa.String(length=255), nullable=True))
    op.add_column("documents", sa.Column("source_type", sa.String(length=20), nullable=True))
    op.create_index("ix_documents_source_party", "documents", ["source_party"])
    op.create_index("ix_documents_source_type", "documents", ["source_type"])


def downgrade():
    op.drop_index("ix_documents_source_type", table_name="documents")
    op.drop_index("ix_documents_source_party", table_name="documents")
    op.drop_column("documents", "source_type")
    op.drop_column("documents", "source_party")
```

- [ ] **Step 2: Model** — in `Document` directly after the `custodian` column:

```python
    # P0-SP5 — source designation (who this load came from; set per ingest)
    source_party = Column(String(255), nullable=True, index=True)
    source_type = Column(String(20), nullable=True, index=True)  # 'collection' | 'received'
```

- [ ] **Step 3: Verify** — py_compile, `import app.models`, grep `d0e1f2a3b4c5` → two hits, purity.
- [ ] **Step 4: Commit** — `feat(p0-sp5): source_party/source_type columns on documents`.

---

### Task 2: Ingest propagation

**Files:**
- Modify: `backend/app/routers/ingest.py` (`start_processing`, ~line 134)
- Modify: `backend/app/services/ingest.py` (`_stamp_source` helper; stamp in `ingest_batch` and `ingest_pdf_batch`)
- Modify: `backend/app/services/ingest_native.py` (stamp in `ingest_native_batch`)
- Modify: `backend/app/services/metadata_normalize.py` (`_METADATA_TARGETS`)
- Modify: `backend/app/services/field_mapping.py` (canonical list + aliases)
- Test: `backend/tests/test_source_designation.py` (new)

**Interfaces:** `_stamp_source(doc, job)` in `app.services.ingest`; `/api/ingest/process` accepts `source_party`, `source_type`.

- [ ] **Step 1: Failing tests** — `backend/tests/test_source_designation.py`:

```python
"""Tests for document source designation (P0-SP5)."""

import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException

import app.routers.ingest as ri
from app.services.ingest import _stamp_source
from tests.fakes import FakeSession, FakeUser, _fill_timestamps


class FakeDocLike:
    def __init__(self, source_party=None, source_type=None):
        self.source_party = source_party
        self.source_type = source_type


class FakeJob:
    def __init__(self, field_mapping):
        self.field_mapping = field_mapping


def test_stamp_source_fills_from_job():
    doc = FakeDocLike()
    _stamp_source(doc, FakeJob({"source_party": "ABC Corp", "source_type": "received"}))
    assert doc.source_party == "ABC Corp"
    assert doc.source_type == "received"


def test_stamp_source_never_overwrites_mapped_value():
    doc = FakeDocLike(source_party="From DAT Column")
    _stamp_source(doc, FakeJob({"source_party": "Job Level", "source_type": "collection"}))
    assert doc.source_party == "From DAT Column"
    assert doc.source_type == "collection"


def test_stamp_source_handles_missing_mapping():
    doc = FakeDocLike()
    _stamp_source(doc, FakeJob(None))
    assert doc.source_party is None
    assert doc.source_type is None


# --- /api/ingest/process ----------------------------------------------------

class UuidFakeSession(FakeSession):
    """IngestJob ids are UUIDs; the base fake assigns ints."""

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid4()
        _fill_timestamps(obj)
        self.added.append(obj)


class FakeProduction:
    def __init__(self, owner="u1"):
        self.id = 1
        self.name = "Matter"
        self.owner_id = owner


class FakeBackgroundTasks:
    def __init__(self):
        self.tasks = []

    def add_task(self, fn, *args, **kwargs):
        self.tasks.append((fn, args, kwargs))


def _patch_ingest(monkeypatch):
    import app.services.tasks as task_service
    monkeypatch.setattr(task_service, "is_configured", lambda: False)


def test_process_rejects_bad_source_type(monkeypatch):
    _patch_ingest(monkeypatch)
    db = UuidFakeSession(get_objects={("Production", 1): FakeProduction()})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(ri.start_processing(
            body={"production_id": 1, "source_format": "native",
                  "source_type": "maybe"},
            background_tasks=FakeBackgroundTasks(), db=db, user=FakeUser()))
    assert exc.value.status_code == 422


def test_process_folds_source_into_field_mapping(monkeypatch):
    _patch_ingest(monkeypatch)
    db = UuidFakeSession(get_objects={("Production", 1): FakeProduction()})
    out = asyncio.run(ri.start_processing(
        body={"production_id": 1, "source_format": "native", "custodian": "Jane",
              "source_party": "ABC Corp", "source_type": "received",
              "total_files": 3},
        background_tasks=FakeBackgroundTasks(), db=db, user=FakeUser()))
    job = db.added[0]
    assert job.field_mapping["custodian"] == "Jane"
    assert job.field_mapping["source_party"] == "ABC Corp"
    assert job.field_mapping["source_type"] == "received"
    assert out.total_files == 3
```

- [ ] **Step 2: Verify fail** — ImportError on `_stamp_source`.

- [ ] **Step 3: Implement**

(a) `services/ingest.py` — add near `_apply_metadata`:

```python
def _stamp_source(doc, job) -> None:
    """Stamp job-level source designation; a load-file-mapped source_party wins."""
    fm = job.field_mapping or {}
    if getattr(doc, "source_party", None) is None:
        doc.source_party = fm.get("source_party")
    if getattr(doc, "source_type", None) is None:
        doc.source_type = fm.get("source_type")
```

In `ingest_batch` (relativity) and `ingest_pdf_batch`: call `_stamp_source(doc, job)` immediately after the processor returns a non-None `doc`, before `_persist_document`.

(b) `services/ingest_native.py` — in `ingest_native_batch`, after building `docs` (email path) apply to each, and after `process_native_record` returns `doc`:

```python
                for d in docs:
                    _stamp_source(d, job)
```
```python
                _stamp_source(doc, job)
```
(import `_stamp_source` inside the function beside the other `app.services.ingest` imports).

(c) `routers/ingest.py` — in `start_processing`, replace the `field_mapping` block:

```python
    source_format = body.get("source_format", "relativity")
    field_mapping = body.get("field_mapping") or {}
    if source_format == "native":
        field_mapping = {"custodian": (body.get("custodian") or "").strip() or None}
    source_party = (body.get("source_party") or "").strip() or None
    source_type = body.get("source_type") or None
    if source_type not in (None, "collection", "received"):
        raise HTTPException(status_code=422, detail="source_type must be 'collection' or 'received'")
    field_mapping = {**field_mapping, "source_party": source_party, "source_type": source_type}
```

(d) `metadata_normalize.py` — add `"source_party"` to `_METADATA_TARGETS`.

(e) `field_mapping.py` — add `"source_party"` to the canonical list (line ~13) and aliases:

```python
    "source_party": ["Source Party", "Producing Party", "Party", "Production Source"],
```

- [ ] **Step 4: Verify pass** — 5 passed; also run `test_phase0_sp1` / ingest-related suites if present (`pytest backend/tests -q -k "ingest or metadata"`) to prove no regression.
- [ ] **Step 5: Commit** — `feat(p0-sp5): source designation through ingest (job-level stamp + DAT mapping)`.

---

### Task 3: Search / browse filters + source-parties endpoint

**Files:**
- Modify: `backend/app/routers/search.py`, `backend/app/services/search.py`
- Modify: `backend/app/routers/documents.py` (list filters + `GET /api/documents/source-parties`)
- Test: append to `backend/tests/test_source_designation.py`

Note: the distinct-values endpoint lives in `documents.py` beside `metadata-keys` (registered BEFORE `/documents/{id}` routes) with `production_id` as a query param — matching that file's conventions rather than the spec's `/productions/...` path sketch.

- [ ] **Step 1: Failing tests** — append:

```python
# --- search filters ---------------------------------------------------------

from app.services.search import search_documents
from tests.fakes import FakeResult


def test_search_applies_source_filters():
    db = FakeSession()
    asyncio.run(search_documents(
        db, "", production_id=1, accessible_production_ids=[1],
        source_party="ABC Corp", source_type="received"))
    joined = "\n".join(db.executed)
    assert "documents.source_party" in joined
    assert "documents.source_type" in joined


def test_search_source_filter_alone_is_enough():
    # the no-criteria early return must not swallow source-only browsing
    db = FakeSession()
    results, total = asyncio.run(search_documents(
        db, "", accessible_production_ids=[1], source_type="collection"))
    assert results == []
    assert len(db.executed) >= 1  # it actually queried


def test_source_parties_endpoint(monkeypatch):
    import app.routers.documents as rd

    async def fake_accessible(db, user):
        return [1]

    monkeypatch.setattr(rd, "get_accessible_production_ids", fake_accessible)
    db = FakeSession(responders=[
        ("source_party", FakeResult(rows=[("ABC Corp",), ("Our Collection",)])),
    ])
    out = asyncio.run(rd.list_source_parties(production_id=1, db=db, user=FakeUser()))
    assert out == {"source_parties": ["ABC Corp", "Our Collection"]}


def test_source_parties_403(monkeypatch):
    import app.routers.documents as rd

    async def fake_accessible(db, user):
        return [2]

    monkeypatch.setattr(rd, "get_accessible_production_ids", fake_accessible)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rd.list_source_parties(production_id=1, db=FakeSession(), user=FakeUser()))
    assert exc.value.status_code == 403
```

- [ ] **Step 2: Verify fail.**

- [ ] **Step 3: Implement**

(a) `services/search.py` `search_documents` — add params `source_party: str | None = None, source_type: str | None = None`; extend the early-return guard:

```python
    if (not has_text_query and not metadata_filters and not file_type
            and not source_party and not source_type):
        return [], 0
```

and after the `file_type` block:

```python
    if source_party:
        conditions.append(Document.source_party == source_party)
    if source_type:
        conditions.append(Document.source_type == source_type)
```

(b) `routers/search.py` — add query params `source_party: str | None = None` and `source_type: str | None = Query(None, pattern="^(collection|received)$")`; pass both to `search_documents`; extend the semantic-mode guard so semantic search is skipped when they're set (same treatment as `metadata_filters`/`file_type`).

(c) `routers/documents.py` — `list_documents` gains the same two query params and mirrored `query`/`count_query` conditions:

```python
    if source_party:
        query = query.where(Document.source_party == source_party)
        count_query = count_query.where(Document.source_party == source_party)
    if source_type:
        query = query.where(Document.source_type == source_type)
        count_query = count_query.where(Document.source_type == source_type)
```

New endpoint beside `metadata-keys` (before any `/documents/{id}` route):

```python
@router.get("/documents/source-parties")
async def list_source_parties(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    rows = (await db.execute(
        select(Document.source_party)
        .where(Document.production_id == production_id,
               Document.source_party.is_not(None))
        .distinct()
        .order_by(Document.source_party)
    )).all()
    return {"source_parties": [r[0] for r in rows]}
```

(d) Add `source_party: str | None = None` and `source_type: str | None = None` to the document schema that already carries `custodian` (`schemas.py:578` block) so results expose them.

- [ ] **Step 4: Verify pass** — new tests + `pytest backend/tests -q -k "search or documents"`.
- [ ] **Step 5: Commit** — `feat(p0-sp5): source filters in search/browse + distinct-parties endpoint`.

---

### Task 4: Production-set integration (exclude_received + conflict)

**Files:**
- Modify: `backend/app/schemas.py` (`ProductionSetAddDocuments.exclude_received`)
- Modify: `backend/app/routers/production_sets.py` (`add_documents`)
- Modify: `backend/app/services/production_validation.py` (`received_document` category)
- Test: update + append `backend/tests/test_production_set_endpoints.py`, `backend/tests/test_production_validation.py`

- [ ] **Step 1: Update existing tests for new tuple shapes + response key**

- `add_documents` info query grows to `(id, production_id, family_id, source_type)`: every `("documents.production_id", FakeResult(rows=[...]))` in the add tests gains a trailing `None` (e.g. `(d1, 1, None, None)`).
- `test_add_explicit_docs` expected dict gains `"skipped_received": 0`.
- `compute_conflicts` doc query grows to `(id, control, override, image_paths, source_type, source_party)`: every doc_rows tuple in `test_production_validation.py` and the P2-3.5 endpoint tests gains two trailing values (`None, None`).

- [ ] **Step 2: New failing tests**

Append to `test_production_validation.py`:

```python
def test_received_document_conflict():
    d1 = uuid4()
    db = _db([(d1, "C-1", None, ["p1.jpg"], "received", "ABC Corp")])
    out = _run(db, [d1])
    assert len(out["received_document"]) == 1
    assert "ABC Corp" in out["received_document"][0]["detail"]
    assert out["total"] == 1


def test_null_source_type_not_flagged():
    d1 = uuid4()
    db = _db([(d1, "C-1", None, ["p1.jpg"], None, None)])
    out = _run(db, [d1])
    assert out["received_document"] == []
```

(the `_db` helper's doc_rows docstring updates to the 6-tuple shape.)

Append to `test_production_set_endpoints.py`:

```python
def test_add_exclude_received_drops_received_docs(monkeypatch):
    _patch(monkeypatch)
    d1, d2 = uuid4(), uuid4()
    db = FakeSession(
        get_objects={("ProductionSet", 1): FakePS()},
        responders=[
            ("document_tags", FakeResult(rows=[(d1,), (d2,)])),
            ("source_type", FakeResult(rows=[(d2,)])),  # d2 is received
            ("documents.production_id", FakeResult(rows=[(d1, 1, None, None), (d2, 1, None, "received")])),
        ],
    )
    out = asyncio.run(rps.add_documents(
        set_id=1,
        body=ProductionSetAddDocuments(tag_id=5, exclude_received=True),
        db=db, user=FakeUser()))
    assert out["added"] == 1
    assert out["skipped_received"] == 1


def test_add_exclude_received_never_drops_explicit(monkeypatch):
    _patch(monkeypatch)
    d1 = uuid4()
    db = FakeSession(
        get_objects={("ProductionSet", 1): FakePS()},
        responders=[
            ("source_type", FakeResult(rows=[(d1,)])),
            ("documents.production_id", FakeResult(rows=[(d1, 1, None, "received")])),
        ],
    )
    out = asyncio.run(rps.add_documents(
        set_id=1,
        body=ProductionSetAddDocuments(document_ids=[d1], exclude_received=True),
        db=db, user=FakeUser()))
    assert out["added"] == 1
    assert out["skipped_received"] == 0
```

Responder-order note: `"source_type"` must precede `"documents.production_id"`? No — the received-filter query selects `Document.id ... WHERE ... documents.source_type == 'received'`; it contains BOTH substrings, so register `("source_type", ...)` BEFORE `("documents.production_id", ...)`. The info query contains `documents.source_type` in its SELECT list too — so the info query would also match `"source_type"` first! Distinct substring needed: the received-filter query is the only one with `source_type =` in a WHERE clause; its SQL contains `documents.source_type = `. Use responder substring `"source_type ="` for the received filter (unique), keeping `"documents.production_id"` for the info query. The tests above use `("source_type =", ...)` — adjust when writing.

- [ ] **Step 3: Implement**

(a) `ProductionSetAddDocuments` gains `exclude_received: bool = False`.

(b) `add_documents` info query becomes:

```python
            select(Document.id, Document.production_id, Document.family_id,
                   Document.source_type)
```

After family expansion, before the duplicate filter:

```python
    skipped_received = 0
    if body.exclude_received:
        rec_rows = (await db.execute(
            select(Document.id)
            .where(Document.id.in_(candidates),
                   Document.source_type == "received")
        )).all()
        for (did,) in rec_rows:
            if did not in explicit:
                candidates.discard(did)
                skipped_received += 1
```

`summary` gains `"skipped_received": skipped_received`.

(c) `production_validation.py` — doc query gains `Document.source_type, Document.source_party`; the loop unpacks 6 values; new category:

```python
        if source_type == "received":
            out["received_document"].append({
                "document_id": str(did), "control_number": control,
                "detail": f"received from {source_party or 'another party'} — "
                          "not part of our collection"})
```

`out` initializes `"received_document": []` and `total` sums all four lists.

- [ ] **Step 4: Verify pass** — both files + full production-set suite, 0 warnings.
- [ ] **Step 5: Commit** — `feat(p0-sp5): exclude_received in builder + received_document conflict`.

---

### Task 5: Frontend (wizard block, filters, mode toggle)

**Files:**
- Modify: `frontend/src/api/client.ts` (`startProcessing`, `searchDocuments`, `listDocuments`, new `getSourceParties`)
- Modify: `frontend/src/components/IngestWizard.tsx` (source block for all modes)
- Modify: `frontend/src/App.tsx` (mode toggle + source dropdown + wiring)

- [ ] **Step 1: client.ts**

`startProcessing` gains trailing params `sourceParty = ''`, `sourceType = 'collection'` and posts `source_party: sourceParty, source_type: sourceType`. `searchDocuments` gains `sourceParty?: string, sourceType?: string` → `params.set("source_party"/"source_type", ...)` when set; same for `listDocuments`. Add:

```typescript
export const getSourceParties = (productionId: number) =>
  request<{ source_parties: string[] }>(`/api/documents/source-parties?production_id=${productionId}`);
```

- [ ] **Step 2: IngestWizard**

State: `const [sourceType, setSourceType] = useState<'collection' | 'received'>('collection');` and `const [sourceParty, setSourceParty] = useState('');`. A "Document source" block in the setup stage (all modes), styled like the existing mode buttons: two buttons *Our collection* / *Received production* toggling `sourceType`, plus a labeled text input for `sourceParty` (placeholder `Our Collection` vs `e.g. ABC Corp`). Both `startProcessing` call sites pass `sourceParty, sourceType` (relativity call passes `''` for custodian positionally per current signature — keep argument order `(prodId, count, mode, mapping, custodian, sourceParty, sourceType)`).

- [ ] **Step 3: App.tsx**

- State: `filterSourceParty` (string, '' = all), `workMode` (`'all' | 'incoming' | 'outgoing'`) initialized from `localStorage.getItem('vigilist:mode:' + production.id) ?? 'all'`; setter persists back.
- `const sourceTypeParam = workMode === 'incoming' ? 'received' : workMode === 'outgoing' ? 'collection' : undefined;`
- Pass `filterSourceParty || undefined` and `sourceTypeParam` through `listDocuments` and `searchDocuments` calls; add both to the browse-reload `useEffect` deps and the search re-run effect.
- Header: a three-button segmented control (`All / Incoming / Outgoing`, `btn btn-xs` pattern) beside the existing header controls.
- Source dropdown beside each file-type `<select>` (search + browse views): options "All sources" + values from `getSourceParties(production.id)` loaded in the initial `useEffect`.

- [ ] **Step 4: Verify** — `cd frontend && npm run build` passes (tsc + vite).
- [ ] **Step 5: Commit** — `feat(p0-sp5): ingest source block, source filters, incoming/outgoing workspace toggle`.

---

### Task 6: Full-suite verification + PR

- [ ] **Step 1:** `backend\venv\Scripts\python.exe -m pytest backend\tests -q` — only the known `test_ai_review` failure. `npm run build` green.
- [ ] **Step 2:** Migration head/purity re-check.
- [ ] **Step 3:** Push; PR base `feat/p2-3-5-production-validation`, title `feat(p0-sp5): document source designation + workspace mode`, body covering: columns mirroring custodian, ingest propagation (job-level + DAT-mappable), search/browse filters + distinct-parties endpoint, exclude_received + received_document conflict, wizard block + Incoming/Outgoing toggle. No attribution trailer.
