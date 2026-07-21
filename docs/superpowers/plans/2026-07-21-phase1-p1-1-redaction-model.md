# Phase 1 P1-1 — Redaction Data Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store rectangular redaction regions (page + normalized rect + reason code) on documents, with access-controlled CRUD endpoints and an audit trail.

**Architecture:** A new `Redaction` table mirroring the `Annotation` coordinate model; a pure validation/reason-code service; and a CRUD router mirroring `annotations.py`. Data-only — no burn-in, masking, or production (later sub-projects). Additive migration, ships safely.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, Postgres (Neon), Pydantic. Backend tests run via `backend/venv/Scripts/python.exe -m pytest` from `backend/` (system python lacks deps). Endpoint tests use the repo's fake-session pattern (no DB/TestClient) — see `backend/tests/test_review_endpoints.py`.

## Global Constraints

- New table `redactions` mirrors `Annotation` conventions: `id` Integer autoincrement PK; `document_id` `UUID(as_uuid=True)` FK → `documents.id` `ondelete="CASCADE"`, not null, indexed; `page_num` Integer; `created_by` String(128); `created_at` DateTime `server_default=func.now()`.
- Rectangle: `x_pct`, `y_pct` (top-left), `w_pct`, `h_pct` — Float, all normalized 0–100.
- Reason codes (exact set): `attorney_client`, `work_product`, `pii`, `phi`, `confidential`, `trade_secret`, `non_responsive`, `other`.
- Validation: `1 ≤ page_num ≤ doc.page_count`; `0 ≤ x_pct,y_pct ≤ 100`; `w_pct,h_pct > 0`; `x_pct+w_pct ≤ 100`; `y_pct+h_pct ≤ 100`; `reason_code` in the set.
- Access: read scoped to `get_accessible_production_ids`; create requires **not readonly** (reviewer+); update/delete by **creator or manager+**.
- Every write calls `log_action` (`redaction_created`/`redaction_updated`/`redaction_deleted`).
- Migration `down_revision = "adfc16bff9f3"` (current single alembic head). Verify up/down/re-up on a throwaway `pgvector/pgvector:pg16` Postgres (port 5433, `VIGILIST_DATABASE_URL=postgresql+asyncpg://vigilist:vigilist_dev@localhost:5433/vigilist`).
- No change to internal text/search APIs. Out of scope: burn-in, geometry, masking, QC status, disposition, production.

---

## Task 1 — `Redaction` model + migration

**Files:**
- Modify: `backend/app/models.py` (add `Redaction` after the `Annotation` class, ~line 383)
- Create: `backend/alembic/versions/s1a2b3c4d5e6_add_redactions_table.py`
- Test: migration verified against a throwaway Postgres (no pytest file)

**Interfaces:**
- Consumes: `Document` (existing), `Base`, `UUID`, `ForeignKey`, `Column`, `Integer`, `Float`, `String`, `Text`, `DateTime`, `Index`, `func` (all already imported in `models.py`).
- Produces: `Redaction` ORM model with columns `id, document_id, page_num, x_pct, y_pct, w_pct, h_pct, reason_code, note, created_by, created_at, updated_at`. Tasks 2–3 rely on these names.

- [ ] **Step 1: Add the model**

In `backend/app/models.py`, immediately after the `Annotation` class, add:

```python
class Redaction(Base):
    __tablename__ = "redactions"
    __table_args__ = (
        Index("ix_redactions_document_id", "document_id"),
        Index("ix_redactions_doc_page", "document_id", "page_num"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    page_num = Column(Integer, nullable=False)
    x_pct = Column(Float, nullable=False)
    y_pct = Column(Float, nullable=False)
    w_pct = Column(Float, nullable=False)
    h_pct = Column(Float, nullable=False)
    reason_code = Column(String(40), nullable=False)
    note = Column(Text, nullable=True)
    created_by = Column(String(128), nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, onupdate=func.now(), nullable=True)
```

(If any of `Index`, `Float`, `Text`, `func` is not already imported at the top of `models.py`, add it to the existing `sqlalchemy` import — verify before writing.)

- [ ] **Step 2: Write the migration**

Create `backend/alembic/versions/s1a2b3c4d5e6_add_redactions_table.py`:

```python
"""add redactions table

Revision ID: s1a2b3c4d5e6
Revises: adfc16bff9f3
Create Date: 2026-07-21
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "s1a2b3c4d5e6"
down_revision = "adfc16bff9f3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "redactions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("document_id", UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("page_num", sa.Integer(), nullable=False),
        sa.Column("x_pct", sa.Float(), nullable=False),
        sa.Column("y_pct", sa.Float(), nullable=False),
        sa.Column("w_pct", sa.Float(), nullable=False),
        sa.Column("h_pct", sa.Float(), nullable=False),
        sa.Column("reason_code", sa.String(length=40), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_redactions_document_id", "redactions", ["document_id"])
    op.create_index("ix_redactions_doc_page", "redactions", ["document_id", "page_num"])


def downgrade():
    op.drop_index("ix_redactions_doc_page", table_name="redactions")
    op.drop_index("ix_redactions_document_id", table_name="redactions")
    op.drop_table("redactions")
```

(If `alembic heads` shows a head other than `adfc16bff9f3`, set `down_revision` to the actual head and note it; if the revision id collides, pick the next unused id in the same style.)

- [ ] **Step 3: Verify the migration up/down/re-up against a throwaway Postgres**

```bash
docker rm -f p11pg >/dev/null 2>&1
docker run -d --rm --name p11pg -e POSTGRES_USER=vigilist -e POSTGRES_PASSWORD=vigilist_dev -e POSTGRES_DB=vigilist -p 5433:5432 pgvector/pgvector:pg16
sleep 7
cd backend
export VDB="postgresql+asyncpg://vigilist:vigilist_dev@localhost:5433/vigilist"
VIGILIST_DATABASE_URL="$VDB" venv/Scripts/python.exe -m alembic upgrade head
VIGILIST_DATABASE_URL="$VDB" venv/Scripts/python.exe -m alembic downgrade -1
VIGILIST_DATABASE_URL="$VDB" venv/Scripts/python.exe -m alembic upgrade head
VIGILIST_DATABASE_URL="$VDB" venv/Scripts/python.exe -m alembic heads
cd ..
docker stop p11pg
```

Expected: `upgrade head` reaches `s1a2b3c4d5e6`; `downgrade -1` drops the table/indexes cleanly; re-`upgrade head` succeeds; `heads` shows exactly `s1a2b3c4d5e6 (head)`.

- [ ] **Step 4: Confirm the model imports**

Run (from `backend/`): `venv/Scripts/python.exe -c "import app.models; print(app.models.Redaction.__tablename__)"`
Expected: `redactions`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/models.py backend/alembic/versions/s1a2b3c4d5e6_add_redactions_table.py
git commit -m "feat(p1-1): add Redaction model + migration"
```

---

## Task 2 — Redaction validation service (reason codes + rect validation)

**Files:**
- Create: `backend/app/services/redaction.py`
- Test: `backend/tests/test_redaction_service.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `REDACTION_REASON_CODES: frozenset[str]`
  - `is_valid_reason_code(code: str) -> bool`
  - `validate_rect(page_num: int, x_pct: float, y_pct: float, w_pct: float, h_pct: float, page_count: int) -> str | None` — returns an error message, or `None` if valid.
  Task 3's router calls all three.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_redaction_service.py`:

```python
"""Unit tests for the pure redaction validation service (P1-1). No DB/network."""

from app.services.redaction import (
    REDACTION_REASON_CODES,
    is_valid_reason_code,
    validate_rect,
)


def test_reason_codes_are_the_defined_set():
    assert REDACTION_REASON_CODES == frozenset({
        "attorney_client", "work_product", "pii", "phi",
        "confidential", "trade_secret", "non_responsive", "other",
    })


def test_is_valid_reason_code():
    assert is_valid_reason_code("attorney_client") is True
    assert is_valid_reason_code("pii") is True
    assert is_valid_reason_code("bogus") is False
    assert is_valid_reason_code("") is False


def test_validate_rect_accepts_a_valid_box():
    assert validate_rect(1, 10.0, 20.0, 30.0, 40.0, page_count=5) is None
    # Exactly filling the page is allowed.
    assert validate_rect(2, 0.0, 0.0, 100.0, 100.0, page_count=5) is None


def test_validate_rect_rejects_bad_page_num():
    assert validate_rect(0, 10.0, 10.0, 10.0, 10.0, page_count=5) is not None
    assert validate_rect(6, 10.0, 10.0, 10.0, 10.0, page_count=5) is not None


def test_validate_rect_rejects_out_of_range_origin():
    assert validate_rect(1, -1.0, 10.0, 10.0, 10.0, page_count=5) is not None
    assert validate_rect(1, 10.0, 101.0, 10.0, 10.0, page_count=5) is not None


def test_validate_rect_rejects_nonpositive_size():
    assert validate_rect(1, 10.0, 10.0, 0.0, 10.0, page_count=5) is not None
    assert validate_rect(1, 10.0, 10.0, 10.0, -5.0, page_count=5) is not None


def test_validate_rect_rejects_box_exceeding_page():
    assert validate_rect(1, 80.0, 10.0, 30.0, 10.0, page_count=5) is not None  # x+w=110
    assert validate_rect(1, 10.0, 95.0, 10.0, 20.0, page_count=5) is not None  # y+h=115
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/Scripts/python.exe -m pytest tests/test_redaction_service.py -v`
Expected: FAIL — `No module named 'app.services.redaction'`.

- [ ] **Step 3: Implement the service**

Create `backend/app/services/redaction.py`:

```python
"""Pure redaction validation + reason codes (P1-1). No DB/network."""

from __future__ import annotations

REDACTION_REASON_CODES = frozenset({
    "attorney_client",
    "work_product",
    "pii",
    "phi",
    "confidential",
    "trade_secret",
    "non_responsive",
    "other",
})


def is_valid_reason_code(code: str) -> bool:
    return code in REDACTION_REASON_CODES


def validate_rect(
    page_num: int,
    x_pct: float,
    y_pct: float,
    w_pct: float,
    h_pct: float,
    page_count: int,
) -> str | None:
    """Return an error message if the rectangle is invalid, else None."""
    if page_num < 1 or page_num > page_count:
        return f"page_num must be between 1 and {page_count}"
    if not (0.0 <= x_pct <= 100.0):
        return "x_pct must be between 0 and 100"
    if not (0.0 <= y_pct <= 100.0):
        return "y_pct must be between 0 and 100"
    if w_pct <= 0.0 or h_pct <= 0.0:
        return "w_pct and h_pct must be greater than 0"
    if x_pct + w_pct > 100.0:
        return "x_pct + w_pct must not exceed 100"
    if y_pct + h_pct > 100.0:
        return "y_pct + h_pct must not exceed 100"
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/Scripts/python.exe -m pytest tests/test_redaction_service.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/redaction.py backend/tests/test_redaction_service.py
git commit -m "feat(p1-1): redaction reason codes + rectangle validation"
```

---

## Task 3 — Schemas + CRUD router + registration

**Files:**
- Modify: `backend/app/schemas.py` (add `RedactionCreate`, `RedactionUpdate`, `RedactionOut`)
- Create: `backend/app/routers/redactions.py`
- Modify: `backend/app/main.py` (import + `include_router`)
- Test: `backend/tests/test_redaction_endpoints.py`

**Interfaces:**
- Consumes: `Redaction` (Task 1); `is_valid_reason_code`, `validate_rect` (Task 2); `Document`, `User`, `get_db`, `get_current_user`, `get_accessible_production_ids`, `get_user_role_for_production`, `ROLE_RANK`, `log_action` (existing).
- Produces: endpoints `GET/POST /api/documents/{doc_id}/redactions`, `PUT/DELETE /api/redactions/{redaction_id}`.

- [ ] **Step 1: Add the schemas**

In `backend/app/schemas.py`, near `AnnotationOut` (~line 470), add:

```python
class RedactionCreate(BaseModel):
    page_num: int
    x_pct: float
    y_pct: float
    w_pct: float
    h_pct: float
    reason_code: str
    note: str | None = None


class RedactionUpdate(BaseModel):
    x_pct: float | None = None
    y_pct: float | None = None
    w_pct: float | None = None
    h_pct: float | None = None
    reason_code: str | None = None
    note: str | None = None


class RedactionOut(BaseModel):
    id: int
    document_id: UUID
    page_num: int
    x_pct: float
    y_pct: float
    w_pct: float
    h_pct: float
    reason_code: str
    note: str | None = None
    created_by: str
    created_at: datetime
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}
```

(`BaseModel`, `UUID`, `datetime` are already imported in `schemas.py`.)

- [ ] **Step 2: Write the failing endpoint tests**

Create `backend/tests/test_redaction_endpoints.py`:

```python
"""Fake-session unit tests for redaction CRUD endpoints (P1-1). No DB/network.

Same pattern as tests/test_review_endpoints.py: call the async router functions
directly with a fake session + monkeypatched deps.
"""

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException

import app.routers.redactions as rr
from app.schemas import RedactionCreate, RedactionUpdate

_TS = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)


class FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.email = f"{uid}@thirulaw.com"
        self.display_name = uid


class FakeDoc:
    def __init__(self, doc_id, production_id=1, page_count=10):
        self.id = doc_id
        self.production_id = production_id
        self.page_count = page_count


class FakeRedaction:
    def __init__(self, rid, document_id, created_by, page_num=1):
        self.id = rid
        self.document_id = document_id
        self.created_by = created_by
        self.page_num = page_num
        self.x_pct = 10.0
        self.y_pct = 10.0
        self.w_pct = 10.0
        self.h_pct = 10.0
        self.reason_code = "pii"
        self.note = None
        self.created_at = _TS
        self.updated_at = None


class FakeSession:
    def __init__(self, get_objects=None):
        self._get_objects = get_objects or {}
        self.added = []
        self.deleted = []

    async def get(self, model, key):
        return self._get_objects.get((model.__name__, key))

    def add(self, obj):
        obj.id = 123
        if getattr(obj, "created_at", None) is None:
            obj.created_at = _TS  # DB server_default isn't applied in-memory
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        pass

    async def refresh(self, obj):
        if getattr(obj, "created_at", None) is None:
            obj.created_at = _TS

    async def delete(self, obj):
        self.deleted.append(obj)


def _patch_common(monkeypatch, role="reviewer", accessible=(1,)):
    async def fake_accessible(db, user):
        return list(accessible)

    async def fake_role(db, user, production_id):
        return role

    async def fake_log(*args, **kwargs):
        pass

    monkeypatch.setattr(rr, "get_accessible_production_ids", fake_accessible)
    monkeypatch.setattr(rr, "get_user_role_for_production", fake_role)
    monkeypatch.setattr(rr, "log_action", fake_log)


def test_create_blocked_for_readonly(monkeypatch):
    _patch_common(monkeypatch, role="readonly")
    doc_id = uuid4()
    db = FakeSession(get_objects={("Document", doc_id): FakeDoc(doc_id)})
    body = RedactionCreate(page_num=1, x_pct=10, y_pct=10, w_pct=10, h_pct=10, reason_code="pii")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rr.create_redaction(doc_id=doc_id, body=body, db=db, user=FakeUser("u1")))
    assert exc.value.status_code == 403


def test_create_rejects_invalid_reason_code(monkeypatch):
    _patch_common(monkeypatch, role="reviewer")
    doc_id = uuid4()
    db = FakeSession(get_objects={("Document", doc_id): FakeDoc(doc_id)})
    body = RedactionCreate(page_num=1, x_pct=10, y_pct=10, w_pct=10, h_pct=10, reason_code="bogus")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rr.create_redaction(doc_id=doc_id, body=body, db=db, user=FakeUser("u1")))
    assert exc.value.status_code == 422


def test_create_rejects_box_exceeding_page(monkeypatch):
    _patch_common(monkeypatch, role="reviewer")
    doc_id = uuid4()
    db = FakeSession(get_objects={("Document", doc_id): FakeDoc(doc_id)})
    body = RedactionCreate(page_num=1, x_pct=80, y_pct=10, w_pct=30, h_pct=10, reason_code="pii")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rr.create_redaction(doc_id=doc_id, body=body, db=db, user=FakeUser("u1")))
    assert exc.value.status_code == 422


def test_create_succeeds_for_reviewer(monkeypatch):
    _patch_common(monkeypatch, role="reviewer")
    doc_id = uuid4()
    db = FakeSession(get_objects={("Document", doc_id): FakeDoc(doc_id)})
    body = RedactionCreate(page_num=2, x_pct=10, y_pct=10, w_pct=20, h_pct=20, reason_code="attorney_client", note="privileged")
    out = asyncio.run(rr.create_redaction(doc_id=doc_id, body=body, db=db, user=FakeUser("u1")))
    assert out.reason_code == "attorney_client"
    assert out.page_num == 2
    assert len(db.added) == 1


def test_update_blocked_for_noncreator_reviewer(monkeypatch):
    _patch_common(monkeypatch, role="reviewer")
    doc_id = uuid4()
    red = FakeRedaction(5, doc_id, created_by="owner")
    db = FakeSession(get_objects={("Redaction", 5): red, ("Document", doc_id): FakeDoc(doc_id)})
    body = RedactionUpdate(reason_code="pii")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rr.update_redaction(redaction_id=5, body=body, db=db, user=FakeUser("someone_else")))
    assert exc.value.status_code == 403


def test_update_allowed_for_manager(monkeypatch):
    _patch_common(monkeypatch, role="manager")
    doc_id = uuid4()
    red = FakeRedaction(5, doc_id, created_by="owner")
    db = FakeSession(get_objects={("Redaction", 5): red, ("Document", doc_id): FakeDoc(doc_id)})
    body = RedactionUpdate(reason_code="confidential")
    out = asyncio.run(rr.update_redaction(redaction_id=5, body=body, db=db, user=FakeUser("someone_else")))
    assert out.reason_code == "confidential"


def test_delete_allowed_for_creator(monkeypatch):
    _patch_common(monkeypatch, role="reviewer")
    doc_id = uuid4()
    red = FakeRedaction(7, doc_id, created_by="u1")
    db = FakeSession(get_objects={("Redaction", 7): red, ("Document", doc_id): FakeDoc(doc_id)})
    out = asyncio.run(rr.delete_redaction(redaction_id=7, db=db, user=FakeUser("u1")))
    assert out == {"ok": True}
    assert db.deleted == [red]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `venv/Scripts/python.exe -m pytest tests/test_redaction_endpoints.py -v`
Expected: FAIL — `No module named 'app.routers.redactions'`.

- [ ] **Step 4: Implement the router**

Create `backend/app/routers/redactions.py`:

```python
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_accessible_production_ids, get_user_role_for_production, ROLE_RANK
from app.models import Document, Redaction, User
from app.routers.auth import get_current_user
from app.schemas import RedactionCreate, RedactionOut, RedactionUpdate
from app.services.audit import log_action
from app.services.redaction import is_valid_reason_code, validate_rect

router = APIRouter(prefix="/api", tags=["redactions"])


async def _load_accessible_doc(db: AsyncSession, user: User, doc_id: UUID) -> Document:
    accessible = await get_accessible_production_ids(db, user)
    doc = await db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    return doc


def _validate_or_422(page_num, x_pct, y_pct, w_pct, h_pct, page_count, reason_code):
    err = validate_rect(page_num, x_pct, y_pct, w_pct, h_pct, page_count)
    if err:
        raise HTTPException(status_code=422, detail=err)
    if not is_valid_reason_code(reason_code):
        raise HTTPException(status_code=422, detail="invalid reason_code")


@router.get("/documents/{doc_id}/redactions", response_model=list[RedactionOut])
async def list_redactions(
    doc_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _load_accessible_doc(db, user, doc_id)
    result = await db.execute(
        select(Redaction)
        .where(Redaction.document_id == doc_id)
        .order_by(Redaction.page_num.asc(), Redaction.created_at.asc())
    )
    return [RedactionOut.model_validate(r) for r in result.scalars().all()]


@router.post("/documents/{doc_id}/redactions", response_model=RedactionOut, status_code=201)
async def create_redaction(
    doc_id: UUID,
    body: RedactionCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    doc = await _load_accessible_doc(db, user, doc_id)
    role = await get_user_role_for_production(db, user, doc.production_id)
    if role == "readonly":
        raise HTTPException(status_code=403, detail="Read-only access")

    _validate_or_422(body.page_num, body.x_pct, body.y_pct, body.w_pct, body.h_pct,
                     doc.page_count, body.reason_code)

    red = Redaction(
        document_id=doc_id,
        page_num=body.page_num,
        x_pct=body.x_pct,
        y_pct=body.y_pct,
        w_pct=body.w_pct,
        h_pct=body.h_pct,
        reason_code=body.reason_code,
        note=body.note,
        created_by=user.id,
    )
    db.add(red)
    await db.flush()
    await log_action(
        db, user, "redaction_created", "redaction", str(red.id),
        production_id=doc.production_id,
        details={"document_id": str(doc_id), "page_num": body.page_num, "reason_code": body.reason_code},
    )
    await db.commit()
    await db.refresh(red)
    return RedactionOut.model_validate(red)


async def _load_redaction_for_write(db: AsyncSession, user: User, redaction_id: int):
    accessible = await get_accessible_production_ids(db, user)
    red = await db.get(Redaction, redaction_id)
    if not red:
        raise HTTPException(status_code=404, detail="Redaction not found")
    doc = await db.get(Document, red.document_id)
    if doc and doc.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    if red.created_by != user.id:
        if not doc:
            raise HTTPException(status_code=403, detail="Access denied")
        role = await get_user_role_for_production(db, user, doc.production_id)
        if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
            raise HTTPException(status_code=403, detail="Only the creator or a manager can modify this redaction")
    return red, doc


@router.put("/redactions/{redaction_id}", response_model=RedactionOut)
async def update_redaction(
    redaction_id: int,
    body: RedactionUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    red, doc = await _load_redaction_for_write(db, user, redaction_id)

    x = body.x_pct if body.x_pct is not None else red.x_pct
    y = body.y_pct if body.y_pct is not None else red.y_pct
    w = body.w_pct if body.w_pct is not None else red.w_pct
    h = body.h_pct if body.h_pct is not None else red.h_pct
    reason = body.reason_code if body.reason_code is not None else red.reason_code
    page_count = doc.page_count if doc else red.page_num
    _validate_or_422(red.page_num, x, y, w, h, page_count, reason)

    red.x_pct, red.y_pct, red.w_pct, red.h_pct = x, y, w, h
    red.reason_code = reason
    if body.note is not None:
        red.note = body.note

    await log_action(
        db, user, "redaction_updated", "redaction", str(redaction_id),
        details={"document_id": str(red.document_id)},
    )
    await db.commit()
    await db.refresh(red)
    return RedactionOut.model_validate(red)


@router.delete("/redactions/{redaction_id}")
async def delete_redaction(
    redaction_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    red, _doc = await _load_redaction_for_write(db, user, redaction_id)
    document_id = red.document_id
    await db.delete(red)
    await log_action(
        db, user, "redaction_deleted", "redaction", str(redaction_id),
        details={"document_id": str(document_id)},
    )
    await db.commit()
    return {"ok": True}
```

- [ ] **Step 5: Register the router**

In `backend/app/main.py`: add `redactions` to the `from app.routers import ...` line, and add `app.include_router(redactions.router)` next to `app.include_router(annotations.router)`.

- [ ] **Step 6: Run tests to verify they pass**

Run (from `backend/`):
```bash
venv/Scripts/python.exe -m pytest tests/test_redaction_endpoints.py -v
venv/Scripts/python.exe -c "import app.main; print('router ok')"
```
Expected: all 7 endpoint tests PASS; import prints `router ok`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/schemas.py backend/app/routers/redactions.py backend/app/main.py backend/tests/test_redaction_endpoints.py
git commit -m "feat(p1-1): redaction CRUD endpoints + schemas + registration"
```

---

## Task 4 — Full-suite verification

- [ ] **Step 1: Run the full backend suite**

Run (from `backend/`): `venv/Scripts/python.exe -m pytest tests/ -q`
Expected: all P1-1 tests pass; the only failure is the pre-existing, unrelated `tests/test_ai_review.py::test_build_classification_prompt`.

- [ ] **Step 2: Import + head checks**

Run:
```bash
venv/Scripts/python.exe -c "import app.main, app.models, app.routers.redactions, app.services.redaction; print('imports OK')"
venv/Scripts/python.exe -m alembic heads
```
Expected: `imports OK`; `alembic heads` shows a single head `s1a2b3c4d5e6`.

- [ ] **Step 3: Record completion and hand off to the whole-branch review.**

---

## Notes for the executor
- Backend tests run via `backend/venv/Scripts/python.exe`.
- Endpoint tests use the fake-session pattern (no DB / TestClient) — see `tests/test_review_endpoints.py`.
- The migration is additive (a new table) — no data backfill, no prod-data risk; but it DOES add a table so the deploy runs it. It imports nothing from `app.*`, so it is safe under the deploy's minimal-deps migration step.
- Out of scope (do NOT build): burn-in, word geometry, masking, QC status, disposition, production/export.
