# P1-4/5 Redaction QC + Privilege Log Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-document redaction QC with auto-invalidating approvals, and a privilege log (derived disposition + templated descriptions) exportable as CSV.

**Architecture:** One import-safe migration adds `tags.is_privilege`, two nullable override columns on `documents`, and an append-only `redaction_qc_decisions` table. Pure domain logic (disposition rule, QC freshness, description template) lives in `app/services/privilege.py`; DB-aware log assembly in `app/services/privilege_log.py`; endpoints extend `tags.py`/`redactions.py`/`export.py` plus a new `routers/privilege.py`. QC status and effective disposition are always computed, never stored.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, pytest fake-session tests (no DB).

**Spec:** `docs/superpowers/specs/2026-07-22-p1-4-5-redaction-qc-privilege-log-design.md`

## Global Constraints

- Migration `t2b3c4d5e6f7`, `down_revision = "4278c984ed43"` (current single head). It must import NOTHING from `app.*` (CI runs alembic under minimal deps; a violation cost a prod deploy once).
- Dispositions (exact): `withhold`, `redact_in_part`, `produce`. QC statuses (exact): `not_applicable`, `pending`, `approved`, `rejected`.
- QC freshness rule: pending unless the latest decision's `redaction_count` snapshot equals the current count AND no redaction `updated_at`/`created_at` ≥ `decided_at`.
- Reason-code labels come from `REASON_LABELS` in `app/services/redaction_render.py` — do not duplicate the mapping.
- Descriptions NEVER include `text_content`, `summary`, or `title`.
- Roles: QC decisions, tag privilege flag, and privilege overrides = manager+; reads = any role with production access. Every write audit-logged via `log_action`.
- Tests: fake-session pattern (no DB/TestClient); shared fakes for the NEW test files live in `backend/tests/fakes.py` (do not modify `test_redacted_rendition.py`'s fakes).
- Run tests from repo root: `backend\venv\Scripts\python.exe -m pytest backend\tests\<file> -q`. Test output pristine (0 warnings).
- Verify `git branch --show-current` == `feat/p1-4-5-redaction-qc-privilege-log` before every commit (a parallel session sometimes switches branches in this clone).
- Commit messages end with trailer: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

---

### Task 1: Migration + models

**Files:**
- Create: `backend/alembic/versions/t2b3c4d5e6f7_add_privilege_redaction_qc.py`
- Modify: `backend/app/models.py` (Tag ~line 166, Document ~line 141, new model after `Redaction` ~line 408)

**Interfaces:**
- Consumes: nothing.
- Produces: `Tag.is_privilege: bool`; `Document.privilege_disposition: str|None`; `Document.privilege_description: str|None`; model `RedactionQCDecision` (`__tablename__ = "redaction_qc_decisions"`; fields `id, document_id, decision, note, redaction_count, decided_by, decided_at`). Later tasks import `RedactionQCDecision` from `app.models`.

- [ ] **Step 1: Write the migration**

Create `backend/alembic/versions/t2b3c4d5e6f7_add_privilege_redaction_qc.py`:

```python
"""add privilege flags + redaction qc decisions

Revision ID: t2b3c4d5e6f7
Revises: 4278c984ed43
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "t2b3c4d5e6f7"
down_revision = "4278c984ed43"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tags", sa.Column("is_privilege", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("documents", sa.Column("privilege_disposition", sa.String(length=20), nullable=True))
    op.add_column("documents", sa.Column("privilege_description", sa.Text(), nullable=True))
    op.create_table(
        "redaction_qc_decisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("document_id", UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("redaction_count", sa.Integer(), nullable=False),
        sa.Column("decided_by", sa.String(length=128), nullable=False),
        sa.Column("decided_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_rqc_document_id", "redaction_qc_decisions", ["document_id"])


def downgrade():
    op.drop_index("ix_rqc_document_id", table_name="redaction_qc_decisions")
    op.drop_table("redaction_qc_decisions")
    op.drop_column("documents", "privilege_description")
    op.drop_column("documents", "privilege_disposition")
    op.drop_column("tags", "is_privilege")
```

- [ ] **Step 2: Update models**

In `backend/app/models.py`:

(a) In `Tag` (after `production_id`, ~line 174):

```python
    is_privilege = Column(Boolean, nullable=False, default=False)
```

(b) In `Document` (after `email_subject`, ~line 141):

```python
    # P1-4/5 — privilege overrides (NULL = derived / templated)
    privilege_disposition = Column(String(20), nullable=True)
    privilege_description = Column(Text, nullable=True)
```

(c) After the `Redaction` class (~line 408):

```python
class RedactionQCDecision(Base):
    __tablename__ = "redaction_qc_decisions"
    __table_args__ = (
        Index("ix_rqc_document_id", "document_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    decision = Column(String(20), nullable=False)  # 'approved' | 'rejected'
    note = Column(Text, nullable=True)
    redaction_count = Column(Integer, nullable=False)  # snapshot at decision time
    decided_by = Column(String(128), nullable=False)
    decided_at = Column(DateTime, server_default=func.now(), nullable=False)
```

- [ ] **Step 3: Verify import purity, compile, single head, models import**

```
backend\venv\Scripts\python.exe -m py_compile backend\alembic\versions\t2b3c4d5e6f7_add_privilege_redaction_qc.py
backend\venv\Scripts\python.exe -c "import app.models" (run from backend/ or with PYTHONPATH=backend)
```
Then confirm: the migration file contains no `import app` / `from app`, and no other migration has `down_revision` containing `t2b3c4d5e6f7`'s parent besides it (grep `4278c984ed43` in `backend/alembic/versions` — exactly two hits: the merge migration's own `revision` line and this new file's `down_revision`).

- [ ] **Step 4: Commit**

```bash
git add backend/alembic/versions/t2b3c4d5e6f7_add_privilege_redaction_qc.py backend/app/models.py
git commit -m "feat(p1-4/5): privilege flags + redaction_qc_decisions table"
```

---

### Task 2: Pure privilege service

**Files:**
- Create: `backend/app/services/privilege.py`
- Test: `backend/tests/test_privilege_service.py`

**Interfaces:**
- Consumes: nothing (pure).
- Produces (later tasks import from `app.services.privilege`):
  - `DISPOSITIONS: frozenset[str]`
  - `effective_disposition(has_privilege_tag: bool, has_redactions: bool, override: str | None) -> str | None`
  - `qc_status(redaction_count: int, latest_decision: tuple[str, datetime, int] | None, latest_redaction_change_at: datetime | None) -> str`
  - `log_description(email_from, email_to, date_sent, file_type, basis: list[str], disposition: str | None, manual: str | None) -> str`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_privilege_service.py`:

```python
"""Pure tests for privilege/QC domain logic (P1-4/5). No DB/network."""

from datetime import datetime, timedelta, timezone

from app.services.privilege import (
    DISPOSITIONS,
    effective_disposition,
    log_description,
    qc_status,
)

_T0 = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


# --- effective_disposition -------------------------------------------------

def test_disposition_matrix_derived():
    assert effective_disposition(True, True, None) == "redact_in_part"
    assert effective_disposition(True, False, None) == "withhold"
    assert effective_disposition(False, True, None) == "redact_in_part"
    assert effective_disposition(False, False, None) is None


def test_disposition_override_wins():
    assert effective_disposition(True, True, "withhold") == "withhold"
    assert effective_disposition(False, False, "withhold") == "withhold"
    assert effective_disposition(True, False, "produce") == "produce"


def test_disposition_invalid_override_falls_back_to_derived():
    assert effective_disposition(True, False, "bogus") == "withhold"


def test_dispositions_constant():
    assert DISPOSITIONS == {"withhold", "redact_in_part", "produce"}


# --- qc_status -------------------------------------------------------------

def test_qc_no_redactions_not_applicable():
    assert qc_status(0, None, None) == "not_applicable"
    # even a stale decision doesn't resurrect QC on a now-unredacted doc
    assert qc_status(0, ("approved", _T0, 2), None) == "not_applicable"


def test_qc_no_decision_pending():
    assert qc_status(2, None, _T0) == "pending"


def test_qc_fresh_decision_stands():
    decided = _T0 + timedelta(hours=1)
    assert qc_status(2, ("approved", decided, 2), _T0) == "approved"
    assert qc_status(2, ("rejected", decided, 2), _T0) == "rejected"


def test_qc_edit_after_decision_invalidates():
    decided = _T0
    changed = _T0 + timedelta(minutes=5)
    assert qc_status(2, ("approved", decided, 2), changed) == "pending"


def test_qc_change_at_same_instant_invalidates():
    assert qc_status(2, ("approved", _T0, 2), _T0) == "pending"


def test_qc_delete_after_decision_invalidates_via_count():
    decided = _T0 + timedelta(hours=1)
    # counts differ (3 at decision, 2 now) though timestamps look fresh
    assert qc_status(2, ("approved", decided, 3), _T0) == "pending"


# --- log_description -------------------------------------------------------

def test_description_manual_wins_verbatim():
    out = log_description("a@x.com", "b@y.com", _T0, "eml",
                          ["Attorney-Client"], "withhold", "Hand-crafted text.")
    assert out == "Hand-crafted text."


def test_description_email_template():
    out = log_description("alice@firm.com", "bob@client.com", _T0, "eml",
                          ["Attorney-Client", "WORK PRODUCT"], "withhold", None)
    assert out == ("Email from alice@firm.com to bob@client.com dated 2026-07-22 "
                   "withheld on the basis of Attorney-Client, WORK PRODUCT.")


def test_description_redact_in_part_wording():
    out = log_description("alice@firm.com", None, _T0, "eml", ["PII"],
                          "redact_in_part", None)
    assert out == ("Email from alice@firm.com dated 2026-07-22 "
                   "produced in redacted form on the basis of PII.")


def test_description_non_email_degrades_gracefully():
    out = log_description(None, None, None, "docx", ["Attorney-Client"],
                          "withhold", None)
    assert out == "DOCX document withheld on the basis of Attorney-Client."


def test_description_no_fields_at_all():
    out = log_description(None, None, None, None, [], None, None)
    assert out == "Document."
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_privilege_service.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.privilege'`

- [ ] **Step 3: Write the implementation**

Create `backend/app/services/privilege.py`:

```python
"""Pure privilege/QC domain logic (P1-4/5). No DB/network.

Effective disposition and QC status are always computed from current state —
never stored — so they cannot go stale.
"""

from __future__ import annotations

from datetime import datetime

DISPOSITIONS = frozenset({"withhold", "redact_in_part", "produce"})

_DISPOSITION_PHRASES = {
    "withhold": "withheld",
    "redact_in_part": "produced in redacted form",
}


def effective_disposition(
    has_privilege_tag: bool, has_redactions: bool, override: str | None
) -> str | None:
    """Override wins when valid; else derived. None = ordinary produce."""
    if override in DISPOSITIONS:
        return override
    if has_redactions:
        return "redact_in_part"
    if has_privilege_tag:
        return "withhold"
    return None


def qc_status(
    redaction_count: int,
    latest_decision: tuple[str, datetime, int] | None,
    latest_redaction_change_at: datetime | None,
) -> str:
    """latest_decision = (decision, decided_at, redaction_count_at_decision).

    A decision stands only while the redactions it approved are unchanged:
    the count snapshot catches deletions, the timestamp catches adds/edits.
    """
    if redaction_count == 0:
        return "not_applicable"
    if latest_decision is None:
        return "pending"
    decision, decided_at, count_at_decision = latest_decision
    if count_at_decision != redaction_count:
        return "pending"
    if latest_redaction_change_at is not None and latest_redaction_change_at >= decided_at:
        return "pending"
    return decision


def log_description(
    email_from: str | None,
    email_to: str | None,
    date_sent: datetime | None,
    file_type: str | None,
    basis: list[str],
    disposition: str | None,
    manual: str | None,
) -> str:
    """Deterministic template from safe metadata only. Manual wins verbatim.

    NEVER include text_content, summary, or title here — the log is read by
    opposing counsel and must not reveal privileged substance.
    """
    if manual:
        return manual
    if email_from or email_to:
        kind = "Email"
    elif file_type:
        kind = f"{file_type.upper()} document"
    else:
        kind = "Document"
    parts = [kind]
    if email_from:
        parts.append(f"from {email_from}")
    if email_to:
        parts.append(f"to {email_to}")
    if date_sent:
        parts.append(f"dated {date_sent.date().isoformat()}")
    phrase = _DISPOSITION_PHRASES.get(disposition or "")
    if phrase:
        parts.append(phrase)
    if basis:
        parts.append("on the basis of " + ", ".join(basis))
    return " ".join(parts) + "."
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_privilege_service.py -q`
Expected: 14 passed, 0 warnings.

Note one intentional subtlety `test_disposition_matrix_derived` pins: redactions-only (no privilege tag) still yields `redact_in_part` — non-privilege redactions (PII etc.) must appear in the log. The implementation orders `has_redactions` before `has_privilege_tag` so privilege+redactions also lands on `redact_in_part`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/privilege.py backend/tests/test_privilege_service.py
git commit -m "feat(p1-4/5): pure privilege domain logic (disposition, qc freshness, log template)"
```

---

### Task 3: Shared fakes + tag privilege flag endpoint

**Files:**
- Create: `backend/tests/fakes.py`
- Modify: `backend/app/schemas.py:51-58` (TagOut), add `TagPrivilegeUpdate` after `TagCreate` (~line 66)
- Modify: `backend/app/routers/tags.py` (imports + new endpoint after `create_tag`)
- Test: `backend/tests/test_tag_privilege.py`

**Interfaces:**
- Consumes: `Tag.is_privilege` (Task 1).
- Produces: `TagOut.is_privilege: bool`; `PUT /api/tags/{tag_id}` body `{"is_privilege": bool}`; shared fakes module `backend/tests/fakes.py` exporting `FakeUser`, `FakeResult`, `FakeSession` (Tasks 4-6 import from it).

- [ ] **Step 1: Create the shared fakes module**

Create `backend/tests/fakes.py`:

```python
"""Shared fake-session test doubles for P1-4/5 endpoint tests. No DB.

FakeSession.execute dispatches on substrings of the compiled SQL; each test
file registers (substring, result) pairs via the `responders` list — first
match wins, so order specific substrings before general ones.
"""

from datetime import datetime, timezone

TS = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


class FakeUser:
    def __init__(self, uid="u1"):
        self.id = uid
        self.email = f"{uid}@thirulaw.com"
        self.display_name = uid


class FakeResult:
    def __init__(self, items=None, scalar=None, rows=None):
        self._items = items or []
        self._scalar = scalar
        self._rows = rows if rows is not None else []

    def scalars(self):
        return self

    def unique(self):
        return self

    def all(self):
        return self._items if self._items else self._rows

    def scalar(self):
        return self._scalar

    def scalar_one_or_none(self):
        return self._scalar


class FakeSession:
    """get() serves objects by (ModelName, key); execute() dispatches on SQL
    substrings via `responders`: list of (substring, FakeResult-or-callable)."""

    def __init__(self, get_objects=None, responders=None):
        self._get_objects = get_objects or {}
        self.responders = responders or []
        self.executed = []
        self.added = []

    async def get(self, model, key):
        return self._get_objects.get((model.__name__, key))

    async def execute(self, stmt):
        sql = str(stmt)
        self.executed.append(sql)
        for substring, result in self.responders:
            if substring in sql:
                return result(sql) if callable(result) else result
        return FakeResult()

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = 1000 + len(self.added)
        if getattr(obj, "decided_at", None) is None and hasattr(obj, "decided_at"):
            obj.decided_at = TS
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        pass

    async def refresh(self, obj):
        if getattr(obj, "decided_at", None) is None and hasattr(obj, "decided_at"):
            obj.decided_at = TS

    async def delete(self, obj):
        pass
```

- [ ] **Step 2: Write the failing tests**

Create `backend/tests/test_tag_privilege.py`:

```python
"""Fake-session tests for the tag privilege flag endpoint (P1-4/5)."""

import asyncio

import pytest
from fastapi import HTTPException

import app.routers.tags as rt
from app.schemas import TagPrivilegeUpdate
from tests.fakes import FakeSession, FakeUser


class FakeTag:
    def __init__(self, tag_id=7, production_id=1, is_privilege=False):
        self.id = tag_id
        self.name = "Attorney-Client"
        self.category = "privilege"
        self.color = "red"
        self.keyboard_shortcut = None
        self.production_id = production_id
        self.is_privilege = is_privilege


def _patch(monkeypatch, role="manager", accessible=(1,)):
    async def fake_accessible(db, user):
        return list(accessible)

    async def fake_role(db, user, production_id):
        return role

    async def fake_log(*args, **kwargs):
        pass

    monkeypatch.setattr(rt, "get_accessible_production_ids", fake_accessible)
    monkeypatch.setattr(rt, "get_user_role_for_production", fake_role)
    monkeypatch.setattr(rt, "log_action", fake_log)


def test_set_privilege_flag_as_manager(monkeypatch):
    _patch(monkeypatch, role="manager")
    tag = FakeTag()
    db = FakeSession(get_objects={("Tag", 7): tag})
    out = asyncio.run(rt.update_tag_privilege(
        tag_id=7, body=TagPrivilegeUpdate(is_privilege=True), db=db, user=FakeUser()))
    assert out.is_privilege is True
    assert tag.is_privilege is True


def test_set_privilege_flag_blocked_for_reviewer(monkeypatch):
    _patch(monkeypatch, role="reviewer")
    db = FakeSession(get_objects={("Tag", 7): FakeTag()})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rt.update_tag_privilege(
            tag_id=7, body=TagPrivilegeUpdate(is_privilege=True), db=db, user=FakeUser()))
    assert exc.value.status_code == 403


def test_set_privilege_flag_unknown_tag_404(monkeypatch):
    _patch(monkeypatch)
    db = FakeSession()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rt.update_tag_privilege(
            tag_id=99, body=TagPrivilegeUpdate(is_privilege=True), db=db, user=FakeUser()))
    assert exc.value.status_code == 404


def test_global_tag_requires_manager_somewhere(monkeypatch):
    _patch(monkeypatch, role="reviewer")
    db = FakeSession(get_objects={("Tag", 7): FakeTag(production_id=None)})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rt.update_tag_privilege(
            tag_id=7, body=TagPrivilegeUpdate(is_privilege=True), db=db, user=FakeUser()))
    assert exc.value.status_code == 403


def test_global_tag_allowed_for_manager(monkeypatch):
    _patch(monkeypatch, role="manager")
    tag = FakeTag(production_id=None)
    db = FakeSession(get_objects={("Tag", 7): tag})
    out = asyncio.run(rt.update_tag_privilege(
        tag_id=7, body=TagPrivilegeUpdate(is_privilege=True), db=db, user=FakeUser()))
    assert out.is_privilege is True
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_tag_privilege.py -q`
Expected: FAIL with `ImportError: cannot import name 'TagPrivilegeUpdate'`

- [ ] **Step 4: Implement**

(a) `backend/app/schemas.py` — in `TagOut` add after `keyboard_shortcut`:

```python
    is_privilege: bool = False
```

and after `TagCreate` add:

```python
class TagPrivilegeUpdate(BaseModel):
    is_privilege: bool
```

(b) `backend/app/routers/tags.py`:
- extend the dependencies import: `from app.dependencies import ROLE_RANK, get_accessible_production_ids, get_user_role_for_production`
- add `TagPrivilegeUpdate` to the `app.schemas` import list
- add after `create_tag`:

```python
@router.put("/tags/{tag_id}", response_model=TagOut)
async def update_tag_privilege(
    tag_id: int,
    body: TagPrivilegeUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    tag = await db.get(Tag, tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    if tag.production_id is not None:
        role = await get_user_role_for_production(db, user, tag.production_id)
        if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
            raise HTTPException(status_code=403, detail="Manager or higher role required")
    else:
        # Global tag: manager+ on at least one accessible production.
        accessible = await get_accessible_production_ids(db, user)
        for pid in accessible:
            role = await get_user_role_for_production(db, user, pid)
            if ROLE_RANK.get(role, 0) >= ROLE_RANK["manager"]:
                break
        else:
            raise HTTPException(status_code=403, detail="Manager or higher role required")
    tag.is_privilege = body.is_privilege
    await log_action(db, user, "tag_privilege_set", "tag", str(tag_id),
                     details={"is_privilege": body.is_privilege})
    await db.commit()
    await db.refresh(tag)
    return TagOut.model_validate(tag)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_tag_privilege.py -q`
Expected: 5 passed, 0 warnings. Also run `backend\venv\Scripts\python.exe -m pytest backend\tests\test_review_tags.py backend\tests\test_redacted_rendition.py -q` to prove `TagOut.is_privilege = False` default breaks nothing.

- [ ] **Step 6: Commit**

```bash
git add backend/tests/fakes.py backend/app/schemas.py backend/app/routers/tags.py backend/tests/test_tag_privilege.py
git commit -m "feat(p1-4/5): tag privilege flag endpoint + shared endpoint-test fakes"
```

---

### Task 4: Redaction QC endpoints

**Files:**
- Modify: `backend/app/schemas.py` (after `RedactionOut`/redaction schemas), `backend/app/routers/redactions.py`
- Test: `backend/tests/test_redaction_qc_endpoints.py`

**Interfaces:**
- Consumes: `RedactionQCDecision` model (Task 1), `qc_status` (Task 2), fakes (Task 3).
- Produces: `POST /api/documents/{doc_id}/redaction-qc` (body `{decision, note?}` → `RedactionQCDecisionOut`, 201); `GET /api/productions/{production_id}/redaction-qc` → `list[RedactionQCQueueItem]`. Schemas: `RedactionQCDecisionCreate`, `RedactionQCDecisionOut` (`id, document_id, decision, note, redaction_count, decided_by, decided_at`), `RedactionQCQueueItem` (`document_id, bates_begin, redaction_count, qc_status, latest_decision`).

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_redaction_qc_endpoints.py`:

```python
"""Fake-session tests for redaction QC endpoints (P1-4)."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException

import app.routers.redactions as rr
from app.schemas import RedactionQCDecisionCreate
from tests.fakes import TS, FakeResult, FakeSession, FakeUser


class FakeDoc:
    def __init__(self, doc_id, production_id=1, page_count=10):
        self.id = doc_id
        self.production_id = production_id
        self.page_count = page_count


class FakeDecision:
    def __init__(self, document_id, decision="approved", decided_at=None,
                 redaction_count=2, dec_id=5):
        self.id = dec_id
        self.document_id = document_id
        self.decision = decision
        self.note = None
        self.redaction_count = redaction_count
        self.decided_by = "u1"
        self.decided_at = decided_at or TS


def _patch(monkeypatch, role="manager", accessible=(1,)):
    async def fake_accessible(db, user):
        return list(accessible)

    async def fake_role(db, user, production_id):
        return role

    async def fake_log(*args, **kwargs):
        pass

    monkeypatch.setattr(rr, "get_accessible_production_ids", fake_accessible)
    monkeypatch.setattr(rr, "get_user_role_for_production", fake_role)
    monkeypatch.setattr(rr, "log_action", fake_log)


# --- POST /documents/{id}/redaction-qc ------------------------------------

def test_qc_decision_created_with_count_snapshot(monkeypatch):
    _patch(monkeypatch, role="manager")
    doc_id = uuid4()
    db = FakeSession(
        get_objects={("Document", doc_id): FakeDoc(doc_id)},
        responders=[("count(redactions.id", FakeResult(scalar=3))],
    )
    body = RedactionQCDecisionCreate(decision="approved", note="looks right")
    out = asyncio.run(rr.decide_redaction_qc(doc_id=doc_id, body=body, db=db, user=FakeUser()))
    assert out.decision == "approved"
    assert out.redaction_count == 3
    assert len(db.added) == 1


def test_qc_decision_422_when_no_redactions(monkeypatch):
    _patch(monkeypatch, role="manager")
    doc_id = uuid4()
    db = FakeSession(
        get_objects={("Document", doc_id): FakeDoc(doc_id)},
        responders=[("count(redactions.id", FakeResult(scalar=0))],
    )
    body = RedactionQCDecisionCreate(decision="approved")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rr.decide_redaction_qc(doc_id=doc_id, body=body, db=db, user=FakeUser()))
    assert exc.value.status_code == 422


def test_qc_decision_blocked_for_reviewer(monkeypatch):
    _patch(monkeypatch, role="reviewer")
    doc_id = uuid4()
    db = FakeSession(get_objects={("Document", doc_id): FakeDoc(doc_id)})
    body = RedactionQCDecisionCreate(decision="approved")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rr.decide_redaction_qc(doc_id=doc_id, body=body, db=db, user=FakeUser()))
    assert exc.value.status_code == 403


def test_qc_decision_rejects_invalid_decision_value():
    with pytest.raises(Exception):
        RedactionQCDecisionCreate(decision="maybe")


# --- GET /productions/{id}/redaction-qc -----------------------------------

def test_qc_queue_computes_statuses(monkeypatch):
    _patch(monkeypatch)
    d1, d2, d3 = uuid4(), uuid4(), uuid4()
    changed_late = TS + timedelta(hours=2)
    agg_rows = [
        (d1, "DOC-001", 2, TS),            # fresh approval below -> approved
        (d2, "DOC-002", 2, changed_late),  # edited after decision -> pending
        (d3, "DOC-003", 1, TS),            # no decision -> pending
    ]
    decisions = [
        FakeDecision(d1, "approved", TS + timedelta(hours=1), 2, dec_id=1),
        FakeDecision(d2, "approved", TS + timedelta(hours=1), 2, dec_id=2),
    ]
    db = FakeSession(responders=[
        ("JOIN redactions", FakeResult(rows=agg_rows)),
        ("FROM redaction_qc_decisions", FakeResult(items=decisions)),
    ])
    out = asyncio.run(rr.redaction_qc_queue(production_id=1, db=db, user=FakeUser()))
    by_bates = {i.bates_begin: i for i in out}
    assert by_bates["DOC-001"].qc_status == "approved"
    assert by_bates["DOC-002"].qc_status == "pending"
    assert by_bates["DOC-003"].qc_status == "pending"
    assert by_bates["DOC-001"].latest_decision.decision == "approved"
    assert by_bates["DOC-003"].latest_decision is None


def test_qc_queue_403_outside_accessible_productions(monkeypatch):
    _patch(monkeypatch, accessible=(2,))
    db = FakeSession()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rr.redaction_qc_queue(production_id=1, db=db, user=FakeUser()))
    assert exc.value.status_code == 403
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_redaction_qc_endpoints.py -q`
Expected: FAIL with `ImportError: cannot import name 'RedactionQCDecisionCreate'`

- [ ] **Step 3: Implement**

(a) `backend/app/schemas.py` — add near the redaction schemas (`Literal` may need adding to the `typing` import):

```python
class RedactionQCDecisionCreate(BaseModel):
    decision: Literal["approved", "rejected"]
    note: str | None = None


class RedactionQCDecisionOut(BaseModel):
    id: int
    document_id: UUID
    decision: str
    note: str | None
    redaction_count: int
    decided_by: str
    decided_at: datetime

    model_config = {"from_attributes": True}


class RedactionQCQueueItem(BaseModel):
    document_id: UUID
    bates_begin: str
    redaction_count: int
    qc_status: str
    latest_decision: RedactionQCDecisionOut | None = None
```

(b) `backend/app/routers/redactions.py`:
- imports: add `func` to the sqlalchemy import; add `RedactionQCDecision` to the models import; add the three new schemas to the schemas import; add `from app.services.privilege import qc_status`.
- append endpoints:

```python
@router.post("/documents/{doc_id}/redaction-qc", response_model=RedactionQCDecisionOut, status_code=201)
async def decide_redaction_qc(
    doc_id: UUID,
    body: RedactionQCDecisionCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    doc = await _load_accessible_doc(db, user, doc_id)
    role = await get_user_role_for_production(db, user, doc.production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or higher role required")

    count = (await db.execute(
        select(func.count(Redaction.id)).where(Redaction.document_id == doc_id)
    )).scalar() or 0
    if count == 0:
        raise HTTPException(status_code=422, detail="Document has no redactions to QC")

    dec = RedactionQCDecision(
        document_id=doc_id,
        decision=body.decision,
        note=body.note,
        redaction_count=count,
        decided_by=user.id,
    )
    db.add(dec)
    await db.flush()
    await log_action(
        db, user, "redaction_qc_decided", "redaction_qc", str(dec.id),
        production_id=doc.production_id,
        details={"document_id": str(doc_id), "decision": body.decision,
                 "redaction_count": count},
    )
    await db.commit()
    await db.refresh(dec)
    return RedactionQCDecisionOut.model_validate(dec)


@router.get("/productions/{production_id}/redaction-qc", response_model=list[RedactionQCQueueItem])
async def redaction_qc_queue(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")

    agg = await db.execute(
        select(
            Document.id,
            Document.bates_begin,
            func.count(Redaction.id).label("cnt"),
            func.max(func.coalesce(Redaction.updated_at, Redaction.created_at)).label("changed"),
        )
        .join(Redaction, Redaction.document_id == Document.id)
        .where(Document.production_id == production_id)
        .group_by(Document.id, Document.bates_begin)
        .order_by(Document.bates_begin)
    )
    rows = agg.all()

    latest: dict = {}
    doc_ids = [r[0] for r in rows]
    if doc_ids:
        dec_result = await db.execute(
            select(RedactionQCDecision)
            .where(RedactionQCDecision.document_id.in_(doc_ids))
            .order_by(RedactionQCDecision.document_id,
                      RedactionQCDecision.decided_at.desc(),
                      RedactionQCDecision.id.desc())
        )
        for d in dec_result.scalars().all():
            latest.setdefault(d.document_id, d)

    items = []
    for did, bates, cnt, changed in rows:
        d = latest.get(did)
        status = qc_status(cnt, (d.decision, d.decided_at, d.redaction_count) if d else None, changed)
        items.append(RedactionQCQueueItem(
            document_id=did,
            bates_begin=bates,
            redaction_count=cnt,
            qc_status=status,
            latest_decision=RedactionQCDecisionOut.model_validate(d) if d else None,
        ))
    return items
```

(c) `_load_accessible_doc` already exists in `redactions.py` — reuse it; do not redefine.

- [ ] **Step 4: Run tests to verify they pass**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_redaction_qc_endpoints.py backend\tests\test_redaction_endpoints.py -q`
Expected: all pass (6 new + 7 existing), 0 warnings.

- [ ] **Step 5: Commit**

```bash
git add backend/app/schemas.py backend/app/routers/redactions.py backend/tests/test_redaction_qc_endpoints.py
git commit -m "feat(p1-4/5): redaction QC decision + auto-invalidating queue endpoints"
```

---

### Task 5: Privilege overrides + log assembly + JSON endpoint

**Files:**
- Create: `backend/app/services/privilege_log.py`, `backend/app/routers/privilege.py`
- Modify: `backend/app/schemas.py` (add `PrivilegeOverrideUpdate`), `backend/app/main.py` (import + register the router)
- Test: `backend/tests/test_privilege_log.py`

**Interfaces:**
- Consumes: Tasks 1-2 models/logic, fakes (Task 3), `REASON_LABELS` from `app.services.redaction_render`.
- Produces:
  - `PUT /api/documents/{doc_id}/privilege` body `{disposition?: str|null, description?: str|null}` — field present ⇒ applied (null clears); manager+.
  - `GET /api/productions/{production_id}/privilege-log` → `list[dict]` rows.
  - `build_privilege_log_rows(db, production_id) -> list[dict]` in `app.services.privilege_log` — Task 6's CSV export imports THIS function. Row keys (exact): `document_id, bates_begin, bates_end, doc_date, custodian, author, recipients, file_type, disposition, basis, description, qc_status`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_privilege_log.py`:

```python
"""Fake-session tests for privilege overrides + privilege log (P1-5)."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException

import app.routers.privilege as rp
from app.schemas import PrivilegeOverrideUpdate
from app.services.privilege_log import build_privilege_log_rows
from tests.fakes import TS, FakeResult, FakeSession, FakeUser


class FakeDoc:
    def __init__(self, doc_id, production_id=1, **kw):
        self.id = doc_id
        self.production_id = production_id
        self.bates_begin = kw.get("bates_begin", "DOC-001")
        self.bates_end = kw.get("bates_end", "DOC-002")
        self.custodian = kw.get("custodian", "T. Owner")
        self.date_sent = kw.get("date_sent", TS)
        self.date_received = kw.get("date_received", None)
        self.email_from = kw.get("email_from", "alice@firm.com")
        self.email_to = kw.get("email_to", "bob@client.com")
        self.file_type = kw.get("file_type", "eml")
        self.privilege_disposition = kw.get("privilege_disposition", None)
        self.privilege_description = kw.get("privilege_description", None)


class FakeRedactionRow:
    def __init__(self, document_id, reason_code="pii", created_at=None, updated_at=None):
        self.document_id = document_id
        self.reason_code = reason_code
        self.created_at = created_at or TS
        self.updated_at = updated_at


class FakeDecision:
    def __init__(self, document_id, decision="approved", decided_at=None, redaction_count=1):
        self.document_id = document_id
        self.decision = decision
        self.decided_at = decided_at or (TS + timedelta(hours=1))
        self.redaction_count = redaction_count


def _log_db(tagged_rows, redactions, docs, decisions=(), override_docs=()):
    """tagged_rows: (doc_id, tag_name) tuples; docs: list[FakeDoc]."""
    return FakeSession(responders=[
        ("JOIN tags", FakeResult(rows=list(tagged_rows))),
        ("FROM redactions", FakeResult(items=list(redactions))),
        ("FROM redaction_qc_decisions", FakeResult(items=list(decisions))),
        ("privilege_disposition IS NOT NULL", FakeResult(items=list(override_docs))),
        ("FROM documents", FakeResult(items=list(docs))),
    ])


def test_log_withhold_row_for_privilege_tagged_doc():
    doc_id = uuid4()
    doc = FakeDoc(doc_id)
    db = _log_db([(doc_id, "Attorney-Client")], [], [doc])
    rows = asyncio.run(build_privilege_log_rows(db, 1))
    assert len(rows) == 1
    r = rows[0]
    assert r["disposition"] == "withhold"
    assert r["basis"] == ["Attorney-Client"]
    assert r["qc_status"] == "not_applicable"
    assert r["description"] == ("Email from alice@firm.com to bob@client.com "
                                "dated 2026-07-22 withheld on the basis of Attorney-Client.")


def test_log_redact_in_part_merges_tag_and_reason_basis():
    doc_id = uuid4()
    doc = FakeDoc(doc_id)
    db = _log_db(
        [(doc_id, "Attorney-Client")],
        [FakeRedactionRow(doc_id, "pii"), FakeRedactionRow(doc_id, "attorney_client")],
        [doc],
    )
    rows = asyncio.run(build_privilege_log_rows(db, 1))
    r = rows[0]
    assert r["disposition"] == "redact_in_part"
    assert r["basis"] == ["ATTORNEY-CLIENT", "Attorney-Client", "PII"]  # deduped, sorted
    assert r["qc_status"] == "pending"


def test_log_redactions_only_doc_included_with_reason_basis():
    doc_id = uuid4()
    doc = FakeDoc(doc_id)
    db = _log_db([], [FakeRedactionRow(doc_id, "trade_secret")], [doc])
    rows = asyncio.run(build_privilege_log_rows(db, 1))
    r = rows[0]
    assert r["disposition"] == "redact_in_part"
    assert r["basis"] == ["TRADE SECRET"]


def test_log_produce_override_excluded():
    doc_id = uuid4()
    doc = FakeDoc(doc_id, privilege_disposition="produce")
    db = _log_db([(doc_id, "Attorney-Client")], [], [doc], override_docs=[doc])
    rows = asyncio.run(build_privilege_log_rows(db, 1))
    assert rows == []


def test_log_override_doc_without_tag_or_redactions_included():
    doc_id = uuid4()
    doc = FakeDoc(doc_id, privilege_disposition="withhold")
    db = _log_db([], [], [doc], override_docs=[doc])
    rows = asyncio.run(build_privilege_log_rows(db, 1))
    assert len(rows) == 1
    assert rows[0]["disposition"] == "withhold"


def test_log_manual_description_wins():
    doc_id = uuid4()
    doc = FakeDoc(doc_id, privilege_description="Letter re legal advice.")
    db = _log_db([(doc_id, "Attorney-Client")], [], [doc])
    rows = asyncio.run(build_privilege_log_rows(db, 1))
    assert rows[0]["description"] == "Letter re legal advice."


def test_log_qc_approved_reflected():
    doc_id = uuid4()
    doc = FakeDoc(doc_id)
    db = _log_db(
        [(doc_id, "Attorney-Client")],
        [FakeRedactionRow(doc_id, "pii")],
        [doc],
        decisions=[FakeDecision(doc_id, "approved", TS + timedelta(hours=1), 1)],
    )
    rows = asyncio.run(build_privilege_log_rows(db, 1))
    assert rows[0]["qc_status"] == "approved"


# --- PUT /documents/{id}/privilege ----------------------------------------

def _patch(monkeypatch, role="manager", accessible=(1,)):
    async def fake_accessible(db, user):
        return list(accessible)

    async def fake_role(db, user, production_id):
        return role

    async def fake_log(*args, **kwargs):
        pass

    monkeypatch.setattr(rp, "get_accessible_production_ids", fake_accessible)
    monkeypatch.setattr(rp, "get_user_role_for_production", fake_role)
    monkeypatch.setattr(rp, "log_action", fake_log)


def test_override_set_and_clear(monkeypatch):
    _patch(monkeypatch)
    doc_id = uuid4()
    doc = FakeDoc(doc_id, privilege_disposition="withhold")
    db = FakeSession(get_objects={("Document", doc_id): doc})
    out = asyncio.run(rp.update_privilege(
        doc_id=doc_id,
        body=PrivilegeOverrideUpdate(disposition=None, description="X."),
        db=db, user=FakeUser()))
    assert doc.privilege_disposition is None      # explicit null cleared it
    assert doc.privilege_description == "X."
    assert out["disposition"] is None
    assert out["description"] == "X."


def test_override_omitted_field_untouched(monkeypatch):
    _patch(monkeypatch)
    doc_id = uuid4()
    doc = FakeDoc(doc_id, privilege_disposition="withhold")
    db = FakeSession(get_objects={("Document", doc_id): doc})
    asyncio.run(rp.update_privilege(
        doc_id=doc_id, body=PrivilegeOverrideUpdate(description="Y."),
        db=db, user=FakeUser()))
    assert doc.privilege_disposition == "withhold"  # not in fields_set -> untouched


def test_override_invalid_disposition_422(monkeypatch):
    _patch(monkeypatch)
    doc_id = uuid4()
    db = FakeSession(get_objects={("Document", doc_id): FakeDoc(doc_id)})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rp.update_privilege(
            doc_id=doc_id, body=PrivilegeOverrideUpdate(disposition="bogus"),
            db=db, user=FakeUser()))
    assert exc.value.status_code == 422


def test_override_blocked_for_reviewer(monkeypatch):
    _patch(monkeypatch, role="reviewer")
    doc_id = uuid4()
    db = FakeSession(get_objects={("Document", doc_id): FakeDoc(doc_id)})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rp.update_privilege(
            doc_id=doc_id, body=PrivilegeOverrideUpdate(disposition="withhold"),
            db=db, user=FakeUser()))
    assert exc.value.status_code == 403
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_privilege_log.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.routers.privilege'` (or the schemas ImportError first — either is the missing-feature failure).

- [ ] **Step 3: Implement**

(a) `backend/app/schemas.py`:

```python
class PrivilegeOverrideUpdate(BaseModel):
    disposition: str | None = None
    description: str | None = None
```

(b) Create `backend/app/services/privilege_log.py`:

```python
"""Assemble privilege-log rows. DB-aware; pure logic lives in privilege.py."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, DocumentTag, Redaction, RedactionQCDecision, Tag
from app.services.privilege import effective_disposition, log_description, qc_status
from app.services.redaction_render import REASON_LABELS


async def build_privilege_log_rows(db: AsyncSession, production_id: int) -> list[dict]:
    tagged = (await db.execute(
        select(Document.id, Tag.name)
        .join(DocumentTag, DocumentTag.document_id == Document.id)
        .join(Tag, Tag.id == DocumentTag.tag_id)
        .where(Document.production_id == production_id, Tag.is_privilege.is_(True))
    )).all()
    tag_names: dict = {}
    for did, name in tagged:
        tag_names.setdefault(did, set()).add(name)

    red_rows = (await db.execute(
        select(Redaction)
        .join(Document, Document.id == Redaction.document_id)
        .where(Document.production_id == production_id)
    )).scalars().all()
    reds: dict = {}
    for r in red_rows:
        reds.setdefault(r.document_id, []).append(r)

    override_docs = (await db.execute(
        select(Document).where(
            Document.production_id == production_id,
            Document.privilege_disposition.is_not(None),
        )
    )).scalars().all()

    candidate_ids = set(tag_names) | set(reds) | {d.id for d in override_docs}
    if not candidate_ids:
        return []

    docs = (await db.execute(
        select(Document).where(
            Document.production_id == production_id,
            Document.id.in_(candidate_ids),
        )
    )).scalars().all()

    decisions = (await db.execute(
        select(RedactionQCDecision)
        .where(RedactionQCDecision.document_id.in_(candidate_ids))
        .order_by(RedactionQCDecision.document_id,
                  RedactionQCDecision.decided_at.desc(),
                  RedactionQCDecision.id.desc())
    )).scalars().all()
    latest: dict = {}
    for d in decisions:
        latest.setdefault(d.document_id, d)

    rows = []
    for doc in sorted(docs, key=lambda d: d.bates_begin):
        doc_reds = reds.get(doc.id, [])
        disposition = effective_disposition(
            has_privilege_tag=doc.id in tag_names,
            has_redactions=bool(doc_reds),
            override=doc.privilege_disposition,
        )
        if disposition in (None, "produce"):
            continue

        basis = set(tag_names.get(doc.id, set()))
        if doc_reds:
            basis.update(REASON_LABELS.get(r.reason_code, "REDACTED") for r in doc_reds)
        basis_list = sorted(basis)

        changed = None
        if doc_reds:
            changed = max((r.updated_at or r.created_at) for r in doc_reds)
        dec = latest.get(doc.id)
        status = qc_status(
            len(doc_reds),
            (dec.decision, dec.decided_at, dec.redaction_count) if dec else None,
            changed,
        )

        doc_date = doc.date_sent or doc.date_received
        rows.append({
            "document_id": str(doc.id),
            "bates_begin": doc.bates_begin,
            "bates_end": doc.bates_end,
            "doc_date": doc_date.date().isoformat() if doc_date else None,
            "custodian": doc.custodian,
            "author": doc.email_from,
            "recipients": doc.email_to,
            "file_type": doc.file_type,
            "disposition": disposition,
            "basis": basis_list,
            "description": log_description(
                doc.email_from, doc.email_to, doc_date, doc.file_type,
                basis_list, disposition, doc.privilege_description,
            ),
            "qc_status": status,
        })
    return rows
```

(c) Create `backend/app/routers/privilege.py`:

```python
"""Privilege overrides + privilege log (P1-5)."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import ROLE_RANK, get_accessible_production_ids, get_user_role_for_production
from app.models import Document, User
from app.routers.auth import get_current_user
from app.schemas import PrivilegeOverrideUpdate
from app.services.audit import log_action
from app.services.privilege import DISPOSITIONS
from app.services.privilege_log import build_privilege_log_rows

router = APIRouter(prefix="/api", tags=["privilege"])


@router.put("/documents/{doc_id}/privilege")
async def update_privilege(
    doc_id: UUID,
    body: PrivilegeOverrideUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    doc = await db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    role = await get_user_role_for_production(db, user, doc.production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or higher role required")

    fields = body.model_fields_set
    if "disposition" in fields:
        if body.disposition is not None and body.disposition not in DISPOSITIONS:
            raise HTTPException(status_code=422, detail="invalid disposition")
        doc.privilege_disposition = body.disposition
    if "description" in fields:
        doc.privilege_description = body.description

    await log_action(
        db, user, "privilege_override_set", "document", str(doc_id),
        production_id=doc.production_id,
        details={"disposition": doc.privilege_disposition,
                 "has_description": doc.privilege_description is not None},
    )
    await db.commit()
    return {"disposition": doc.privilege_disposition,
            "description": doc.privilege_description}


@router.get("/productions/{production_id}/privilege-log")
async def get_privilege_log(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    return await build_privilege_log_rows(db, production_id)
```

(d) `backend/app/main.py`: add `privilege` to the `from app.routers import ...` list (line 7, alphabetical) and `app.include_router(privilege.router)` next to the other registrations.

- [ ] **Step 4: Run tests to verify they pass**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_privilege_log.py backend\tests\test_privilege_service.py -q`
Expected: all pass, 0 warnings.

- [ ] **Step 5: Commit**

```bash
git add backend/app/schemas.py backend/app/services/privilege_log.py backend/app/routers/privilege.py backend/app/main.py backend/tests/test_privilege_log.py
git commit -m "feat(p1-4/5): privilege overrides + privilege-log assembly and endpoint"
```

---

### Task 6: Privilege log CSV export

**Files:**
- Modify: `backend/app/routers/export.py`
- Test: `backend/tests/test_privilege_log.py` (append)

**Interfaces:**
- Consumes: `build_privilege_log_rows` (Task 5).
- Produces: `GET /api/export/privilege-log/csv?production_id=` → `text/csv` attachment `privilege_log.csv`. (Confirm the actual route prefix in `export.py` and match it — the existing endpoints there are the authority.)

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_privilege_log.py`:

```python
# --- CSV export ------------------------------------------------------------

def test_privilege_log_csv_shape(monkeypatch):
    import app.routers.export as re_

    async def fake_accessible(db, user):
        return [1]

    monkeypatch.setattr(re_, "get_accessible_production_ids", fake_accessible)

    async def fake_rows(db, production_id):
        return [{
            "document_id": "x", "bates_begin": "DOC-001", "bates_end": "DOC-002",
            "doc_date": "2026-07-22", "custodian": "T. Owner",
            "author": "alice@firm.com", "recipients": "bob@client.com",
            "file_type": "eml", "disposition": "withhold",
            "basis": ["Attorney-Client", "PII"],
            "description": "Email from alice@firm.com dated 2026-07-22 withheld.",
            "qc_status": "not_applicable",
        }]

    monkeypatch.setattr(re_, "build_privilege_log_rows", fake_rows)
    out = asyncio.run(re_.export_privilege_log_csv(production_id=1, db=FakeSession(), user=FakeUser()))
    text = out.body.decode()
    lines = text.strip().splitlines()
    assert lines[0] == ("Bates Begin,Bates End,Date,Custodian,Author,Recipients,"
                       "Doc Type,Disposition,Privilege Basis,Description,Redaction QC")
    assert "DOC-001" in lines[1]
    assert "Attorney-Client; PII" in lines[1]
    assert "privilege_log.csv" in out.headers["content-disposition"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_privilege_log.py -q -k csv`
Expected: FAIL with `AttributeError: ... no attribute 'export_privilege_log_csv'` (or `build_privilege_log_rows` not found on the module before implementation).

- [ ] **Step 3: Implement**

In `backend/app/routers/export.py` (match the file's existing imports/patterns; add `from app.services.privilege_log import build_privilege_log_rows` and reuse its existing `io`/`csv`/`Response` imports and access-check style):

```python
@router.get("/privilege-log/csv")
async def export_privilege_log_csv(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Export the production's privilege log as CSV."""
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")

    rows = await build_privilege_log_rows(db, production_id)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Bates Begin", "Bates End", "Date", "Custodian", "Author",
                     "Recipients", "Doc Type", "Disposition", "Privilege Basis",
                     "Description", "Redaction QC"])
    for r in rows:
        writer.writerow([
            r["bates_begin"], r["bates_end"], r["doc_date"] or "",
            r["custodian"] or "", r["author"] or "", r["recipients"] or "",
            r["file_type"] or "", r["disposition"], "; ".join(r["basis"]),
            r["description"], r["qc_status"],
        ])

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=privilege_log.csv"},
    )
```

If `export.py`'s router prefix means the full path differs from `/api/export/privilege-log/csv`, keep the file's convention (the route lives beside `/documents/csv` and `/search/csv`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests\test_privilege_log.py backend\tests\test_privilege_service.py backend\tests\test_tag_privilege.py backend\tests\test_redaction_qc_endpoints.py -q`
Expected: all pass, 0 warnings.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/export.py backend/tests/test_privilege_log.py
git commit -m "feat(p1-4/5): privilege log CSV export"
```

---

### Task 7: Full-suite verification + PR

**Files:** none new.

**Interfaces:** n/a — verification gate.

- [ ] **Step 1: Full suite**

Run: `backend\venv\Scripts\python.exe -m pytest backend\tests -q`
Expected: everything passes except the known pre-existing failure `test_ai_review.py::test_build_classification_prompt` (fails on origin/main too). Any other failure = regression; fix code, never old tests.

- [ ] **Step 2: Migration head + purity re-check**

Grep `backend/alembic/versions` for `down_revision` containing `4278c984ed43` — exactly one file (`t2b3c4d5e6f7...`). Confirm that file has no `app.` imports.

- [ ] **Step 3: Push and open PR**

```bash
git push -u origin feat/p1-4-5-redaction-qc-privilege-log
gh pr create --title "feat(p1-4/5): redaction QC + privilege log" --body "$(cat <<'EOF'
## Summary
- Redaction QC: append-only decisions with computed, auto-invalidating status (edit/add/delete after approval reverts to pending), per-production QC queue
- Privilege log: is_privilege tag flag, derived disposition (withhold / redact-in-part) with per-doc override, deterministic metadata-only descriptions with manual override, JSON + CSV export
- One import-safe migration (t2b3c4d5e6f7): tag flag, two nullable document columns, redaction_qc_decisions table

Spec: docs/superpowers/specs/2026-07-22-p1-4-5-redaction-qc-privilege-log-design.md

## Test plan
- [x] Pure-logic tests: disposition matrix + override, QC freshness (edit/same-instant/delete cases), description templates
- [x] Fake-session endpoint tests: role gates, 422s, count snapshot, queue status computation, log assembly (tag/redaction/override permutations), CSV shape
- [x] Full backend suite green (1 pre-existing unrelated failure)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
