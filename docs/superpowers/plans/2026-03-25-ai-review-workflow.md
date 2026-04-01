# AI Review Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a structured AI-assisted document review workflow where attorneys define review criteria, AI classifies documents with confidence scores and reasoning, and attorneys validate results through a human-in-the-loop interface.

**Architecture:** Review Projects store prompt criteria and status. The Claude API classifies documents with structured JSON output (decision, confidence, reasoning, excerpts). Results are stored per-document and reviewed by attorneys who agree/override. Processing uses Cloud Tasks for parallelism. The frontend adds a dedicated AI Review page with a queue view and document review panel.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy async, Anthropic Claude API (claude-sonnet-4-6 for review, claude-haiku-4-5 for cost-sensitive), PostgreSQL (Neon), React 18 + TypeScript, Cloud Tasks

**Spec:** `EDISCOVERY_AI_REVIEW_SUPPLEMENT.md` Sections 2-3

---

## Scope: Phase 7B-1

This plan covers the core review workflow (spec sections 2.1-2.2, 3.1-3.2):
- Review Project CRUD with prompt versioning
- Sample analysis (AI classifies a sample of documents)
- Attorney review interface (agree/override/flag)
- Full corpus analysis with progress tracking
- Cost tracking per project (token counts)

**Deferred to Phase 7B-2:** Validation metrics (Section 4), issue coding (Section 5), privilege review (Section 6), validation report export (Section 4.3), prompt refinement with diff view (Section 3.3).

---

## File Structure

| Action | Path | Responsibility |
|--------|------|---------------|
| Create | `backend/app/models_review.py` | ReviewProject + AIReviewResult models |
| Create | `backend/app/routers/review.py` | Review project CRUD, sample, full-run, results endpoints |
| Create | `backend/app/services/ai_review.py` | Claude API classification logic |
| Create | `backend/app/schemas_review.py` | Pydantic schemas for review API |
| Create | `backend/alembic/versions/h1c6d5e04f37_add_review_project_tables.py` | Migration |
| Create | `backend/tests/test_ai_review.py` | Unit tests for AI classification |
| Create | `frontend/src/components/AIReviewPage.tsx` | Main AI review page |
| Create | `frontend/src/components/AIReviewQueue.tsx` | Document queue with sorting |
| Create | `frontend/src/components/AIReviewPanel.tsx` | Document review panel (AI analysis + viewer) |
| Create | `frontend/src/components/ReviewProjectSetup.tsx` | Create/edit review project dialog |
| Modify | `backend/app/main.py` | Register review router |
| Modify | `frontend/src/api/client.ts` | Add review API functions |
| Modify | `frontend/src/types/index.ts` | Add review types |
| Modify | `frontend/src/App.tsx` | Add AI Review navigation |

---

### Task 1: Review Data Models and Migration

**Files:**
- Create: `backend/app/models_review.py`
- Create: `backend/alembic/versions/h1c6d5e04f37_add_review_project_tables.py`

- [ ] **Step 1: Create models**

Create `backend/app/models_review.py`:
```python
"""AI Review Workflow models."""

import uuid

from sqlalchemy import (
    Column, DateTime, Float, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from app.models import Base


class ReviewProject(Base):
    __tablename__ = "review_projects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    production_id = Column(Integer, ForeignKey("productions.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(255), nullable=False)
    prompt_text = Column(Text, nullable=False)
    prompt_versions = Column(JSONB, nullable=False, default=list)  # [{version, text, created_at}]
    sample_size = Column(Integer, nullable=False, default=50)
    agreement_threshold = Column(Float, nullable=False, default=0.80)
    status = Column(String(20), nullable=False, default="draft")
    # status: draft, sampling, reviewing_sample, running, paused, complete
    total_documents = Column(Integer, nullable=False, default=0)
    processed_documents = Column(Integer, nullable=False, default=0)
    total_cost_tokens = Column(Integer, nullable=False, default=0)
    created_by = Column(String(128), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    production = relationship("Production")
    creator = relationship("User", foreign_keys=[created_by])
    results = relationship("AIReviewResult", back_populates="project", cascade="all, delete-orphan")


class AIReviewResult(Base):
    __tablename__ = "ai_review_results"
    __table_args__ = (
        UniqueConstraint("project_id", "document_id", name="uq_project_doc"),
        Index("ix_review_results_project", "project_id"),
        Index("ix_review_results_confidence", "project_id", "confidence_score"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("review_projects.id", ondelete="CASCADE"), nullable=False)
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    is_sample = Column(Integer, nullable=False, default=0)  # 1 if part of sample set
    ai_decision = Column(String(20), nullable=False)  # responsive, not_responsive, needs_review
    confidence_score = Column(Integer, nullable=False)  # 0-100
    reasoning = Column(Text, nullable=False)
    key_excerpts = Column(JSONB, nullable=False, default=list)  # [{text, start_offset, end_offset}]
    considerations = Column(Text, nullable=True)
    attorney_decision = Column(String(30), nullable=True)  # agree, override_responsive, override_not_responsive
    attorney_note = Column(Text, nullable=True)
    prompt_version = Column(Integer, nullable=False, default=1)
    api_model = Column(String(50), nullable=False)
    api_cost_tokens = Column(Integer, nullable=False, default=0)  # input + output tokens
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    project = relationship("ReviewProject", back_populates="results")
    document = relationship("Document")
```

- [ ] **Step 2: Create migration**

Create `backend/alembic/versions/h1c6d5e04f37_add_review_project_tables.py`:
```python
"""add review_projects and ai_review_results tables

Revision ID: h1c6d5e04f37
Revises: g9b5c4d03e26
Create Date: 2026-03-25 22:00:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = 'h1c6d5e04f37'
down_revision: Union[str, None] = 'g9b5c4d03e26'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'review_projects',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('production_id', sa.Integer, sa.ForeignKey('productions.id', ondelete='CASCADE'), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('prompt_text', sa.Text, nullable=False),
        sa.Column('prompt_versions', JSONB, nullable=False, server_default='[]'),
        sa.Column('sample_size', sa.Integer, nullable=False, server_default='50'),
        sa.Column('agreement_threshold', sa.Float, nullable=False, server_default='0.8'),
        sa.Column('status', sa.String(20), nullable=False, server_default='draft'),
        sa.Column('total_documents', sa.Integer, nullable=False, server_default='0'),
        sa.Column('processed_documents', sa.Integer, nullable=False, server_default='0'),
        sa.Column('total_cost_tokens', sa.Integer, nullable=False, server_default='0'),
        sa.Column('created_by', sa.String(128), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime, server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        'ai_review_results',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('project_id', sa.Integer, sa.ForeignKey('review_projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('document_id', UUID(as_uuid=True), sa.ForeignKey('documents.id', ondelete='CASCADE'), nullable=False),
        sa.Column('is_sample', sa.Integer, nullable=False, server_default='0'),
        sa.Column('ai_decision', sa.String(20), nullable=False),
        sa.Column('confidence_score', sa.Integer, nullable=False),
        sa.Column('reasoning', sa.Text, nullable=False),
        sa.Column('key_excerpts', JSONB, nullable=False, server_default='[]'),
        sa.Column('considerations', sa.Text, nullable=True),
        sa.Column('attorney_decision', sa.String(30), nullable=True),
        sa.Column('attorney_note', sa.Text, nullable=True),
        sa.Column('prompt_version', sa.Integer, nullable=False, server_default='1'),
        sa.Column('api_model', sa.String(50), nullable=False),
        sa.Column('api_cost_tokens', sa.Integer, nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('project_id', 'document_id', name='uq_project_doc'),
    )
    op.create_index('ix_review_results_project', 'ai_review_results', ['project_id'])
    op.create_index('ix_review_results_confidence', 'ai_review_results', ['project_id', 'confidence_score'])


def downgrade() -> None:
    op.drop_table('ai_review_results')
    op.drop_table('review_projects')
```

- [ ] **Step 3: Run migration against Neon**

```bash
cd backend
VIGILIST_DATABASE_URL="postgresql+asyncpg://neondb_owner:REDACTED-DB-PASSWORD@ep-noisy-frog-a8h520r3-pooler.eastus2.azure.neon.tech/neondb" python -m alembic upgrade head
```

- [ ] **Step 4: Commit**

```bash
git add backend/app/models_review.py backend/alembic/versions/h1c6d5e04f37_add_review_project_tables.py
git commit -m "feat: add ReviewProject and AIReviewResult models"
```

---

### Task 2: Pydantic Schemas for Review API

**Files:**
- Create: `backend/app/schemas_review.py`

- [ ] **Step 1: Create schemas**

Create `backend/app/schemas_review.py`:
```python
"""Pydantic schemas for AI Review Workflow."""

from datetime import datetime
from uuid import UUID
from pydantic import BaseModel


class ReviewProjectCreate(BaseModel):
    name: str
    prompt_text: str
    sample_size: int = 50
    agreement_threshold: float = 0.80


class ReviewProjectUpdate(BaseModel):
    name: str | None = None
    prompt_text: str | None = None
    sample_size: int | None = None
    agreement_threshold: float | None = None


class ReviewProjectOut(BaseModel):
    id: int
    production_id: int
    name: str
    prompt_text: str
    prompt_versions: list[dict]
    sample_size: int
    agreement_threshold: float
    status: str
    total_documents: int
    processed_documents: int
    total_cost_tokens: int
    created_by: str
    created_at: datetime
    updated_at: datetime
    # Computed fields
    sample_agreement_rate: float | None = None
    decision_breakdown: dict | None = None

    model_config = {"from_attributes": True}


class AIReviewResultOut(BaseModel):
    id: int
    project_id: int
    document_id: UUID
    bates_begin: str | None = None
    title: str | None = None
    is_sample: int
    ai_decision: str
    confidence_score: int
    reasoning: str
    key_excerpts: list[dict]
    considerations: str | None
    attorney_decision: str | None
    attorney_note: str | None
    prompt_version: int
    api_model: str
    api_cost_tokens: int
    created_at: datetime

    model_config = {"from_attributes": True}


class AttorneyDecision(BaseModel):
    decision: str  # agree, override_responsive, override_not_responsive
    note: str | None = None


class PaginatedResults(BaseModel):
    results: list[AIReviewResultOut]
    total: int
    page: int
    per_page: int
    agreement_rate: float | None = None
```

- [ ] **Step 2: Commit**

```bash
git add backend/app/schemas_review.py
git commit -m "feat: add review workflow Pydantic schemas"
```

---

### Task 3: AI Classification Service

**Files:**
- Create: `backend/app/services/ai_review.py`
- Create: `backend/tests/test_ai_review.py`

- [ ] **Step 1: Create tests**

Create `backend/tests/test_ai_review.py`:
```python
"""Tests for AI review classification service."""

import json
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.services.ai_review import parse_classification_response, build_classification_prompt


def test_build_classification_prompt():
    prompt = build_classification_prompt(
        review_criteria="Documents about use of force",
        document_text="Officer used a taser during the arrest.",
    )
    assert "use of force" in prompt
    assert "Officer used a taser" in prompt
    assert "responsive" in prompt.lower()


def test_parse_valid_response():
    raw = json.dumps({
        "decision": "responsive",
        "confidence": 92,
        "reasoning": "The document discusses use of a taser, which is a use of force.",
        "key_excerpts": [{"text": "Officer used a taser", "start_offset": 0, "end_offset": 20}],
        "considerations": "Clear use of force reference."
    })
    result = parse_classification_response(raw)
    assert result["decision"] == "responsive"
    assert result["confidence"] == 92
    assert len(result["key_excerpts"]) == 1


def test_parse_invalid_json():
    result = parse_classification_response("not json at all")
    assert result["decision"] == "needs_review"
    assert result["confidence"] == 0


def test_parse_missing_fields():
    raw = json.dumps({"decision": "responsive"})
    result = parse_classification_response(raw)
    assert result["decision"] == "responsive"
    assert result["confidence"] == 50  # default
    assert result["reasoning"] != ""
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && python -m pytest tests/test_ai_review.py -v
```
Expected: ImportError

- [ ] **Step 3: Implement classification service**

Create `backend/app/services/ai_review.py`:
```python
"""AI-powered document classification for review workflows."""

import json
import logging

import anthropic

from app.config import settings

logger = logging.getLogger(__name__)

CLASSIFICATION_SYSTEM = """You are a legal document review assistant. You classify documents based on the attorney's review criteria.

You MUST respond with a JSON object containing exactly these fields:
{
  "decision": "responsive" | "not_responsive" | "needs_review",
  "confidence": 0-100,
  "reasoning": "2-4 sentence explanation",
  "key_excerpts": [{"text": "exact quote from document", "start_offset": 0, "end_offset": 50}],
  "considerations": "any caveats or notes for the reviewer"
}

Rules:
- "responsive" = clearly matches the review criteria
- "not_responsive" = clearly does not match
- "needs_review" = ambiguous, reviewer should examine manually
- confidence 0-100: how certain you are about your decision
- key_excerpts: quote the EXACT text passages that informed your decision, with character offsets
- Be conservative: when in doubt, use "needs_review"
- Respond with ONLY the JSON object, no other text"""


def build_classification_prompt(review_criteria: str, document_text: str) -> str:
    """Build the user message for document classification."""
    truncated = document_text[:12000]  # ~3000 tokens
    return f"""## Review Criteria

{review_criteria}

## Document Text

{truncated}

Classify this document according to the review criteria above. Respond with JSON only."""


def parse_classification_response(raw: str) -> dict:
    """Parse Claude's classification response into a structured dict."""
    try:
        # Strip markdown code fences if present
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()

        data = json.loads(cleaned)
        return {
            "decision": data.get("decision", "needs_review"),
            "confidence": max(0, min(100, int(data.get("confidence", 50)))),
            "reasoning": data.get("reasoning", "No reasoning provided."),
            "key_excerpts": data.get("key_excerpts", []),
            "considerations": data.get("considerations"),
        }
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.warning("Failed to parse classification response: %s", e)
        return {
            "decision": "needs_review",
            "confidence": 0,
            "reasoning": f"Failed to parse AI response: {raw[:200]}",
            "key_excerpts": [],
            "considerations": "AI response could not be parsed. Manual review required.",
        }


async def classify_document(
    review_criteria: str,
    document_text: str,
    model: str = "claude-sonnet-4-6",
) -> tuple[dict, int]:
    """Classify a single document against review criteria.

    Returns (parsed_result, total_tokens).
    """
    if not settings.anthropic_api_key:
        return parse_classification_response("{}"), 0

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    prompt = build_classification_prompt(review_criteria, document_text)

    try:
        response = await client.messages.create(
            model=model,
            max_tokens=1000,
            system=CLASSIFICATION_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )

        raw_text = ""
        for block in response.content:
            if block.type == "text":
                raw_text = block.text
                break

        total_tokens = response.usage.input_tokens + response.usage.output_tokens
        result = parse_classification_response(raw_text)
        return result, total_tokens

    except Exception as e:
        logger.error("Classification failed: %s", e)
        return parse_classification_response("{}"), 0
```

- [ ] **Step 4: Run tests**

```bash
cd backend && python -m pytest tests/test_ai_review.py -v
```
Expected: All 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/ai_review.py backend/tests/test_ai_review.py
git commit -m "feat: add AI document classification service"
```

---

### Task 4: Review Router — CRUD + Sample + Full Run

**Files:**
- Create: `backend/app/routers/review.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Create review router**

Create `backend/app/routers/review.py`:
```python
"""AI Review Workflow endpoints."""

import asyncio
import logging
import random

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Document, Production, User
from app.models_review import AIReviewResult, ReviewProject
from app.routers.auth import get_current_user
from app.schemas_review import (
    AIReviewResultOut, AttorneyDecision, PaginatedResults,
    ReviewProjectCreate, ReviewProjectOut, ReviewProjectUpdate,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/review", tags=["review"])


# ── Project CRUD ──

@router.get("/projects/{production_id}", response_model=list[ReviewProjectOut])
async def list_projects(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(ReviewProject)
        .where(ReviewProject.production_id == production_id)
        .order_by(ReviewProject.created_at.desc())
    )
    projects = result.scalars().all()
    out = []
    for p in projects:
        out.append(await _project_out(db, p))
    return out


@router.post("/projects/{production_id}", response_model=ReviewProjectOut)
async def create_project(
    production_id: int,
    body: ReviewProjectCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    production = await db.get(Production, production_id)
    if not production:
        raise HTTPException(status_code=404, detail="Production not found")

    project = ReviewProject(
        production_id=production_id,
        name=body.name,
        prompt_text=body.prompt_text,
        prompt_versions=[{"version": 1, "text": body.prompt_text, "created_at": str(func.now())}],
        sample_size=body.sample_size,
        agreement_threshold=body.agreement_threshold,
        created_by=user.id,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return await _project_out(db, project)


@router.put("/projects/{production_id}/{project_id}", response_model=ReviewProjectOut)
async def update_project(
    production_id: int,
    project_id: int,
    body: ReviewProjectUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project = await db.get(ReviewProject, project_id)
    if not project or project.production_id != production_id:
        raise HTTPException(status_code=404, detail="Project not found")

    if body.name is not None:
        project.name = body.name
    if body.sample_size is not None:
        project.sample_size = body.sample_size
    if body.agreement_threshold is not None:
        project.agreement_threshold = body.agreement_threshold
    if body.prompt_text is not None and body.prompt_text != project.prompt_text:
        # Version the prompt
        versions = project.prompt_versions or []
        from datetime import datetime
        versions.append({
            "version": len(versions) + 1,
            "text": body.prompt_text,
            "created_at": datetime.utcnow().isoformat(),
        })
        project.prompt_text = body.prompt_text
        project.prompt_versions = versions

    await db.commit()
    await db.refresh(project)
    return await _project_out(db, project)


@router.delete("/projects/{production_id}/{project_id}")
async def delete_project(
    production_id: int,
    project_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project = await db.get(ReviewProject, project_id)
    if not project or project.production_id != production_id:
        raise HTTPException(status_code=404, detail="Project not found")
    await db.delete(project)
    await db.commit()
    return {"status": "deleted"}


# ── Sample Analysis ──

@router.post("/projects/{production_id}/{project_id}/sample")
async def run_sample(
    production_id: int,
    project_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Select a random sample and classify each document via Claude API."""
    project = await db.get(ReviewProject, project_id)
    if not project or project.production_id != production_id:
        raise HTTPException(status_code=404, detail="Project not found")

    # Get all document IDs with text in this production
    result = await db.execute(
        select(Document.id)
        .where(Document.production_id == production_id)
        .where(Document.text_content.isnot(None))
    )
    all_doc_ids = [row[0] for row in result.all()]

    if not all_doc_ids:
        raise HTTPException(status_code=400, detail="No documents with text in this production")

    sample_ids = random.sample(all_doc_ids, min(project.sample_size, len(all_doc_ids)))

    # Clear existing sample results
    await db.execute(
        delete(AIReviewResult)
        .where(AIReviewResult.project_id == project_id)
        .where(AIReviewResult.is_sample == 1)
    )

    project.status = "sampling"
    project.total_documents = len(sample_ids)
    project.processed_documents = 0
    await db.commit()

    background_tasks.add_task(
        _run_classification_batch,
        project_id=project_id,
        doc_ids=[str(d) for d in sample_ids],
        is_sample=True,
    )

    return {"status": "sampling", "sample_size": len(sample_ids)}


# ── Full Corpus Analysis ──

@router.post("/projects/{production_id}/{project_id}/run")
async def run_full(
    production_id: int,
    project_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Run AI classification on all documents in the production."""
    project = await db.get(ReviewProject, project_id)
    if not project or project.production_id != production_id:
        raise HTTPException(status_code=404, detail="Project not found")

    # Get docs not yet classified for this project
    already_done = select(AIReviewResult.document_id).where(AIReviewResult.project_id == project_id)
    result = await db.execute(
        select(Document.id)
        .where(Document.production_id == production_id)
        .where(Document.text_content.isnot(None))
        .where(Document.id.notin_(already_done))
    )
    doc_ids = [str(row[0]) for row in result.all()]

    if not doc_ids:
        return {"status": "complete", "remaining": 0}

    project.status = "running"
    project.total_documents = len(doc_ids) + project.processed_documents
    await db.commit()

    background_tasks.add_task(
        _run_classification_batch,
        project_id=project_id,
        doc_ids=doc_ids,
        is_sample=False,
    )

    return {"status": "running", "remaining": len(doc_ids)}


@router.post("/projects/{production_id}/{project_id}/pause")
async def pause_run(
    production_id: int,
    project_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project = await db.get(ReviewProject, project_id)
    if not project or project.production_id != production_id:
        raise HTTPException(status_code=404, detail="Project not found")
    project.status = "paused"
    await db.commit()
    return {"status": "paused"}


# ── Results ──

@router.get("/projects/{production_id}/{project_id}/results", response_model=PaginatedResults)
async def list_results(
    production_id: int,
    project_id: int,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    sort: str = Query("confidence_asc", pattern="^(confidence_asc|confidence_desc|decision|recent)$"),
    decision_filter: str | None = None,
    sample_only: bool = False,
    needs_review_only: bool = False,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = (
        select(AIReviewResult, Document.bates_begin, Document.title)
        .join(Document, Document.id == AIReviewResult.document_id)
        .where(AIReviewResult.project_id == project_id)
    )

    if sample_only:
        query = query.where(AIReviewResult.is_sample == 1)
    if decision_filter:
        query = query.where(AIReviewResult.ai_decision == decision_filter)
    if needs_review_only:
        query = query.where(AIReviewResult.attorney_decision.is_(None))

    # Count
    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    # Sort
    if sort == "confidence_asc":
        query = query.order_by(AIReviewResult.confidence_score.asc())
    elif sort == "confidence_desc":
        query = query.order_by(AIReviewResult.confidence_score.desc())
    elif sort == "decision":
        query = query.order_by(AIReviewResult.ai_decision)
    else:
        query = query.order_by(AIReviewResult.created_at.desc())

    query = query.offset((page - 1) * per_page).limit(per_page)
    rows = (await db.execute(query)).all()

    results = []
    for r, bates, title in rows:
        results.append(AIReviewResultOut(
            id=r.id, project_id=r.project_id, document_id=r.document_id,
            bates_begin=bates, title=title,
            is_sample=r.is_sample, ai_decision=r.ai_decision,
            confidence_score=r.confidence_score, reasoning=r.reasoning,
            key_excerpts=r.key_excerpts or [], considerations=r.considerations,
            attorney_decision=r.attorney_decision, attorney_note=r.attorney_note,
            prompt_version=r.prompt_version, api_model=r.api_model,
            api_cost_tokens=r.api_cost_tokens, created_at=r.created_at,
        ))

    # Compute agreement rate for sample
    agreement_rate = None
    if sample_only:
        reviewed = [r for r in results if r.attorney_decision is not None]
        if reviewed:
            agreed = sum(1 for r in reviewed if r.attorney_decision == "agree")
            agreement_rate = round(agreed / len(reviewed), 4)

    return PaginatedResults(
        results=results, total=total, page=page, per_page=per_page,
        agreement_rate=agreement_rate,
    )


# ── Attorney Decision ──

@router.put("/results/{result_id}/decide", response_model=AIReviewResultOut)
async def record_decision(
    result_id: int,
    body: AttorneyDecision,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.get(AIReviewResult, result_id)
    if not result:
        raise HTTPException(status_code=404, detail="Result not found")

    result.attorney_decision = body.decision
    result.attorney_note = body.note
    await db.commit()
    await db.refresh(result)

    doc = await db.get(Document, result.document_id)
    return AIReviewResultOut(
        id=result.id, project_id=result.project_id, document_id=result.document_id,
        bates_begin=doc.bates_begin if doc else None, title=doc.title if doc else None,
        is_sample=result.is_sample, ai_decision=result.ai_decision,
        confidence_score=result.confidence_score, reasoning=result.reasoning,
        key_excerpts=result.key_excerpts or [], considerations=result.considerations,
        attorney_decision=result.attorney_decision, attorney_note=result.attorney_note,
        prompt_version=result.prompt_version, api_model=result.api_model,
        api_cost_tokens=result.api_cost_tokens, created_at=result.created_at,
    )


# ── Status Polling ──

@router.get("/projects/{production_id}/{project_id}/status")
async def get_project_status(
    production_id: int,
    project_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project = await db.get(ReviewProject, project_id)
    if not project or project.production_id != production_id:
        raise HTTPException(status_code=404, detail="Project not found")
    return {
        "status": project.status,
        "total_documents": project.total_documents,
        "processed_documents": project.processed_documents,
        "total_cost_tokens": project.total_cost_tokens,
    }


# ── Helpers ──

async def _project_out(db: AsyncSession, project: ReviewProject) -> ReviewProjectOut:
    """Build ReviewProjectOut with computed fields."""
    # Decision breakdown
    result = await db.execute(
        select(AIReviewResult.ai_decision, func.count())
        .where(AIReviewResult.project_id == project.id)
        .group_by(AIReviewResult.ai_decision)
    )
    breakdown = dict(result.all())

    # Sample agreement rate
    sample_result = await db.execute(
        select(
            func.count().filter(AIReviewResult.attorney_decision.isnot(None)),
            func.count().filter(AIReviewResult.attorney_decision == "agree"),
        )
        .where(AIReviewResult.project_id == project.id)
        .where(AIReviewResult.is_sample == 1)
    )
    row = sample_result.one()
    reviewed_count, agree_count = row[0], row[1]
    agreement_rate = round(agree_count / reviewed_count, 4) if reviewed_count > 0 else None

    return ReviewProjectOut(
        id=project.id, production_id=project.production_id,
        name=project.name, prompt_text=project.prompt_text,
        prompt_versions=project.prompt_versions or [],
        sample_size=project.sample_size, agreement_threshold=project.agreement_threshold,
        status=project.status, total_documents=project.total_documents,
        processed_documents=project.processed_documents,
        total_cost_tokens=project.total_cost_tokens,
        created_by=project.created_by, created_at=project.created_at,
        updated_at=project.updated_at,
        sample_agreement_rate=agreement_rate,
        decision_breakdown=breakdown if breakdown else None,
    )


async def _run_classification_batch(
    project_id: int,
    doc_ids: list[str],
    is_sample: bool,
):
    """Background task: classify a batch of documents."""
    from app.database import async_session_factory
    from app.services.ai_review import classify_document

    async with async_session_factory() as db:
        project = await db.get(ReviewProject, project_id)
        if not project:
            return

        prompt_version = len(project.prompt_versions) if project.prompt_versions else 1

        for i, doc_id in enumerate(doc_ids):
            # Check if paused
            await db.refresh(project)
            if project.status == "paused":
                logger.info("Review project %d paused at %d/%d", project_id, i, len(doc_ids))
                return

            doc = await db.get(Document, doc_id)
            if not doc or not doc.text_content:
                continue

            result_data, tokens = await classify_document(
                project.prompt_text,
                doc.text_content,
            )

            # Upsert result
            existing = await db.execute(
                select(AIReviewResult)
                .where(AIReviewResult.project_id == project_id)
                .where(AIReviewResult.document_id == doc.id)
            )
            existing_result = existing.scalar_one_or_none()

            if existing_result:
                existing_result.ai_decision = result_data["decision"]
                existing_result.confidence_score = result_data["confidence"]
                existing_result.reasoning = result_data["reasoning"]
                existing_result.key_excerpts = result_data["key_excerpts"]
                existing_result.considerations = result_data["considerations"]
                existing_result.api_cost_tokens = tokens
                existing_result.prompt_version = prompt_version
            else:
                review_result = AIReviewResult(
                    project_id=project_id,
                    document_id=doc.id,
                    is_sample=1 if is_sample else 0,
                    ai_decision=result_data["decision"],
                    confidence_score=result_data["confidence"],
                    reasoning=result_data["reasoning"],
                    key_excerpts=result_data["key_excerpts"],
                    considerations=result_data.get("considerations"),
                    prompt_version=prompt_version,
                    api_model="claude-sonnet-4-6",
                    api_cost_tokens=tokens,
                )
                db.add(review_result)

            project.processed_documents = (project.processed_documents or 0) + 1
            project.total_cost_tokens = (project.total_cost_tokens or 0) + tokens

            if (i + 1) % 5 == 0:
                await db.commit()

        # Final status
        if is_sample:
            project.status = "reviewing_sample"
        else:
            project.status = "complete"
        await db.commit()
        logger.info("Review project %d: classified %d documents", project_id, len(doc_ids))
```

- [ ] **Step 2: Register router in main.py**

In `backend/app/main.py`, add to the imports:
```python
from app.routers import review
```

Add to router registration:
```python
app.include_router(review.router)
```

- [ ] **Step 3: Commit**

```bash
git add backend/app/routers/review.py backend/app/main.py
git commit -m "feat: add AI review workflow router with CRUD, sample, and full-run"
```

---

### Task 5: Frontend Types and API Client

**Files:**
- Modify: `frontend/src/types/index.ts`
- Modify: `frontend/src/api/client.ts`

- [ ] **Step 1: Add TypeScript types**

Add to `frontend/src/types/index.ts`:
```typescript
// ── AI Review ──

export interface ReviewProject {
  id: number;
  production_id: number;
  name: string;
  prompt_text: string;
  prompt_versions: { version: number; text: string; created_at: string }[];
  sample_size: number;
  agreement_threshold: number;
  status: string;
  total_documents: number;
  processed_documents: number;
  total_cost_tokens: number;
  created_by: string;
  created_at: string;
  updated_at: string;
  sample_agreement_rate: number | null;
  decision_breakdown: Record<string, number> | null;
}

export interface AIReviewResult {
  id: number;
  project_id: number;
  document_id: string;
  bates_begin: string | null;
  title: string | null;
  is_sample: number;
  ai_decision: string;
  confidence_score: number;
  reasoning: string;
  key_excerpts: { text: string; start_offset: number; end_offset: number }[];
  considerations: string | null;
  attorney_decision: string | null;
  attorney_note: string | null;
  prompt_version: number;
  api_model: string;
  api_cost_tokens: number;
  created_at: string;
}

export interface PaginatedReviewResults {
  results: AIReviewResult[];
  total: number;
  page: number;
  per_page: number;
  agreement_rate: number | null;
}
```

- [ ] **Step 2: Add API functions**

Add to `frontend/src/api/client.ts`:
```typescript
// ── AI Review ──

export const listReviewProjects = (productionId: number) =>
  request<ReviewProject[]>(`/api/review/projects/${productionId}`);

export const createReviewProject = (productionId: number, data: { name: string; prompt_text: string; sample_size?: number }) =>
  request<ReviewProject>(`/api/review/projects/${productionId}`, json(data));

export const updateReviewProject = (productionId: number, projectId: number, data: { name?: string; prompt_text?: string }) =>
  request<ReviewProject>(`/api/review/projects/${productionId}/${projectId}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });

export const deleteReviewProject = (productionId: number, projectId: number) =>
  request(`/api/review/projects/${productionId}/${projectId}`, { method: 'DELETE' });

export const runSample = (productionId: number, projectId: number) =>
  request<{ status: string; sample_size: number }>(`/api/review/projects/${productionId}/${projectId}/sample`, { method: 'POST' });

export const runFull = (productionId: number, projectId: number) =>
  request<{ status: string; remaining: number }>(`/api/review/projects/${productionId}/${projectId}/run`, { method: 'POST' });

export const pauseRun = (productionId: number, projectId: number) =>
  request(`/api/review/projects/${productionId}/${projectId}/pause`, { method: 'POST' });

export const getProjectStatus = (productionId: number, projectId: number) =>
  request<{ status: string; total_documents: number; processed_documents: number; total_cost_tokens: number }>(
    `/api/review/projects/${productionId}/${projectId}/status`
  );

export const listReviewResults = (
  productionId: number, projectId: number,
  page = 1, perPage = 50, sort = 'confidence_asc',
  options?: { decision_filter?: string; sample_only?: boolean; needs_review_only?: boolean },
) => {
  const params = new URLSearchParams({ page: String(page), per_page: String(perPage), sort });
  if (options?.decision_filter) params.set('decision_filter', options.decision_filter);
  if (options?.sample_only) params.set('sample_only', 'true');
  if (options?.needs_review_only) params.set('needs_review_only', 'true');
  return request<PaginatedReviewResults>(`/api/review/projects/${productionId}/${projectId}/results?${params}`);
};

export const recordDecision = (resultId: number, decision: string, note?: string) =>
  request<AIReviewResult>(`/api/review/results/${resultId}/decide`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ decision, note }),
  });
```

Add the imports to the types import in `client.ts`:
```typescript
import type { ..., ReviewProject, AIReviewResult, PaginatedReviewResults } from '../types';
```

- [ ] **Step 3: Build**

```bash
cd frontend && npm run build
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/api/client.ts
git commit -m "feat: add AI review types and API client functions"
```

---

### Task 6: Review Project Setup Component

**Files:**
- Create: `frontend/src/components/ReviewProjectSetup.tsx`

- [ ] **Step 1: Create component**

Create `frontend/src/components/ReviewProjectSetup.tsx`:
```tsx
import { useState } from 'react';
import { createReviewProject } from '../api/client';
import type { ReviewProject } from '../types';

interface Props {
  productionId: number;
  onCreated: (project: ReviewProject) => void;
  onCancel: () => void;
}

export default function ReviewProjectSetup({ productionId, onCreated, onCancel }: Props) {
  const [name, setName] = useState('');
  const [prompt, setPrompt] = useState('');
  const [sampleSize, setSampleSize] = useState(50);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const handleCreate = async () => {
    if (!name.trim() || !prompt.trim()) return;
    setLoading(true);
    setError('');
    try {
      const project = await createReviewProject(productionId, {
        name: name.trim(),
        prompt_text: prompt.trim(),
        sample_size: sampleSize,
      });
      onCreated(project);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div className="modal-panel" style={{ width: 600 }} onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h3 style={{ margin: 0, fontFamily: 'var(--font-serif)', fontSize: 'var(--text-lg)' }}>
            New AI Review Project
          </h3>
          <button className="btn btn-ghost btn-sm" onClick={onCancel}>Close</button>
        </div>

        <div style={{ padding: 'var(--space-4)', display: 'flex', flexDirection: 'column', gap: 'var(--space-4)' }}>
          <div>
            <label style={{ display: 'block', fontSize: 'var(--text-xs)', fontWeight: 600, color: 'var(--color-neutral-500)', marginBottom: 'var(--space-1)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
              Project Name
            </label>
            <input className="input" value={name} onChange={e => setName(e.target.value)}
              placeholder="e.g., Responsiveness Review" />
          </div>

          <div>
            <label style={{ display: 'block', fontSize: 'var(--text-xs)', fontWeight: 600, color: 'var(--color-neutral-500)', marginBottom: 'var(--space-1)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
              Review Criteria
            </label>
            <textarea
              className="input"
              value={prompt}
              onChange={e => setPrompt(e.target.value)}
              placeholder="Describe what makes a document responsive. Be specific about parties, topics, date ranges, and document types..."
              rows={8}
              style={{ resize: 'vertical', fontFamily: 'inherit' }}
            />
            <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-400)', marginTop: 'var(--space-1)' }}>
              The AI will use these criteria to classify each document as Responsive, Not Responsive, or Needs Review.
            </div>
          </div>

          <div>
            <label style={{ display: 'block', fontSize: 'var(--text-xs)', fontWeight: 600, color: 'var(--color-neutral-500)', marginBottom: 'var(--space-1)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
              Sample Size
            </label>
            <input className="input" type="number" value={sampleSize} onChange={e => setSampleSize(Number(e.target.value))}
              min={10} max={200} style={{ width: 100 }} />
            <span style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-400)', marginLeft: 'var(--space-2)' }}>
              documents for initial sample analysis
            </span>
          </div>

          {error && (
            <div style={{ padding: 'var(--space-2) var(--space-3)', fontSize: 'var(--text-sm)', color: 'var(--color-danger-700)', background: 'var(--color-danger-50)', border: '1px solid var(--color-danger-100)', borderRadius: 'var(--radius-md)' }}>
              {error}
            </div>
          )}

          <button className="btn btn-primary" onClick={handleCreate}
            disabled={!name.trim() || !prompt.trim() || loading} style={{ width: '100%' }}>
            {loading ? 'Creating...' : 'Create Project & Run Sample'}
          </button>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/components/ReviewProjectSetup.tsx
git commit -m "feat: add ReviewProjectSetup component"
```

---

### Task 7: AI Review Page — Queue + Review Panel

**Files:**
- Create: `frontend/src/components/AIReviewPage.tsx`

- [ ] **Step 1: Create the main AI Review page**

Create `frontend/src/components/AIReviewPage.tsx`:
```tsx
import { useEffect, useState } from 'react';
import {
  deleteReviewProject, getProjectStatus, listReviewProjects, listReviewResults,
  pauseRun, recordDecision, runFull, runSample,
} from '../api/client';
import type { AIReviewResult, PaginatedReviewResults, ReviewProject } from '../types';
import ReviewProjectSetup from './ReviewProjectSetup';

interface Props {
  productionId: number;
  onViewDocument: (docId: string) => void;
  onBack: () => void;
}

const DECISION_COLORS: Record<string, string> = {
  responsive: 'var(--color-success-600)',
  not_responsive: 'var(--color-danger-600)',
  needs_review: 'var(--color-warning-600)',
};

const DECISION_LABELS: Record<string, string> = {
  responsive: 'Responsive',
  not_responsive: 'Not Responsive',
  needs_review: 'Needs Review',
};

export default function AIReviewPage({ productionId, onViewDocument, onBack }: Props) {
  const [projects, setProjects] = useState<ReviewProject[]>([]);
  const [activeProject, setActiveProject] = useState<ReviewProject | null>(null);
  const [showSetup, setShowSetup] = useState(false);
  const [results, setResults] = useState<PaginatedReviewResults | null>(null);
  const [selectedResult, setSelectedResult] = useState<AIReviewResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [sort, setSort] = useState('confidence_asc');
  const [decisionNote, setDecisionNote] = useState('');

  // Load projects
  useEffect(() => {
    listReviewProjects(productionId).then(setProjects).catch(() => {});
  }, [productionId]);

  // Load results when active project changes
  useEffect(() => {
    if (!activeProject) { setResults(null); return; }
    setLoading(true);
    listReviewResults(productionId, activeProject.id, 1, 50, sort, { sample_only: activeProject.status === 'reviewing_sample' })
      .then(setResults)
      .finally(() => setLoading(false));
  }, [activeProject, sort]);

  // Poll status when running/sampling
  useEffect(() => {
    if (!activeProject || !['sampling', 'running'].includes(activeProject.status)) return;
    const interval = setInterval(async () => {
      try {
        const status = await getProjectStatus(productionId, activeProject.id);
        setActiveProject(prev => prev ? { ...prev, ...status } : prev);
        if (['reviewing_sample', 'complete'].includes(status.status)) {
          clearInterval(interval);
          // Refresh project list and results
          const updated = await listReviewProjects(productionId);
          setProjects(updated);
          const proj = updated.find(p => p.id === activeProject.id);
          if (proj) setActiveProject(proj);
        }
      } catch { /* ignore */ }
    }, 2000);
    return () => clearInterval(interval);
  }, [activeProject?.id, activeProject?.status]);

  const handleProjectCreated = async (project: ReviewProject) => {
    setShowSetup(false);
    setProjects(prev => [project, ...prev]);
    setActiveProject(project);
    // Auto-run sample
    await runSample(productionId, project.id);
    setActiveProject({ ...project, status: 'sampling' });
  };

  const handleRunFull = async () => {
    if (!activeProject) return;
    await runFull(productionId, activeProject.id);
    setActiveProject({ ...activeProject, status: 'running' });
  };

  const handlePause = async () => {
    if (!activeProject) return;
    await pauseRun(productionId, activeProject.id);
    setActiveProject({ ...activeProject, status: 'paused' });
  };

  const handleDecision = async (decision: string) => {
    if (!selectedResult) return;
    const updated = await recordDecision(selectedResult.id, decision, decisionNote || undefined);
    setResults(prev => prev ? {
      ...prev,
      results: prev.results.map(r => r.id === updated.id ? updated : r),
    } : prev);
    setSelectedResult(updated);
    setDecisionNote('');
    // Auto-advance to next unreviewed
    if (results) {
      const nextIdx = results.results.findIndex(r => r.id === selectedResult.id) + 1;
      const next = results.results.slice(nextIdx).find(r => !r.attorney_decision);
      if (next) setSelectedResult(next);
    }
  };

  const handleDelete = async (id: number) => {
    await deleteReviewProject(productionId, id);
    setProjects(prev => prev.filter(p => p.id !== id));
    if (activeProject?.id === id) { setActiveProject(null); setResults(null); }
  };

  const isProcessing = activeProject && ['sampling', 'running'].includes(activeProject.status);
  const progressPct = activeProject && activeProject.total_documents > 0
    ? Math.round((activeProject.processed_documents / activeProject.total_documents) * 100) : 0;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh' }}>
      {/* Header */}
      <div className="app-header">
        <button className="btn-header" onClick={onBack}>← Back</button>
        <span className="logo">AI Review</span>
        <button className="btn btn-primary btn-sm" onClick={() => setShowSetup(true)}>
          + New Review Project
        </button>
      </div>

      <div style={{ display: 'flex', flex: 1, overflow: 'hidden' }}>
        {/* Left: Project list */}
        <div style={{ width: 280, borderRight: '1px solid var(--color-neutral-200)', overflow: 'auto', padding: 'var(--space-3)' }}>
          <div style={{ fontSize: 'var(--text-xs)', fontWeight: 600, color: 'var(--color-neutral-500)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 'var(--space-2)' }}>
            Review Projects
          </div>
          {projects.map(p => (
            <div key={p.id} onClick={() => setActiveProject(p)} style={{
              padding: 'var(--space-2) var(--space-3)', borderRadius: 'var(--radius-md)', cursor: 'pointer',
              background: activeProject?.id === p.id ? 'var(--color-neutral-100)' : 'transparent',
              marginBottom: 'var(--space-1)',
            }}>
              <div style={{ fontSize: 'var(--text-sm)', fontWeight: 600 }}>{p.name}</div>
              <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-400)', display: 'flex', gap: 'var(--space-2)' }}>
                <span>{p.status}</span>
                {p.decision_breakdown && (
                  <span>{Object.values(p.decision_breakdown).reduce((a, b) => a + b, 0)} docs</span>
                )}
              </div>
            </div>
          ))}
          {projects.length === 0 && (
            <div style={{ fontSize: 'var(--text-sm)', color: 'var(--color-neutral-400)', padding: 'var(--space-4)', textAlign: 'center' }}>
              No review projects yet
            </div>
          )}
        </div>

        {/* Center: Results queue */}
        <div style={{ flex: 1, overflow: 'auto', padding: 'var(--space-3)' }}>
          {!activeProject ? (
            <div style={{ textAlign: 'center', padding: 'var(--space-8)', color: 'var(--color-neutral-400)' }}>
              Select a review project or create a new one
            </div>
          ) : (
            <>
              {/* Project header */}
              <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)', marginBottom: 'var(--space-3)' }}>
                <div style={{ flex: 1 }}>
                  <h2 style={{ margin: 0, fontFamily: 'var(--font-serif)', fontSize: 'var(--text-lg)' }}>{activeProject.name}</h2>
                  <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-400)' }}>
                    {activeProject.processed_documents} / {activeProject.total_documents} documents
                    {activeProject.total_cost_tokens > 0 && ` · ${(activeProject.total_cost_tokens / 1000).toFixed(1)}K tokens`}
                    {activeProject.sample_agreement_rate !== null && ` · ${Math.round(activeProject.sample_agreement_rate * 100)}% agreement`}
                  </div>
                </div>
                {activeProject.status === 'reviewing_sample' && (
                  <button className="btn btn-primary btn-sm" onClick={handleRunFull}>
                    Run Full Corpus
                  </button>
                )}
                {isProcessing && (
                  <button className="btn btn-secondary btn-sm" onClick={handlePause}>Pause</button>
                )}
                {activeProject.status === 'paused' && (
                  <button className="btn btn-primary btn-sm" onClick={handleRunFull}>Resume</button>
                )}
                <button className="btn btn-ghost btn-sm" style={{ color: 'var(--color-danger-500)' }}
                  onClick={() => handleDelete(activeProject.id)}>Delete</button>
              </div>

              {/* Progress bar */}
              {isProcessing && (
                <div style={{ height: 4, background: 'var(--color-neutral-200)', borderRadius: 2, marginBottom: 'var(--space-3)' }}>
                  <div style={{ height: '100%', width: `${progressPct}%`, background: 'var(--color-brand-500)', borderRadius: 2, transition: 'width 0.3s' }} />
                </div>
              )}

              {/* Sort controls */}
              <div style={{ display: 'flex', gap: 'var(--space-2)', marginBottom: 'var(--space-2)', fontSize: 'var(--text-xs)' }}>
                {[
                  { value: 'confidence_asc', label: 'Least confident' },
                  { value: 'confidence_desc', label: 'Most confident' },
                  { value: 'decision', label: 'By decision' },
                ].map(s => (
                  <button key={s.value} onClick={() => setSort(s.value)}
                    className={`btn btn-sm ${sort === s.value ? 'btn-secondary' : 'btn-ghost'}`}>
                    {s.label}
                  </button>
                ))}
              </div>

              {/* Results list */}
              {loading ? (
                <div className="loading-center"><span className="spinner spinner-md" /></div>
              ) : results?.results.map(r => (
                <div key={r.id} onClick={() => setSelectedResult(r)} style={{
                  padding: 'var(--space-2) var(--space-3)', borderRadius: 'var(--radius-md)', cursor: 'pointer',
                  border: '1px solid var(--color-neutral-200)', marginBottom: 'var(--space-1-5)',
                  background: selectedResult?.id === r.id ? 'var(--color-neutral-50)' : 'white',
                  display: 'flex', alignItems: 'center', gap: 'var(--space-3)',
                }}>
                  <span style={{
                    fontSize: 'var(--text-xs)', fontWeight: 700, padding: '2px 8px', borderRadius: 'var(--radius-sm)',
                    color: '#fff', background: DECISION_COLORS[r.ai_decision] || 'gray',
                  }}>
                    {DECISION_LABELS[r.ai_decision] || r.ai_decision}
                  </span>
                  <span style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-500)', width: 30, textAlign: 'center' }}>
                    {r.confidence_score}%
                  </span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 'var(--text-sm)', fontWeight: 500 }}>{r.title || r.bates_begin}</div>
                    <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-400)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                      {r.reasoning}
                    </div>
                  </div>
                  {r.attorney_decision && (
                    <span style={{ fontSize: 'var(--text-xs)', color: r.attorney_decision === 'agree' ? 'var(--color-success-600)' : 'var(--color-warning-600)' }}>
                      {r.attorney_decision === 'agree' ? '✓' : '✎'}
                    </span>
                  )}
                </div>
              ))}
            </>
          )}
        </div>

        {/* Right: Review panel */}
        {selectedResult && (
          <div style={{ width: 400, borderLeft: '1px solid var(--color-neutral-200)', overflow: 'auto', padding: 'var(--space-3)', display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' }}>
            {/* AI Decision */}
            <div>
              <span style={{
                fontSize: 'var(--text-sm)', fontWeight: 700, padding: '4px 12px', borderRadius: 'var(--radius-md)',
                color: '#fff', background: DECISION_COLORS[selectedResult.ai_decision] || 'gray',
              }}>
                {DECISION_LABELS[selectedResult.ai_decision]} — {selectedResult.confidence_score}%
              </span>
            </div>

            {/* Document info */}
            <div>
              <div style={{ fontSize: 'var(--text-sm)', fontWeight: 600 }}>{selectedResult.title || selectedResult.bates_begin}</div>
              <button className="btn btn-ghost btn-sm" style={{ padding: 0, fontSize: 'var(--text-xs)' }}
                onClick={() => onViewDocument(selectedResult.document_id)}>
                View document →
              </button>
            </div>

            {/* Reasoning */}
            <div>
              <div style={{ fontSize: 'var(--text-xs)', fontWeight: 600, color: 'var(--color-neutral-500)', textTransform: 'uppercase', marginBottom: 'var(--space-1)' }}>Reasoning</div>
              <div style={{ fontSize: 'var(--text-sm)', lineHeight: 1.5 }}>{selectedResult.reasoning}</div>
            </div>

            {/* Key excerpts */}
            {selectedResult.key_excerpts.length > 0 && (
              <div>
                <div style={{ fontSize: 'var(--text-xs)', fontWeight: 600, color: 'var(--color-neutral-500)', textTransform: 'uppercase', marginBottom: 'var(--space-1)' }}>Key Excerpts</div>
                {selectedResult.key_excerpts.map((ex, i) => (
                  <div key={i} style={{ fontSize: 'var(--text-sm)', padding: 'var(--space-2)', background: 'var(--color-warning-50)', borderLeft: '3px solid var(--color-warning-400)', borderRadius: 'var(--radius-sm)', marginBottom: 'var(--space-1)' }}>
                    "{ex.text}"
                  </div>
                ))}
              </div>
            )}

            {/* Considerations */}
            {selectedResult.considerations && (
              <div>
                <div style={{ fontSize: 'var(--text-xs)', fontWeight: 600, color: 'var(--color-neutral-500)', textTransform: 'uppercase', marginBottom: 'var(--space-1)' }}>Considerations</div>
                <div style={{ fontSize: 'var(--text-sm)', color: 'var(--color-neutral-600)', fontStyle: 'italic' }}>{selectedResult.considerations}</div>
              </div>
            )}

            {/* Attorney decision */}
            <div style={{ borderTop: '1px solid var(--color-neutral-200)', paddingTop: 'var(--space-3)' }}>
              {selectedResult.attorney_decision ? (
                <div style={{ fontSize: 'var(--text-sm)', color: 'var(--color-neutral-500)' }}>
                  Decision: <strong>{selectedResult.attorney_decision}</strong>
                  {selectedResult.attorney_note && <div style={{ marginTop: 'var(--space-1)' }}>Note: {selectedResult.attorney_note}</div>}
                </div>
              ) : (
                <>
                  <textarea className="input" placeholder="Optional note..." rows={2}
                    value={decisionNote} onChange={e => setDecisionNote(e.target.value)}
                    style={{ marginBottom: 'var(--space-2)', resize: 'none' }} />
                  <div style={{ display: 'flex', gap: 'var(--space-2)' }}>
                    <button className="btn btn-sm" style={{ flex: 1, background: 'var(--color-success-600)', color: '#fff', border: 'none' }}
                      onClick={() => handleDecision('agree')}>
                      Agree
                    </button>
                    <button className="btn btn-sm" style={{ flex: 1, background: 'var(--color-danger-600)', color: '#fff', border: 'none' }}
                      onClick={() => handleDecision(selectedResult.ai_decision === 'responsive' ? 'override_not_responsive' : 'override_responsive')}>
                      Override
                    </button>
                  </div>
                </>
              )}
            </div>
          </div>
        )}
      </div>

      {showSetup && <ReviewProjectSetup productionId={productionId} onCreated={handleProjectCreated} onCancel={() => setShowSetup(false)} />}
    </div>
  );
}
```

- [ ] **Step 2: Build and verify**

```bash
cd frontend && npm run build
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/AIReviewPage.tsx
git commit -m "feat: add AI Review page with queue and review panel"
```

---

### Task 8: Wire AI Review into App Navigation

**Files:**
- Modify: `frontend/src/App.tsx`

- [ ] **Step 1: Add AI Review navigation**

In `frontend/src/App.tsx`:

1. Import the component:
```typescript
import AIReviewPage from './components/AIReviewPage';
```

2. Add state:
```typescript
const [showAIReview, setShowAIReview] = useState(false);
```

3. Render AIReviewPage when active (similar to how DocumentViewer is conditionally rendered):
```typescript
if (showAIReview) {
  return <AIReviewPage productionId={production.id} onViewDocument={(id) => { setShowAIReview(false); setViewDocId(id); }} onBack={() => setShowAIReview(false)} />;
}
```

4. Add a button in the header/toolbar area:
```tsx
<button className="btn btn-secondary btn-sm" onClick={() => setShowAIReview(true)}>
  <span className="ai-indicator" style={{ padding: '0 4px', fontSize: 9 }}>AI</span>
  AI Review
</button>
```

- [ ] **Step 2: Build and verify**

```bash
cd frontend && npm run build
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/App.tsx
git commit -m "feat: add AI Review navigation to main app"
```

---

### Task 9: Deploy and Test

- [ ] **Step 1: Run migration**

```bash
cd backend
VIGILIST_DATABASE_URL="postgresql+asyncpg://..." python -m alembic upgrade head
```

- [ ] **Step 2: Deploy backend**

```bash
gcloud run deploy vigilist-api --source . --region us-central1
```

- [ ] **Step 3: Deploy frontend**

```bash
cd .. && npx firebase deploy --only hosting
```

- [ ] **Step 4: End-to-end test**

1. Click "AI Review" button in the app
2. Create a new review project with criteria like: "Documents about use of force by police officers, complaints about officer conduct, or internal affairs investigations"
3. Sample analysis runs automatically on 50 documents
4. Review AI decisions: agree or override each one
5. If agreement rate is good, click "Run Full Corpus"
6. Watch progress update in real time
7. Review results sorted by confidence (least confident first)
