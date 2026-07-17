# Phase 4 "Review Workspace" Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One ✦ Review workspace where the AI classification lane and the human queue lane feed each other — accepting an AI decision writes a real tag, queues can be cut from AI slices, classification can be kicked off (cost-gated) from the ingest wizard, and AI relevance markers appear in the document list until a human confirms them.

**Architecture:** Backend first: a `is_primary` flag makes one ReviewProject per production authoritative for list markers; `record_decision` grows a tag-write via a category→tag resolver (seeded responsiveness/issues tags, get-or-create for custom categories) with `ai_suggestion_accepted/overridden` audit actions; `get_queue_document_ids` learns an `{"ai": {...}}` filter; a cost-estimate endpoint + an auto-classify endpoint (creates a primary project from `case_context`, reuses the existing BackgroundTasks run worker). Frontend: `AIReviewPage` becomes an embedded `AIReviewLane`, `QueueManager`'s content becomes `HumanReviewLane`, both hosted by a new `ReviewWorkspace` full-screen page (URL `view=review`, legacy `view=ai` accepted); the ingest wizard's complete screen gains the pre-checked classify box; the document list gains ✦ relevance markers that vanish on acceptance.

**Tech Stack:** FastAPI + async SQLAlchemy + Alembic; existing review run worker (sequential, BackgroundTasks, pause/resume via per-doc status checks — kept as-is); React 19 + token CSS.

**Spec:** `docs/superpowers/specs/2026-07-16-ui-redesign-ambient-ai-design.md` §3-Review-workspace, §2 cost-gated classification, §3-Home relevance markers, §4 telemetry names.

## Global Constraints

- No new dependencies either side; no router lib; no frontend test framework. Backend tests `cd backend && python -m pytest tests/ -v`; the single pre-existing failure `test_ai_review.py::test_build_classification_prompt` stays untouched.
- Frontend: touched files `npx eslint <file>` → 0 errors, NO eslint-disable, no setState-in-effect prop-sync; `npm run build` green per task; no hardcoded colors in TSX; ✦ marks unconfirmed AI output and disappears once a human accepts.
- Audit actions (spec §4 names, verbatim): `ai_suggestion_accepted`, `ai_suggestion_overridden`, `ai_suggestions_bulk_accepted`, `classification_run`. `log_action` never commits — callers commit.
- Tag semantics: accepted/overridden decisions write into the SAME tag namespace humans use (seeded categories; custom review categories get-or-create a Tag). `DocumentTag` unique `(document_id, tag_id)` — duplicate applies must be skip-not-error (mirror `bulk_tag` at tags.py:169-180).
- Category→tag mapping (binding): `relevant`→Responsive (responsiveness), `not_relevant`→Not Responsive (responsiveness), `needs_review`→Needs Review (responsiveness), `key_document`→Key Document (issues); custom category → get-or-create `Tag(name=<display name>, category='custom', color=<category color or 'blue'>)`. Matching by lowercased tag name within the stated category.
- Pricing constants (binding, comment their source): Sonnet input $3/MTok, output $15/MTok; estimate per doc = `min(len(text_content), 12000)/4 + 800` input tokens (prompt overhead) + 300 output tokens.
- Migration follows the `d4b2e8f13a59` house shape; current head is `l5a2b3c94d61`.
- URL state: `view=review` is the workspace; reads accept legacy `'ai'`; `useUrlState` keys otherwise unchanged.
- Existing behavior preserved: BatchReview flow + My Review Batches strip on Home; QC review inside the human lane; pause/resume of runs; review run worker stays sequential (known slowness, noted).
- Commit after every task with the given message.

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `backend/alembic/versions/m6b3c4d05e72_add_review_project_is_primary.py` | create | `review_projects.is_primary` |
| `backend/app/models_review.py` | modify | the flag |
| `backend/app/services/review_tags.py` | create | category→tag resolver + accept/override tag writes |
| `backend/app/routers/review.py` | modify | decision→tag wiring, bulk-accept, is_primary handling, auto-classify, estimate |
| `backend/app/services/batching.py` | modify | AI-slice queue filters |
| `backend/app/routers/documents.py` + `backend/app/schemas.py` | modify | primary-project decision enrichment + `ai_decision` filter |
| `backend/tests/test_review_tags.py`, `test_ai_queue_filter.py`, `test_classify_estimate.py` | create | TDD |
| `frontend/src/types/index.ts`, `frontend/src/api/client.ts` | modify | new fields/endpoints |
| `frontend/src/components/AIReviewLane.tsx` | create (from AIReviewPage) | AI lane, embedded |
| `frontend/src/components/HumanReviewLane.tsx` | create (from QueueManager) | queues/batches lane, embedded |
| `frontend/src/components/ReviewWorkspace.tsx` | create | two-lane shell |
| `frontend/src/components/AIReviewPage.tsx`, `QueueManager.tsx` | **delete** (after extraction) | superseded |
| `frontend/src/components/IngestWizard.tsx` | modify | classify checkbox + estimate on complete screen |
| `frontend/src/App.tsx` | modify | view=review wiring, gear cleanup, list markers + filter |
| `frontend/src/styles/components.css` / `layout.css` | modify | workspace + marker styles |

---

### Task 1: Migration + `is_primary`

**Files:**
- Create: `backend/alembic/versions/m6b3c4d05e72_add_review_project_is_primary.py`
- Modify: `backend/app/models_review.py` (ReviewProject)
- Modify: `backend/app/routers/review.py` (create/update honor the flag)

**Interfaces:**
- Produces: `ReviewProject.is_primary: Boolean, nullable=False, server_default 'false'`. Rules: `POST /projects/{production_id}` body gains optional `is_primary: bool = False`; when a project is created with `is_primary=True` OR updated to it, all other projects of that production get `is_primary=False` in the same transaction (single UPDATE). The FIRST project created for a production is auto-primary (query count==0 → force True). `ReviewProjectOut` exposes the field.

- [ ] **Step 1: Migration** (house shape):

```python
"""add is_primary to review_projects

Revision ID: m6b3c4d05e72
Revises: l5a2b3c94d61
Create Date: 2026-07-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'm6b3c4d05e72'
down_revision: str = 'l5a2b3c94d61'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'review_projects',
        sa.Column('is_primary', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    )
    # Backfill: newest project per production becomes primary.
    op.execute(
        """
        UPDATE review_projects rp SET is_primary = true
        WHERE rp.id = (
            SELECT rp2.id FROM review_projects rp2
            WHERE rp2.production_id = rp.production_id
            ORDER BY rp2.created_at DESC LIMIT 1
        )
        """
    )


def downgrade() -> None:
    op.drop_column('review_projects', 'is_primary')
```

- [ ] **Step 2: Model** — `is_primary = Column(Boolean, nullable=False, server_default=text("false"))` on ReviewProject (import `text` if absent; `Boolean` likewise).

- [ ] **Step 3: Router honor** — in `create_project` (review.py:45): after computing the new project, if `body.is_primary` or the production has zero existing projects, set `project.is_primary = True` and `await db.execute(update(ReviewProject).where(ReviewProject.production_id == production_id, ReviewProject.id != project.id).values(is_primary=False))` (flush the project first so it has an id). In `update_project` (review.py:72): same clear-others block when `body.is_primary` is True. Add `is_primary: bool = False` to `ReviewProjectCreate`, `is_primary: bool | None = None` to `ReviewProjectUpdate`, `is_primary: bool` to `ReviewProjectOut` (find these Pydantic models — likely in review.py or schemas.py — and adapt names).

- [ ] **Step 4: Verify + commit**

`cd backend && python -m alembic upgrade head` → `l5a2b3c94d61 -> m6b3c4d05e72`; `python -m alembic heads` → single head; `python -m pytest tests/ -q` → baseline (64+1); `python -c "from app.main import app"` → 0.

```bash
git add backend/alembic/versions/m6b3c4d05e72_add_review_project_is_primary.py backend/app/models_review.py backend/app/routers/review.py
git commit -m "feat(api): primary review project per production"
```

---

### Task 2: Accept→tag service + decision wiring

**Files:**
- Create: `backend/app/services/review_tags.py`
- Modify: `backend/app/routers/review.py` (`record_decision`, review.py:305; add bulk-accept endpoint)
- Test: `backend/tests/test_review_tags.py` (create)

**Interfaces:**
- Produces:
  - `CATEGORY_TAG_MAP: dict[str, tuple[str, str]]` — `{"relevant": ("Responsive", "responsiveness"), "not_relevant": ("Not Responsive", "responsiveness"), "needs_review": ("Needs Review", "responsiveness"), "key_document": ("Key Document", "issues")}`
  - `decision_to_category(decision: str) -> str | None` (pure): `"agree"` → None (caller uses the AI decision); `"override_<cat>"` → `"<cat>"`; anything else → None.
  - `async resolve_tag_for_category(db, category_name: str, categories: list[dict]) -> Tag` — mapped lookup by `(name.lower(), category)` on the Tag table; else get-or-create `Tag(name=<display>, category='custom', color=<from categories list or 'blue'>)` where `<display>` = the category dict's name titled (`key_document` → `Key Document` style: `category_name.replace('_', ' ').title()`).
  - `async apply_decision_tag(db, user, result: AIReviewResult, decision: str, project: ReviewProject) -> int | None` — computes final category (`decision_to_category(decision) or result.ai_decision`), resolves the tag, inserts `DocumentTag(document_id=result.document_id, tag_id=tag.id, applied_by=user.id)` skip-if-exists, logs `ai_suggestion_accepted` (decision=="agree") or `ai_suggestion_overridden` (override) with `details={"project_id": project.id, "result_id": result.id, "tag_id": tag.id, "category": final_category}`, `production_id=project.production_id`, `resource_type="document"`, `resource_id=str(result.document_id)`. Returns tag id (None if category resolves to nothing). Does NOT commit.
  - `PUT /api/review/results/{result_id}/decide` now calls `apply_decision_tag` before its commit.
  - `POST /api/review/projects/{production_id}/{project_id}/bulk-accept` body `{"min_confidence": int}` (manager+): all results with `attorney_decision IS NULL AND confidence_score >= min_confidence AND ai_decision != 'needs_review'` get `attorney_decision='agree'` + tag write each; one audit `ai_suggestions_bulk_accepted` with `details={"count": n, "min_confidence": x}`; returns `{"accepted": n}`.

- [ ] **Step 1: Failing tests** — `backend/tests/test_review_tags.py`:

```python
"""Category->tag resolution for accepted/overridden AI decisions."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from app.services.review_tags import (
    CATEGORY_TAG_MAP,
    decision_to_category,
    resolve_tag_for_category,
)


def test_decision_to_category():
    assert decision_to_category("agree") is None
    assert decision_to_category("override_key_document") == "key_document"
    assert decision_to_category("override_custom_cat") == "custom_cat"
    assert decision_to_category("something_else") is None


def test_map_covers_default_categories():
    assert CATEGORY_TAG_MAP["relevant"] == ("Responsive", "responsiveness")
    assert CATEGORY_TAG_MAP["key_document"] == ("Key Document", "issues")


def _db_returning(tag):
    result = MagicMock()
    result.scalar_one_or_none.return_value = tag
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


def test_resolve_finds_seeded_tag():
    seeded = MagicMock()
    db = _db_returning(seeded)
    out = asyncio.run(resolve_tag_for_category(db, "relevant", []))
    assert out is seeded
    db.add.assert_not_called()


def test_resolve_creates_custom_tag_when_missing():
    db = _db_returning(None)
    db.add = MagicMock()
    db.flush = AsyncMock()
    out = asyncio.run(
        resolve_tag_for_category(db, "hot_topic", [{"name": "hot_topic", "color": "red"}])
    )
    db.add.assert_called_once()
    created = db.add.call_args[0][0]
    assert created.name == "Hot Topic"
    assert created.category == "custom"
    assert created.color == "red"
    assert out is created
```

Run: `cd backend && python -m pytest tests/test_review_tags.py -v` → ImportError (capture).

- [ ] **Step 2: Implement `backend/app/services/review_tags.py`** — complete module per the Produces contract. Structure:

```python
"""Bridge AI review decisions into the shared tag namespace.

Accepting (or overriding) an AI classification writes a real DocumentTag —
the same tags humans apply — so exports, filters, and queues don't care who
decided. Seeded categories map to the seeded responsiveness/issues tags;
custom review categories get-or-create a 'custom' tag. Nothing here commits.
"""

from sqlalchemy import select

from app.models import DocumentTag, Tag, User
from app.models_review import AIReviewResult, ReviewProject
from app.services.audit import log_action

CATEGORY_TAG_MAP: dict[str, tuple[str, str]] = {
    "relevant": ("Responsive", "responsiveness"),
    "not_relevant": ("Not Responsive", "responsiveness"),
    "needs_review": ("Needs Review", "responsiveness"),
    "key_document": ("Key Document", "issues"),
}


def decision_to_category(decision: str) -> str | None:
    if decision.startswith("override_"):
        return decision[len("override_"):] or None
    return None
```

…then `resolve_tag_for_category` (lookup: mapped name/category, or custom-name/'custom' via `func.lower(Tag.name)`; on create use `db.add` + `await db.flush()` so the id exists) and `apply_decision_tag` (skip-if-exists check mirroring tags.py bulk_tag's existing-pair select; audit action chosen by `decision == "agree"`). Write the full functions — the tests define the exact behavior for the resolver; `apply_decision_tag` follows the Produces contract verbatim.

- [ ] **Step 3: Wire `record_decision`** — in review.py:305 flow, after setting `attorney_decision`/`attorney_note` and before commit: load the project (`db.get(ReviewProject, result.project_id)`) and `await apply_decision_tag(db, user, result, body.decision, project)`.

- [ ] **Step 4: Bulk-accept endpoint** — per Produces; manager+ gate like the run endpoints; loop results sequentially calling `apply_decision_tag(..., "agree", ...)` and setting `attorney_decision="agree"`; single commit at end.

- [ ] **Step 5: Tests green + suite + commit**

`python -m pytest tests/test_review_tags.py tests/ -q` → +4, baseline otherwise; import check.

```bash
git add backend/app/services/review_tags.py backend/app/routers/review.py backend/tests/test_review_tags.py
git commit -m "feat(api): accepted AI decisions write real tags with audit trail"
```

---

### Task 3: AI-slice queue filters

**Files:**
- Modify: `backend/app/services/batching.py` (`get_queue_document_ids`, batching.py:10)
- Test: `backend/tests/test_ai_queue_filter.py` (create)

**Interfaces:**
- Produces: queue `filters` JSONB understands `{"ai": {"project_id": int, "decision": str, "min_confidence": int (default 0), "exclude_decided": bool (default true)}}`. Pure helper `build_ai_filter_conditions(ai: dict) -> dict` normalizes/validates (fills defaults, coerces ints, raises `ValueError` on missing project_id/decision). `get_queue_document_ids`: when `queue.filters.get("ai")` is present it queries `AIReviewResult` (project_id, ai_decision == decision, confidence_score >= min_confidence, and `attorney_decision.is_(None)` when exclude_decided), intersected with the production's documents, ordered by bates — and IGNORES `queue.query` in that case (AI slice wins; document the precedence in the docstring).

- [ ] **Step 1: Failing tests** — `backend/tests/test_ai_queue_filter.py`:

```python
"""Normalization of the AI-slice queue filter."""

import pytest

from app.services.batching import build_ai_filter_conditions


def test_defaults_filled():
    out = build_ai_filter_conditions({"project_id": 3, "decision": "relevant"})
    assert out == {
        "project_id": 3,
        "decision": "relevant",
        "min_confidence": 0,
        "exclude_decided": True,
    }


def test_coercions_and_overrides():
    out = build_ai_filter_conditions(
        {"project_id": "7", "decision": "key_document", "min_confidence": "80", "exclude_decided": False}
    )
    assert out["project_id"] == 7
    assert out["min_confidence"] == 80
    assert out["exclude_decided"] is False


def test_missing_required_raises():
    with pytest.raises(ValueError):
        build_ai_filter_conditions({"decision": "relevant"})
    with pytest.raises(ValueError):
        build_ai_filter_conditions({"project_id": 1})
```

Run → ImportError (capture).

- [ ] **Step 2: Implement** — the pure helper + the query branch in `get_queue_document_ids` (join `AIReviewResult.document_id == Document.id`, `Document.production_id == queue.production_id`, order by `Document.bates_begin`). Import `AIReviewResult` from `app.models_review`.

- [ ] **Step 3: Green + suite + commit**

```bash
git add backend/app/services/batching.py backend/tests/test_ai_queue_filter.py
git commit -m "feat(api): review queues can be cut from AI result slices"
```

---

### Task 4: Cost estimate + auto-classify endpoints

**Files:**
- Modify: `backend/app/routers/review.py`
- Test: `backend/tests/test_classify_estimate.py` (create)

**Interfaces:**
- Produces:
  - Pure `estimate_classification_cost(doc_count: int, avg_chars: float) -> dict` in review.py: per binding pricing constants; returns `{"doc_count": n, "est_input_tokens": int, "est_output_tokens": int, "est_usd": round(float, 2)}` where per-doc input = `min(avg_chars, 12000)/4 + 800`, output = 300; totals × doc_count; USD = `in*3/1e6 + out*15/1e6`.
  - `GET /api/review/estimate/{production_id}` (any role): one query `select(func.count(Document.id), func.avg(func.length(Document.text_content))).where(Document.production_id == pid, Document.text_content.isnot(None))` → estimate dict (avg None → avg 0 → still returns zeros).
  - `POST /api/review/auto-classify/{production_id}` (manager+): 409 if a project named `"Initial relevance pass"` already exists for the production; 400 if `production.case_context` is empty; creates `ReviewProject(name="Initial relevance pass", prompt_text=production.case_context, categories=DEFAULT_CATEGORIES, sample_size=0, status="running", is_primary=True (clear others), created_by=user.id, total_documents=<count>)`; logs `classification_run` with `details={"source": "ingest_wizard", "doc_count": n}`; commits; then `background_tasks.add_task(_run_classification_batch, project.id, production_id, False)` — match `_run_classification_batch`'s ACTUAL signature at review.py:395 (read it; adapt arg names/order; the `False`/flag distinguishes full-run vs sample if that's how it's parameterized). Returns `ReviewProjectOut`.

- [ ] **Step 1: Failing tests** — `backend/tests/test_classify_estimate.py`:

```python
"""Classification cost estimation math."""

from app.routers.review import estimate_classification_cost


def test_estimate_scales_with_count():
    out = estimate_classification_cost(1000, 8000.0)
    per_doc_in = 8000 / 4 + 800  # 2800
    assert out["doc_count"] == 1000
    assert out["est_input_tokens"] == int(per_doc_in * 1000)
    assert out["est_output_tokens"] == 300 * 1000
    assert out["est_usd"] == round((per_doc_in * 1000 * 3 + 300 * 1000 * 15) / 1_000_000, 2)


def test_estimate_caps_at_truncation_limit():
    capped = estimate_classification_cost(10, 50000.0)
    assert capped["est_input_tokens"] == int((12000 / 4 + 800) * 10)


def test_zero_docs():
    out = estimate_classification_cost(0, 0)
    assert out["est_usd"] == 0.0
```

Run → ImportError (capture).

- [ ] **Step 2: Implement** both endpoints + the pure function (pricing constants module-level with a comment `# Sonnet list pricing 2026-07: $3/M input, $15/M output`).

- [ ] **Step 3: Green + suite + import check + commit**

```bash
git add backend/app/routers/review.py backend/tests/test_classify_estimate.py
git commit -m "feat(api): classification cost estimate and ingest auto-classify"
```

---

### Task 5: Document-list relevance enrichment + filter

**Files:**
- Modify: `backend/app/routers/documents.py` (`list_documents`), `backend/app/schemas.py` (DocumentSummary)
- Test: `backend/tests/test_ai_decision_map.py` (create)

**Interfaces:**
- Produces: `DocumentSummary` gains `ai_decision: str | None = None`, `ai_confidence: int | None = None`, `ai_decided: bool = False` (True when `attorney_decision` is set). Enrichment: after the cluster batch query, find the production's primary project (`select(ReviewProject.id).where(production_id==, is_primary==True)` — cache per request); if present, one batch query over the page's doc_ids on `AIReviewResult` selecting `(document_id, ai_decision, confidence_score, attorney_decision)`; pure helper `ai_decision_map(rows) -> dict[str, dict]` mirrors `cluster_label_map`. New query param `ai_decision: str | None = None` on `list_documents`: filters via `EXISTS` subquery against the primary project's results (`ai_decision == value`, no confidence floor). Frontend client passes it as `ai_decision`.

- [ ] **Step 1: Failing test** — `backend/tests/test_ai_decision_map.py`:

```python
import uuid

from app.routers.documents import ai_decision_map


def test_maps_rows():
    d = uuid.uuid4()
    out = ai_decision_map([(d, "relevant", 92, None), ])
    assert out[str(d)] == {"ai_decision": "relevant", "ai_confidence": 92, "ai_decided": False}


def test_decided_flag():
    d = uuid.uuid4()
    out = ai_decision_map([(d, "relevant", 92, "agree")])
    assert out[str(d)]["ai_decided"] is True


def test_empty():
    assert ai_decision_map([]) == {}
```

Run → ImportError.

- [ ] **Step 2: Implement** helper + schema fields + batch query + `ai_decision` filter param (EXISTS subquery; when no primary project exists the filter matches nothing — return empty page rather than erroring).

- [ ] **Step 3: Green + suite + commit**

```bash
git add backend/app/routers/documents.py backend/app/schemas.py backend/tests/test_ai_decision_map.py
git commit -m "feat(api): primary-project AI decisions on document listing"
```

---

### Task 6: Frontend groundwork — types + client

**Files:**
- Modify: `frontend/src/types/index.ts`, `frontend/src/api/client.ts`

**Interfaces:**
- Produces (binding):

```typescript
// types: on DocumentSummary
ai_decision?: string | null;
ai_confidence?: number | null;
ai_decided?: boolean;
// ReviewProject type (find the existing one client-side or add):
is_primary?: boolean;
export interface ClassifyEstimate {
  doc_count: number;
  est_input_tokens: number;
  est_output_tokens: number;
  est_usd: number;
}
```

```typescript
// client.ts
export const getClassifyEstimate = (productionId: number): Promise<ClassifyEstimate> =>
  request(`/api/review/estimate/${productionId}`);
export const startAutoClassification = (productionId: number) =>
  request(`/api/review/auto-classify/${productionId}`, { method: 'POST' });
export const bulkAcceptResults = (productionId: number, projectId: number, minConfidence: number): Promise<{ accepted: number }> =>
  request(`/api/review/projects/${productionId}/${projectId}/bulk-accept`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ min_confidence: minConfidence }),
  });
```

Also: `listDocuments` gains a trailing optional `aiDecision?: string` param appended as `ai_decision` query param (follow its existing param-building style); `createQueue` gains an optional `filters` argument actually passed through (today it hardcodes `{}` — check client.ts:411 and thread a `filters: Record<string, unknown> = {}` param, it may already exist — adapt).

- [ ] **Step 1: Make the edits.** — [ ] **Step 2: Verify** `npx eslint src/types/index.ts src/api/client.ts` → 0 errors; build green. — [ ] **Step 3: Commit** `git add ... && git commit -m "feat(frontend): review workspace client surface"`

---

### Task 7: AIReviewLane (from AIReviewPage) + bulk accept + slice-to-queue

**Files:**
- Create: `frontend/src/components/AIReviewLane.tsx` (start from `git mv`-style copy of AIReviewPage.tsx)
- Delete: `frontend/src/components/AIReviewPage.tsx`
- Modify: `frontend/src/styles/components.css` (lane styles as needed)

**Interfaces:**
- Consumes: everything AIReviewPage consumed, plus `bulkAcceptResults`, `createQueue` (with filters).
- Produces: `<AIReviewLane productionId={number} onViewDocument={(id: string) => void} />` — NO `onBack` (the workspace owns chrome); root element `.review-lane` (not full-screen). Behavior added vs AIReviewPage:
  1. Results header gains "Bulk accept ≥ [number input, default 80]%" button (manager-gated implicitly by the endpoint; on 403 toast). Calls `bulkAcceptResults`, toasts `Accepted N suggestions`, refreshes results + status.
  2. Results header gains "→ Queue from this slice": creates a queue via `createQueue(productionId, name, desc, '', {ai: {project_id, decision: currentDecisionFilter ?? 'relevant', min_confidence: 80, exclude_decided: true}})` with a small inline form (name prefilled `AI ${decision} ≥80%`); toasts success. (Queue then appears in the human lane.)
  3. Decision buttons unchanged — backend now writes tags on decide; after a decision the row shows the ✦-free confirmed state (re-render from refreshed results: `attorney_decision` set).
- Everything else (project list, setup modal, polling, sort tabs) survives the move intact.

- [ ] **Step 1:** Copy AIReviewPage.tsx → AIReviewLane.tsx; rename component; drop the full-screen wrapper/back-button chrome (keep internal three-pane layout inside `.review-lane`); remove `onBack` prop.
- [ ] **Step 2:** Add the two header affordances per contract (complete implementations, small inline state; no new files).
- [ ] **Step 3:** `git rm frontend/src/components/AIReviewPage.tsx`; fix any importers (App.tsx still references it — leave App broken ONLY if Task 8 lands in the same session is NOT allowed; instead, keep App compiling by switching its import to `AIReviewLane` temporarily wrapped in a minimal full-screen div — Task 8 replaces that with ReviewWorkspace).
- [ ] **Step 4:** Verify eslint (0 errors, no disables) + build; commit `feat(frontend): AI review lane with bulk accept and slice-to-queue`.

---

### Task 8: HumanReviewLane + ReviewWorkspace + App wiring

**Files:**
- Create: `frontend/src/components/HumanReviewLane.tsx` (from QueueManager content), `frontend/src/components/ReviewWorkspace.tsx`
- Delete: `frontend/src/components/QueueManager.tsx`
- Modify: `frontend/src/App.tsx`, `frontend/src/components/AppHeader.tsx`, `frontend/src/hooks/useUrlState.ts` (comment only), `frontend/src/styles/layout.css`

**Interfaces:**
- Produces:
  - `<HumanReviewLane productionId={number} onOpenBatch={(batchId: number) => void} />` — QueueManager's content (queue list/create/delete, batch tables, reviewer assignment, QC) without the modal shell; root `.review-lane`. Queue cards whose `filters.ai` exists show a small `✦ AI slice` badge.
  - `<ReviewWorkspace production={ProductionInfo} onViewDocument={(id) => void} onOpenBatch={(batchId) => void} onBack={() => void} />` — full-screen page: slim header (back arrow + serif "Review" title + production name) and a two-lane body: left `.review-lane-ai` (AIReviewLane), right `.review-lane-human` (HumanReviewLane), 60/40 split, stacking vertically under 1100px. CSS: `.review-workspace`, `.review-workspace-header`, `.review-lanes { display:flex; gap: var(--space-4); }`, lanes `min-width: 0` + `overflow-y: auto`.
  - App.tsx: `showAIReview` state renamed `showReview`; seeded by `initialUrl.view === 'review' || initialUrl.view === 'ai'`; `useSyncUrl` writes `view: showReview ? 'review' : undefined`; early-return branch renders `<ReviewWorkspace production={production} onViewDocument={(id) => { setShowReview(false); setViewDocId(id); }} onOpenBatch={(id) => { setShowReview(false); setActiveBatchId(id); }} onBack={() => setShowReview(false)} />`. Gear menu: remove the interim "Review queues" item (`onOpenQueues` prop dropped from AppHeader + App) — queues live in the workspace now.

- [ ] **Step 1:** Extract HumanReviewLane from QueueManager (content only; keep QC overlay working inside the lane; `onOpenBatch` replaces any direct batch-open pathway if one exists — QueueManager today doesn't open batches, it manages them; keep management-only if so and drop `onOpenBatch` from HumanReviewLane's props IF unused — adjust ReviewWorkspace accordingly and note it).
- [ ] **Step 2:** ReviewWorkspace shell + CSS per contract.
- [ ] **Step 3:** App/AppHeader wiring per contract; `git rm frontend/src/components/QueueManager.tsx`; update useUrlState.ts's `view` comment to `'review' | 'ai' (legacy) | ...`.
- [ ] **Step 4:** Verify: `grep -rn "QueueManager\|AIReviewPage\|onOpenQueues" frontend/src` → no matches; eslint on all touched files → 0 errors; build green. Commit `feat(frontend): unified review workspace with AI and human lanes`.

---

### Task 9: Ingest wizard classify step + list relevance markers

**Files:**
- Modify: `frontend/src/components/IngestWizard.tsx` (complete stage), `frontend/src/App.tsx` (list markers + AI filter), `frontend/src/styles/components.css`

**Interfaces:**
- Consumes: `getClassifyEstimate`, `startAutoClassification` (Task 6); `DocumentSummary.ai_*` fields (Task 5→6).
- Produces:
  1. **Wizard complete stage:** on entering `'complete'`, fetch `getClassifyEstimate(productionId)`; render below the ingested-count summary: a pre-checked checkbox `✦ Classify all {doc_count} documents against your case description — est. ${est_usd}` (hidden entirely when `caseContext.trim()` is empty or estimate fetch fails); the existing "View Production" button becomes the single continue action — if the box is checked it first `await startAutoClassification(productionId)` (toast error non-blocking: classification failure must not stop the transition; toast `Classification started` on success).
  2. **List markers:** in App.tsx's list view, a new "AI" column after Theme: when `d.ai_decision && !d.ai_decided`, render `.ai-marker` — `✦ {label} {confidence}%` where label = decision with `_`→' ' (e.g. `✦ relevant 92%`), colored `var(--color-success)` for `relevant`/`key_document`, `var(--color-neutral-400)` for `not_relevant`, `var(--color-warning)` for `needs_review`; `d.ai_decided` → render nothing (the human's tag already shows in Tags). Grid view: same marker appended to `.doc-grid-meta`. Marker is text-only, NOT clickable (filtering is via the dropdown below).
  3. **AI filter:** the browse toolbar gains a select `AI: All / Relevant / Key document / Not relevant / Needs review` bound to new state `filterAiDecision: string`, passed to `listDocuments(..., aiDecision)` (thread through the existing effect deps + `loadDocuments`).
  4. CSS: `.ai-marker { font-size: var(--text-xs); white-space: nowrap; } .ai-marker .ai-marker-star { color: var(--color-brass); }` (or equivalent — the ✦ in brass, the text in the decision color).

- [ ] **Step 1:** Wizard changes. — [ ] **Step 2:** Markers + filter + CSS. — [ ] **Step 3:** Verify eslint (App.tsx 0 errors + the known warning; IngestWizard 0) + build; commit `feat(frontend): cost-gated classify at ingest and AI relevance markers in list`.

---

### Task 10: Phase verification sweep

**Files:** none planned — fixes only.

- [ ] **Step 1:** `cd backend && python -m pytest tests/ -v` → all new tests green, single known failure. `python -m alembic heads` → `m6b3c4d05e72`. `cd frontend && npm run build` → green; eslint over App.tsx, client.ts, types, AIReviewLane, HumanReviewLane, ReviewWorkspace, IngestWizard → 0 errors; `grep -rn "eslint-disable" frontend/src/components/ReviewWorkspace.tsx frontend/src/components/AIReviewLane.tsx frontend/src/components/HumanReviewLane.tsx` → none.
- [ ] **Step 2:** Live pass (requires user's Chrome; ALSO run the deferred Phase 3 checklist in the same session — it's in `.superpowers/sdd/progress.md`):
1. ✦ Review opens the workspace; both lanes render; back returns Home; `?view=ai` legacy URL still lands in the workspace; `?view=review` round-trips.
2. AI lane: project list, run status, results sort; decide → toast; the document now shows the tag in Home's Tags column and NO ✦ marker (ai_decided).
3. Bulk accept ≥80 → count toast; results refresh with attorney decisions; tags applied (spot-check a doc).
4. "Queue from this slice" → queue appears in human lane with ✦ AI slice badge; create batches from it; batch shows only matching docs.
5. Wizard (small test ingest or mocked): complete screen shows estimate + pre-checked box (absent when no case context); continue starts classification; Review workspace shows the "Initial relevance pass" project running.
6. List: ✦ markers with confidence; AI filter dropdown narrows; markers absent for decided docs.
7. Audit log shows `ai_suggestion_accepted` / `ai_suggestions_bulk_accepted` / `classification_run` rows.
- [ ] **Step 3:** `git add -A && git commit -m "fix: phase 4 verification fixes"` (if changes).

---

## Self-Review Notes

- **Spec §3-Review coverage:** two lanes ✔ (T7/T8), confidence-sorted results with reasoning/excerpts ✔ (survives the move), accept/override per doc ✔ + now writes tags (T2), bulk-accept threshold ✔ (T2/T7), queues from AI slices ("all AI-relevant ≥80% not yet human-confirmed") ✔ (T3/T7), accepted decision = same tag namespace + audit attribution ✔ (T2), status + token cost ✔ (existing, surfaced in lane). §2 cost-gated classification ✔ (T4/T9) — auto-created from case context, pre-checked estimate box, never blocks ingest. §3-Home relevance markers ✔ (T5/T9), ✦ disappears on acceptance ✔, sortable/filterable ✔ (filter; sort deliberately omitted — filter covers the workflow, sort-by-confidence lives in the AI lane; documented deviation). §4 audit names ✔.
- **Known deviations:** classification runs on the existing sequential BackgroundTasks worker (Cloud Run long-run risk pre-exists; noted, not expanded here). `sample_size=0` on auto-created projects skips the sample stage by design (wizard users chose bulk). List sort-by-AI-decision omitted (above).
- **Type consistency:** `bulkAcceptResults(productionId, projectId, minConfidence)` ↔ endpoint path/body; `{"ai": {project_id, decision, min_confidence, exclude_decided}}` shape identical in T3 helper, T7 createQueue call, and T8 badge check; `ai_decision/ai_confidence/ai_decided` names identical in T5 schema, T6 types, T9 markers; `estimate_classification_cost` fields ↔ `ClassifyEstimate`.
