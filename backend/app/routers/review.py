"""AI Review Workflow endpoints."""

import asyncio
import logging
import random
import re

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import ROLE_RANK, get_user_role_for_production
from app.models import Document, Production, User
from app.models_review import AIReviewResult, ReviewProject
from app.routers.auth import get_current_user
from app.schemas_review import (
    AIReviewResultOut, AttorneyDecision, BulkAcceptRequest, PaginatedResults,
    ReviewProjectCreate, ReviewProjectOut, ReviewProjectUpdate,
)
from app.services.ai_review import DEFAULT_CATEGORIES
from app.services.audit import log_action
from app.services.review_tags import apply_decision_tag

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/review", tags=["review"])

# Sonnet list pricing 2026-07: $3/M input, $15/M output
PRICE_PER_INPUT_TOKEN_USD = 3 / 1_000_000
PRICE_PER_OUTPUT_TOKEN_USD = 15 / 1_000_000
MAX_DOC_CHARS_FOR_CLASSIFICATION = 12000
EST_INPUT_CHAR_OVERHEAD_TOKENS = 800  # system prompt + category list overhead
EST_OUTPUT_TOKENS_PER_DOC = 300

# A decision is either a plain "agree" or an "override_<category>" pick.
_DECISION_PATTERN = re.compile(r"^override_[a-z0-9_]+$", re.IGNORECASE)


# ── Project CRUD ──

@router.get("/projects/{production_id}", response_model=list[ReviewProjectOut])
async def list_projects(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await get_user_role_for_production(db, user, production_id)
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

    role = await get_user_role_for_production(db, user, production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or higher role required")

    # Check if this is the first project for the production
    count_result = await db.execute(
        select(func.count()).select_from(ReviewProject).where(ReviewProject.production_id == production_id)
    )
    existing_count = count_result.scalar() or 0

    # First project is always primary; otherwise honor body.is_primary
    is_primary = existing_count == 0 or body.is_primary

    # Clear is_primary on other projects FIRST if this one will be primary
    if is_primary:
        await db.execute(
            update(ReviewProject)
            .where(ReviewProject.production_id == production_id)
            .values(is_primary=False)
        )

    project = ReviewProject(
        production_id=production_id,
        name=body.name,
        prompt_text=body.prompt_text,
        categories=body.categories if body.categories else DEFAULT_CATEGORIES,
        prompt_versions=[{"version": 1, "text": body.prompt_text, "created_at": str(func.now())}],
        sample_size=body.sample_size,
        agreement_threshold=body.agreement_threshold,
        is_primary=is_primary,
        created_by=user.id,
    )
    db.add(project)
    await db.flush()  # Flush to get the ID

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

    role = await get_user_role_for_production(db, user, production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or higher role required")

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

    # Handle is_primary update
    if body.is_primary is True:
        # Clear is_primary on other projects FIRST
        await db.execute(
            update(ReviewProject)
            .where(ReviewProject.production_id == production_id)
            .where(ReviewProject.id != project.id)
            .values(is_primary=False)
        )
        project.is_primary = True
        await db.flush()  # Flush to ensure project is updated

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

    role = await get_user_role_for_production(db, user, production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or higher role required")

    was_primary = project.is_primary
    await db.delete(project)
    await db.flush()

    # If the deleted project was primary, promote the newest remaining project
    if was_primary:
        result = await db.execute(
            select(ReviewProject)
            .where(ReviewProject.production_id == production_id)
            .order_by(ReviewProject.created_at.desc(), ReviewProject.id.desc())
            .limit(1)
        )
        newest = result.scalars().first()
        if newest:
            await db.execute(
                update(ReviewProject)
                .where(ReviewProject.id == newest.id)
                .values(is_primary=True)
            )

    await db.commit()
    return {"status": "deleted"}


# ── Cost Estimate & Auto-Classify ──

@router.get("/estimate/{production_id}")
async def get_classification_estimate(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Estimate the cost of classifying all documents in a production."""
    await get_user_role_for_production(db, user, production_id)

    result = await db.execute(
        select(func.count(Document.id), func.avg(func.length(Document.text_content)))
        .where(Document.production_id == production_id)
        .where(Document.text_content.isnot(None))
    )
    doc_count, avg_chars = result.one()
    doc_count = doc_count or 0
    avg_chars = float(avg_chars) if avg_chars is not None else 0.0

    return estimate_classification_cost(doc_count, avg_chars)


@router.post("/auto-classify/{production_id}", response_model=ReviewProjectOut)
async def auto_classify(
    production_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Create the default 'Initial relevance pass' review project and kick off a full classification run."""
    production = await db.get(Production, production_id)
    if not production:
        raise HTTPException(status_code=404, detail="Production not found")

    role = await get_user_role_for_production(db, user, production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or higher role required")

    existing = await db.execute(
        select(ReviewProject.id)
        .where(ReviewProject.production_id == production_id)
        .where(ReviewProject.name == "Initial relevance pass")
    )
    if existing.scalars().first() is not None:
        raise HTTPException(status_code=409, detail="Initial relevance pass already exists for this production")

    if not production.case_context:
        raise HTTPException(status_code=400, detail="Production has no case context")

    doc_result = await db.execute(
        select(Document.id)
        .where(Document.production_id == production_id)
        .where(Document.text_content.isnot(None))
    )
    doc_ids = [str(row[0]) for row in doc_result.all()]

    # Clear is_primary on other projects FIRST — a partial unique index enforces one primary per production
    await db.execute(
        update(ReviewProject)
        .where(ReviewProject.production_id == production_id)
        .values(is_primary=False)
    )

    project = ReviewProject(
        production_id=production_id,
        name="Initial relevance pass",
        prompt_text=production.case_context,
        categories=DEFAULT_CATEGORIES,
        sample_size=0,
        status="running",
        is_primary=True,
        created_by=user.id,
        total_documents=len(doc_ids),
    )
    db.add(project)
    await db.flush()  # Flush to get the ID

    await log_action(
        db, user, "classification_run", "review_project", str(project.id),
        production_id=production_id,
        details={"source": "ingest_wizard", "doc_count": len(doc_ids)},
    )

    await db.commit()
    await db.refresh(project)

    background_tasks.add_task(
        _run_classification_batch,
        project_id=project.id,
        doc_ids=doc_ids,
        is_sample=False,
    )

    return await _project_out(db, project)


# ── Sample Analysis ──

@router.post("/projects/{production_id}/{project_id}/sample")
async def run_sample(
    production_id: int,
    project_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Select a diverse sample using embeddings and classify via Claude API."""
    project = await db.get(ReviewProject, project_id)
    if not project or project.production_id != production_id:
        raise HTTPException(status_code=404, detail="Project not found")

    role = await get_user_role_for_production(db, user, production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or higher role required")

    # Use embedding-based diverse sampling (falls back to random if no embeddings)
    from app.services.sampling import select_diverse_sample
    sample_ids = await select_diverse_sample(db, production_id, project.sample_size)

    if not sample_ids:
        raise HTTPException(status_code=400, detail="No documents with text in this production")

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

    role = await get_user_role_for_production(db, user, production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or higher role required")

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

    role = await get_user_role_for_production(db, user, production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or higher role required")

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
    project = await db.get(ReviewProject, project_id)
    if project is None or project.production_id != production_id:
        raise HTTPException(status_code=404, detail="Project not found")

    await get_user_role_for_production(db, user, production_id)
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

    # Compute agreement rate across ALL sample results (not just current page)
    agreement_rate = None
    if sample_only:
        agg = await db.execute(
            select(
                func.count().filter(AIReviewResult.attorney_decision.isnot(None)),
                func.count().filter(AIReviewResult.attorney_decision == "agree"),
            )
            .where(AIReviewResult.project_id == project_id)
            .where(AIReviewResult.is_sample == 1)
        )
        row = agg.one()
        reviewed_count, agree_count = row[0], row[1]
        if reviewed_count > 0:
            agreement_rate = round(agree_count / reviewed_count, 4)

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
    if body.decision != "agree" and not _DECISION_PATTERN.match(body.decision):
        raise HTTPException(status_code=400, detail="Invalid decision")

    result = await db.get(AIReviewResult, result_id)
    if not result:
        raise HTTPException(status_code=404, detail="Result not found")

    project = await db.get(ReviewProject, result.project_id)
    if project:
        role = await get_user_role_for_production(db, user, project.production_id)
        if role == "readonly":
            raise HTTPException(status_code=403, detail="Read-only access")

    result.attorney_decision = body.decision
    result.attorney_note = body.note

    if project:
        await apply_decision_tag(db, user, result, body.decision, project)

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


@router.post("/projects/{production_id}/{project_id}/bulk-accept")
async def bulk_accept(
    production_id: int,
    project_id: int,
    body: BulkAcceptRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Accept all undecided AI results at/above a confidence threshold, tagging each. Manager+."""
    project = await db.get(ReviewProject, project_id)
    if not project or project.production_id != production_id:
        raise HTTPException(status_code=404, detail="Project not found")

    role = await get_user_role_for_production(db, user, production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or higher role required")

    result = await db.execute(
        select(AIReviewResult)
        .where(AIReviewResult.project_id == project_id)
        .where(AIReviewResult.attorney_decision.is_(None))
        .where(AIReviewResult.confidence_score >= body.min_confidence)
        .where(AIReviewResult.ai_decision != "needs_review")
    )
    results = result.scalars().all()

    # Prefetch existing (document_id, tag_id) pairs for candidate docs
    doc_ids = [r.document_id for r in results]
    if doc_ids:
        from app.models import DocumentTag
        existing_result = await db.execute(
            select(DocumentTag.document_id, DocumentTag.tag_id)
            .where(DocumentTag.document_id.in_(doc_ids))
        )
        existing_pairs: set[tuple] = set(existing_result.all())
    else:
        existing_pairs = set()

    # Cache resolved tags per category to avoid redundant queries
    tag_cache: dict = {}

    count = 0
    for r in results:
        r.attorney_decision = "agree"
        await apply_decision_tag(
            db, user, r, "agree", project,
            tag_cache=tag_cache, existing_pairs=existing_pairs
        )
        count += 1

    await log_action(
        db, user, "ai_suggestions_bulk_accepted", "review_project", str(project_id),
        production_id=production_id,
        details={"count": count, "min_confidence": body.min_confidence},
    )

    await db.commit()
    return {"accepted": count}


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

    await get_user_role_for_production(db, user, production_id)

    return {
        "status": project.status,
        "total_documents": project.total_documents,
        "processed_documents": project.processed_documents,
        "total_cost_tokens": project.total_cost_tokens,
    }


# ── Helpers ──

def estimate_classification_cost(doc_count: int, avg_chars: float) -> dict:
    """Pure cost estimate for classifying doc_count documents averaging avg_chars each."""
    avg_chars = avg_chars or 0
    per_doc_input_tokens = min(avg_chars, MAX_DOC_CHARS_FOR_CLASSIFICATION) / 4 + EST_INPUT_CHAR_OVERHEAD_TOKENS
    est_input_tokens = int(per_doc_input_tokens * doc_count)
    est_output_tokens = EST_OUTPUT_TOKENS_PER_DOC * doc_count
    est_usd = round(
        est_input_tokens * PRICE_PER_INPUT_TOKEN_USD + est_output_tokens * PRICE_PER_OUTPUT_TOKEN_USD,
        2,
    )
    return {
        "doc_count": doc_count,
        "est_input_tokens": est_input_tokens,
        "est_output_tokens": est_output_tokens,
        "est_usd": est_usd,
    }


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
        categories=project.categories or DEFAULT_CATEGORIES,
        sample_size=project.sample_size, agreement_threshold=project.agreement_threshold,
        status=project.status, is_primary=project.is_primary,
        total_documents=project.total_documents,
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
    from app.services.ai_review import classify_document_cascade

    async with async_session_factory() as db:
        project = await db.get(ReviewProject, project_id)
        if not project:
            return

        prompt_version = len(project.prompt_versions) if project.prompt_versions else 1
        failed_count = 0

        for i, doc_id in enumerate(doc_ids):
            # Check if paused
            await db.refresh(project)
            if project.status == "paused":
                logger.info("Review project %d paused at %d/%d", project_id, i, len(doc_ids))
                return

            doc = await db.get(Document, doc_id)
            if not doc or not doc.text_content:
                # Count skipped docs so progress stays accurate
                project.processed_documents = (project.processed_documents or 0) + 1
                if (i + 1) % 3 == 0:
                    await db.commit()
                continue

            result_data, tokens, model_used = await classify_document_cascade(
                project.prompt_text,
                doc.text_content,
                categories=project.categories,
            )

            # classify_document returns 0 tokens whenever the API call never
            # produced a real answer — no key configured, or the request raised
            # (see app/services/ai_review.py:classify_document, which falls back
            # to parse_classification_response("{}") in both cases). Treat that
            # as a failed classification rather than a real "needs_review"
            # decision: skip the upsert so the document stays unclassified and
            # a re-run (POST /run only selects docs with no AIReviewResult row
            # yet) picks it back up.
            if tokens == 0:
                failed_count += 1
                continue

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
                existing_result.api_model = model_used
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
                    api_model=model_used,
                    api_cost_tokens=tokens,
                )
                db.add(review_result)

            project.processed_documents = (project.processed_documents or 0) + 1
            project.total_cost_tokens = (project.total_cost_tokens or 0) + tokens

            # Commit every doc so polling sees real-time progress
            await db.commit()

        # Final status. If any documents failed to classify, leave the project
        # paused (rather than complete/reviewing_sample) so it reads as needing
        # attention; processed_documents already reflects only real results
        # since failed docs were skipped above without incrementing it.
        if failed_count > 0:
            project.status = "paused"
        elif is_sample:
            project.status = "reviewing_sample"
        else:
            project.status = "complete"
        await db.commit()
        logger.info(
            "Review project %d: classified %d documents (%d failed)",
            project_id, len(doc_ids) - failed_count, failed_count,
        )
