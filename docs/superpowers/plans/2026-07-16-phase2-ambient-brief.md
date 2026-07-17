# Phase 2 "Ambient Pipeline + Production Brief" Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When ingest finishes, Vigilist automatically clusters the production, summarizes every document, and writes an AI Production Brief that reviewers see at the top of Home — with case context captured in the ingest wizard, per-stage status/retry, and a retrofit path for existing productions.

**Architecture:** A new `pipeline.py` service orchestrates three stages (clustering → summaries → brief) with per-stage status persisted in a `productions.ai_pipeline_status` JSONB column; it owns its DB sessions so it can run from a Cloud Tasks worker (prod) or a fire-and-forget asyncio task (local), triggered from `_finalize_job_if_done`. The brief's *themes are the clusters themselves* (deterministic, chip-filterable); the model generates only overview/key-players/date-range/notable-docs, stored in `productions.brief` JSONB. On the frontend, a new `ProductionBrief` component replaces the TopicGroups strip and absorbs CorpusAnalysis's donut; CorpusAnalysis and TopicGroups are deleted.

**Tech Stack:** FastAPI + async SQLAlchemy + Alembic; Anthropic AsyncAnthropic (`claude-haiku-4-5` summaries, `claude-sonnet-4-6` brief); React 19 + plain token CSS.

**Spec:** `docs/superpowers/specs/2026-07-16-ui-redesign-ambient-ai-design.md` §2, §3-Home, §1 page-inventory rows for TopicGroups/CorpusAnalysis.

## Global Constraints

- No new npm dependencies; no router library; no frontend test framework. Backend may not add pip dependencies either.
- Every touched frontend file passes `npx eslint <file>` with 0 errors, no eslint-disable comments; `npm run build` passes after every frontend task.
- No hardcoded colors in new/modified TSX; new CSS prefers `var(--…)` tokens.
- The ✦ character marks AI-generated content (never the old "AI" pill).
- Backend tests: `cd backend && python -m pytest tests/ -v`. Known pre-existing failure `test_ai_review.py::test_build_classification_prompt` must be left alone. Test style: plain pytest functions; mock the SDK by patching `app.services.<mod>._get_client` (see `test_embeddings.py`); service tests avoid the DB by testing pure functions.
- Migration follows the `d4b2e8f13a59_add_document_summary.py` shape; new head chains off `k4f9a1b73c80`.
- The pipeline must never block or fail ingest: every stage wrapped, failures recorded in status, documents searchable regardless.
- `log_action` adds to the session but does NOT commit — caller commits. Ambient (userless) pipeline runs skip audit logging; only user-triggered runs log.
- URL state keys stay `doc/q/batch/view/prod`; `view=analysis` is removed with CorpusAnalysis.
- Existing behavior preserved: cluster chip filtering via `listDocuments(..., clusterId)`, Summary tab reads `documents.summary` (now usually pre-computed).
- Commit after every task with the message given in the task.

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `backend/alembic/versions/l5a2b3c94d61_add_case_context_brief_pipeline.py` | create | 3 new `productions` columns |
| `backend/app/models.py:74-87` | modify | `case_context`, `brief`, `ai_pipeline_status` on Production |
| `backend/app/services/ai.py` | modify | `generate_summaries_batch` (mirrors titles batch) |
| `backend/app/services/brief.py` | create | prompt build/parse (pure) + `generate_brief` |
| `backend/app/services/pipeline.py` | create | stage orchestration, status writes, own sessions |
| `backend/app/services/ingest.py:348-403` | modify | trigger pipeline in `_finalize_job_if_done` |
| `backend/app/services/tasks.py` | modify | `enqueue_pipeline(production_id)` |
| `backend/app/routers/ingest.py` | modify | `case_context` on create; OIDC `POST /api/ingest/run-pipeline` |
| `backend/app/routers/productions.py` | modify | `PATCH /{id}`, `GET /{id}/pipeline`, `POST /{id}/pipeline/run`; expose `case_context`+`brief` in listing |
| `backend/app/routers/intelligence.py` | modify | `GET /api/productions/{id}/clusters/{cluster_id}/documents` |
| `backend/app/schemas.py` | modify | `ProductionUpdate`, `PipelineStatusOut`, cluster-doc row |
| `backend/tests/test_summaries_batch.py`, `test_brief.py`, `test_pipeline.py` | create | TDD for the above |
| `frontend/src/types/index.ts` | modify | `ProductionBriefData`, `PipelineStatus`, `ClusterDocument`; `case_context`/`brief` on ProductionInfo |
| `frontend/src/api/client.ts` | modify | `case_context` in create; `getPipeline`, `runPipeline`, `updateProduction`, `getClusterDocuments`; delete `nlSearch` |
| `frontend/src/components/IngestWizard.tsx` | modify | "About this case" textarea in setup stage |
| `frontend/src/components/ProductionBrief.tsx` | create | Brief card: skeleton/failed/empty/full states, chips, expansion donut + key docs |
| `frontend/src/components/ProductionSettings.tsx` | create | owner modal editing description + case context |
| `frontend/src/components/TopicGroups.tsx`, `CorpusAnalysis.tsx` | **delete** | superseded by ProductionBrief |
| `frontend/src/App.tsx` | modify | mount Brief, remove TopicGroups/CorpusAnalysis/`view=analysis`, gear gains Production settings |
| `frontend/src/styles/variables.css` | modify | 8 archival theme-hue tokens |
| `frontend/src/styles/components.css` | modify | brief card styles |

**Design deviations from spec text, decided here:** (1) "About this case" is a labeled section of the wizard's existing setup screen, not a separate step — IngestWizard has a flat `Stage` union, not a step framework, and a second screen adds friction without adding information. (2) Brief themes render from live cluster rows (deterministic ids for filtering), not from model output. (3) Ambient runs skip the audit log (no acting user); manual runs log `pipeline_run_requested` / `brief_generated`.

---

### Task 1: Migration + model columns

**Files:**
- Create: `backend/alembic/versions/l5a2b3c94d61_add_case_context_brief_pipeline.py`
- Modify: `backend/app/models.py:74-87` (Production)

**Interfaces:**
- Produces: `Production.case_context: Text|None`, `Production.brief: JSONB|None`, `Production.ai_pipeline_status: JSONB|None` — consumed by every later backend task.

- [ ] **Step 1: Write the migration**

```python
"""add case_context, brief, ai_pipeline_status to productions

Revision ID: l5a2b3c94d61
Revises: k4f9a1b73c80
Create Date: 2026-07-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = 'l5a2b3c94d61'
down_revision: str = 'k4f9a1b73c80'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('productions', sa.Column('case_context', sa.Text(), nullable=True))
    op.add_column('productions', sa.Column('brief', JSONB(), nullable=True))
    op.add_column('productions', sa.Column('ai_pipeline_status', JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column('productions', 'ai_pipeline_status')
    op.drop_column('productions', 'brief')
    op.drop_column('productions', 'case_context')
```

- [ ] **Step 2: Add the model columns**

In `backend/app/models.py`, inside `class Production` after `organization_id`:

```python
    case_context = Column(Text, nullable=True)
    brief = Column(JSONB, nullable=True)
    ai_pipeline_status = Column(JSONB, nullable=True)
```

(`Text` and `JSONB` are already imported in this file.)

- [ ] **Step 3: Run the migration locally and verify**

Run: `cd backend && python -m alembic upgrade head`
Expected: `Running upgrade k4f9a1b73c80 -> l5a2b3c94d61`. (Requires the local Postgres from `docker compose -p descubre ... up -d` to be running.)
Then: `cd backend && python -m alembic heads` → single head `l5a2b3c94d61`.

- [ ] **Step 4: Run the backend suite**

Run: `cd backend && python -m pytest tests/ -v`
Expected: 43 passed, 1 pre-existing failure (`test_build_classification_prompt`).

- [ ] **Step 5: Commit**

```bash
git add backend/alembic/versions/l5a2b3c94d61_add_case_context_brief_pipeline.py backend/app/models.py
git commit -m "feat(db): case_context, brief, ai_pipeline_status columns on productions"
```

---

### Task 2: `generate_summaries_batch` in ai.py

**Files:**
- Modify: `backend/app/services/ai.py` (add below `generate_titles_batch`, ai.py:239-252)
- Test: `backend/tests/test_summaries_batch.py` (create)

**Interfaces:**
- Consumes: existing `generate_summary(text: str) -> str | None` (ai.py:93) and `_get_client()`.
- Produces: `async def generate_summaries_batch(texts: list[tuple[str, str | None]]) -> dict[str, str | None]` — input `(doc_id, text)` pairs, returns `{doc_id: summary_or_None}`. Consumed by pipeline (Task 4).

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_summaries_batch.py`:

```python
"""generate_summaries_batch: batches per-document summaries with bounded concurrency."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.ai import generate_summaries_batch


def _mock_response(text):
    block = MagicMock()
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


def test_summaries_batch_returns_per_doc_results():
    with patch("app.services.ai._get_client") as mock_get:
        client = MagicMock()
        client.messages.create = AsyncMock(
            side_effect=[_mock_response("Summary A."), _mock_response("Summary B.")]
        )
        mock_get.return_value = client

        out = asyncio.run(
            generate_summaries_batch([("doc-1", "text one"), ("doc-2", "text two")])
        )

    assert out == {"doc-1": "Summary A.", "doc-2": "Summary B."}
    assert client.messages.create.call_count == 2


def test_summaries_batch_skips_empty_text_without_calling_model():
    with patch("app.services.ai._get_client") as mock_get:
        client = MagicMock()
        client.messages.create = AsyncMock(return_value=_mock_response("Summary A."))
        mock_get.return_value = client

        out = asyncio.run(
            generate_summaries_batch([("doc-1", None), ("doc-2", ""), ("doc-3", "real text")])
        )

    assert out["doc-1"] is None
    assert out["doc-2"] is None
    assert out["doc-3"] == "Summary A."
    assert client.messages.create.call_count == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_summaries_batch.py -v`
Expected: FAIL — `ImportError: cannot import name 'generate_summaries_batch'`.

- [ ] **Step 3: Implement**

In `backend/app/services/ai.py`, directly after `generate_titles_batch`, mirroring its shape (Semaphore + pacing sleep):

```python
async def generate_summaries_batch(texts: list[tuple[str, str | None]]) -> dict[str, str | None]:
    """Generate summaries for (doc_id, text) pairs with bounded concurrency.

    Skips empty texts without a model call. Returns {doc_id: summary or None}.
    """
    semaphore = asyncio.Semaphore(2)

    async def _gen(doc_id: str, text: str | None) -> tuple[str, str | None]:
        if not text or not text.strip():
            return doc_id, None
        async with semaphore:
            summary = await generate_summary(text)
            await asyncio.sleep(0.5)
        return doc_id, summary

    results = await asyncio.gather(*(_gen(d, t) for d, t in texts))
    return dict(results)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_summaries_batch.py -v`
Expected: 2 passed. Note: the tests exercise `generate_summary` internally, so responses flow through `SUMMARY_PROMPT`/`_extract_text` — real behavior, mocked SDK only.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/ai.py backend/tests/test_summaries_batch.py
git commit -m "feat(ai): batched per-document summary generation"
```

---

### Task 3: Brief service

**Files:**
- Create: `backend/app/services/brief.py`
- Test: `backend/tests/test_brief.py` (create)

**Interfaces:**
- Consumes: `_get_client` pattern (own lazy client, same as ai.py), `DocumentCluster`/`DocumentClusterAssignment`/`Document`/`Production` models.
- Produces:
  - `BRIEF_MODEL = "claude-sonnet-4-6"`
  - `build_brief_prompt(case_context: str | None, doc_count: int, date_hint: str | None, themes: list[dict], samples: list[dict]) -> str` (pure)
  - `parse_brief_response(raw: str) -> dict | None` (pure; tolerates ```json fences; requires `overview` key)
  - `async def generate_brief(db, production_id: int) -> dict | None` — gathers inputs, calls the model, returns the brief dict (does NOT write the DB; pipeline owns persistence).
  Brief dict shape stored in `productions.brief`:
  `{"overview": str, "key_players": [str], "date_range": str|null, "notable_documents": [{"bates": str, "reason": str}], "generated_at": iso-str, "model": str}`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_brief.py`:

```python
"""Brief prompt construction and response parsing (pure functions)."""

from app.services.brief import build_brief_prompt, parse_brief_response


def test_prompt_includes_case_context_themes_and_samples():
    prompt = build_brief_prompt(
        case_context="Product-liability suit over the March recall.",
        doc_count=4218,
        date_hint="ACME-000001 .. ACME-004218",
        themes=[{"label": "Recall timeline", "doc_count": 1204}],
        samples=[{"bates": "ACME-000412", "title": "Board minutes", "snippet": "The board voted"}],
    )
    assert "March recall" in prompt
    assert "4218" in prompt or "4,218" in prompt
    assert "Recall timeline" in prompt
    assert "ACME-000412" in prompt
    assert "JSON" in prompt


def test_prompt_handles_missing_case_context():
    prompt = build_brief_prompt(None, 10, None, [], [])
    assert "No case description was provided" in prompt


def test_parse_accepts_fenced_json():
    raw = '```json\n{"overview": "O.", "key_players": ["A"], "date_range": null, "notable_documents": []}\n```'
    brief = parse_brief_response(raw)
    assert brief is not None
    assert brief["overview"] == "O."
    assert brief["key_players"] == ["A"]


def test_parse_rejects_garbage_and_missing_overview():
    assert parse_brief_response("not json at all") is None
    assert parse_brief_response('{"key_players": []}') is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_brief.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.brief'`.

- [ ] **Step 3: Implement `backend/app/services/brief.py`**

```python
"""Production Brief generation: an AI-written orientation for a production.

Themes are NOT model output — they are the live cluster rows, passed in only
as context. The model contributes overview, key players, date range, and
notable documents. Parsing is defensive: any malformed response yields None
and the pipeline records the brief stage as failed.
"""

import json
import logging
import re
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Document, DocumentCluster, DocumentClusterAssignment, Production

logger = logging.getLogger(__name__)

BRIEF_MODEL = "claude-sonnet-4-6"
SAMPLES_PER_THEME = 2
SNIPPET_CHARS = 400


def _get_client():
    if not settings.anthropic_api_key:
        return None
    import anthropic

    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)


def build_brief_prompt(
    case_context: str | None,
    doc_count: int,
    date_hint: str | None,
    themes: list[dict],
    samples: list[dict],
) -> str:
    context_block = (
        f"Case description from counsel:\n{case_context}"
        if case_context
        else "No case description was provided."
    )
    theme_lines = "\n".join(
        f"- {t['label']} ({t['doc_count']} documents)" for t in themes
    ) or "- (no themes detected)"
    sample_lines = "\n\n".join(
        f"[{s['bates']}] {s.get('title') or 'Untitled'}\n{(s.get('snippet') or '')[:SNIPPET_CHARS]}"
        for s in samples
    ) or "(no samples available)"
    bates_line = f"Bates range: {date_hint}\n" if date_hint else ""

    return f"""You are briefing a legal team on a newly received document production.

{context_block}

Production facts:
- {doc_count} documents
{bates_line}- Detected themes:
{theme_lines}

Representative documents:
{sample_lines}

Write a JSON object with exactly these keys:
- "overview": 2-4 sentences orienting a reviewer — what this production contains and what stands out. Plain prose, no hedging boilerplate.
- "key_players": array of up to 6 people/organizations that recur (empty array if unclear).
- "date_range": human-readable date span of the documents if evident from their content, else null.
- "notable_documents": array of up to 4 objects {{"bates": "...", "reason": "..."}} drawn ONLY from the representative documents above.

Respond with ONLY the JSON object."""


def parse_brief_response(raw: str) -> dict | None:
    if not raw:
        return None
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("overview"):
        return None
    return {
        "overview": str(data["overview"]),
        "key_players": [str(p) for p in data.get("key_players") or []][:6],
        "date_range": data.get("date_range") or None,
        "notable_documents": [
            {"bates": str(d.get("bates", "")), "reason": str(d.get("reason", ""))}
            for d in (data.get("notable_documents") or [])
            if isinstance(d, dict)
        ][:4],
    }


async def generate_brief(db: AsyncSession, production_id: int) -> dict | None:
    """Build inputs from the DB, call the model once, return the brief dict."""
    client = _get_client()
    if client is None:
        logger.warning("Brief generation skipped: no Anthropic API key")
        return None

    prod = await db.get(Production, production_id)
    if prod is None:
        return None

    doc_count = (
        await db.execute(
            select(func.count(Document.id)).where(Document.production_id == production_id)
        )
    ).scalar() or 0

    bates = (
        await db.execute(
            select(func.min(Document.bates_begin), func.max(Document.bates_end)).where(
                Document.production_id == production_id
            )
        )
    ).one()
    date_hint = f"{bates[0]} .. {bates[1]}" if bates[0] else None

    clusters = (
        (
            await db.execute(
                select(DocumentCluster)
                .where(DocumentCluster.production_id == production_id)
                .order_by(DocumentCluster.doc_count.desc())
            )
        )
        .scalars()
        .all()
    )
    themes = [{"label": c.label or "Unlabeled", "doc_count": c.doc_count} for c in clusters]

    samples: list[dict] = []
    for c in clusters[:8]:
        rows = (
            await db.execute(
                select(Document.bates_begin, Document.title, Document.text_content)
                .join(
                    DocumentClusterAssignment,
                    DocumentClusterAssignment.document_id == Document.id,
                )
                .where(DocumentClusterAssignment.cluster_id == c.id)
                .order_by(Document.bates_begin)
                .limit(SAMPLES_PER_THEME)
            )
        ).all()
        samples.extend(
            {"bates": r[0], "title": r[1], "snippet": r[2] or ""} for r in rows
        )
    if not samples:
        rows = (
            await db.execute(
                select(Document.bates_begin, Document.title, Document.text_content)
                .where(Document.production_id == production_id)
                .order_by(Document.bates_begin)
                .limit(6)
            )
        ).all()
        samples = [{"bates": r[0], "title": r[1], "snippet": r[2] or ""} for r in rows]

    prompt = build_brief_prompt(prod.case_context, doc_count, date_hint, themes, samples)
    try:
        response = await client.messages.create(
            model=BRIEF_MODEL,
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text if response.content else ""
    except Exception:
        logger.exception("Brief model call failed for production %s", production_id)
        return None

    brief = parse_brief_response(raw)
    if brief is None:
        logger.warning("Brief response unparseable for production %s", production_id)
        return None
    brief["generated_at"] = datetime.now(timezone.utc).isoformat()
    brief["model"] = BRIEF_MODEL
    return brief
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_brief.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/brief.py backend/tests/test_brief.py
git commit -m "feat(ai): production brief service (prompt, parser, generator)"
```

---

### Task 4: Pipeline orchestrator

**Files:**
- Create: `backend/app/services/pipeline.py`
- Test: `backend/tests/test_pipeline.py` (create)

**Interfaces:**
- Consumes: `cluster_production(db, production_id)` (clustering.py:102), `generate_summaries_batch` (Task 2), `generate_brief` (Task 3), `async_session` factory from `app.database`.
- Produces:
  - `STAGES = ("clustering", "summaries", "brief")`
  - `stages_to_run(status: dict | None, force: bool) -> list[str]` (pure): stages not `"done"`, or all stages when `force`.
  - `merge_stage(status: dict | None, stage: str, state: str, error: str | None = None) -> dict` (pure): returns a NEW status dict with `stage` set, `updated_at` refreshed, `errors[stage]` set/cleared.
  - `async def run_ambient_pipeline(production_id: int, force: bool = False) -> None` — opens its own sessions; safe to fire-and-forget. Consumed by Task 5's trigger + endpoints.
  Status JSON shape on `productions.ai_pipeline_status`:
  `{"clustering": "pending|running|done|failed", "summaries": ..., "brief": ..., "errors": {stage: msg}, "updated_at": iso-str}`.

- [ ] **Step 1: Write the failing tests (pure functions)**

Create `backend/tests/test_pipeline.py`:

```python
"""Pipeline stage selection and status merging (pure functions)."""

from app.services.pipeline import STAGES, merge_stage, stages_to_run


def test_fresh_status_runs_all_stages():
    assert stages_to_run(None, force=False) == list(STAGES)


def test_done_stages_are_skipped_unless_forced():
    status = {"clustering": "done", "summaries": "failed", "brief": "pending"}
    assert stages_to_run(status, force=False) == ["summaries", "brief"]
    assert stages_to_run(status, force=True) == list(STAGES)


def test_merge_stage_sets_state_and_timestamp():
    out = merge_stage(None, "clustering", "running")
    assert out["clustering"] == "running"
    assert "updated_at" in out
    assert out.get("errors", {}) == {}


def test_merge_stage_records_and_clears_errors():
    failed = merge_stage({}, "brief", "failed", error="model unavailable")
    assert failed["errors"]["brief"] == "model unavailable"
    recovered = merge_stage(failed, "brief", "done")
    assert "brief" not in recovered["errors"]
    # merge is non-destructive to other stages
    assert failed is not recovered
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_pipeline.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.pipeline'`.

- [ ] **Step 3: Implement `backend/app/services/pipeline.py`**

```python
"""Ambient AI pipeline: clustering -> summaries -> brief.

Runs after ingest completes (or on demand). Owns its DB sessions so it can be
invoked from a Cloud Tasks worker, a background task, or an endpoint without
holding a request session across long model calls. Every stage is wrapped:
a failure marks that stage "failed" and the pipeline moves on — ingest and
document availability are never affected.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.database import async_session
from app.models import Document, Production
from app.services.ai import generate_summaries_batch
from app.services.brief import generate_brief
from app.services.clustering import cluster_production

logger = logging.getLogger(__name__)

STAGES = ("clustering", "summaries", "brief")
SUMMARY_BATCH_SIZE = 25


def stages_to_run(status: dict | None, force: bool) -> list[str]:
    if force or not status:
        return list(STAGES)
    return [s for s in STAGES if status.get(s) != "done"]


def merge_stage(status: dict | None, stage: str, state: str, error: str | None = None) -> dict:
    out = dict(status or {})
    errors = dict(out.get("errors") or {})
    out[stage] = state
    if error:
        errors[stage] = error
    else:
        errors.pop(stage, None)
    out["errors"] = errors
    out["updated_at"] = datetime.now(timezone.utc).isoformat()
    return out


async def _set_stage(production_id: int, stage: str, state: str, error: str | None = None) -> None:
    """Persist one stage transition in its own short transaction."""
    async with async_session() as db:
        prod = await db.get(Production, production_id)
        if prod is None:
            return
        prod.ai_pipeline_status = merge_stage(prod.ai_pipeline_status, stage, state, error)
        await db.commit()


async def _run_clustering(production_id: int) -> None:
    async with async_session() as db:
        await cluster_production(db, production_id)
        await db.commit()


async def _run_summaries(production_id: int) -> None:
    """Summarize documents that don't have a summary yet, in DB batches."""
    while True:
        async with async_session() as db:
            rows = (
                await db.execute(
                    select(Document.id, Document.text_content)
                    .where(
                        Document.production_id == production_id,
                        Document.summary.is_(None),
                        Document.text_content.isnot(None),
                    )
                    .order_by(Document.bates_begin)
                    .limit(SUMMARY_BATCH_SIZE)
                )
            ).all()
            if not rows:
                return
            results = await generate_summaries_batch(
                [(str(r[0]), r[1]) for r in rows]
            )
            wrote_any = False
            for doc_id, summary in results.items():
                if summary:
                    doc = await db.get(Document, doc_id)
                    if doc is not None:
                        doc.summary = summary
                        wrote_any = True
            await db.commit()
            if not wrote_any:
                # Model returned nothing for a whole batch (no key / all empty
                # text): stop instead of spinning on the same rows forever.
                return


async def _run_brief(production_id: int) -> None:
    async with async_session() as db:
        brief = await generate_brief(db, production_id)
        if brief is None:
            raise RuntimeError("brief generation returned no result")
        prod = await db.get(Production, production_id)
        if prod is None:
            return
        prod.brief = brief
        await db.commit()


_STAGE_RUNNERS = {
    "clustering": _run_clustering,
    "summaries": _run_summaries,
    "brief": _run_brief,
}


async def run_ambient_pipeline(production_id: int, force: bool = False) -> None:
    async with async_session() as db:
        prod = await db.get(Production, production_id)
        if prod is None:
            return
        pending = stages_to_run(prod.ai_pipeline_status, force)

    for stage in pending:
        await _set_stage(production_id, stage, "running")
        try:
            await _STAGE_RUNNERS[stage](production_id)
        except Exception as exc:  # never let one stage kill the rest
            logger.exception("Pipeline stage %s failed for production %s", stage, production_id)
            await _set_stage(production_id, stage, "failed", error=str(exc)[:300])
        else:
            await _set_stage(production_id, stage, "done")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_pipeline.py -v`
Expected: 4 passed.

- [ ] **Step 5: Run the full backend suite**

Run: `cd backend && python -m pytest tests/ -v`
Expected: prior passes + 10 new (Tasks 2-4), same single pre-existing failure.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/pipeline.py backend/tests/test_pipeline.py
git commit -m "feat(ai): ambient pipeline orchestrator with per-stage status"
```

---

### Task 5: Triggers + backend endpoints

**Files:**
- Modify: `backend/app/services/ingest.py:348-403` (`_finalize_job_if_done`)
- Modify: `backend/app/services/tasks.py` (add `enqueue_pipeline`)
- Modify: `backend/app/routers/ingest.py` (accept `case_context` in create at ingest.py:21-64; add OIDC `POST /api/ingest/run-pipeline`)
- Modify: `backend/app/routers/productions.py` (add `PATCH /{production_id}`, `GET /{production_id}/pipeline`, `POST /{production_id}/pipeline/run`; add `case_context`/`brief` to the listing)
- Modify: `backend/app/schemas.py` (`ProductionUpdate`, `PipelineStatusOut`; extend `ProductionWithAccess`)

**Interfaces:**
- Consumes: `run_ambient_pipeline(production_id, force=False)` (Task 4), `task_service.is_configured()`, `verify_cloud_tasks_request` (existing OIDC dep used by `process_batch_handler`, ingest.py:199), `get_user_role_for_production` (used by intelligence.py for role gating).
- Produces (frontend contracts for Tasks 6-9):
  - `POST /api/ingest/create` body gains optional `case_context: str`.
  - `GET /api/productions/{id}/pipeline` → `{"status": dict|null, "brief": dict|null, "case_context": str|null}`.
  - `POST /api/productions/{id}/pipeline/run` (owner or manager+) body `{"force": bool}` → `{"started": true}`; 409 if any stage currently `"running"`.
  - `PATCH /api/productions/{id}` (owner only) body `{description?, case_context?}` → updated `ProductionWithAccess`.
  - `ProductionWithAccess` gains `case_context: str | None = None` and `has_brief: bool = False` (listing stays light — full brief comes from the pipeline endpoint).

- [ ] **Step 1: Trigger in `_finalize_job_if_done`**

In `backend/app/services/ingest.py`, at the very end of `_finalize_job_if_done` (after the best-effort `embed_production_documents` block at ingest.py:399-403), add:

```python
    # Ambient AI pipeline (clustering -> summaries -> brief). Best-effort:
    # never blocks ingest completion. Prod fans out via Cloud Tasks so the
    # long-running work doesn't ride on this request; locally we detach.
    try:
        from app.services import tasks as task_service
        from app.services.pipeline import run_ambient_pipeline

        if task_service.is_configured():
            task_service.enqueue_pipeline(production_id)
        else:
            asyncio.create_task(run_ambient_pipeline(production_id))
    except Exception:
        logger.exception("Failed to start ambient pipeline for production %s", production_id)
```

(`asyncio` and `logger` already exist in this module; verify imports at the top and add if missing.)

- [ ] **Step 2: `enqueue_pipeline` in tasks.py**

Mirror `enqueue_ingest_batch` (tasks.py:27-72) — same queue path, OIDC token, and URL pattern, with the maximum dispatch deadline because the pipeline is long-running:

```python
def enqueue_pipeline(production_id: int) -> None:
    """Enqueue one ambient-pipeline run for a production."""
    from google.cloud import tasks_v2
    from google.protobuf import duration_pb2

    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(
        settings.gcp_project_id, settings.gcp_location, settings.cloud_tasks_queue
    )
    body = json.dumps({"production_id": production_id}).encode()
    task = tasks_v2.Task(
        http_request=tasks_v2.HttpRequest(
            http_method=tasks_v2.HttpMethod.POST,
            url=f"{settings.cloud_run_service_url}/api/ingest/run-pipeline",
            headers={"Content-Type": "application/json"},
            body=body,
            oidc_token=tasks_v2.OidcToken(
                service_account_email=settings.cloud_tasks_service_account,
                audience=settings.cloud_run_service_url,
            ),
        ),
        dispatch_deadline=duration_pb2.Duration(seconds=1800),
    )
    client.create_task(parent=parent, task=task)
```

Match the module's existing import/style conventions (if `enqueue_ingest_batch` imports `tasks_v2` at module level, do the same; keep `dispatch_deadline` — 30 min is the Cloud Tasks maximum).

- [ ] **Step 3: Ingest router — case_context + OIDC pipeline handler**

In `create_production_for_ingest` (ingest.py:21-64), where the `Production` row is constructed, add the field:

```python
    production = Production(
        name=name,
        description=body.get("description") or None,
        case_context=(body.get("case_context") or "").strip() or None,
        owner_id=user.id,
        organization_id=org_id,
    )
```

(Adapt to the actual constructor call in that function — add only the `case_context` line.)

Add the worker endpoint next to `process_batch_handler` (ingest.py:199-226):

```python
@router.post("/ingest/run-pipeline")
async def run_pipeline_handler(
    body: dict,
    _verified: None = Depends(verify_cloud_tasks_request),
):
    """Cloud Tasks worker: run the ambient AI pipeline for one production."""
    production_id = body.get("production_id")
    if not production_id:
        raise HTTPException(status_code=400, detail="production_id required")
    from app.services.pipeline import run_ambient_pipeline

    await run_ambient_pipeline(int(production_id))
    return {"ok": True}
```

- [ ] **Step 4: Productions router — pipeline read/run + PATCH**

In `backend/app/schemas.py`:

```python
class ProductionUpdate(BaseModel):
    description: str | None = None
    case_context: str | None = None


class PipelineStatusOut(BaseModel):
    status: dict | None = None
    brief: dict | None = None
    case_context: str | None = None
```

and extend `ProductionWithAccess` (schemas.py:218) with:

```python
    case_context: str | None = None
    has_brief: bool = False
```

In `backend/app/routers/productions.py` — extend the `list_productions` comprehension with `case_context=p.case_context, has_brief=bool(p.brief)`, and add three endpoints after `delete_production`:

```python
@router.get("/{production_id}/pipeline", response_model=PipelineStatusOut)
async def get_pipeline(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    role = await get_user_role_for_production(db, user, production_id)
    if role is None:
        raise HTTPException(status_code=404, detail="Production not found")
    prod = await db.get(Production, production_id)
    if prod is None:
        raise HTTPException(status_code=404, detail="Production not found")
    return PipelineStatusOut(
        status=prod.ai_pipeline_status, brief=prod.brief, case_context=prod.case_context
    )


@router.post("/{production_id}/pipeline/run")
async def run_pipeline(
    production_id: int,
    body: dict | None = None,
    background_tasks: BackgroundTasks = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    role = await get_user_role_for_production(db, user, production_id)
    if role not in ("owner", "manager"):
        raise HTTPException(status_code=403, detail="Manager role required")
    prod = await db.get(Production, production_id)
    if prod is None:
        raise HTTPException(status_code=404, detail="Production not found")
    status = prod.ai_pipeline_status or {}
    if any(status.get(s) == "running" for s in ("clustering", "summaries", "brief")):
        raise HTTPException(status_code=409, detail="Pipeline already running")
    force = bool((body or {}).get("force"))
    await log_action(
        db, user, "pipeline_run_requested", "production", str(production_id),
        production_id=production_id, details={"force": force},
    )
    await db.commit()

    from app.services import tasks as task_service
    from app.services.pipeline import run_ambient_pipeline

    if task_service.is_configured():
        task_service.enqueue_pipeline(production_id)
    else:
        background_tasks.add_task(run_ambient_pipeline, production_id, force)
    return {"started": True}


@router.patch("/{production_id}", response_model=ProductionWithAccess)
async def update_production(
    production_id: int,
    body: ProductionUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    prod = await db.get(Production, production_id)
    if prod is None:
        raise HTTPException(status_code=404, detail="Production not found")
    if prod.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Owner only")
    if body.description is not None:
        prod.description = body.description.strip() or None
    if body.case_context is not None:
        prod.case_context = body.case_context.strip() or None
    await db.commit()
    await db.refresh(prod)
    doc_count = (
        await db.execute(
            select(func.count(Document.id)).where(Document.production_id == production_id)
        )
    ).scalar() or 0
    return ProductionWithAccess(
        id=prod.id, name=prod.name, description=prod.description,
        owner_id=prod.owner_id, is_owner=True, created_at=prod.created_at,
        document_count=doc_count, case_context=prod.case_context,
        has_brief=bool(prod.brief),
    )
```

Add the needed imports to productions.py: `BackgroundTasks` (fastapi), `func` (sqlalchemy — already imported since Phase 1), `Document` (already imported since Phase 1), `get_user_role_for_production` (already imported at productions.py:8), `ProductionUpdate, PipelineStatusOut` from schemas. **Route-order caution:** register `GET /{production_id}/pipeline` and `POST /{production_id}/pipeline/run` BEFORE any existing `/{production_id}/...` catch-alls if FastAPI matching requires it (it matches by path template, so order is not critical here, but keep the new routes grouped after `delete_production` for readability).

- [ ] **Step 5: Run the backend suite + smoke the endpoints**

Run: `cd backend && python -m pytest tests/ -v` → same pass set as Task 4.
Smoke (backend running locally): `curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/api/productions/1/pipeline` → `401` (auth required proves the route exists; anything but 404-with-FastAPI's-"Not Found"-body).

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/ingest.py backend/app/services/tasks.py backend/app/routers/ingest.py backend/app/routers/productions.py backend/app/schemas.py
git commit -m "feat(api): ambient pipeline trigger, status/run endpoints, case_context intake"
```

---

### Task 6: Cluster key-documents endpoint

**Files:**
- Modify: `backend/app/routers/intelligence.py` (after `list_clusters`, intelligence.py:58-86)
- Modify: `backend/app/schemas.py` (add `ClusterDocumentOut`)
- Test: `backend/tests/test_cluster_documents.py` (create)

**Interfaces:**
- Produces: `GET /api/productions/{production_id}/clusters/{cluster_id}/documents?limit=5` → `list[ClusterDocumentOut]` where `ClusterDocumentOut = {document_id: str, bates_begin: str, title: str | None}`. Consumed by the Brief expansion (Task 8). Ordered by `bates_begin`, `limit` clamped to 1..20.

- [ ] **Step 1: Write the failing test (clamp logic, pure)**

The endpoint is thin; the only logic worth a unit test is the clamp. Put it in a helper so it's testable without a DB. Create `backend/tests/test_cluster_documents.py`:

```python
from app.routers.intelligence import clamp_limit


def test_clamp_limit_bounds():
    assert clamp_limit(5) == 5
    assert clamp_limit(0) == 1
    assert clamp_limit(-3) == 1
    assert clamp_limit(999) == 20
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_cluster_documents.py -v`
Expected: FAIL — `ImportError: cannot import name 'clamp_limit'`.

- [ ] **Step 3: Implement**

In `backend/app/schemas.py`:

```python
class ClusterDocumentOut(BaseModel):
    document_id: str
    bates_begin: str
    title: str | None = None
```

In `backend/app/routers/intelligence.py` (imports: add `ClusterDocumentOut`; `Document`, `DocumentClusterAssignment`, `DocumentCluster` — reuse existing imports where present):

```python
def clamp_limit(limit: int) -> int:
    return max(1, min(20, limit))


@router.get(
    "/productions/{production_id}/clusters/{cluster_id}/documents",
    response_model=list[ClusterDocumentOut],
)
async def list_cluster_documents(
    production_id: int,
    cluster_id: int,
    limit: int = 5,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    role = await get_user_role_for_production(db, user, production_id)
    if role is None:
        raise HTTPException(status_code=404, detail="Production not found")
    cluster = await db.get(DocumentCluster, cluster_id)
    if cluster is None or cluster.production_id != production_id:
        raise HTTPException(status_code=404, detail="Cluster not found")
    rows = (
        await db.execute(
            select(Document.id, Document.bates_begin, Document.title)
            .join(
                DocumentClusterAssignment,
                DocumentClusterAssignment.document_id == Document.id,
            )
            .where(DocumentClusterAssignment.cluster_id == cluster_id)
            .order_by(Document.bates_begin)
            .limit(clamp_limit(limit))
        )
    ).all()
    return [
        ClusterDocumentOut(document_id=str(r[0]), bates_begin=r[1], title=r[2])
        for r in rows
    ]
```

Match the auth/role-gating imports already used in this router (`get_current_user`, `get_user_role_for_production` are present for the cluster endpoints).

- [ ] **Step 4: Run tests**

Run: `cd backend && python -m pytest tests/test_cluster_documents.py tests/ -v`
Expected: new test passes; suite otherwise unchanged.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/intelligence.py backend/app/schemas.py backend/tests/test_cluster_documents.py
git commit -m "feat(api): per-cluster key documents endpoint"
```

---

### Task 7: Frontend groundwork — types, client, theme tokens

**Files:**
- Modify: `frontend/src/types/index.ts`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/styles/variables.css`

**Interfaces:**
- Produces (consumed by Tasks 8-9):

```typescript
// types/index.ts
export interface ProductionBriefData {
  overview: string;
  key_players: string[];
  date_range: string | null;
  notable_documents: { bates: string; reason: string }[];
  generated_at: string;
  model: string;
}
export type PipelineStageState = 'pending' | 'running' | 'done' | 'failed';
export interface PipelineStatus {
  clustering?: PipelineStageState;
  summaries?: PipelineStageState;
  brief?: PipelineStageState;
  errors?: Record<string, string>;
  updated_at?: string;
}
export interface PipelineInfo {
  status: PipelineStatus | null;
  brief: ProductionBriefData | null;
  case_context: string | null;
}
export interface ClusterDocument {
  document_id: string;
  bates_begin: string;
  title: string | null;
}
```

Also add to `ProductionInfo`: `case_context?: string | null; has_brief?: boolean;` (optional — older cached responses lack them).

```typescript
// api/client.ts — new functions
export const getPipeline = (productionId: number): Promise<PipelineInfo> =>
  request(`/api/productions/${productionId}/pipeline`);
export const runPipeline = (productionId: number, force = false) =>
  request<{ started: boolean }>(`/api/productions/${productionId}/pipeline/run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ force }),
  });
export const updateProduction = (productionId: number, data: { description?: string; case_context?: string }): Promise<ProductionInfo> =>
  request(`/api/productions/${productionId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
export const getClusterDocuments = (productionId: number, clusterId: number, limit = 5): Promise<ClusterDocument[]> =>
  request(`/api/productions/${productionId}/clusters/${clusterId}/documents?limit=${limit}`);
```

(Follow the exact `request()` call style used by neighbors in client.ts — e.g. `createQueue` at client.ts:411 for POST-with-body shape.) Also: `createProductionForIngest(name, description)` (client.ts:391) gains a third parameter `caseContext: string` sent as `case_context` in the body. **Delete the orphaned `nlSearch` function (client.ts:225)** — its only consumer died in Phase 1, and the import list of any file must not reference it (grep first: `grep -rn "nlSearch" frontend/src`).

Theme tokens in `variables.css` after the brass block:

```css
  /* ── Theme hues (cluster chips, brief donut) — muted archival palette ── */
  --theme-1: #5b6e9e;
  --theme-2: #8a6d4f;
  --theme-3: #6d8a71;
  --theme-4: #9e6a5b;
  --theme-5: #7a6a95;
  --theme-6: #5f8a8a;
  --theme-7: #a3855c;
  --theme-8: #85607a;
```

- [ ] **Step 1: Make the three edits above** (complete code given in Interfaces).
- [ ] **Step 2: Verify**

Run: `cd frontend && grep -rn "nlSearch" src` → no matches.
Run: `cd frontend && npx eslint src/types/index.ts src/api/client.ts` → 0 errors.
Run: `cd frontend && npm run build` → succeeds.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/api/client.ts frontend/src/styles/variables.css
git commit -m "feat(frontend): pipeline/brief types, client functions, theme hue tokens"
```

---

### Task 8: ProductionBrief component + Home integration (deletes TopicGroups & CorpusAnalysis)

**Files:**
- Create: `frontend/src/components/ProductionBrief.tsx`
- Modify: `frontend/src/styles/components.css` (append brief styles)
- Modify: `frontend/src/App.tsx`
- Delete: `frontend/src/components/TopicGroups.tsx`, `frontend/src/components/CorpusAnalysis.tsx`

**Interfaces:**
- Consumes: `getPipeline`, `runPipeline`, `getClusterDocuments` (Task 7), existing `clusters: ClusterInfo[]` / `filterClusterId` / `setFilterClusterId` state in Home (App.tsx:117-118), `ProductionInfo.is_owner`.
- Produces:

```typescript
interface ProductionBriefProps {
  production: ProductionInfo;
  clusters: ClusterInfo[];
  activeClusterId: number | null;
  onSelectCluster: (id: number | null) => void;
  onViewDocument: (id: string) => void;
}
```

Component behavior (states, in priority order):
1. **Loading**: `getPipeline` in flight → nothing (no layout jump).
2. **Running**: any stage `running` (or `pending` while another is `running`) → skeleton card: "✦ AI is reading the production…" + a three-dot stage row (Clustering · Summaries · Brief) with per-stage state glyphs (`✓` done, `…` running, `·` pending, `!` failed); poll `getPipeline` every 5s while any stage is running.
3. **Brief present**: full card (see below).
4. **No brief, stage failed**: owner/manager sees "Brief generation failed — Retry" (button → `runPipeline(id)` then switch to polling); others see nothing.
5. **No brief, no status** (pre-pipeline production): owner sees the **retrofit card** — "✦ Generate a Production Brief — AI will cluster, summarize, and brief this production." with a Generate button → `runPipeline(id)`; non-owners see nothing.

Full card layout: header row (serif "Production Brief" + ✦ mark + generated date + collapse toggle); overview paragraph; meta row (key players as plain text list, date range); **theme chips** — one chip per cluster, background `var(--theme-N)` cycling by index (`(i % 8) + 1`), click → `onSelectCluster(active ? null : c.id)`; "Explore themes ▾" expansion → donut (SVG arcs, same trig as the deleted CorpusAnalysis DonutChart but colored with the theme tokens) + per-theme key documents fetched lazily via `getClusterDocuments` on expand, each row clickable → `onViewDocument(d.document_id)`; notable documents from the brief (bates + reason — bates NOT clickable; brief bates strings aren't guaranteed to resolve to ids). Collapsed state persists per production in `localStorage` key `vigilist.brief.collapsed.<productionId>` (wrapped in try/catch like `useOnboarding`'s storage helpers); collapsed form is a single line: "✦ Production Brief" + theme chips.

- [ ] **Step 1: Write the component.** Full implementation is the deliverable; key skeleton (the implementer writes the complete file — every state above must exist):

```tsx
import { useCallback, useEffect, useRef, useState } from 'react';
import { getClusterDocuments, getPipeline, runPipeline } from '../api/client';
import { showToast } from './Toast';
import type { ClusterDocument, ClusterInfo, PipelineInfo, ProductionInfo } from '../types';

const POLL_MS = 5000;

function briefCollapseKey(productionId: number) {
  return `vigilist.brief.collapsed.${productionId}`;
}

function safeGet(key: string): string | null {
  try { return window.localStorage.getItem(key); } catch { return null; }
}
function safeSet(key: string, value: string) {
  try { window.localStorage.setItem(key, value); } catch { /* storage unavailable */ }
}

interface ProductionBriefProps {
  production: ProductionInfo;
  clusters: ClusterInfo[];
  activeClusterId: number | null;
  onSelectCluster: (id: number | null) => void;
  onViewDocument: (id: string) => void;
}

export default function ProductionBrief({ production, clusters, activeClusterId, onSelectCluster, onViewDocument }: ProductionBriefProps) {
  const [info, setInfo] = useState<PipelineInfo | null>(null);
  const [collapsed, setCollapsed] = useState(() => safeGet(briefCollapseKey(production.id)) === '1');
  const [expanded, setExpanded] = useState(false);
  const [themeDocs, setThemeDocs] = useState<Record<number, ClusterDocument[]>>({});
  const pollRef = useRef<number | null>(null);
  // ... load + poll-while-running effect, retry/generate handlers, render states 1-5
}
```

Poll effect contract: fetch `getPipeline(production.id)` on mount and whenever a run is started; while `info?.status` has any stage `=== 'running'`, `window.setInterval` at `POLL_MS`; clear on unmount and when nothing is running. When the pipeline transitions to done, the parent's cluster list may be stale — call `getClusters` again? No: Home already refetches clusters on its filter effect; instead expose refresh via a `key` — **Home remounts per production (Phase 1), and cluster refetch is handled in Task 8 Step 3 below.**

Donut: copy the arc-path math from the deleted `CorpusAnalysis.tsx` `DonutChart` (lines 23-60) into a private `ThemeDonut({ clusters })` function inside ProductionBrief.tsx, replacing `TOPIC_COLORS[i]` with `var(--theme-${(i % 8) + 1})` via `style={{ fill: \`var(--theme-${(i % 8) + 1})\` }}`. No hex literals in the TSX.

- [ ] **Step 2: Append brief styles to `frontend/src/styles/components.css`**

Class set (all colors via tokens): `.brief-card` (parchment card, `--shadow-sm`, `--radius-lg`, left rule `3px solid var(--color-ink)`), `.brief-header` (flex; serif title via `--font-serif` `--text-xl`), `.brief-ai-mark` (the ✦: `color: var(--color-brass)`, `font-size: var(--text-sm)`), `.brief-overview` (`--text-sm`, `--leading-relaxed`, `--color-neutral-600`), `.brief-meta` (`--text-xs`, `--color-neutral-500`), `.brief-chips` (flex wrap, gap `--space-2`), `.brief-chip` (pill: white text on the theme hue, `--radius-full`, `--text-xs`; `.brief-chip.is-active` ring via `--shadow-ring`; inactive-but-filtered chips at 50% opacity), `.brief-skeleton` (pulsing bar animation), `.brief-stages` (stage glyph row, `--font-mono` `--text-xs`), `.brief-expand` (expansion region: donut left, key-doc list right, stacks under 720px), `.brief-doc-row` (clickable row, hover `--color-card-hover`), `.brief-collapsed` (single-line variant). Write real CSS for each — no placeholders.

- [ ] **Step 3: App.tsx integration**

1. Remove imports of `TopicGroups` (App.tsx:18) and `CorpusAnalysis` (App.tsx:7); import `ProductionBrief`.
2. Delete state `showCorpusAnalysis` (App.tsx:75-ish) and its early-return block (`if (showCorpusAnalysis) { return <CorpusAnalysis .../> }`), and remove `view: ... showCorpusAnalysis ? 'analysis' : ...` from the `useSyncUrl` call (App.tsx:100-106) — `view` now only ever carries `'ai'`. Also drop `initialUrl.view === 'analysis'` seeding.
3. Replace the `<TopicGroups .../>` element (App.tsx:343-348) with:

```tsx
        <ProductionBrief
          production={production}
          clusters={clusters}
          activeClusterId={filterClusterId}
          onSelectCluster={setFilterClusterId}
          onViewDocument={setViewDocId}
        />
```

4. Cluster freshness after a pipeline run: change the clusters effect (App.tsx:126-127) dependency handling — extract `const refreshClusters = useCallback(() => { getClusters(production.id).then(setClusters).catch(e => console.warn('getClusters failed:', e)); }, [production.id]);`, call it in the existing effect, and pass `onPipelineSettled={refreshClusters}` — add that optional prop to `ProductionBriefProps` (`onPipelineSettled?: () => void`) and invoke it inside ProductionBrief when polling observes clustering transition to `done`.
5. `git rm frontend/src/components/TopicGroups.tsx frontend/src/components/CorpusAnalysis.tsx`.

- [ ] **Step 4: Verify**

Run: `cd frontend && grep -rn "TopicGroups\|CorpusAnalysis\|showCorpusAnalysis\|'analysis'" src` → no matches.
Run: `cd frontend && npx eslint src/components/ProductionBrief.tsx src/App.tsx` → 0 errors, no eslint-disable.
Run: `cd frontend && npm run build` → succeeds.
Manual (backend + dev server running, signed in): Home shows the retrofit card on the seeded production (owner); clicking Generate flips to the skeleton with stage glyphs; with no local Anthropic key, clustering fails (no Voyage embeddings locally either) → failed state with Retry appears; chips section absent (no clusters). This exercises states 2, 4, 5. State 3 (full brief) is verified in prod or with keys in `backend/.env`.

- [ ] **Step 5: Commit**

```bash
git add -A frontend/src
git commit -m "feat(frontend): Production Brief card replaces TopicGroups and CorpusAnalysis"
```

---

### Task 9: About-this-case intake + Production settings modal

**Files:**
- Modify: `frontend/src/components/IngestWizard.tsx` (setup stage, near lines 299-325)
- Create: `frontend/src/components/ProductionSettings.tsx`
- Modify: `frontend/src/components/AppHeader.tsx` (gear item)
- Modify: `frontend/src/App.tsx` (settings modal state + prop)

**Interfaces:**
- Consumes: `createProductionForIngest(name, description, caseContext)` (Task 7), `updateProduction` (Task 7), `AppHeaderProps` gear pattern (`onOpenSettings?: () => void` renders "Production settings" between "Share…" and "＋ Ingest a production").
- Produces: `<ProductionSettings production={ProductionInfo} onClose={() => void} onSaved={(p: ProductionInfo) => void} />` modal using the `.modal-*` system.

- [ ] **Step 1: IngestWizard — About this case**

In the setup stage form, after the description input (IngestWizard.tsx:305), add state `const [caseContext, setCaseContext] = useState('');` and:

```tsx
          <label className="input-label" htmlFor="ingest-case-context">
            About this case <span className="brief-ai-mark">✦</span>
          </label>
          <p className="input-hint">
            A few sentences: what the case is about and what makes a document
            relevant. The AI uses this to brief your team and, later, to
            classify documents. You can edit it anytime in Production settings.
          </p>
          <textarea
            id="ingest-case-context"
            className="input"
            rows={4}
            value={caseContext}
            onChange={e => setCaseContext(e.target.value)}
            placeholder="e.g. Product-liability suit over the March 2024 recall. Relevant: anything about the recall decision, board discussions, or customer injuries."
          />
```

Pass it through `handleStart`: `createProductionForIngest(name.trim(), description.trim(), caseContext.trim())`. If `.input-label` / `.input-hint` classes don't exist in components.css, add them (label: `--text-xs` semibold `--color-neutral-600`; hint: `--text-xs` `--color-neutral-400`, margin below).

- [ ] **Step 2: ProductionSettings modal**

Create `frontend/src/components/ProductionSettings.tsx` — a `.modal-overlay`/`.modal-panel` modal (mirror ManageAccess's shell) titled "Production settings" with two fields (Description input, About-this-case textarea with the same hint copy), Cancel/Save buttons, saving via `updateProduction(production.id, { description, case_context })`, `showToast('Settings saved', 'success')` on success, toast error on failure, `onSaved(updated)` callback. Seed field state from `production.description` / `production.case_context ?? ''`. Complete file, standard modal accessibility (`role="dialog"`, `aria-modal`, Esc closes — copy the pattern from OnboardingGuide.tsx if ManageAccess lacks it).

- [ ] **Step 3: Wire into AppHeader + App.tsx**

AppHeader: add `onOpenSettings?: () => void` to `AppHeaderProps`; in `gearItems`, insert `onOpenSettings && { label: 'Production settings', action: onOpenSettings },` immediately after the Share entry. App.tsx Home: `const [showSettings, setShowSettings] = useState(false);`, pass `onOpenSettings={production.is_owner ? () => setShowSettings(true) : undefined}`, render:

```tsx
      {showSettings && (
        <ProductionSettings
          production={production}
          onClose={() => setShowSettings(false)}
          onSaved={() => setShowSettings(false)}
        />
      )}
```

(Production list refresh after save is unnecessary — Home already holds the production object; description/case_context edits surface on next load, acceptable.)

- [ ] **Step 4: Verify**

Run: `cd frontend && npx eslint src/components/IngestWizard.tsx src/components/ProductionSettings.tsx src/components/AppHeader.tsx src/App.tsx` → 0 errors.
Run: `cd frontend && npm run build` → succeeds.
Manual: gear shows "Production settings" for owner only; modal opens, edits save (verify via `GET /api/productions` response or reopening the modal); ingest wizard setup screen shows the About-this-case textarea.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/IngestWizard.tsx frontend/src/components/ProductionSettings.tsx frontend/src/components/AppHeader.tsx frontend/src/App.tsx frontend/src/styles/components.css
git commit -m "feat(frontend): case-context intake in ingest wizard and production settings"
```

---

### Task 10: Phase verification sweep

**Files:** none planned — fixes only.

- [ ] **Step 1: Full builds/tests**

`cd frontend && npm run build` → succeeds.
`cd backend && python -m pytest tests/ -v` → all Phase 2 tests pass; only the known pre-existing failure remains.
`cd frontend && npx eslint src/App.tsx src/api/client.ts src/types/index.ts src/components/ProductionBrief.tsx src/components/ProductionSettings.tsx src/components/IngestWizard.tsx src/components/AppHeader.tsx` → 0 errors.

- [ ] **Step 2: Migration check** — `cd backend && python -m alembic heads` → single head `l5a2b3c94d61`; local DB upgraded.

- [ ] **Step 3: Live pass** (backend :8000 + Vite :5173, signed in; drive via browser):
1. Home (seeded Acme production, owner): retrofit "Generate a Production Brief" card visible; non-owner production (Smith) shows no card.
2. Click Generate → skeleton + stage glyphs; without local API keys stages fail → failed card with Retry (owner). `GET /api/productions/1/pipeline` shows `status.errors` populated.
3. Gear → Production settings: edit case context, save, reopen — persisted.
4. Ingest wizard: About-this-case textarea present in setup; (full ingest run optional locally).
5. Confirm Clusters strip and Corpus Analysis are gone; `?view=analysis` URL no longer triggers anything (loads Home).
6. Document viewer Summary tab still works (on-demand path unchanged).
7. If `backend/.env` gains `VIGILIST_ANTHROPIC_API_KEY`/`VIGILIST_VOYAGE_API_KEY` for the session: rerun Generate and verify the full brief card (state 3), chips filter the list, expansion donut + key docs render.

- [ ] **Step 4: Commit fixes if any**

```bash
git add -A && git commit -m "fix: phase 2 verification fixes"
```

---

## Self-Review Notes

- **Spec §2 coverage:** ingest "About this case" ✔ (T9, as setup-section — deviation documented), auto-run clustering/summaries/brief ✔ (T4-5), cost-gated classification — **Phase 4, correctly absent**, per-stage status + skeleton + retry + graceful degradation ✔ (T4, T8), docs searchable regardless ✔ (pipeline detached from ingest), retrofit ✔ (T8 state 5), case-context editable later ✔ (T9).
- **Spec §3-Home coverage:** Brief card with serif headline/overview/players/date-range/theme-chip filtering ✔, expansion donut + per-theme key docs ✔ (T6 endpoint + T8), collapsible with remembered state ✔, TopicGroups deleted ✔, CorpusAnalysis deleted ✔. List AI columns — **Phase 3, correctly absent**.
- **Consistency check:** `run_ambient_pipeline(production_id, force=False)` used identically in T4/T5; `PipelineInfo` field names match `PipelineStatusOut`; `getClusterDocuments` limit default 5 matches endpoint; `ProductionBriefProps` names match the App.tsx call site; `createProductionForIngest` 3-arg form matches T5's backend `case_context` intake.
- **Known risks accepted:** pipeline via Cloud Tasks has a 30-min dispatch ceiling — very large productions may need stage-level fan-out later (noted, out of scope); local verification of the full-brief state requires API keys.
