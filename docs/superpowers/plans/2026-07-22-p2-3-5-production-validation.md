# P2-3.5 Production Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Relativity-style pre-lock validation: surface QC/privilege/no-image conflicts, block lock until they're resolved or explicitly overridden, and record the override (who/when) on the set + audit log.

**Architecture:** `compute_conflicts(db, ps, doc_ids)` in a new `app/services/production_validation.py` reuses `effective_disposition`/`qc_status` from Phase 1. `GET .../validation` exposes it; `POST .../lock` gains an optional `{"override_conflicts": bool}` body and two new audited columns.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, pytest fake-session tests.

**Spec:** `docs/superpowers/specs/2026-07-22-p2-3-5-production-validation-design.md`

## Global Constraints

- Branch `feat/p2-3-5-production-validation` (stacked on P2-3; PR base = `feat/p2-3-loadfiles-packaging`). Verify branch before commits.
- Migration `d0e1f2a3b4c5`, `down_revision = "c9d0e1f2a3b4"`, no `app.*` imports.
- Conflict categories (exact keys): `qc_pending`, `privilege_produce`, `no_images`, plus `total`.
- Dispositions derived LIVE via `effective_disposition` (works on draft sets); QC freshness via `qc_status` with the QC queue's tie-break (`decided_at desc, id desc`).
- Existing lock gates (draft-only 409, empty 422) run BEFORE the conflict check.
- Fake-session responder substrings for the service's queries: docs → `"documents.image_paths"`, privilege → `"is_privilege"`, redactions agg → `"coalesce"` (register BEFORE any plain `"redactions"` responder), QC → `"redaction_qc_decisions"`. `compute_conflicts` takes `doc_ids` explicitly — it runs no membership query.
- Tests 0 warnings; no AI-attribution trailers on commits/PR.

---

### Task 1: Migration + model columns

**Files:**
- Create: `backend/alembic/versions/d0e1f2a3b4c5_add_conflict_override.py`
- Modify: `backend/app/models.py` (`ProductionSet` after `packaged_at`)

- [ ] **Step 1: Migration**

```python
"""add production-set conflict override audit fields

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa

revision = "d0e1f2a3b4c5"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("production_sets", sa.Column("conflicts_overridden_by", sa.String(length=128), nullable=True))
    op.add_column("production_sets", sa.Column("conflicts_overridden_at", sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column("production_sets", "conflicts_overridden_at")
    op.drop_column("production_sets", "conflicts_overridden_by")
```

- [ ] **Step 2: Model** — in `ProductionSet` after `packaged_at`:

```python
    # P2-3.5 — validation conflict override audit (Relativity "Restriction override by/on")
    conflicts_overridden_by = Column(String(128), nullable=True)
    conflicts_overridden_at = Column(DateTime, nullable=True)
```

- [ ] **Step 3: Verify** — py_compile, `import app.models`, grep `c9d0e1f2a3b4` → exactly two hits, no `app.` imports.
- [ ] **Step 4: Commit** — `feat(p2-3.5): conflict-override audit columns`.

---

### Task 2: Validation service

**Files:**
- Create: `backend/app/services/production_validation.py`
- Test: `backend/tests/test_production_validation.py`

**Interfaces:**
- Consumes: `effective_disposition`, `qc_status` (`app.services.privilege`); models.
- Produces (Task 3 imports): `compute_conflicts(db, ps, doc_ids) -> dict` with keys `qc_pending | privilege_produce | no_images | total`; entries `{"document_id", "control_number", "detail"}`.

- [ ] **Step 1: Failing tests** — `backend/tests/test_production_validation.py`:

```python
"""Fake-session tests for production validation conflicts (P2-3.5)."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import app.services.production_validation as pv
from tests.fakes import TS, FakeResult, FakeSession


class FakePS:
    def __init__(self):
        self.id = 1
        self.production_id = 1


class FakeDecision:
    def __init__(self, document_id, decision="approved", decided_at=None,
                 redaction_count=1, dec_id=1):
        self.id = dec_id
        self.document_id = document_id
        self.decision = decision
        self.decided_at = decided_at or (TS + timedelta(hours=1))
        self.redaction_count = redaction_count


def _db(doc_rows, privileged=(), red_rows=(), decisions=()):
    """doc_rows: (id, control, override, image_paths); red_rows: (id, count, changed)."""
    return FakeSession(responders=[
        ("documents.image_paths", FakeResult(rows=list(doc_rows))),
        ("is_privilege", FakeResult(rows=[(d,) for d in privileged])),
        ("coalesce", FakeResult(rows=list(red_rows))),
        ("redaction_qc_decisions", FakeResult(items=list(decisions))),
    ])


def _run(db, doc_ids):
    return asyncio.run(pv.compute_conflicts(db, FakePS(), doc_ids))


def test_clean_set_no_conflicts():
    d1 = uuid4()
    db = _db([(d1, "C-1", None, ["p1.jpg"])])
    out = _run(db, [d1])
    assert out["total"] == 0
    assert out == {"qc_pending": [], "privilege_produce": [],
                   "no_images": [], "total": 0}


def test_redactions_without_approval_conflict():
    d1 = uuid4()
    db = _db([(d1, "C-1", None, ["p1.jpg"])], red_rows=[(d1, 2, TS)])
    out = _run(db, [d1])
    assert out["total"] == 1
    assert out["qc_pending"][0]["control_number"] == "C-1"
    assert "pending" in out["qc_pending"][0]["detail"]


def test_fresh_approved_qc_no_conflict():
    d1 = uuid4()
    db = _db([(d1, "C-1", None, ["p1.jpg"])],
             red_rows=[(d1, 2, TS)],
             decisions=[FakeDecision(d1, "approved", TS + timedelta(hours=1), 2)])
    out = _run(db, [d1])
    assert out["total"] == 0


def test_stale_approval_conflicts():
    # redaction changed AFTER the decision -> auto-invalidated -> pending
    d1 = uuid4()
    db = _db([(d1, "C-1", None, ["p1.jpg"])],
             red_rows=[(d1, 2, TS + timedelta(hours=2))],
             decisions=[FakeDecision(d1, "approved", TS + timedelta(hours=1), 2)])
    out = _run(db, [d1])
    assert len(out["qc_pending"]) == 1


def test_rejected_qc_conflicts():
    d1 = uuid4()
    db = _db([(d1, "C-1", None, ["p1.jpg"])],
             red_rows=[(d1, 2, TS)],
             decisions=[FakeDecision(d1, "rejected", TS + timedelta(hours=1), 2)])
    out = _run(db, [d1])
    assert "rejected" in out["qc_pending"][0]["detail"]


def test_privilege_produce_override_conflicts():
    d1 = uuid4()
    db = _db([(d1, "C-1", "produce", ["p1.jpg"])], privileged=[d1])
    out = _run(db, [d1])
    assert len(out["privilege_produce"]) == 1
    assert out["total"] == 1


def test_privileged_withhold_is_fine():
    d1 = uuid4()
    db = _db([(d1, "C-1", None, [])], privileged=[d1])  # withhold, no images OK
    out = _run(db, [d1])
    assert out["total"] == 0


def test_no_images_conflict_for_produce_doc():
    d1 = uuid4()
    db = _db([(d1, "C-1", None, [])])
    out = _run(db, [d1])
    assert len(out["no_images"]) == 1


def test_empty_doc_ids():
    out = _run(FakeSession(), [])
    assert out["total"] == 0
```

- [ ] **Step 2: Verify fail** — ModuleNotFoundError.

- [ ] **Step 3: Implement** — `backend/app/services/production_validation.py`:

```python
"""Pre-lock validation for production sets (P2-3.5). DB-aware.

Adapted from Relativity's staging validation: conflicts are surfaced and
must be resolved or explicitly overridden — never silently produced.
Dispositions are derived live so validation works on draft sets.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Document,
    DocumentTag,
    ProductionSet,
    Redaction,
    RedactionQCDecision,
    Tag,
)
from app.services.privilege import effective_disposition, qc_status


async def compute_conflicts(db: AsyncSession, ps: ProductionSet,
                            doc_ids: list) -> dict:
    out: dict = {"qc_pending": [], "privilege_produce": [], "no_images": [],
                 "total": 0}
    if not doc_ids:
        return out

    doc_rows = (await db.execute(
        select(Document.id, Document.bates_begin,
               Document.privilege_disposition, Document.image_paths)
        .where(Document.id.in_(doc_ids))
    )).all()

    privileged = {r[0] for r in (await db.execute(
        select(DocumentTag.document_id)
        .join(Tag, Tag.id == DocumentTag.tag_id)
        .where(Tag.is_privilege.is_(True), DocumentTag.document_id.in_(doc_ids))
    )).all()}

    red_rows = (await db.execute(
        select(Redaction.document_id, func.count(Redaction.id),
               func.max(func.coalesce(Redaction.updated_at, Redaction.created_at)))
        .where(Redaction.document_id.in_(doc_ids))
        .group_by(Redaction.document_id)
    )).all()
    reds = {r[0]: (r[1], r[2]) for r in red_rows}

    latest: dict = {}
    for d in (await db.execute(
        select(RedactionQCDecision)
        .where(RedactionQCDecision.document_id.in_(doc_ids))
        .order_by(RedactionQCDecision.document_id,
                  RedactionQCDecision.decided_at.desc(),
                  RedactionQCDecision.id.desc())
    )).scalars().all():
        latest.setdefault(d.document_id, d)

    for did, control, override, image_paths in doc_rows:
        count, changed = reds.get(did, (0, None))
        disposition = effective_disposition(
            has_privilege_tag=did in privileged,
            has_redactions=count > 0,
            override=override,
        ) or "produce"
        if disposition == "redact_in_part":
            dec = latest.get(did)
            status = qc_status(
                count,
                (dec.decision, dec.decided_at, dec.redaction_count) if dec else None,
                changed,
            )
            if status != "approved":
                out["qc_pending"].append({
                    "document_id": str(did), "control_number": control,
                    "detail": f"redaction QC is {status}"})
        if did in privileged and disposition == "produce":
            out["privilege_produce"].append({
                "document_id": str(did), "control_number": control,
                "detail": "privilege-tagged document would be produced unredacted"})
        if disposition != "withhold" and not image_paths:
            out["no_images"].append({
                "document_id": str(did), "control_number": control,
                "detail": "no page images to produce"})

    out["total"] = (len(out["qc_pending"]) + len(out["privilege_produce"])
                    + len(out["no_images"]))
    return out
```

- [ ] **Step 4: Verify pass** — 9 passed, 0 warnings.
- [ ] **Step 5: Commit** — `feat(p2-3.5): conflict computation (QC gating, privilege-produce, no-images)`.

---

### Task 3: Validation endpoint + lock gating

**Files:**
- Modify: `backend/app/schemas.py` (`ProductionSetLockRequest`; `ProductionSetOut` override fields)
- Modify: `backend/app/routers/production_sets.py` (import, validation endpoint, lock body + gate)
- Test: `backend/tests/test_production_set_endpoints.py` (append + update 2 lock tests, extend FakePS)

- [ ] **Step 1: Schemas**

After `ProductionSetLockOut`:

```python
class ProductionSetLockRequest(BaseModel):
    override_conflicts: bool = False
```

In `ProductionSetOut` after `packaged_at`:

```python
    conflicts_overridden_by: str | None = None
    conflicts_overridden_at: datetime | None = None
```

- [ ] **Step 2: Update existing tests + add failing tests**

`FakePS.__init__` gains:

```python
        self.conflicts_overridden_by = None
        self.conflicts_overridden_at = None
```

In `test_lock_assigns_and_snapshots` and `test_lock_produce_override_keeps_full_pages`, insert `("coalesce", FakeResult()),` as the FIRST responder (the conflict check's redaction-agg query contains both "coalesce" and "redactions"; without this it would swallow the plain `("redactions", ...)` responder's 2-tuples). With no `"documents.image_paths"` responder those tests see zero member metadata → zero conflicts → lock proceeds as before.

Append:

```python
# --- validation + lock gating (P2-3.5) -------------------------------------

def test_validation_endpoint_reports_conflicts(monkeypatch):
    _patch(monkeypatch)
    d1 = uuid4()
    item = FakeItem(d1, item_id=1)
    db = FakeSession(
        get_objects={("ProductionSet", 1): FakePS()},
        responders=[
            ("coalesce", FakeResult(rows=[(d1, 2, TS)])),
            ("production_set_items", FakeResult(items=[item])),
            ("documents.image_paths", FakeResult(rows=[(d1, "C-1", None, ["p1.jpg"])])),
        ],
    )
    out = asyncio.run(rps.get_validation(set_id=1, db=db, user=FakeUser()))
    assert out["total"] == 1
    assert out["qc_pending"][0]["control_number"] == "C-1"


def test_lock_409_on_conflicts_without_override(monkeypatch):
    _patch(monkeypatch)
    d1 = uuid4()
    items = [FakeItem(d1, item_id=1)]
    db = FakeSession(
        get_objects={("ProductionSet", 1): FakePS()},
        responders=[
            ("coalesce", FakeResult(rows=[(d1, 2, TS)])),
            ("production_set_items", FakeResult(items=items)),
            ("documents.image_paths", FakeResult(rows=[(d1, "C-1", None, ["p1.jpg"])])),
        ],
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.lock_production_set(set_id=1, body=None, db=db, user=FakeUser()))
    assert exc.value.status_code == 409
    assert "qc_pending=1" in exc.value.detail


def test_lock_override_proceeds_and_stamps_audit(monkeypatch):
    _patch(monkeypatch)
    from app.schemas import ProductionSetLockRequest
    d1 = uuid4()
    items = [FakeItem(d1, item_id=1)]
    ps = FakePS()
    db = FakeSession(
        get_objects={("ProductionSet", 1): ps},
        responders=[
            ("coalesce", FakeResult(rows=[(d1, 2, TS)])),
            ("redaction_qc_decisions", FakeResult()),
            ("production_set_items", FakeResult(items=items)),
            ("documents.image_paths", FakeResult(rows=[(d1, "C-1", None, ["p1.jpg"])])),
            ("is_privilege", FakeResult()),
            ("redactions", FakeResult(rows=[(d1, 2)])),
            ("documents.page_count", FakeResult(rows=[(d1, "C-1", None, None, TS, None, 3, None)])),
        ],
    )
    out = asyncio.run(rps.lock_production_set(
        set_id=1, body=ProductionSetLockRequest(override_conflicts=True),
        db=db, user=FakeUser()))
    assert out.doc_count == 1
    assert ps.status == "locked"
    assert ps.conflicts_overridden_by == "u1"
    assert ps.conflicts_overridden_at is not None


def test_clean_lock_leaves_override_null(monkeypatch):
    _patch(monkeypatch)
    d1 = uuid4()
    items = [FakeItem(d1, item_id=1)]
    ps = FakePS()
    db = FakeSession(
        get_objects={("ProductionSet", 1): ps},
        responders=[
            ("coalesce", FakeResult()),
            ("production_set_items", FakeResult(items=items)),
            ("documents.image_paths", FakeResult(rows=[(d1, "C-1", None, ["p1.jpg"])])),
            ("is_privilege", FakeResult()),
            ("redactions", FakeResult()),
            ("documents.page_count", FakeResult(rows=[(d1, "C-1", None, None, TS, None, 3, None)])),
        ],
    )
    asyncio.run(rps.lock_production_set(set_id=1, body=None, db=db, user=FakeUser()))
    assert ps.status == "locked"
    assert ps.conflicts_overridden_by is None
```

NOTE the responder-order rule in these lock tests: `"coalesce"` and `"redaction_qc_decisions"` (conflict queries) BEFORE `"redactions"`; `"documents.image_paths"` BEFORE `"documents.page_count"` is not required (distinct substrings) but keep conflict responders first for clarity.

- [ ] **Step 3: Verify fail** — `get_validation` AttributeError; lock tests fail on missing gate.

- [ ] **Step 4: Implement router changes**

- imports: add `ProductionSetLockRequest` to the schemas import; add `from app.services.production_validation import compute_conflicts`.
- New endpoint (place before `lock_production_set`):

```python
@router.get("/production-sets/{set_id}/validation")
async def get_validation(
    set_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ps = await _load_set(db, user, set_id)
    items = (await db.execute(
        select(ProductionSetItem)
        .where(ProductionSetItem.production_set_id == set_id)
    )).scalars().all()
    return await compute_conflicts(db, ps, [i.document_id for i in items])
```

- `lock_production_set`: signature gains `body: ProductionSetLockRequest | None = None` (after `set_id`). After the empty-set 422 and `doc_ids = [...]`, insert:

```python
    conflicts = await compute_conflicts(db, ps, doc_ids)
    if conflicts["total"]:
        counts = {k: len(v) for k, v in conflicts.items() if k != "total"}
        if not (body and body.override_conflicts):
            summary = ", ".join(f"{k}={v}" for k, v in counts.items())
            raise HTTPException(
                status_code=409,
                detail=f"Validation conflicts: {summary}. Resolve them or lock with override_conflicts.")
        ps.conflicts_overridden_by = user.id
        ps.conflicts_overridden_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await log_action(db, user, "production_set_conflicts_overridden",
                         "production_set", str(set_id),
                         production_id=ps.production_id,
                         details={**counts, "total": conflicts["total"]})
```

- [ ] **Step 5: Verify pass** — endpoint file total 50 (46 + 4), plus validation/other suites, 0 warnings.
- [ ] **Step 6: Commit** — `feat(p2-3.5): validation endpoint + conflict-gated lock with audited override`.

---

### Task 4: Full-suite verification + PR

- [ ] **Step 1:** Full suite — only known `test_ai_review` failure allowed.
- [ ] **Step 2:** Migration head/purity re-check (`c9d0e1f2a3b4` → exactly the new file).
- [ ] **Step 3:** Push + PR base `feat/p2-3-loadfiles-packaging`, title `feat(p2-3.5): production validation — QC gating, privilege conflicts, audited override`, body summarizing the Relativity-modeled validation flow (no attribution trailer).
