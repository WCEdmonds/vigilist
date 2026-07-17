# Phase 0 · SP3 — Email Family & Threading — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Populate the empty `family_id`/`thread_id`/`is_inclusive` Document columns from load-file columns and surface families/threads via an endpoint, tag-propagation, and a document-viewer panel.

**Architecture:** Extend SP1's field-mapping + `promote_record` (adding boolean handling) so new ingests fill the three fields; an alias-only data backfill fills existing docs; a `GET /documents/{id}/family` endpoint + the stubbed `propagate_tag` family/thread branches consume them; a frontend panel displays them.

**Tech Stack:** Python/FastAPI + SQLAlchemy async + Alembic (backend), React/TypeScript/Vite (frontend), pytest (deterministic, no DB).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-17-phase0-sp3-email-family-threading-design.md`.
- **No schema migration** — `family_id`, `thread_id`, `is_inclusive` already exist on `Document`. The only migration is a DATA backfill.
- `is_inclusive` is a boolean (`normalize_bool`: `yes`/`y`/`true`/`t`/`1` → True; `no`/`n`/`false`/`f`/`0` → False; else None).
- Backfill is alias-only (no AI), idempotent, per-batch-committed (`autocommit_block` + LIMIT/OFFSET), mirroring SP1's `n6b1c3d95e02`. Its `down_revision` is the current alembic head — confirm with `alembic heads`.
- Endpoint + propagate-tag are access-scoped to `get_accessible_production_ids(db, user)`; propagate-tag family/thread scope is within the same production, excluding self, only when the id is non-null.
- Deriving family/thread/inclusive from parsed emails is OUT OF SCOPE (SP4). Only load-file columns here.
- Backend tests deterministic, no DB/network, `backend/tests/` conventions. Run backend tests from `backend/` with `python -m pytest`; frontend from `frontend/` with `npm run build`.

---

## File Structure
- `backend/app/services/field_mapping.py` — add 3 canonical fields + aliases.
- `backend/app/services/metadata_normalize.py` — `normalize_bool`, `_BOOL_TARGETS`, add targets, route bools in `promote_record`.
- `backend/tests/test_metadata_normalize.py` / `test_field_mapping.py` — tests.
- `backend/alembic/versions/<new>_backfill_family_thread.py` *(new)* — data backfill.
- `backend/app/schemas.py` — `FamilyMemberOut`, `FamilyThreadOut`.
- `backend/app/routers/intelligence.py` — `GET /documents/{id}/family`; `propagate_tag` family/thread branches.
- `frontend/src/api/client.ts`, `frontend/src/types/index.ts`, `frontend/src/components/DocumentViewer.tsx` — panel.

---

## Task 1: Mapping + boolean promotion

**Files:**
- Modify: `backend/app/services/field_mapping.py`
- Modify: `backend/app/services/metadata_normalize.py`
- Test: `backend/tests/test_metadata_normalize.py`, `backend/tests/test_field_mapping.py`

**Interfaces:**
- Produces: `normalize_bool(value: str) -> bool | None`; `promote_record` now emits `family_id`/`thread_id` (str) and `is_inclusive` (bool). `CANONICAL_FIELDS` gains `family_id`, `thread_id`, `is_inclusive`.

- [ ] **Step 1: Write failing tests**

Append to `backend/tests/test_metadata_normalize.py`:

```python
from app.services.metadata_normalize import normalize_bool


def test_normalize_bool():
    for v in ("Yes", "y", "TRUE", "t", "1"):
        assert normalize_bool(v) is True
    for v in ("No", "n", "false", "F", "0"):
        assert normalize_bool(v) is False
    for v in ("", "maybe", "  "):
        assert normalize_bool(v) is None
    assert normalize_bool(None) is None


def test_promote_record_family_thread_inclusive():
    record = {"Group Identifier": "FAM-1", "Thread ID": "TH-9", "Inclusive Email": "Yes"}
    mapping = {"family_id": "Group Identifier", "thread_id": "Thread ID", "is_inclusive": "Inclusive Email"}
    from app.services.metadata_normalize import promote_record
    typed, _ = promote_record(record, mapping)
    assert typed["family_id"] == "FAM-1"
    assert typed["thread_id"] == "TH-9"
    assert typed["is_inclusive"] is True
```

Append to `backend/tests/test_field_mapping.py`:

```python
def test_match_aliases_family_thread_inclusive():
    m = fm.match_aliases(["Group Identifier", "Conversation Index", "Inclusive Email"])
    assert m["family_id"] == "Group Identifier"
    assert m["thread_id"] == "Conversation Index"
    assert m["is_inclusive"] == "Inclusive Email"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_metadata_normalize.py tests/test_field_mapping.py -v` (from `backend/`)
Expected: FAIL — `normalize_bool` not defined / new aliases not matched.

- [ ] **Step 3: Extend the mapping**

In `backend/app/services/field_mapping.py`, add to `CANONICAL_FIELDS` (append these three):

```python
    "family_id", "thread_id", "is_inclusive",
```

and add to `ALIAS_DICT`:

```python
    "family_id": ["Group Identifier", "GroupID", "Group ID", "Family Range", "Family ID", "Family", "Parent Doc ID", "Parent ID", "Attachment Parent"],
    "thread_id": ["Thread ID", "ThreadID", "Conversation Index", "Conversation ID", "Email Thread"],
    "is_inclusive": ["Inclusive Email", "Email Inclusive", "Inclusive", "Is Inclusive"],
```

- [ ] **Step 4: Add boolean handling to normalization**

In `backend/app/services/metadata_normalize.py`:

Add `family_id`, `thread_id`, `is_inclusive` to `_METADATA_TARGETS`, and a bool-targets set:

```python
_METADATA_TARGETS = {
    "custodian", "date_sent", "date_received", "date_created", "date_modified",
    "file_hash_md5", "file_hash_sha256", "file_type", "file_name", "source_path",
    "email_from", "email_to", "email_cc", "email_bcc", "email_subject",
    "family_id", "thread_id", "is_inclusive",
}
_BOOL_TARGETS = {"is_inclusive"}
```

Add `normalize_bool` (near `normalize_date`):

```python
_TRUE = {"yes", "y", "true", "t", "1"}
_FALSE = {"no", "n", "false", "f", "0"}


def normalize_bool(value) -> bool | None:
    """Parse a load-file truthy string to a bool, or None if unrecognized."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    return None
```

In `promote_record`, add a bool branch to the routing (after the `_DATE_TARGETS` branch, before `file_type`):

```python
        if canon in _DATE_TARGETS:
            dt = normalize_date(raw)
            if dt is not None:
                typed[canon] = dt
        elif canon in _BOOL_TARGETS:
            b = normalize_bool(raw)
            if b is not None:
                typed[canon] = b
        elif canon == "file_type":
            typed[canon] = raw.lstrip(".").lower()[:50]
        else:
            typed[canon] = raw
```

- [ ] **Step 5: Run tests to verify pass**

Run: `python -m pytest tests/test_metadata_normalize.py tests/test_field_mapping.py -v`
Expected: PASS (all, including the new tests).

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/field_mapping.py backend/app/services/metadata_normalize.py backend/tests/test_metadata_normalize.py backend/tests/test_field_mapping.py
git commit -m "feat(ingest): map + promote family_id/thread_id/is_inclusive (bool)"
```

---

## Task 2: Backfill migration for existing documents

**Files:**
- Create: `backend/alembic/versions/<rev>_backfill_family_thread.py`

**Interfaces:**
- Consumes: `match_aliases`, `promote_record` (Task 1 behavior).

- [ ] **Step 1: Confirm the current head**

Run (from `backend/`): `python -m alembic heads` (or `venv/Scripts/python.exe -m alembic heads`).
Expected: one head (the SP1 backfill `n6b1c3d95e02`). Use it as `down_revision`. If different, use the actual head.

- [ ] **Step 2: Write the migration**

Create `backend/alembic/versions/p7c2d4e06f13_backfill_family_thread.py` (read SP1's `n6b1c3d95e02_backfill_document_metadata.py` first and mirror its structure — `autocommit_block`, LIMIT/OFFSET batching, `row._mapping["metadata"]`):

```python
"""backfill family_id/thread_id/is_inclusive from metadata_ (alias-only)

Revision ID: p7c2d4e06f13
Revises: n6b1c3d95e02
Create Date: 2026-07-17
"""
from alembic import op
import sqlalchemy as sa

revision = "p7c2d4e06f13"
down_revision = "n6b1c3d95e02"
branch_labels = None
depends_on = None

_BATCH_SIZE = 500


def upgrade():
    from app.services.field_mapping import match_aliases
    from app.services.metadata_normalize import promote_record

    with op.get_context().autocommit_block():
        conn = op.get_bind()
        offset = 0
        while True:
            rows = conn.execute(sa.text(
                "SELECT id, metadata FROM documents ORDER BY id LIMIT :lim OFFSET :off"
            ), {"lim": _BATCH_SIZE, "off": offset}).fetchall()
            if not rows:
                break
            for row in rows:
                meta = row._mapping["metadata"] or {}
                if not meta:
                    continue
                mapping = match_aliases(list(meta.keys()))
                # Only the three SP3 fields matter here.
                mapping = {k: v for k, v in mapping.items()
                           if k in ("family_id", "thread_id", "is_inclusive")}
                if not mapping:
                    continue
                typed, _ = promote_record(meta, mapping)
                if not typed:
                    continue
                # family_id/thread_id: fill only when currently NULL.
                # is_inclusive: NOT NULL default False — only set when the column
                #   resolved to True (never clobber with False).
                sets, params = [], {"id": row._mapping["id"]}
                if "family_id" in typed:
                    sets.append("family_id = COALESCE(family_id, :family_id)")
                    params["family_id"] = typed["family_id"]
                if "thread_id" in typed:
                    sets.append("thread_id = COALESCE(thread_id, :thread_id)")
                    params["thread_id"] = typed["thread_id"]
                if typed.get("is_inclusive") is True:
                    sets.append("is_inclusive = TRUE")
                if not sets:
                    continue
                conn.execute(sa.text(
                    f"UPDATE documents SET {', '.join(sets)} WHERE id = :id"
                ), params)
            offset += _BATCH_SIZE


def downgrade():
    # Data backfill; columns pre-exist, so downgrade is a no-op.
    pass
```

- [ ] **Step 3: Verify the migration**

Run (from `backend/`):
- `python -m py_compile alembic/versions/p7c2d4e06f13_backfill_family_thread.py`
- `python -m alembic history | head` → confirm `n6b1c3d95e02 -> p7c2d4e06f13 (head)`.
- If a local Postgres is reachable: `python -m alembic upgrade head`. Otherwise note live-Postgres verification pending (exercised separately).
- `python -c "import app.routers.ingest"` (import smoke).

- [ ] **Step 4: Commit**

```bash
git add backend/alembic/versions/p7c2d4e06f13_backfill_family_thread.py
git commit -m "feat(ingest): backfill family_id/thread_id/is_inclusive for existing docs"
```

---

## Task 3: Family/Thread endpoint + propagate-tag branches

**Files:**
- Modify: `backend/app/schemas.py`
- Modify: `backend/app/routers/intelligence.py`

**Interfaces:**
- Produces: `GET /documents/{id}/family -> FamilyThreadOut`; `propagate_tag` handles `"family"`/`"thread"`.
- Schemas: `FamilyMemberOut {document_id: UUID, bates_begin: str, title: str | None, is_inclusive: bool}`, `FamilyThreadOut {family: list[FamilyMemberOut], thread: list[FamilyMemberOut]}`.

- [ ] **Step 1: Add schemas**

In `backend/app/schemas.py`, near `DuplicateEntryOut`:

```python
class FamilyMemberOut(BaseModel):
    document_id: UUID
    bates_begin: str
    title: str | None
    is_inclusive: bool


class FamilyThreadOut(BaseModel):
    family: list[FamilyMemberOut]
    thread: list[FamilyMemberOut]
```

- [ ] **Step 2: Add the endpoint**

In `backend/app/routers/intelligence.py`, import `FamilyThreadOut, FamilyMemberOut` (add to the existing `from app.schemas import ...`), and add the endpoint (place near `get_document_duplicates`):

```python
@router.get("/documents/{doc_id}/family", response_model=FamilyThreadOut)
async def get_document_family(
    doc_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    doc = await db.get(Document, doc_id)
    if not doc or doc.production_id not in accessible:
        raise HTTPException(status_code=404, detail="Document not found")

    async def _members(id_col, id_val):
        if not id_val:
            return []
        result = await db.execute(
            select(Document.id, Document.bates_begin, Document.title, Document.is_inclusive)
            .where(id_col == id_val)
            .where(Document.production_id.in_(accessible))
            .where(Document.id != doc_id)
            .order_by(Document.bates_begin)
        )
        return [
            FamilyMemberOut(document_id=r[0], bates_begin=r[1], title=r[2], is_inclusive=r[3])
            for r in result.all()
        ]

    return FamilyThreadOut(
        family=await _members(Document.family_id, doc.family_id),
        thread=await _members(Document.thread_id, doc.thread_id),
    )
```

- [ ] **Step 3: Implement propagate_tag family/thread branches**

In `propagate_tag`, after the existing `if body.relationship_type == "duplicate":` block (which sets `related_ids`), add:

```python
    elif body.relationship_type == "family" and doc.family_id:
        members = await db.execute(
            select(Document.id)
            .where(Document.family_id == doc.family_id)
            .where(Document.production_id == doc.production_id)
            .where(Document.id != doc_id)
        )
        related_ids = [r[0] for r in members.all()]
    elif body.relationship_type == "thread" and doc.thread_id:
        members = await db.execute(
            select(Document.id)
            .where(Document.thread_id == doc.thread_id)
            .where(Document.production_id == doc.production_id)
            .where(Document.id != doc_id)
        )
        related_ids = [r[0] for r in members.all()]
```

(Leave the tag-apply + audit-log loop below it unchanged.)

- [ ] **Step 4: Verify**

Run (from `backend/`): `python -c "import app.routers.intelligence"` → no ImportError.
Run: `python -m pytest -q` → no NEW failures (pre-existing `test_ai_review.py::test_build_classification_prompt` may remain).

- [ ] **Step 5: Commit**

```bash
git add backend/app/schemas.py backend/app/routers/intelligence.py
git commit -m "feat(family): document family/thread endpoint + propagate-tag branches"
```

---

## Task 4: Frontend Family/Thread panel

**Files:**
- Modify: `frontend/src/types/index.ts`, `frontend/src/api/client.ts`, `frontend/src/components/DocumentViewer.tsx`

**Interfaces:**
- Consumes: `GET /documents/{id}/family` → `{ family: FamilyMember[]; thread: FamilyMember[] }`.

- [ ] **Step 1: Add types + client fn**

In `frontend/src/types/index.ts`, add:

```typescript
export interface FamilyMember {
  document_id: string;
  bates_begin: string;
  title: string | null;
  is_inclusive: boolean;
}

export interface FamilyThread {
  family: FamilyMember[];
  thread: FamilyMember[];
}
```

In `frontend/src/api/client.ts` (near `getDocumentDuplicates`), add `FamilyThread` to the `import type { ... }` list and:

```typescript
export function getDocumentFamily(docId: string): Promise<FamilyThread> {
  return request<FamilyThread>(`/api/documents/${docId}/family`);
}
```

- [ ] **Step 2: Fetch on doc change + render the panel**

In `frontend/src/components/DocumentViewer.tsx`:

- Add `FamilyThread` (and reuse `FamilyMember`) to the `import type { ... } from '../types'` line, and `getDocumentFamily` to the client import.
- Add state near the `duplicates` state: `const [family, setFamily] = useState<FamilyThread>({ family: [], thread: [] });`
- In the effect that resets state on `docId` change (where `setDuplicates([])` is), add `setFamily({ family: [], thread: [] });`
- Alongside the `getDocumentDuplicates(docId).then(setDuplicates)...` call, add:
  `getDocumentFamily(docId).then(setFamily).catch(e => console.warn('getDocumentFamily failed:', e));`
- After the duplicates panel block, add a Family/Thread panel. Add this helper above the component (module scope):

```tsx
const FamilyList = ({ label, items, onNavigate }: {
  label: string; items: import('../types').FamilyMember[]; onNavigate: (id: string) => void;
}) => {
  if (items.length === 0) return null;
  return (
    <div style={{ flex: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden', borderTop: '1px solid rgba(44,62,107,0.08)' }}>
      <div className="panel-header">{label} ({items.length})</div>
      <div style={{ flex: 1, overflowY: 'auto', padding: 'var(--space-2)' }}>
        {items.map(m => (
          <div key={m.document_id} onClick={() => onNavigate(m.document_id)}
            style={{ padding: 'var(--space-1-5)', cursor: 'pointer', fontSize: 'var(--text-xs)', borderBottom: '1px solid rgba(44,62,107,0.06)' }}>
            <div style={{ fontWeight: 600 }}>{m.bates_begin}</div>
            <div style={{ color: 'rgba(44,62,107,0.5)' }}>{m.title || 'No title'}</div>
            {m.is_inclusive && <span className="badge badge-gray" style={{ fontSize: 9 }}>Inclusive</span>}
          </div>
        ))}
      </div>
    </div>
  );
};
```

and render it after the duplicates panel (inside the same left-column container):

```tsx
            <FamilyList label="Family" items={family.family} onNavigate={onNavigate} />
            <FamilyList label="Thread" items={family.thread} onNavigate={onNavigate} />
```

- [ ] **Step 3: Build + lint**

Run (from `frontend/`): `npm run build` → 0 type errors.
Run: `npx eslint src/components/DocumentViewer.tsx src/api/client.ts src/types/index.ts` → no NEW errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/api/client.ts frontend/src/components/DocumentViewer.tsx
git commit -m "feat(family): Family/Thread panel in the document viewer"
```

---

## Self-Review

**Spec coverage:** mapping + bool promotion (Task 1) ✓; backfill, no schema migration (Task 2) ✓; family endpoint + propagate-tag branches (Task 3) ✓; frontend panel with inclusive badge (Task 4) ✓; SP4-derivation & full-threading out of scope — not implemented ✓.

**Placeholder scan:** No TBD/TODO; every code step has complete code.

**Type consistency:** `normalize_bool(value) -> bool | None` (Task 1) used in `promote_record` + backfill (Task 2). `FamilyMemberOut`/`FamilyThreadOut` (Task 3) mirrored by `FamilyMember`/`FamilyThread` (Task 4). Canonical literals `family_id`/`thread_id`/`is_inclusive` consistent across mapping (Task 1), backfill (Task 2), endpoint (Task 3). `is_inclusive` typed bool end-to-end.

**Note for reviewer:** Task 2 is a prod-touching data migration (verify against real Postgres before merge; `is_inclusive` is NOT NULL default False — the backfill only ever sets it TRUE, never clobbers). The endpoint/propagate-tag/panel are DB/UI-bound; the pure logic (`normalize_bool`, aliases, `promote_record`) carries the unit tests.
