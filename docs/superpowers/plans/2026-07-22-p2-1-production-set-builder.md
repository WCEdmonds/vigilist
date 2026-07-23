# P2-1 Production Set Builder + Bates Numbering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Production sets (deliverable volumes within a matter) with draft→lock lifecycle, membership building (tags/families/dedup), and gap-free Bates assignment at lock.

**Architecture:** One import-safe migration adds `production_sets` + `production_set_items`. Pure ordering/numbering logic lives in `app/services/production_numbering.py`; a new `routers/production_sets.py` holds all endpoints. Bates numbers, pages, and dispositions are snapshotted onto membership rows at lock time and never change afterward; documents' own `bates_begin` stays an ingest control number.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, pytest fake-session tests (no DB).

**Spec:** `docs/superpowers/specs/2026-07-22-p2-1-production-set-builder-design.md`

## Global Constraints

- Migration `a9b8c7d6e5f4`, `down_revision = "t2b3c4d5e6f7"` (current single head). It must import NOTHING from `app.*` (CI runs alembic under minimal deps).
- Statuses (exact): `draft`, `locked`. Dispositions (exact, snapshot values): `produce`, `redact_in_part`, `withhold`. Sort keys (exact): `control_number`, `custodian_date`.
- Locked sets are immutable: any mutation (add/remove/delete/lock) of a locked set → 409. There is no unlock.
- Roles: all writes = manager+ on the matter; reads = any role with matter access. Every write audit-logged via `log_action(db, user, action, resource_type, resource_id, production_id=..., details=...)`.
- Disposition logic reuses `effective_disposition` from `app.services.privilege` — do not duplicate it. `None` result snapshots as `"produce"`.
- Tests: fake-session pattern (no DB/TestClient); shared fakes in `backend/tests/fakes.py`. Run from repo root: `backend\venv\Scripts\python.exe -m pytest backend\tests\<file> -q`. Test output pristine (0 warnings).
- Verify `git branch --show-current` == `feat/p2-1-production-set-builder` before every commit.
- Do NOT add `Co-Authored-By`, "Generated with", or any AI-attribution trailers to commits or the PR body (user preference; overrides any tool default).

---

### Task 1: Migration + models

**Files:**
- Create: `backend/alembic/versions/a9b8c7d6e5f4_add_production_sets.py`
- Modify: `backend/app/models.py` (append after `RedactionQCDecision`, ~line 425)

**Interfaces:**
- Consumes: nothing.
- Produces: models `ProductionSet` and `ProductionSetItem` (later tasks import both from `app.models`). Field lists exactly as below.

- [ ] **Step 1: Write the migration**

Create `backend/alembic/versions/a9b8c7d6e5f4_add_production_sets.py`:

```python
"""add production sets + items

Revision ID: a9b8c7d6e5f4
Revises: t2b3c4d5e6f7
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "a9b8c7d6e5f4"
down_revision = "t2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "production_sets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("production_id", sa.Integer(), sa.ForeignKey("productions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("prefix", sa.String(length=50), nullable=False),
        sa.Column("padding", sa.Integer(), nullable=False, server_default=sa.text("6")),
        sa.Column("start_number", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("sort_key", sa.String(length=30), nullable=False, server_default=sa.text("'control_number'")),
        sa.Column("designation", sa.String(length=100), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("locked_by", sa.String(length=128), nullable=True),
        sa.Column("locked_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("production_id", "name", name="uq_prodset_name"),
    )
    op.create_index("ix_production_sets_production_id", "production_sets", ["production_id"])
    op.create_table(
        "production_set_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("production_set_id", sa.Integer(), sa.ForeignKey("production_sets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_id", UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=True),
        sa.Column("bates_begin", sa.String(length=50), nullable=True),
        sa.Column("bates_end", sa.String(length=50), nullable=True),
        sa.Column("pages", sa.Integer(), nullable=True),
        sa.Column("disposition", sa.String(length=20), nullable=True),
        sa.Column("designation", sa.String(length=100), nullable=True),
        sa.UniqueConstraint("production_set_id", "document_id", name="uq_prodset_item_doc"),
    )
    op.create_index("ix_prodset_items_set_id", "production_set_items", ["production_set_id"])
    op.create_index("ix_prodset_items_document_id", "production_set_items", ["document_id"])


def downgrade():
    op.drop_index("ix_prodset_items_document_id", table_name="production_set_items")
    op.drop_index("ix_prodset_items_set_id", table_name="production_set_items")
    op.drop_table("production_set_items")
    op.drop_index("ix_production_sets_production_id", table_name="production_sets")
    op.drop_table("production_sets")
```

- [ ] **Step 2: Add the models**

In `backend/app/models.py`, append after the `RedactionQCDecision` class:

```python
class ProductionSet(Base):
    """A deliverable volume built within a matter (P2-1). NOT the Production
    model above — that is the matter/case container."""

    __tablename__ = "production_sets"
    __table_args__ = (
        UniqueConstraint("production_id", "name", name="uq_prodset_name"),
        Index("ix_production_sets_production_id", "production_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    production_id = Column(Integer, ForeignKey("productions.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(255), nullable=False)
    status = Column(String(20), nullable=False, default="draft")  # 'draft' | 'locked'
    prefix = Column(String(50), nullable=False)
    padding = Column(Integer, nullable=False, default=6)
    start_number = Column(Integer, nullable=False, default=1)
    sort_key = Column(String(30), nullable=False, default="control_number")
    designation = Column(String(100), nullable=True)
    created_by = Column(String(128), nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    locked_by = Column(String(128), nullable=True)
    locked_at = Column(DateTime, nullable=True)

    items = relationship("ProductionSetItem", back_populates="production_set", cascade="all, delete-orphan")


class ProductionSetItem(Base):
    __tablename__ = "production_set_items"
    __table_args__ = (
        UniqueConstraint("production_set_id", "document_id", name="uq_prodset_item_doc"),
        Index("ix_prodset_items_set_id", "production_set_id"),
        Index("ix_prodset_items_document_id", "document_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    production_set_id = Column(Integer, ForeignKey("production_sets.id", ondelete="CASCADE"), nullable=False)
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    # Filled at lock, NULL while draft, immutable after:
    sort_order = Column(Integer, nullable=True)
    bates_begin = Column(String(50), nullable=True)
    bates_end = Column(String(50), nullable=True)
    pages = Column(Integer, nullable=True)          # snapshot: 1 for withhold, else page_count
    disposition = Column(String(20), nullable=True) # snapshot: 'produce' | 'redact_in_part' | 'withhold'
    designation = Column(String(100), nullable=True)  # per-item override of the set default

    production_set = relationship("ProductionSet", back_populates="items")
```

- [ ] **Step 3: Verify compile, import purity, single head**

```
backend\venv\Scripts\python.exe -m py_compile backend\alembic\versions\a9b8c7d6e5f4_add_production_sets.py
cd backend && venv\Scripts\python.exe -c "import app.models"
```
Then confirm: the migration file contains no `import app` / `from app`, and grep `t2b3c4d5e6f7` in `backend/alembic/versions` yields exactly two hits (that migration's own `revision` line and this new file's `down_revision`).

- [ ] **Step 4: Commit**

```bash
git add backend/alembic/versions/a9b8c7d6e5f4_add_production_sets.py backend/app/models.py
git commit -m "feat(p2-1): production_sets + production_set_items tables and models"
```

---

### Task 2: Pure numbering/ordering service

**Files:**
- Create: `backend/app/services/production_numbering.py`
- Test: `backend/tests/test_production_numbering.py`

**Interfaces:**
- Consumes: nothing (pure).
- Produces (later tasks import from `app.services.production_numbering`):
  - `SORT_KEYS: frozenset[str]`
  - `MemberInfo` frozen dataclass: `(document_id, control_number: str, family_id: str | None, custodian: str | None, doc_date: datetime | None)`
  - `format_bates(prefix: str, number: int, padding: int) -> str`
  - `pages_for(disposition: str | None, page_count: int) -> int`
  - `order_members(members: list[MemberInfo], sort_key: str) -> list[MemberInfo]`
  - `assign_bates(ordered: list[tuple[Any, int]], prefix: str, padding: int, start_number: int) -> list[tuple[Any, int, str, str]]` — returns `(document_id, sort_order, bates_begin, bates_end)`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_production_numbering.py`:

```python
"""Pure tests for production-set ordering + Bates numbering (P2-1). No DB."""

from datetime import datetime, timezone

from app.services.production_numbering import (
    SORT_KEYS,
    MemberInfo,
    assign_bates,
    format_bates,
    order_members,
    pages_for,
)

_T = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _m(cn, family_id=None, custodian=None, doc_date=None):
    return MemberInfo(document_id=cn, control_number=cn, family_id=family_id,
                      custodian=custodian, doc_date=doc_date)


# --- format_bates -----------------------------------------------------------

def test_format_bates_pads():
    assert format_bates("SMITH", 1, 6) == "SMITH000001"
    assert format_bates("SMITH", 999999, 6) == "SMITH999999"


def test_format_bates_overflow_grows_never_truncates():
    assert format_bates("SMITH", 1000000, 6) == "SMITH1000000"


def test_sort_keys_constant():
    assert SORT_KEYS == {"control_number", "custodian_date"}


# --- pages_for --------------------------------------------------------------

def test_pages_for_withhold_is_one_slipsheet_page():
    assert pages_for("withhold", 10) == 1


def test_pages_for_other_dispositions_use_page_count():
    assert pages_for("redact_in_part", 10) == 10
    assert pages_for("produce", 10) == 10


def test_pages_for_floors_at_one():
    assert pages_for("produce", 0) == 1


# --- order_members ----------------------------------------------------------

def test_order_control_number():
    ms = [_m("C-3"), _m("C-1"), _m("C-2")]
    out = [m.control_number for m in order_members(ms, "control_number")]
    assert out == ["C-1", "C-2", "C-3"]


def test_order_families_contiguous_parent_first():
    # C-1 and C-5 share a family; the lower control number (the parent,
    # ingested first) heads the group, and C-5 rides with it ahead of C-3.
    ms = [_m("C-5", family_id="F1"), _m("C-1", family_id="F1"), _m("C-3")]
    out = [m.control_number for m in order_members(ms, "control_number")]
    assert out == ["C-1", "C-5", "C-3"]


def test_order_custodian_date():
    a = _m("C-2", custodian="Alice", doc_date=_T)
    b = _m("C-1", custodian="Bob", doc_date=_T)
    c = _m("C-3", custodian="Alice",
           doc_date=datetime(2026, 6, 1, tzinfo=timezone.utc))
    out = [m.control_number for m in order_members([a, b, c], "custodian_date")]
    assert out == ["C-3", "C-2", "C-1"]


def test_order_custodian_date_missing_fields_deterministic():
    # Missing custodian sorts first (empty string); missing date sorts after
    # dated docs for the same custodian; control number breaks all ties.
    a = _m("C-1", custodian=None, doc_date=None)
    b = _m("C-2", custodian="Alice", doc_date=None)
    c = _m("C-4", custodian="Alice", doc_date=_T)
    out = [m.control_number for m in order_members([a, b, c], "custodian_date")]
    assert out == ["C-1", "C-4", "C-2"]


def test_order_unknown_sort_key_raises():
    try:
        order_members([_m("C-1")], "bogus")
        assert False, "expected ValueError"
    except ValueError:
        pass


# --- assign_bates -----------------------------------------------------------

def test_assign_bates_gap_free_across_mixed_page_counts():
    out = assign_bates([("a", 3), ("b", 1), ("c", 2)], "SMITH", 6, 1)
    assert out == [
        ("a", 1, "SMITH000001", "SMITH000003"),
        ("b", 2, "SMITH000004", "SMITH000004"),
        ("c", 3, "SMITH000005", "SMITH000006"),
    ]


def test_assign_bates_start_number_offset():
    out = assign_bates([("a", 2)], "VOL", 4, 100)
    assert out == [("a", 1, "VOL0100", "VOL0101")]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_production_numbering.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.production_numbering'`

- [ ] **Step 3: Write the implementation**

Create `backend/app/services/production_numbering.py`:

```python
"""Pure production-set ordering + Bates numbering (P2-1). No DB/network.

Bates numbers are assigned at lock time from the set's own prefix; a
document's own bates_begin is an ingest control number, never reused here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

SORT_KEYS = frozenset({"control_number", "custodian_date"})


@dataclass(frozen=True)
class MemberInfo:
    document_id: Any
    control_number: str
    family_id: str | None
    custodian: str | None
    doc_date: datetime | None


def format_bates(prefix: str, number: int, padding: int) -> str:
    """SMITH + 1/6 -> SMITH000001. Wider numbers grow, never truncate."""
    return f"{prefix}{number:0{padding}d}"


def pages_for(disposition: str | None, page_count: int) -> int:
    """Withheld docs occupy exactly one page (the future slip-sheet)."""
    if disposition == "withhold":
        return 1
    return max(page_count, 1)


def _group_head_key(head: MemberInfo, sort_key: str):
    # isoformat strings compare like the datetimes they encode (single-corpus
    # timezones are uniform) and avoid naive/aware comparison errors that a
    # datetime sentinel for "missing" would introduce.
    if sort_key == "custodian_date":
        return (
            head.custodian or "",
            0 if head.doc_date is not None else 1,
            head.doc_date.isoformat() if head.doc_date is not None else "",
            head.control_number,
        )
    return (head.control_number,)


def order_members(members: list[MemberInfo], sort_key: str) -> list[MemberInfo]:
    """Families stay contiguous; groups interleave by the group head's key.

    Within a family, control-number order — parents are ingested before their
    attachments, so the parent sorts first and heads the group.
    """
    if sort_key not in SORT_KEYS:
        raise ValueError(f"unknown sort_key: {sort_key}")
    groups: dict[str, list[MemberInfo]] = {}
    for m in sorted(members, key=lambda m: m.control_number):
        key = m.family_id or f"\x00solo:{m.control_number}"
        groups.setdefault(key, []).append(m)
    ordered = sorted(groups.values(), key=lambda g: _group_head_key(g[0], sort_key))
    return [m for g in ordered for m in g]


def assign_bates(
    ordered: list[tuple[Any, int]], prefix: str, padding: int, start_number: int
) -> list[tuple[Any, int, str, str]]:
    """(doc_id, pages) in final order -> (doc_id, sort_order, begin, end).

    Gap-free: each doc starts where the previous ended + 1.
    """
    out = []
    n = start_number
    for i, (doc_id, pages) in enumerate(ordered, start=1):
        out.append((doc_id, i, format_bates(prefix, n, padding),
                    format_bates(prefix, n + pages - 1, padding)))
        n += pages
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_production_numbering.py -q`
Expected: 13 passed, 0 warnings.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/production_numbering.py backend/tests/test_production_numbering.py
git commit -m "feat(p2-1): pure production numbering (ordering, pages, gap-free Bates)"
```

---

### Task 3: Schemas + router (create / list / detail / members / delete)

**Files:**
- Modify: `backend/tests/fakes.py` (generalize timestamp defaults)
- Modify: `backend/app/schemas.py` (append production-set schemas at end)
- Create: `backend/app/routers/production_sets.py`
- Modify: `backend/app/main.py` (import + register router)
- Test: `backend/tests/test_production_set_endpoints.py`

**Interfaces:**
- Consumes: `ProductionSet`, `ProductionSetItem` (Task 1); fakes.
- Produces:
  - Schemas: `ProductionSetCreate`, `ProductionSetOut`, `ProductionSetMemberOut`, `ProductionSetAddDocuments`, `ProductionSetRemoveDocuments`, `ProductionSetLockOut` (all defined now; Add/Remove/Lock used by Tasks 4-5).
  - Endpoints: `POST/GET /api/productions/{production_id}/production-sets`, `GET /api/production-sets/{set_id}`, `GET /api/production-sets/{set_id}/documents`, `DELETE /api/production-sets/{set_id}`.
  - Router helper `_load_set(db, user, set_id, require_manager=False)` reused by Tasks 4-5.
  - Test module `tests/test_production_set_endpoints.py` with `FakePS` and `_patch` reused by Tasks 4-5.

- [ ] **Step 1: Generalize fakes timestamp defaults**

In `backend/tests/fakes.py`, replace the `add` and `refresh` methods of `FakeSession` (currently hard-coded to `decided_at`) with:

```python
    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = 1000 + len(self.added)
        _fill_timestamps(obj)
        self.added.append(obj)
```

```python
    async def refresh(self, obj):
        _fill_timestamps(obj)
```

and add at module level (below `TS`):

```python
_TS_DEFAULT_FIELDS = ("decided_at", "created_at")


def _fill_timestamps(obj):
    """Stand in for server_default timestamps on flush/refresh."""
    for field in _TS_DEFAULT_FIELDS:
        if hasattr(obj, field) and getattr(obj, field) is None:
            setattr(obj, field, TS)
```

Run `backend\venv\Scripts\python.exe -m pytest backend\tests\test_tag_privilege.py backend\tests\test_redaction_qc_endpoints.py backend\tests\test_privilege_log.py -q` — all must still pass (the change is behavior-preserving for `decided_at`).

- [ ] **Step 2: Write the failing tests**

Create `backend/tests/test_production_set_endpoints.py`:

```python
"""Fake-session tests for production-set endpoints (P2-1)."""

import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException

import app.routers.production_sets as rps
from app.schemas import ProductionSetCreate
from tests.fakes import TS, FakeResult, FakeSession, FakeUser


class FakePS:
    def __init__(self, set_id=1, production_id=1, status="draft", **kw):
        self.id = set_id
        self.production_id = production_id
        self.name = kw.get("name", "Vol 1")
        self.status = status
        self.prefix = kw.get("prefix", "SMITH")
        self.padding = kw.get("padding", 6)
        self.start_number = kw.get("start_number", 1)
        self.sort_key = kw.get("sort_key", "control_number")
        self.designation = kw.get("designation", None)
        self.created_by = "u1"
        self.created_at = TS
        self.locked_by = None
        self.locked_at = None


class FakeItem:
    def __init__(self, document_id, **kw):
        self.id = kw.get("item_id", None)
        self.document_id = document_id
        self.sort_order = kw.get("sort_order", None)
        self.bates_begin = kw.get("bates_begin", None)
        self.bates_end = kw.get("bates_end", None)
        self.pages = kw.get("pages", None)
        self.disposition = kw.get("disposition", None)
        self.designation = kw.get("designation", None)


def _patch(monkeypatch, role="manager", accessible=(1,)):
    async def fake_accessible(db, user):
        return list(accessible)

    async def fake_role(db, user, production_id):
        return role

    async def fake_log(*args, **kwargs):
        pass

    monkeypatch.setattr(rps, "get_accessible_production_ids", fake_accessible)
    monkeypatch.setattr(rps, "get_user_role_for_production", fake_role)
    monkeypatch.setattr(rps, "log_action", fake_log)


# --- POST /productions/{id}/production-sets --------------------------------

def test_create_draft_set(monkeypatch):
    _patch(monkeypatch, role="manager")
    db = FakeSession()
    out = asyncio.run(rps.create_production_set(
        production_id=1,
        body=ProductionSetCreate(name="Vol 1", prefix="SMITH"),
        db=db, user=FakeUser()))
    assert out.status == "draft"
    assert out.prefix == "SMITH"
    assert out.padding == 6
    assert out.doc_count == 0
    assert len(db.added) == 1


def test_create_blocked_for_reviewer(monkeypatch):
    _patch(monkeypatch, role="reviewer")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.create_production_set(
            production_id=1, body=ProductionSetCreate(name="V", prefix="P"),
            db=FakeSession(), user=FakeUser()))
    assert exc.value.status_code == 403


def test_create_403_outside_accessible(monkeypatch):
    _patch(monkeypatch, accessible=(2,))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.create_production_set(
            production_id=1, body=ProductionSetCreate(name="V", prefix="P"),
            db=FakeSession(), user=FakeUser()))
    assert exc.value.status_code == 403


def test_create_rejects_whitespace_prefix(monkeypatch):
    _patch(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.create_production_set(
            production_id=1, body=ProductionSetCreate(name="V", prefix="SMITH VOL"),
            db=FakeSession(), user=FakeUser()))
    assert exc.value.status_code == 422


def test_create_rejects_unknown_sort_key(monkeypatch):
    _patch(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.create_production_set(
            production_id=1,
            body=ProductionSetCreate(name="V", prefix="P", sort_key="bogus"),
            db=FakeSession(), user=FakeUser()))
    assert exc.value.status_code == 422


def test_create_duplicate_name_409(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(responders=[("FROM production_sets", FakeResult(scalar=7))])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.create_production_set(
            production_id=1, body=ProductionSetCreate(name="Vol 1", prefix="P"),
            db=db, user=FakeUser()))
    assert exc.value.status_code == 409


# --- GET list / detail ------------------------------------------------------

def test_list_sets_with_doc_counts(monkeypatch):
    _patch(monkeypatch)
    s1, s2 = FakePS(set_id=1), FakePS(set_id=2, name="Vol 2", status="locked")
    db = FakeSession(responders=[
        ("FROM production_set_items", FakeResult(rows=[(1, 3)])),
        ("FROM production_sets", FakeResult(items=[s1, s2])),
    ])
    out = asyncio.run(rps.list_production_sets(production_id=1, db=db, user=FakeUser()))
    assert [o.doc_count for o in out] == [3, 0]


def test_detail_locked_set_aggregates(monkeypatch):
    _patch(monkeypatch)
    d1, d2 = uuid4(), uuid4()
    ps = FakePS(status="locked")
    items = [
        FakeItem(d1, item_id=1, sort_order=1, bates_begin="SMITH000001",
                 bates_end="SMITH000003", pages=3, disposition="produce"),
        FakeItem(d2, item_id=2, sort_order=2, bates_begin="SMITH000004",
                 bates_end="SMITH000004", pages=1, disposition="withhold"),
    ]
    db = FakeSession(
        get_objects={("ProductionSet", 1): ps},
        responders=[("FROM production_set_items", FakeResult(items=items))],
    )
    out = asyncio.run(rps.get_production_set(set_id=1, db=db, user=FakeUser()))
    assert out.doc_count == 2
    assert out.page_count == 4
    assert out.bates_begin == "SMITH000001"
    assert out.bates_end == "SMITH000004"


def test_detail_404(monkeypatch):
    _patch(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.get_production_set(set_id=9, db=FakeSession(), user=FakeUser()))
    assert exc.value.status_code == 404


# --- GET members ------------------------------------------------------------

def test_members_list_maps_rows(monkeypatch):
    _patch(monkeypatch)
    d1 = uuid4()
    ps = FakePS()
    item = FakeItem(d1, item_id=1)
    db = FakeSession(
        get_objects={("ProductionSet", 1): ps},
        responders=[("JOIN documents", FakeResult(rows=[(item, "C-001")]))],
    )
    out = asyncio.run(rps.list_production_set_documents(set_id=1, db=db, user=FakeUser()))
    assert len(out) == 1
    assert out[0].document_id == d1
    assert out[0].control_number == "C-001"
    assert out[0].bates_begin is None  # draft: not yet assigned


# --- DELETE set -------------------------------------------------------------

def test_delete_draft_set(monkeypatch):
    _patch(monkeypatch)
    ps = FakePS()
    db = FakeSession(get_objects={("ProductionSet", 1): ps})
    out = asyncio.run(rps.delete_production_set(set_id=1, db=db, user=FakeUser()))
    assert out == {"ok": True}


def test_delete_locked_set_409(monkeypatch):
    _patch(monkeypatch)
    ps = FakePS(status="locked")
    db = FakeSession(get_objects={("ProductionSet", 1): ps})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.delete_production_set(set_id=1, db=db, user=FakeUser()))
    assert exc.value.status_code == 409
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_production_set_endpoints.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.routers.production_sets'` (or `ImportError: cannot import name 'ProductionSetCreate'`).

- [ ] **Step 4: Implement**

(a) `backend/app/schemas.py` — append at the end (reuse the file's existing `BaseModel`/`UUID`/`datetime` imports):

```python
# --- P2-1: production sets --------------------------------------------------

class ProductionSetCreate(BaseModel):
    name: str
    prefix: str
    padding: int = 6
    start_number: int = 1
    sort_key: str = "control_number"
    designation: str | None = None


class ProductionSetOut(BaseModel):
    id: int
    production_id: int
    name: str
    status: str
    prefix: str
    padding: int
    start_number: int
    sort_key: str
    designation: str | None
    created_by: str
    created_at: datetime
    locked_by: str | None
    locked_at: datetime | None
    doc_count: int = 0
    page_count: int | None = None
    bates_begin: str | None = None
    bates_end: str | None = None

    model_config = {"from_attributes": True}


class ProductionSetMemberOut(BaseModel):
    document_id: UUID
    control_number: str
    sort_order: int | None
    bates_begin: str | None
    bates_end: str | None
    pages: int | None
    disposition: str | None
    designation: str | None


class ProductionSetAddDocuments(BaseModel):
    document_ids: list[UUID] | None = None
    tag_id: int | None = None
    include_families: bool = False
    exclude_duplicates: bool = False


class ProductionSetRemoveDocuments(BaseModel):
    document_ids: list[UUID]


class ProductionSetLockOut(BaseModel):
    doc_count: int
    page_count: int
    bates_begin: str
    bates_end: str
```

(b) Create `backend/app/routers/production_sets.py`:

```python
"""Production sets: deliverable volumes with draft->lock Bates assignment (P2-1)."""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import ROLE_RANK, get_accessible_production_ids, get_user_role_for_production
from app.models import (
    Document,
    DocumentDuplicate,
    DocumentTag,
    DuplicateGroup,
    ProductionSet,
    ProductionSetItem,
    Redaction,
    Tag,
    User,
)
from app.routers.auth import get_current_user
from app.schemas import (
    ProductionSetAddDocuments,
    ProductionSetCreate,
    ProductionSetLockOut,
    ProductionSetMemberOut,
    ProductionSetOut,
    ProductionSetRemoveDocuments,
)
from app.services.audit import log_action
from app.services.privilege import effective_disposition
from app.services.production_numbering import (
    SORT_KEYS,
    MemberInfo,
    assign_bates,
    order_members,
    pages_for,
)

router = APIRouter(prefix="/api", tags=["production-sets"])


async def _load_set(
    db: AsyncSession, user: User, set_id: int, require_manager: bool = False
) -> ProductionSet:
    ps = await db.get(ProductionSet, set_id)
    if not ps:
        raise HTTPException(status_code=404, detail="Production set not found")
    accessible = await get_accessible_production_ids(db, user)
    if ps.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    if require_manager:
        role = await get_user_role_for_production(db, user, ps.production_id)
        if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
            raise HTTPException(status_code=403, detail="Manager or higher role required")
    return ps


@router.post("/productions/{production_id}/production-sets",
             response_model=ProductionSetOut, status_code=201)
async def create_production_set(
    production_id: int,
    body: ProductionSetCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    role = await get_user_role_for_production(db, user, production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or higher role required")
    if not body.prefix or any(c.isspace() for c in body.prefix):
        raise HTTPException(status_code=422, detail="prefix must be non-empty with no whitespace")
    if body.sort_key not in SORT_KEYS:
        raise HTTPException(status_code=422, detail="invalid sort_key")
    if not (1 <= body.padding <= 12) or body.start_number < 1:
        raise HTTPException(status_code=422, detail="invalid padding or start_number")

    dup = (await db.execute(
        select(ProductionSet.id).where(
            ProductionSet.production_id == production_id,
            ProductionSet.name == body.name,
        )
    )).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(status_code=409, detail="A production set with this name already exists")

    # Pass every column explicitly: Python-side Column defaults only apply at
    # flush, and the fake-session tests never flush against a real DB.
    ps = ProductionSet(
        production_id=production_id, name=body.name, status="draft",
        prefix=body.prefix, padding=body.padding, start_number=body.start_number,
        sort_key=body.sort_key, designation=body.designation, created_by=user.id,
    )
    db.add(ps)
    await db.flush()
    await log_action(db, user, "production_set_created", "production_set", str(ps.id),
                     production_id=production_id,
                     details={"name": body.name, "prefix": body.prefix})
    await db.commit()
    await db.refresh(ps)
    return ProductionSetOut.model_validate(ps)


@router.get("/productions/{production_id}/production-sets",
            response_model=list[ProductionSetOut])
async def list_production_sets(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    sets = (await db.execute(
        select(ProductionSet)
        .where(ProductionSet.production_id == production_id)
        .order_by(ProductionSet.created_at, ProductionSet.id)
    )).scalars().all()
    counts: dict[int, int] = {}
    if sets:
        rows = (await db.execute(
            select(ProductionSetItem.production_set_id, func.count(ProductionSetItem.id))
            .where(ProductionSetItem.production_set_id.in_([s.id for s in sets]))
            .group_by(ProductionSetItem.production_set_id)
        )).all()
        counts = {r[0]: r[1] for r in rows}
    out = []
    for s in sets:
        o = ProductionSetOut.model_validate(s)
        o.doc_count = counts.get(s.id, 0)
        out.append(o)
    return out


@router.get("/production-sets/{set_id}", response_model=ProductionSetOut)
async def get_production_set(
    set_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ps = await _load_set(db, user, set_id)
    items = (await db.execute(
        select(ProductionSetItem)
        .where(ProductionSetItem.production_set_id == set_id)
        .order_by(ProductionSetItem.sort_order)
    )).scalars().all()
    out = ProductionSetOut.model_validate(ps)
    out.doc_count = len(items)
    if ps.status == "locked" and items:
        out.page_count = sum(i.pages or 0 for i in items)
        out.bates_begin = items[0].bates_begin
        out.bates_end = items[-1].bates_end
    return out


@router.get("/production-sets/{set_id}/documents",
            response_model=list[ProductionSetMemberOut])
async def list_production_set_documents(
    set_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ps = await _load_set(db, user, set_id)
    rows = (await db.execute(
        select(ProductionSetItem, Document.bates_begin)
        .join(Document, Document.id == ProductionSetItem.document_id)
        .where(ProductionSetItem.production_set_id == set_id)
        .order_by(ProductionSetItem.sort_order, Document.bates_begin)
    )).all()
    return [
        ProductionSetMemberOut(
            document_id=item.document_id, control_number=control,
            sort_order=item.sort_order, bates_begin=item.bates_begin,
            bates_end=item.bates_end, pages=item.pages,
            disposition=item.disposition, designation=item.designation,
        )
        for item, control in rows
    ]


@router.delete("/production-sets/{set_id}")
async def delete_production_set(
    set_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ps = await _load_set(db, user, set_id, require_manager=True)
    if ps.status != "draft":
        raise HTTPException(status_code=409, detail="Locked production sets cannot be deleted")
    await log_action(db, user, "production_set_deleted", "production_set", str(set_id),
                     production_id=ps.production_id, details={"name": ps.name})
    await db.delete(ps)
    await db.commit()
    return {"ok": True}
```

(c) `backend/app/main.py`: add `production_sets` to the `from app.routers import ...` list (alphabetical) and `app.include_router(production_sets.router)` next to the other registrations.

- [ ] **Step 5: Run tests to verify they pass**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_production_set_endpoints.py backend\tests\test_tag_privilege.py backend\tests\test_redaction_qc_endpoints.py backend\tests\test_privilege_log.py -q`
Expected: all pass (12 new + existing), 0 warnings.

- [ ] **Step 6: Commit**

```bash
git add backend/tests/fakes.py backend/app/schemas.py backend/app/routers/production_sets.py backend/app/main.py backend/tests/test_production_set_endpoints.py
git commit -m "feat(p2-1): production set schemas + create/list/detail/members/delete endpoints"
```

---

### Task 4: Add / remove members (tags, families, dedup)

**Files:**
- Modify: `backend/app/routers/production_sets.py` (append two endpoints)
- Test: `backend/tests/test_production_set_endpoints.py` (append)

**Interfaces:**
- Consumes: `_load_set`, schemas (Task 3), models (Task 1).
- Produces: `POST /api/production-sets/{set_id}/documents` → `{added, skipped_existing, skipped_duplicates, families_added}`; `DELETE /api/production-sets/{set_id}/documents` → `{removed}` (count of requested ids). Task 5 consumes members created here.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_production_set_endpoints.py`:

```python
# --- POST /production-sets/{id}/documents ----------------------------------
# FakeSession dispatches on the FIRST matching substring, so register
# responders in this order: "document_tags", "family_id IN",
# "document_duplicates", "production_set_items", "documents.production_id"
# (the last is a substring of several queries' WHERE clauses).

from app.schemas import ProductionSetAddDocuments, ProductionSetRemoveDocuments


def test_add_explicit_docs(monkeypatch):
    _patch(monkeypatch)
    d1, d2 = uuid4(), uuid4()
    db = FakeSession(
        get_objects={("ProductionSet", 1): FakePS()},
        responders=[
            ("documents.production_id", FakeResult(rows=[(d1, 1, None), (d2, 1, None)])),
        ],
    )
    out = asyncio.run(rps.add_documents(
        set_id=1, body=ProductionSetAddDocuments(document_ids=[d1, d2]),
        db=db, user=FakeUser()))
    assert out == {"added": 2, "skipped_existing": 0,
                   "skipped_duplicates": 0, "families_added": 0}
    assert len(db.added) == 2


def test_add_by_tag(monkeypatch):
    _patch(monkeypatch)
    d1, d2 = uuid4(), uuid4()
    db = FakeSession(
        get_objects={("ProductionSet", 1): FakePS()},
        responders=[
            ("document_tags", FakeResult(rows=[(d1,), (d2,)])),
            ("documents.production_id", FakeResult(rows=[(d1, 1, None), (d2, 1, None)])),
        ],
    )
    out = asyncio.run(rps.add_documents(
        set_id=1, body=ProductionSetAddDocuments(tag_id=5), db=db, user=FakeUser()))
    assert out["added"] == 2


def test_add_doc_from_other_matter_422(monkeypatch):
    _patch(monkeypatch)
    d1 = uuid4()
    db = FakeSession(
        get_objects={("ProductionSet", 1): FakePS()},
        responders=[
            ("documents.production_id", FakeResult(rows=[(d1, 2, None)])),
        ],
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.add_documents(
            set_id=1, body=ProductionSetAddDocuments(document_ids=[d1]),
            db=db, user=FakeUser()))
    assert exc.value.status_code == 422


def test_add_unknown_doc_422(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(get_objects={("ProductionSet", 1): FakePS()})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.add_documents(
            set_id=1, body=ProductionSetAddDocuments(document_ids=[uuid4()]),
            db=db, user=FakeUser()))
    assert exc.value.status_code == 422


def test_add_nothing_specified_422(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(get_objects={("ProductionSet", 1): FakePS()})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.add_documents(
            set_id=1, body=ProductionSetAddDocuments(), db=db, user=FakeUser()))
    assert exc.value.status_code == 422


def test_add_include_families_pulls_family_members(monkeypatch):
    _patch(monkeypatch)
    d1, d3 = uuid4(), uuid4()  # d1 explicit (family F1); d3 = its attachment
    db = FakeSession(
        get_objects={("ProductionSet", 1): FakePS()},
        responders=[
            ("family_id IN", FakeResult(rows=[(d1,), (d3,)])),
            ("documents.production_id", FakeResult(rows=[(d1, 1, "F1")])),
        ],
    )
    out = asyncio.run(rps.add_documents(
        set_id=1,
        body=ProductionSetAddDocuments(document_ids=[d1], include_families=True),
        db=db, user=FakeUser()))
    assert out["added"] == 2
    assert out["families_added"] == 1


def test_add_exclude_duplicates_keeps_primary(monkeypatch):
    _patch(monkeypatch)
    d1, d2 = uuid4(), uuid4()  # same hash group; d2 has the lower control -> primary
    db = FakeSession(
        get_objects={("ProductionSet", 1): FakePS()},
        responders=[
            ("document_tags", FakeResult(rows=[(d1,), (d2,)])),
            ("document_duplicates", FakeResult(rows=[(10, d1, "C-2"), (10, d2, "C-1")])),
            ("documents.production_id", FakeResult(rows=[(d1, 1, None), (d2, 1, None)])),
        ],
    )
    out = asyncio.run(rps.add_documents(
        set_id=1,
        body=ProductionSetAddDocuments(tag_id=5, exclude_duplicates=True),
        db=db, user=FakeUser()))
    assert out["added"] == 1
    assert out["skipped_duplicates"] == 1


def test_add_exclude_duplicates_never_drops_explicit_ids(monkeypatch):
    _patch(monkeypatch)
    d1, d2 = uuid4(), uuid4()  # d1 explicitly listed but NOT the primary
    db = FakeSession(
        get_objects={("ProductionSet", 1): FakePS()},
        responders=[
            ("document_duplicates", FakeResult(rows=[(10, d1, "C-2"), (10, d2, "C-1")])),
            ("documents.production_id", FakeResult(rows=[(d1, 1, None)])),
        ],
    )
    out = asyncio.run(rps.add_documents(
        set_id=1,
        body=ProductionSetAddDocuments(document_ids=[d1], exclude_duplicates=True),
        db=db, user=FakeUser()))
    assert out["added"] == 1
    assert out["skipped_duplicates"] == 0


def test_add_skips_existing_members(monkeypatch):
    _patch(monkeypatch)
    d1, d2 = uuid4(), uuid4()
    db = FakeSession(
        get_objects={("ProductionSet", 1): FakePS()},
        responders=[
            ("production_set_items", FakeResult(rows=[(d1,)])),
            ("documents.production_id", FakeResult(rows=[(d1, 1, None), (d2, 1, None)])),
        ],
    )
    out = asyncio.run(rps.add_documents(
        set_id=1, body=ProductionSetAddDocuments(document_ids=[d1, d2]),
        db=db, user=FakeUser()))
    assert out["added"] == 1
    assert out["skipped_existing"] == 1


def test_add_to_locked_set_409(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(get_objects={("ProductionSet", 1): FakePS(status="locked")})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.add_documents(
            set_id=1, body=ProductionSetAddDocuments(document_ids=[uuid4()]),
            db=db, user=FakeUser()))
    assert exc.value.status_code == 409


def test_add_blocked_for_reviewer(monkeypatch):
    _patch(monkeypatch, role="reviewer")
    db = FakeSession(get_objects={("ProductionSet", 1): FakePS()})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.add_documents(
            set_id=1, body=ProductionSetAddDocuments(document_ids=[uuid4()]),
            db=db, user=FakeUser()))
    assert exc.value.status_code == 403


# --- DELETE /production-sets/{id}/documents --------------------------------

def test_remove_documents_draft(monkeypatch):
    _patch(monkeypatch)
    d1 = uuid4()
    db = FakeSession(get_objects={("ProductionSet", 1): FakePS()})
    out = asyncio.run(rps.remove_documents(
        set_id=1, body=ProductionSetRemoveDocuments(document_ids=[d1]),
        db=db, user=FakeUser()))
    assert out == {"removed": 1}


def test_remove_documents_locked_409(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(get_objects={("ProductionSet", 1): FakePS(status="locked")})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.remove_documents(
            set_id=1, body=ProductionSetRemoveDocuments(document_ids=[uuid4()]),
            db=db, user=FakeUser()))
    assert exc.value.status_code == 409
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_production_set_endpoints.py -q`
Expected: new tests FAIL with `AttributeError: module 'app.routers.production_sets' has no attribute 'add_documents'`; Task 3 tests still pass.

- [ ] **Step 3: Implement**

Append to `backend/app/routers/production_sets.py`:

```python
@router.post("/production-sets/{set_id}/documents")
async def add_documents(
    set_id: int,
    body: ProductionSetAddDocuments,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ps = await _load_set(db, user, set_id, require_manager=True)
    if ps.status != "draft":
        raise HTTPException(status_code=409, detail="Production set is locked")
    if not body.document_ids and body.tag_id is None:
        raise HTTPException(status_code=422, detail="Provide document_ids and/or tag_id")

    explicit = set(body.document_ids or [])
    candidates = set(explicit)
    if body.tag_id is not None:
        tag_rows = (await db.execute(
            select(DocumentTag.document_id)
            .join(Document, Document.id == DocumentTag.document_id)
            .where(DocumentTag.tag_id == body.tag_id,
                   Document.production_id == ps.production_id)
        )).all()
        candidates.update(r[0] for r in tag_rows)

    info_rows = []
    if candidates:
        info_rows = (await db.execute(
            select(Document.id, Document.production_id, Document.family_id)
            .where(Document.id.in_(candidates))
        )).all()
    found = {r[0] for r in info_rows}
    if (explicit - found) or any(r[1] != ps.production_id for r in info_rows):
        raise HTTPException(status_code=422, detail="Documents not found in this matter")

    families_added = 0
    if body.include_families:
        fams = {r[2] for r in info_rows if r[2]}
        if fams:
            fam_rows = (await db.execute(
                select(Document.id)
                .where(Document.production_id == ps.production_id,
                       Document.family_id.in_(fams))
            )).all()
            fam_ids = {r[0] for r in fam_rows}
            families_added = len(fam_ids - candidates)
            candidates |= fam_ids

    skipped_duplicates = 0
    if body.exclude_duplicates:
        dup_rows = (await db.execute(
            select(DocumentDuplicate.group_id, DocumentDuplicate.document_id,
                   Document.bates_begin)
            .join(DuplicateGroup, DuplicateGroup.id == DocumentDuplicate.group_id)
            .join(Document, Document.id == DocumentDuplicate.document_id)
            .where(DuplicateGroup.production_id == ps.production_id,
                   DuplicateGroup.type == "hash")
        )).all()
        groups: dict[int, list[tuple[str, object]]] = {}
        for gid, did, control in dup_rows:
            groups.setdefault(gid, []).append((control, did))
        for members in groups.values():
            primary = min(members)[1]  # lowest control number wins
            for _, did in members:
                if did in candidates and did != primary and did not in explicit:
                    candidates.discard(did)
                    skipped_duplicates += 1

    existing_rows = (await db.execute(
        select(ProductionSetItem.document_id)
        .where(ProductionSetItem.production_set_id == set_id)
    )).all()
    existing = {r[0] for r in existing_rows}
    to_add = candidates - existing
    for did in sorted(to_add, key=str):
        db.add(ProductionSetItem(production_set_id=set_id, document_id=did))

    summary = {"added": len(to_add), "skipped_existing": len(candidates & existing),
               "skipped_duplicates": skipped_duplicates, "families_added": families_added}
    await log_action(db, user, "production_set_documents_added", "production_set",
                     str(set_id), production_id=ps.production_id, details=summary)
    await db.commit()
    return summary


@router.delete("/production-sets/{set_id}/documents")
async def remove_documents(
    set_id: int,
    body: ProductionSetRemoveDocuments,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ps = await _load_set(db, user, set_id, require_manager=True)
    if ps.status != "draft":
        raise HTTPException(status_code=409, detail="Production set is locked")
    await db.execute(
        sa_delete(ProductionSetItem).where(
            ProductionSetItem.production_set_id == set_id,
            ProductionSetItem.document_id.in_(body.document_ids),
        )
    )
    await log_action(db, user, "production_set_documents_removed", "production_set",
                     str(set_id), production_id=ps.production_id,
                     details={"document_ids": [str(i) for i in body.document_ids]})
    await db.commit()
    # count of requested ids (fake sessions have no rowcount)
    return {"removed": len(body.document_ids)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_production_set_endpoints.py -q`
Expected: all pass (25 total), 0 warnings.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/production_sets.py backend/tests/test_production_set_endpoints.py
git commit -m "feat(p2-1): add/remove production-set members (tags, families, dedup)"
```

---

### Task 5: Lock endpoint (dispositions, ordering, Bates assignment)

**Files:**
- Modify: `backend/app/routers/production_sets.py` (append lock endpoint)
- Test: `backend/tests/test_production_set_endpoints.py` (append)

**Interfaces:**
- Consumes: `_load_set`, `ProductionSetLockOut` (Task 3), pure service (Task 2), `effective_disposition` (`app.services.privilege`), members (Task 4).
- Produces: `POST /api/production-sets/{set_id}/lock` → `ProductionSetLockOut {doc_count, page_count, bates_begin, bates_end}`. P2-2 will render from the snapshotted item rows.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_production_set_endpoints.py`:

```python
# --- POST /production-sets/{id}/lock ---------------------------------------
# Responder order for lock tests: "is_privilege", "redactions",
# "production_set_items", "documents.page_count".


def test_lock_assigns_and_snapshots(monkeypatch):
    _patch(monkeypatch)
    d1, d2, d3 = uuid4(), uuid4(), uuid4()
    items = [FakeItem(d1, item_id=1), FakeItem(d2, item_id=2), FakeItem(d3, item_id=3)]
    ps = FakePS()
    doc_rows = [
        # (id, control, family_id, custodian, date_sent, date_received,
        #  page_count, privilege_disposition)
        (d1, "C-1", None, "Alice", TS, None, 5, None),  # privilege tag -> withhold, 1 page
        (d2, "C-2", None, "Bob", TS, None, 3, None),    # redactions -> redact_in_part
        (d3, "C-3", None, "Cara", TS, None, 2, None),   # nothing -> produce
    ]
    db = FakeSession(
        get_objects={("ProductionSet", 1): ps},
        responders=[
            ("is_privilege", FakeResult(rows=[(d1,)])),
            ("redactions", FakeResult(rows=[(d2, 4)])),
            ("production_set_items", FakeResult(items=items)),
            ("documents.page_count", FakeResult(rows=doc_rows)),
        ],
    )
    out = asyncio.run(rps.lock_production_set(set_id=1, db=db, user=FakeUser()))
    assert out.doc_count == 3
    assert out.page_count == 1 + 3 + 2
    assert out.bates_begin == "SMITH000001"
    assert out.bates_end == "SMITH000006"
    by_doc = {i.document_id: i for i in items}
    assert by_doc[d1].disposition == "withhold"
    assert by_doc[d1].pages == 1
    assert (by_doc[d1].bates_begin, by_doc[d1].bates_end) == ("SMITH000001", "SMITH000001")
    assert by_doc[d2].disposition == "redact_in_part"
    assert (by_doc[d2].bates_begin, by_doc[d2].bates_end) == ("SMITH000002", "SMITH000004")
    assert by_doc[d3].disposition == "produce"
    assert (by_doc[d3].bates_begin, by_doc[d3].bates_end) == ("SMITH000005", "SMITH000006")
    assert [by_doc[d].sort_order for d in (d1, d2, d3)] == [1, 2, 3]
    assert ps.status == "locked"
    assert ps.locked_by == "u1"
    assert ps.locked_at is not None


def test_lock_produce_override_keeps_full_pages(monkeypatch):
    _patch(monkeypatch)
    d1 = uuid4()
    items = [FakeItem(d1, item_id=1)]
    ps = FakePS()
    doc_rows = [(d1, "C-1", None, None, None, None, 4, "produce")]
    db = FakeSession(
        get_objects={("ProductionSet", 1): ps},
        responders=[
            ("is_privilege", FakeResult(rows=[(d1,)])),  # tagged, but override wins
            ("redactions", FakeResult(rows=[])),
            ("production_set_items", FakeResult(items=items)),
            ("documents.page_count", FakeResult(rows=doc_rows)),
        ],
    )
    out = asyncio.run(rps.lock_production_set(set_id=1, db=db, user=FakeUser()))
    assert items[0].disposition == "produce"
    assert items[0].pages == 4
    assert out.page_count == 4


def test_lock_empty_set_422(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(get_objects={("ProductionSet", 1): FakePS()})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.lock_production_set(set_id=1, db=db, user=FakeUser()))
    assert exc.value.status_code == 422


def test_lock_already_locked_409(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession(get_objects={("ProductionSet", 1): FakePS(status="locked")})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.lock_production_set(set_id=1, db=db, user=FakeUser()))
    assert exc.value.status_code == 409


def test_lock_blocked_for_reviewer(monkeypatch):
    _patch(monkeypatch, role="reviewer")
    db = FakeSession(get_objects={("ProductionSet", 1): FakePS()})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rps.lock_production_set(set_id=1, db=db, user=FakeUser()))
    assert exc.value.status_code == 403
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_production_set_endpoints.py -q -k lock`
Expected: FAIL with `AttributeError: module 'app.routers.production_sets' has no attribute 'lock_production_set'`

- [ ] **Step 3: Implement**

Append to `backend/app/routers/production_sets.py`:

```python
@router.post("/production-sets/{set_id}/lock", response_model=ProductionSetLockOut)
async def lock_production_set(
    set_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ps = await _load_set(db, user, set_id, require_manager=True)
    if ps.status != "draft":
        raise HTTPException(status_code=409, detail="Production set is already locked")

    items = (await db.execute(
        select(ProductionSetItem).where(ProductionSetItem.production_set_id == set_id)
    )).scalars().all()
    if not items:
        raise HTTPException(status_code=422, detail="Cannot lock an empty production set")
    doc_ids = [i.document_id for i in items]

    doc_rows = (await db.execute(
        select(Document.id, Document.bates_begin, Document.family_id,
               Document.custodian, Document.date_sent, Document.date_received,
               Document.page_count, Document.privilege_disposition)
        .where(Document.id.in_(doc_ids))
    )).all()

    priv_rows = (await db.execute(
        select(DocumentTag.document_id)
        .join(Tag, Tag.id == DocumentTag.tag_id)
        .where(Tag.is_privilege.is_(True), DocumentTag.document_id.in_(doc_ids))
    )).all()
    privileged = {r[0] for r in priv_rows}

    red_rows = (await db.execute(
        select(Redaction.document_id, func.count(Redaction.id))
        .where(Redaction.document_id.in_(doc_ids))
        .group_by(Redaction.document_id)
    )).all()
    red_counts = {r[0]: r[1] for r in red_rows}

    members: list[MemberInfo] = []
    meta: dict = {}  # document_id -> (disposition, pages)
    for did, control, family_id, custodian, date_sent, date_received, page_count, override in doc_rows:
        disposition = effective_disposition(
            has_privilege_tag=did in privileged,
            has_redactions=red_counts.get(did, 0) > 0,
            override=override,
        ) or "produce"
        meta[did] = (disposition, pages_for(disposition, page_count or 1))
        members.append(MemberInfo(
            document_id=did, control_number=control, family_id=family_id,
            custodian=custodian, doc_date=date_sent or date_received,
        ))

    ordered = order_members(members, ps.sort_key)
    assignments = assign_bates(
        [(m.document_id, meta[m.document_id][1]) for m in ordered],
        ps.prefix, ps.padding, ps.start_number,
    )
    items_by_doc = {i.document_id: i for i in items}
    for did, sort_order, begin, end in assignments:
        item = items_by_doc[did]
        item.sort_order = sort_order
        item.bates_begin = begin
        item.bates_end = end
        item.disposition, item.pages = meta[did]

    ps.status = "locked"
    ps.locked_by = user.id
    ps.locked_at = datetime.now(timezone.utc).replace(tzinfo=None)

    summary = {
        "doc_count": len(assignments),
        "page_count": sum(meta[d][1] for d in items_by_doc),
        "bates_begin": assignments[0][2],
        "bates_end": assignments[-1][3],
    }
    await log_action(db, user, "production_set_locked", "production_set", str(set_id),
                     production_id=ps.production_id, details=summary)
    await db.commit()
    return ProductionSetLockOut(**summary)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_production_set_endpoints.py backend\tests\test_production_numbering.py -q`
Expected: all pass (30 endpoint + 13 pure), 0 warnings.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/production_sets.py backend/tests/test_production_set_endpoints.py
git commit -m "feat(p2-1): lock endpoint — dispositions, ordering, gap-free Bates assignment"
```

---

### Task 6: Full-suite verification + PR

**Files:** none new.

**Interfaces:** n/a — verification gate.

- [ ] **Step 1: Full suite**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests -q`
Expected: everything passes except the known pre-existing failure `test_ai_review.py::test_build_classification_prompt` (fails on origin/main too). Any other failure = regression; fix code, never old tests.

- [ ] **Step 2: Migration head + purity re-check**

Grep `backend/alembic/versions` for `down_revision` containing `t2b3c4d5e6f7` — exactly one file (`a9b8c7d6e5f4...`). Confirm that file has no `app.` imports.

- [ ] **Step 3: Push and open PR**

```bash
git push -u origin feat/p2-1-production-set-builder
gh pr create --title "feat(p2-1): production set builder + Bates numbering" --body "$(cat <<'EOF'
## Summary
- New ProductionSet/ProductionSetItem model: deliverable volumes within a matter (distinct from the Production matter container), draft -> lock lifecycle
- Membership builder: add by ids and/or tag, optional family expansion, optional hash-dedup filtering (primary = lowest control number; explicit ids never dropped)
- Lock = one-way gate: computes dispositions (reusing privilege logic), keeps families contiguous, assigns fresh gap-free Bates from the set's prefix, snapshots pages/disposition per member
- Pure numbering/ordering service with exhaustive unit tests; import-safe migration (a9b8c7d6e5f4)

Spec: docs/superpowers/specs/2026-07-22-p2-1-production-set-builder-design.md

## Test plan
- [x] Pure tests: Bates formatting/overflow, withhold=1 page, family contiguity, both sort keys, deterministic ordering with missing fields, gap-free assignment
- [x] Fake-session endpoint tests: role gates, draft/locked state machine, add-by-tag/families/dedup permutations, lock snapshots + summary
- [x] Full backend suite green (1 pre-existing unrelated failure)
EOF
)"
```
