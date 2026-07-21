# Phase 0 · SP2 — Hash-based Deduplication — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a byte-identical (SHA-256) duplicate tier to the existing duplicate-detection batch, independent of text, with a derived custodian rollup, surfaced in the document duplicates panel.

**Architecture:** A pure `group_by_hash` helper + a third pass inside `detect_duplicates` create `DuplicateGroup(type="hash")` rows for documents sharing a `file_hash_sha256`. The per-document duplicates endpoint returns each duplicate's custodian. The frontend relabels tiers honestly and shows custodians.

**Tech Stack:** Python/FastAPI + SQLAlchemy async (backend), React/TypeScript/Vite (frontend), pytest (deterministic, no DB).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-17-phase0-sp2-hash-dedup-design.md`.
- New duplicate group type is exactly `"hash"`; `DocumentDuplicate.similarity = 1.0` for hash members.
- The hash pass runs **before** and **independent of** the existing `len(text_docs) < 2` early-return; byte-identical image/no-text docs must still be grouped.
- Only documents with a non-null, non-empty `file_hash_sha256` participate; no-hash docs are silently skipped.
- Custodian rollup is **derived** from `Document.custodian` (SP1) — no new column/table.
- v1 is **identify only** — do NOT suppress duplicates from review or change queues/batches.
- Do NOT change the MinHash (`"exact"`) or embedding (`"similar"`) tiers beyond the UI relabel.
- Backend tests deterministic, no DB/network, following `backend/tests/` conventions.
- Run backend tests from `backend/` with `python -m pytest`; frontend checks from `frontend/` with `npm run build`.

---

## File Structure
- `backend/app/services/duplicates.py` — add `group_by_hash` + the hash pass in `detect_duplicates`.
- `backend/tests/test_duplicates.py` *(new)* — unit tests for `group_by_hash`.
- `backend/app/schemas.py` — add `custodian` to `DuplicateEntryOut`.
- `backend/app/routers/intelligence.py` — select + return `custodian` in `get_document_duplicates`.
- `frontend/src/types/index.ts` — add `custodian` to `DuplicateEntry`.
- `frontend/src/components/DocumentViewer.tsx` — relabel tiers, show custodian, sort hash-first.

---

## Task 1: Byte-identical grouping pass

**Files:**
- Modify: `backend/app/services/duplicates.py`
- Test: `backend/tests/test_duplicates.py` (new)

**Interfaces:**
- Produces: `group_by_hash(rows: list[tuple[str, str]]) -> list[list[str]]` — input `(doc_id, sha256)` rows; output groups of doc-ids of size ≥ 2, in first-seen order.
- Produces: `detect_duplicates(...)` return dict now includes `"hash_groups": int` and counts hash members in `"total_documents_grouped"`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_duplicates.py`:

```python
"""Unit tests for byte-identical hash grouping."""

from app.services.duplicates import group_by_hash


def test_group_by_hash_groups_identical():
    assert group_by_hash([("a", "H1"), ("b", "H1"), ("c", "H2")]) == [["a", "b"]]


def test_group_by_hash_excludes_singletons_and_empty():
    assert group_by_hash([("a", "H1"), ("b", "H2"), ("c", ""), ("d", None)]) == []


def test_group_by_hash_three_identical():
    assert group_by_hash([("a", "H"), ("b", "H"), ("c", "H")]) == [["a", "b", "c"]]


def test_group_by_hash_multiple_groups_preserve_order():
    groups = group_by_hash([("a", "H1"), ("b", "H1"), ("c", "H2"), ("d", "H2"), ("e", "H3")])
    assert groups == [["a", "b"], ["c", "d"]]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_duplicates.py -v` (from `backend/`)
Expected: FAIL — `ImportError: cannot import name 'group_by_hash'`.

- [ ] **Step 3: Add `group_by_hash`**

In `backend/app/services/duplicates.py`, after the imports/`logger` (before `_compute_minhash`), add:

```python
def group_by_hash(rows: list[tuple[str, str]]) -> list[list[str]]:
    """Group document ids by identical SHA-256 hash.

    ``rows`` is a list of ``(doc_id, sha256)``. Returns a list of doc-id
    groups, each of size >= 2 (a hash held by one doc is not a duplicate).
    Rows with an empty/None hash are ignored. First-seen order is preserved
    for both groups and members (deterministic).
    """
    buckets: dict[str, list[str]] = {}
    for doc_id, sha in rows:
        if not sha:
            continue
        buckets.setdefault(sha, []).append(doc_id)
    return [ids for ids in buckets.values() if len(ids) >= 2]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_duplicates.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Wire the hash pass into `detect_duplicates`**

In `detect_duplicates`, the current structure is: clear old groups → query text docs → `if len(docs) < 2: return {...}` → MinHash pass → embedding pass → store → `await db.commit()` → return.

Make these edits so the hash pass always runs and results always commit:

(a) Immediately **after** the "Clear previous results" block (right after the `delete(...)` statements), insert the hash pass:

```python
    # --- Byte-identical (SHA-256) pass — independent of text ---
    hash_result = await db.execute(
        select(Document.id, Document.file_hash_sha256)
        .where(Document.production_id == production_id)
        .where(Document.file_hash_sha256.isnot(None))
        .where(Document.file_hash_sha256 != "")
    )
    hash_rows = [(str(r[0]), r[1]) for r in hash_result.all()]
    hash_components = group_by_hash(hash_rows)
    hash_doc_count = 0
    for ids in hash_components:
        group = DuplicateGroup(production_id=production_id, type="hash")
        db.add(group)
        await db.flush()
        for doc_id in ids:
            db.add(DocumentDuplicate(document_id=doc_id, group_id=group.id, similarity=1.0))
            hash_doc_count += 1
    logger.info("Hash: found %d byte-identical groups", len(hash_components))
```

(b) Replace the early-return guard

```python
    if len(docs) < 2:
        return {"exact_groups": 0, "similar_groups": 0, "total_documents_grouped": 0}
```

with a commit + return that preserves the hash results:

```python
    if len(docs) < 2:
        await db.commit()
        return {
            "hash_groups": len(hash_components),
            "exact_groups": 0,
            "similar_groups": 0,
            "total_documents_grouped": hash_doc_count,
        }
```

(c) Update the final return (currently `{"exact_groups": ..., "similar_groups": ..., "total_documents_grouped": exact_doc_count + similar_doc_count}`) to include hash results:

```python
    return {
        "hash_groups": len(hash_components),
        "exact_groups": len(exact_components),
        "similar_groups": len(similar_components),
        "total_documents_grouped": hash_doc_count + exact_doc_count + similar_doc_count,
    }
```

Leave the MinHash and embedding passes, their `type="exact"`/`type="similar"` groups, and the final `await db.commit()` unchanged.

- [ ] **Step 6: Verify full suite (no regression) + commit**

Run: `python -m pytest -q` (from `backend/`)
Expected: `group_by_hash` tests pass; the only failure is the pre-existing unrelated `test_ai_review.py::test_build_classification_prompt` — confirm NO new failures.

```bash
git add backend/app/services/duplicates.py backend/tests/test_duplicates.py
git commit -m "feat(dedup): byte-identical SHA-256 duplicate tier in detect_duplicates"
```

---

## Task 2: Custodian on the per-document duplicates endpoint

**Files:**
- Modify: `backend/app/schemas.py` (`DuplicateEntryOut`)
- Modify: `backend/app/routers/intelligence.py` (`get_document_duplicates`)

**Interfaces:**
- Consumes: `Document.custodian` (SP1 column, `str | None`).
- Produces: `DuplicateEntryOut` gains `custodian: str | None`; `GET /documents/{id}/duplicates` returns it per entry.

- [ ] **Step 1: Add `custodian` to the schema**

In `backend/app/schemas.py`, change `DuplicateEntryOut`:

```python
class DuplicateEntryOut(BaseModel):
    document_id: UUID
    bates_begin: str
    title: str | None
    similarity: float
    type: str
    custodian: str | None = None
```

- [ ] **Step 2: Select + return custodian in `get_document_duplicates`**

In `backend/app/routers/intelligence.py`, `get_document_duplicates` builds a `members_result` query selecting `DocumentDuplicate, Document.bates_begin, Document.title, DuplicateGroup.type` and returns `DuplicateEntryOut(...)` per row. Add `Document.custodian` to that select and pass it through:

- Add `Document.custodian` to the `select(...)` column list (after `Document.title`).
- Update the unpacking loop and the `DuplicateEntryOut(...)` construction to include `custodian=custodian` from the new column.

Concretely, the members query and comprehension become:

```python
    members_result = await db.execute(
        select(DocumentDuplicate, Document.bates_begin, Document.title,
               Document.custodian, DuplicateGroup.type)
        .join(Document, DocumentDuplicate.document_id == Document.id)
        .join(DuplicateGroup, DocumentDuplicate.group_id == DuplicateGroup.id)
        .where(DocumentDuplicate.group_id.in_(group_ids))
        .where(DocumentDuplicate.document_id != doc_id)
    )

    return [
        DuplicateEntryOut(
            document_id=dd.document_id, bates_begin=bates,
            title=title, similarity=dd.similarity, type=dup_type,
            custodian=custodian,
        )
        for dd, bates, title, custodian, dup_type in members_result.all()
    ]
```

(Match the exact existing variable names/aliasing in that function; only add the `custodian` column + argument. Do not change the group-lookup logic above it.)

- [ ] **Step 3: Verify import + no regression**

Run (from `backend/`): `python -c "import app.routers.intelligence"` → no ImportError.
Run: `python -m pytest -q` → no new failures (pre-existing `test_build_classification_prompt` may remain).

- [ ] **Step 4: Commit**

```bash
git add backend/app/schemas.py backend/app/routers/intelligence.py
git commit -m "feat(dedup): include custodian in per-document duplicates response"
```

---

## Task 3: Frontend — relabel tiers, show custodian, sort hash-first

**Files:**
- Modify: `frontend/src/types/index.ts` (`DuplicateEntry`)
- Modify: `frontend/src/components/DocumentViewer.tsx` (duplicates panel, ~line 402-421)

**Interfaces:**
- Consumes: `getDocumentDuplicates(docId) -> DuplicateEntry[]` now includes `custodian`.

- [ ] **Step 1: Add `custodian` to the `DuplicateEntry` type**

In `frontend/src/types/index.ts`, the `DuplicateEntry` interface (has `document_id`, `bates_begin`, `title`, and `similarity`/`type`) — add:

```typescript
  custodian: string | null;
```

- [ ] **Step 2: Relabel tiers, show custodian, sort hash-first**

In `frontend/src/components/DocumentViewer.tsx`, in the duplicates panel:

- Replace the badge label expression (currently `{d.type === 'exact' ? 'Exact' : 'Similar'} · {Math.round(d.similarity * 100)}%`) with a three-tier label and drop the misleading percentage for byte-identical:

```tsx
                      <span className="badge badge-gray" style={{ fontSize: 9 }}>
                        {d.type === 'hash'
                          ? 'Identical file'
                          : d.type === 'exact'
                            ? `Near-identical text · ${Math.round(d.similarity * 100)}%`
                            : `Similar · ${Math.round(d.similarity * 100)}%`}
                      </span>
                      {d.custodian && (
                        <div style={{ color: 'rgba(44,62,107,0.5)', fontSize: 9 }}>
                          Custodian: {d.custodian}
                        </div>
                      )}
```

- Sort so `hash` entries come first, then `exact`, then `similar`. Where `duplicates` is mapped (`{duplicates.map(d => (`), map over a sorted copy instead:

```tsx
                  {[...duplicates].sort((a, b) => tierRank(a.type) - tierRank(b.type)).map(d => (
```

and add this helper near the top of the component module (outside the component function, or as a `const` above the return):

```tsx
const TIER_RANK: Record<string, number> = { hash: 0, exact: 1, similar: 2 };
const tierRank = (t: string): number => TIER_RANK[t] ?? 9;
```

- [ ] **Step 3: Build + lint**

Run (from `frontend/`): `npm run build` → succeeds, 0 type errors.
Run: `npx eslint src/components/DocumentViewer.tsx src/types/index.ts` → no NEW errors (pre-existing warnings elsewhere are fine).

- [ ] **Step 4: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/components/DocumentViewer.tsx
git commit -m "feat(dedup): label identical-file tier and show custodian in duplicates panel"
```

---

## Self-Review

**Spec coverage:** hash pass independent of text (Task 1a/b) ✓; `type="hash"` + similarity 1.0 (Task 1a) ✓; no-hash docs skipped (`group_by_hash` + WHERE) ✓; custodian rollup derived (Task 2) ✓; UI relabel + custodian + sort (Task 3) ✓; identify-only, MinHash/embedding tiers unchanged ✓; suppression/cross-production/MD5 out of scope — not implemented ✓.

**Placeholder scan:** No TBD/TODO/"handle edge cases"; every code step shows complete code.

**Type consistency:** `group_by_hash(list[tuple[str,str]]) -> list[list[str]]` consistent (Task 1 def + tests). `custodian: str | None` consistent across `DuplicateEntryOut` (Task 2), endpoint (Task 2), and `DuplicateEntry`/UI (Task 3). Group `type` literal `"hash"` consistent between backend (Task 1) and frontend label/sort (Task 3).

**Note for reviewer:** Task 1 restructures `detect_duplicates`' early-return so hash results commit even when there are < 2 text docs — verify the commit path and that the return dict includes `hash_groups` on both the early and final returns. The endpoint/DB changes (Task 2) are exercised by the running app, not a unit test (DB-bound); `group_by_hash` carries the unit-tested logic.
