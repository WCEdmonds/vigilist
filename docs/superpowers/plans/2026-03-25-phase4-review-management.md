# Phase 4: Review Management & QC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add review queues, batch assignment, progress dashboards, and QC workflows so that document review can be managed across a team of reviewers.

**Architecture:** Review queues are named document sets defined by a saved search or filter, scoped to a production. Batches divide a queue into reviewer-assigned chunks. A `BatchDocument` join table tracks per-document review status within a batch. QC is modeled as a separate review pass where a QC reviewer can agree with or overturn the original reviewer's coding decisions. Progress dashboards query batch/tag state for real-time metrics. Productions serve as the workspace (matter) unit — the existing `ProductionAccess` model with roles provides per-workspace isolation and RBAC.

**Tech Stack:** Python/FastAPI, SQLAlchemy async, Alembic, PostgreSQL, React 19/TypeScript, Firebase Auth

**Design note — matters vs. productions:** The spec calls for "multi-matter workspaces." Currently, productions already function as isolated workspaces with per-user access control and roles. Rather than adding a `Matter` model that would require restructuring all queries, this plan scopes review workflow features to productions. If matter-level grouping is needed later (e.g., grouping multiple productions from the same case), it can be added as a lightweight container without changing the review workflow models.

---

## File Structure

### New Backend Files
- `backend/app/routers/queues.py` — Review queue CRUD + batch creation endpoints
- `backend/app/routers/batches.py` — Batch assignment, progress, document navigation endpoints
- `backend/app/routers/qc.py` — QC sampling, decision recording, overturn stats endpoints
- `backend/app/routers/dashboard.py` — Review progress metrics endpoints
- `backend/app/services/batching.py` — Batch creation logic (splitting queues into batches)

### New Frontend Files
- `frontend/src/components/QueueManager.tsx` — Queue list, create/edit queue modal
- `frontend/src/components/BatchReview.tsx` — Reviewer's batch view (filtered document viewer with progress)
- `frontend/src/components/Dashboard.tsx` — Progress charts and reviewer metrics
- `frontend/src/components/QCReview.tsx` — QC review interface (see original coding, agree/overturn)

### Modified Files
- `backend/app/models.py` — Add ReviewQueue, ReviewBatch, BatchDocument, QCDecision models
- `backend/app/schemas.py` — Add queue, batch, QC, dashboard schemas
- `backend/app/main.py` — Register new routers
- `frontend/src/types/index.ts` — Add queue, batch, QC, dashboard types
- `frontend/src/api/client.ts` — Add queue, batch, QC, dashboard API functions
- `frontend/src/App.tsx` — Add navigation to queue manager, dashboard, batch review

---

## Task 1: Add Review Queue and Batch Models

**Files:**
- Modify: `backend/app/models.py`

- [ ] **Step 1: Add ReviewQueue model**

In `backend/app/models.py`, add after `AuditLog`:

```python
class ReviewQueue(Base):
    __tablename__ = "review_queues"

    id = Column(Integer, primary_key=True, autoincrement=True)
    production_id = Column(Integer, ForeignKey("productions.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    query = Column(String(1000), nullable=False, default="")
    filters = Column(JSONB, nullable=False, default=dict)  # metadata filters, tag filters, etc.
    status = Column(String(20), nullable=False, default="active")  # active, paused, completed
    created_by = Column(String(128), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    production = relationship("Production")
    creator = relationship("User", foreign_keys=[created_by])
    batches = relationship("ReviewBatch", back_populates="queue", cascade="all, delete-orphan")
```

- [ ] **Step 2: Add ReviewBatch model**

```python
class ReviewBatch(Base):
    __tablename__ = "review_batches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    queue_id = Column(Integer, ForeignKey("review_queues.id", ondelete="CASCADE"), nullable=False)
    reviewer_id = Column(String(128), ForeignKey("users.id"), nullable=True)  # null = unassigned
    status = Column(String(20), nullable=False, default="pending")  # pending, in_progress, completed
    size = Column(Integer, nullable=False, default=0)
    reviewed_count = Column(Integer, nullable=False, default=0)
    assigned_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    queue = relationship("ReviewQueue", back_populates="batches")
    reviewer = relationship("User", foreign_keys=[reviewer_id])
    documents = relationship("BatchDocument", back_populates="batch", cascade="all, delete-orphan")
```

- [ ] **Step 3: Add BatchDocument model**

```python
class BatchDocument(Base):
    __tablename__ = "batch_documents"
    __table_args__ = (
        UniqueConstraint("batch_id", "document_id", name="uq_batch_doc"),
        Index("ix_batch_documents_document_id", "document_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    batch_id = Column(Integer, ForeignKey("review_batches.id", ondelete="CASCADE"), nullable=False)
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    position = Column(Integer, nullable=False)  # order within batch
    reviewed = Column(String(20), nullable=False, default="pending")  # pending, reviewed, skipped
    reviewed_at = Column(DateTime, nullable=True)

    batch = relationship("ReviewBatch", back_populates="documents")
    document = relationship("Document")
```

- [ ] **Step 4: Add QCDecision model**

```python
class QCDecision(Base):
    __tablename__ = "qc_decisions"
    __table_args__ = (
        UniqueConstraint("batch_document_id", "qc_reviewer_id", name="uq_qc_decision"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    batch_document_id = Column(Integer, ForeignKey("batch_documents.id", ondelete="CASCADE"), nullable=False)
    original_reviewer_id = Column(String(128), ForeignKey("users.id"), nullable=False)
    qc_reviewer_id = Column(String(128), ForeignKey("users.id"), nullable=False)
    decision = Column(String(20), nullable=False)  # agree, overturn
    reason = Column(Text, nullable=True)  # required for overturns
    original_tags = Column(JSONB, nullable=False, default=list)  # snapshot of tags at QC time
    new_tags = Column(JSONB, nullable=True)  # tags after overturn (null if agreed)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    batch_document = relationship("BatchDocument")
    original_reviewer = relationship("User", foreign_keys=[original_reviewer_id])
    qc_reviewer = relationship("User", foreign_keys=[qc_reviewer_id])
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/models.py
git commit -m "feat: add ReviewQueue, ReviewBatch, BatchDocument, QCDecision models"
```

---

## Task 2: Add Schemas and Generate Migration

**Files:**
- Modify: `backend/app/schemas.py`
- Create: new Alembic migration

- [ ] **Step 1: Add queue schemas to schemas.py**

Add at the end of `backend/app/schemas.py`:

```python
# ── Review Queues & Batches ──

class ReviewQueueCreate(BaseModel):
    name: str
    description: str = ""
    query: str = ""
    filters: dict = {}


class ReviewQueueOut(BaseModel):
    id: int
    production_id: int
    name: str
    description: str | None
    query: str
    filters: dict
    status: str
    created_by: str
    created_at: datetime
    batch_count: int = 0
    total_documents: int = 0
    reviewed_documents: int = 0

    model_config = {"from_attributes": True}


class ReviewBatchOut(BaseModel):
    id: int
    queue_id: int
    queue_name: str = ""
    reviewer_id: str | None
    reviewer_email: str | None = None
    status: str
    size: int
    reviewed_count: int
    assigned_at: datetime | None
    completed_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class BatchDocumentOut(BaseModel):
    id: int
    batch_id: int
    document_id: UUID
    position: int
    reviewed: str
    reviewed_at: datetime | None
    bates_begin: str = ""
    title: str | None = None

    model_config = {"from_attributes": True}


class BatchCreateRequest(BaseModel):
    batch_size: int = 50
    reviewer_id: str | None = None


class BatchAssignRequest(BaseModel):
    reviewer_id: str


class BatchDocumentUpdate(BaseModel):
    reviewed: str  # "reviewed" or "skipped"


# ── QC ──

class QCSampleRequest(BaseModel):
    queue_id: int
    sample_percent: float = 10.0  # percent of reviewed docs to sample
    reviewer_id: str | None = None  # filter to specific reviewer, or all


class QCDecisionCreate(BaseModel):
    decision: str  # "agree" or "overturn"
    reason: str | None = None
    new_tag_ids: list[int] | None = None


class QCDecisionOut(BaseModel):
    id: int
    batch_document_id: int
    original_reviewer_id: str
    original_reviewer_email: str = ""
    qc_reviewer_id: str
    qc_reviewer_email: str = ""
    decision: str
    reason: str | None
    original_tags: list
    new_tags: list | None
    created_at: datetime
    bates_begin: str = ""

    model_config = {"from_attributes": True}


# ── Dashboard ──

class DashboardStats(BaseModel):
    total_documents: int
    reviewed_documents: int
    pending_documents: int
    percent_complete: float
    tag_breakdown: dict  # {category: {tag_name: count}}
    reviewer_stats: list[dict]  # [{user_id, email, reviewed_count, avg_per_hour}]
    queue_stats: list[dict]  # [{queue_id, name, total, reviewed, batch_count}]


class QCStats(BaseModel):
    total_decisions: int
    agree_count: int
    overturn_count: int
    overturn_rate: float
    by_reviewer: list[dict]  # [{reviewer_id, email, total, overturns, overturn_rate}]
```

- [ ] **Step 2: Generate Alembic migration**

```bash
cd backend && alembic revision --autogenerate -m "add review queues batches and qc tables"
```

**Important:** Review the generated migration file to verify it ONLY creates the four new tables (`review_queues`, `review_batches`, `batch_documents`, `qc_decisions`) and does NOT modify existing tables (autogenerate can sometimes detect drift on the `text_search_vector` TSVector column).

- [ ] **Step 3: Run migration**

```bash
cd backend && alembic upgrade head
```

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "feat: add queue/batch/QC schemas and migration"
```

---

## Task 3: Create Batching Service

**Files:**
- Create: `backend/app/services/batching.py`

- [ ] **Step 1: Create the batching service**

Create `backend/app/services/batching.py`:

```python
import math

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BatchDocument, Document, DocumentTag, ReviewBatch, ReviewQueue
from app.services.search import search_documents


async def get_queue_document_ids(
    db: AsyncSession, queue: ReviewQueue
) -> list[str]:
    """Resolve a queue's query/filters into a list of document IDs."""
    if queue.query or queue.filters:
        # Use the search service — pass production_id as both the filter and the
        # accessible list (this is a service-to-service call, RBAC already checked by caller)
        results, _ = await search_documents(
            db, queue.query, production_id=queue.production_id,
            page=1, per_page=100000, sort="bates",
            accessible_production_ids=[queue.production_id],
            metadata_filters=queue.filters.get("metadata") if queue.filters else None,
        )
        return [str(r["id"]) for r in results]
    else:
        # No query = all documents in the production
        result = await db.execute(
            select(Document.id)
            .where(Document.production_id == queue.production_id)
            .order_by(Document.bates_begin)
        )
        return [str(row[0]) for row in result.all()]


async def get_already_batched_doc_ids(
    db: AsyncSession, queue_id: int
) -> set[str]:
    """Return document IDs already assigned to any batch in this queue."""
    result = await db.execute(
        select(BatchDocument.document_id)
        .join(ReviewBatch, BatchDocument.batch_id == ReviewBatch.id)
        .where(ReviewBatch.queue_id == queue_id)
    )
    return {str(row[0]) for row in result.all()}


async def create_batches(
    db: AsyncSession, queue: ReviewQueue, batch_size: int = 50,
    reviewer_id: str | None = None,
) -> list[ReviewBatch]:
    """Create batches from unbatched documents in the queue."""
    all_doc_ids = await get_queue_document_ids(db, queue)
    already_batched = await get_already_batched_doc_ids(db, queue.id)
    remaining = [did for did in all_doc_ids if did not in already_batched]

    if not remaining:
        return []

    batches = []
    for i in range(0, len(remaining), batch_size):
        chunk = remaining[i:i + batch_size]
        batch = ReviewBatch(
            queue_id=queue.id,
            reviewer_id=reviewer_id,
            status="pending" if reviewer_id is None else "in_progress",
            size=len(chunk),
            reviewed_count=0,
        )
        if reviewer_id:
            batch.assigned_at = func.now()
        db.add(batch)
        await db.flush()  # get batch.id

        for pos, doc_id in enumerate(chunk):
            bd = BatchDocument(
                batch_id=batch.id,
                document_id=doc_id,
                position=pos,
            )
            db.add(bd)

        batches.append(batch)

    return batches
```

- [ ] **Step 2: Commit**

```bash
git add backend/app/services/batching.py
git commit -m "feat: add batching service for queue document splitting"
```

---

## Task 4: Create Queue Router

**Files:**
- Create: `backend/app/routers/queues.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Create queues router**

Create `backend/app/routers/queues.py`:

```python
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_user_role_for_production, ROLE_RANK
from app.models import BatchDocument, ReviewBatch, ReviewQueue, User
from app.routers.auth import get_current_user
from app.schemas import BatchCreateRequest, ReviewBatchOut, ReviewQueueCreate, ReviewQueueOut
from app.services.audit import log_action
from app.services.batching import create_batches

router = APIRouter(prefix="/api/productions/{production_id}/queues", tags=["queues"])


@router.get("", response_model=list[ReviewQueueOut])
async def list_queues(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    role = await get_user_role_for_production(db, user, production_id)

    result = await db.execute(
        select(ReviewQueue).where(ReviewQueue.production_id == production_id)
        .order_by(ReviewQueue.created_at.desc())
    )
    queues = result.scalars().all()

    out = []
    for q in queues:
        # Get batch stats
        stats = await db.execute(
            select(
                func.count(ReviewBatch.id),
                func.coalesce(func.sum(ReviewBatch.size), 0),
                func.coalesce(func.sum(ReviewBatch.reviewed_count), 0),
            ).where(ReviewBatch.queue_id == q.id)
        )
        batch_count, total_docs, reviewed_docs = stats.one()

        out.append(ReviewQueueOut(
            id=q.id, production_id=q.production_id, name=q.name,
            description=q.description, query=q.query, filters=q.filters,
            status=q.status, created_by=q.created_by, created_at=q.created_at,
            batch_count=batch_count, total_documents=total_docs,
            reviewed_documents=reviewed_docs,
        ))
    return out


@router.post("", response_model=ReviewQueueOut)
async def create_queue(
    production_id: int,
    body: ReviewQueueCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    role = await get_user_role_for_production(db, user, production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or admin role required")

    queue = ReviewQueue(
        production_id=production_id,
        name=body.name,
        description=body.description,
        query=body.query,
        filters=body.filters,
        created_by=user.id,
    )
    db.add(queue)
    await log_action(db, user, "queue_created", "queue", None,
                     production_id=production_id, details={"name": body.name})
    await db.commit()
    await db.refresh(queue)

    return ReviewQueueOut(
        id=queue.id, production_id=queue.production_id, name=queue.name,
        description=queue.description, query=queue.query, filters=queue.filters,
        status=queue.status, created_by=queue.created_by, created_at=queue.created_at,
        batch_count=0, total_documents=0, reviewed_documents=0,
    )


@router.delete("/{queue_id}")
async def delete_queue(
    production_id: int, queue_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    role = await get_user_role_for_production(db, user, production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or admin role required")

    queue = await db.get(ReviewQueue, queue_id)
    if not queue or queue.production_id != production_id:
        raise HTTPException(status_code=404, detail="Queue not found")

    await log_action(db, user, "queue_deleted", "queue", str(queue_id),
                     production_id=production_id, details={"name": queue.name})
    await db.delete(queue)
    await db.commit()
    return {"ok": True}


@router.post("/{queue_id}/batches", response_model=list[ReviewBatchOut])
async def create_queue_batches(
    production_id: int, queue_id: int,
    body: BatchCreateRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    role = await get_user_role_for_production(db, user, production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or admin role required")

    queue = await db.get(ReviewQueue, queue_id)
    if not queue or queue.production_id != production_id:
        raise HTTPException(status_code=404, detail="Queue not found")

    batches = await create_batches(db, queue, body.batch_size, body.reviewer_id)
    await log_action(db, user, "batches_created", "queue", str(queue_id),
                     production_id=production_id,
                     details={"count": len(batches), "batch_size": body.batch_size})
    await db.commit()

    # Refresh to get IDs
    for b in batches:
        await db.refresh(b)

    return [
        ReviewBatchOut(
            id=b.id, queue_id=b.queue_id, queue_name=queue.name,
            reviewer_id=b.reviewer_id, status=b.status,
            size=b.size, reviewed_count=b.reviewed_count,
            assigned_at=b.assigned_at, completed_at=b.completed_at,
            created_at=b.created_at,
        )
        for b in batches
    ]
```

- [ ] **Step 2: Register in main.py**

In `backend/app/main.py`, add `queues` to imports and include the router:

```python
from app.routers import ai, audit, auth, batches, dashboard, documents, export, ingest, notes, productions, qc, queues, saved_searches, search, tags
```

Only add `queues` for now — each subsequent task that creates a new router will add its own import:
```python
from app.routers import ai, audit, auth, documents, export, ingest, notes, productions, queues, saved_searches, search, tags
```
```python
app.include_router(queues.router)
```

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "feat: add review queue CRUD and batch creation endpoints"
```

---

## Task 5: Create Batch Router

**Files:**
- Create: `backend/app/routers/batches.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Create batches router**

Create `backend/app/routers/batches.py`:

```python
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.dependencies import get_user_role_for_production, ROLE_RANK
from app.models import BatchDocument, Document, ReviewBatch, ReviewQueue, User
from app.routers.auth import get_current_user
from app.schemas import BatchAssignRequest, BatchDocumentOut, BatchDocumentUpdate, ReviewBatchOut
from app.services.audit import log_action

router = APIRouter(prefix="/api/batches", tags=["batches"])


@router.get("/my", response_model=list[ReviewBatchOut])
async def my_batches(
    production_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get batches assigned to the current user."""
    query = (
        select(ReviewBatch)
        .join(ReviewQueue, ReviewBatch.queue_id == ReviewQueue.id)
        .where(ReviewBatch.reviewer_id == user.id)
        .where(ReviewBatch.status.in_(["pending", "in_progress"]))
    )
    if production_id is not None:
        query = query.where(ReviewQueue.production_id == production_id)

    query = query.order_by(ReviewBatch.created_at)
    result = await db.execute(query)
    batches = result.scalars().all()

    out = []
    for b in batches:
        q = await db.get(ReviewQueue, b.queue_id)
        out.append(ReviewBatchOut(
            id=b.id, queue_id=b.queue_id, queue_name=q.name if q else "",
            reviewer_id=b.reviewer_id, status=b.status,
            size=b.size, reviewed_count=b.reviewed_count,
            assigned_at=b.assigned_at, completed_at=b.completed_at,
            created_at=b.created_at,
        ))
    return out


@router.get("/{batch_id}", response_model=ReviewBatchOut)
async def get_batch(
    batch_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    batch = await db.get(ReviewBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    queue = await db.get(ReviewQueue, batch.queue_id)
    await get_user_role_for_production(db, user, queue.production_id)

    reviewer = await db.get(User, batch.reviewer_id) if batch.reviewer_id else None
    return ReviewBatchOut(
        id=batch.id, queue_id=batch.queue_id, queue_name=queue.name,
        reviewer_id=batch.reviewer_id,
        reviewer_email=reviewer.email if reviewer else None,
        status=batch.status, size=batch.size, reviewed_count=batch.reviewed_count,
        assigned_at=batch.assigned_at, completed_at=batch.completed_at,
        created_at=batch.created_at,
    )


@router.post("/{batch_id}/assign", response_model=ReviewBatchOut)
async def assign_batch(
    batch_id: int, body: BatchAssignRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    batch = await db.get(ReviewBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    queue = await db.get(ReviewQueue, batch.queue_id)
    role = await get_user_role_for_production(db, user, queue.production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or admin role required")

    batch.reviewer_id = body.reviewer_id
    batch.status = "in_progress"
    batch.assigned_at = func.now()
    await log_action(db, user, "batch_assigned", "batch", str(batch_id),
                     production_id=queue.production_id,
                     details={"reviewer_id": body.reviewer_id})
    await db.commit()
    await db.refresh(batch)

    reviewer = await db.get(User, batch.reviewer_id) if batch.reviewer_id else None
    return ReviewBatchOut(
        id=batch.id, queue_id=batch.queue_id, queue_name=queue.name,
        reviewer_id=batch.reviewer_id,
        reviewer_email=reviewer.email if reviewer else None,
        status=batch.status, size=batch.size, reviewed_count=batch.reviewed_count,
        assigned_at=batch.assigned_at, completed_at=batch.completed_at,
        created_at=batch.created_at,
    )


@router.get("/{batch_id}/documents", response_model=list[BatchDocumentOut])
async def list_batch_documents(
    batch_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    batch = await db.get(ReviewBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    queue = await db.get(ReviewQueue, batch.queue_id)
    await get_user_role_for_production(db, user, queue.production_id)

    # Only the assigned reviewer or a manager+ can see batch documents
    role = await get_user_role_for_production(db, user, queue.production_id)
    if batch.reviewer_id != user.id and ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Not your batch")

    result = await db.execute(
        select(BatchDocument, Document.bates_begin, Document.title)
        .join(Document, BatchDocument.document_id == Document.id)
        .where(BatchDocument.batch_id == batch_id)
        .order_by(BatchDocument.position)
    )
    rows = result.all()

    return [
        BatchDocumentOut(
            id=bd.id, batch_id=bd.batch_id, document_id=bd.document_id,
            position=bd.position, reviewed=bd.reviewed, reviewed_at=bd.reviewed_at,
            bates_begin=bates, title=title,
        )
        for bd, bates, title in rows
    ]


@router.put("/{batch_id}/documents/{doc_id}", response_model=BatchDocumentOut)
async def update_batch_document(
    batch_id: int, doc_id: str,
    body: BatchDocumentUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Mark a document as reviewed or skipped within a batch."""
    batch = await db.get(ReviewBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    if batch.reviewer_id != user.id:
        queue = await db.get(ReviewQueue, batch.queue_id)
        role = await get_user_role_for_production(db, user, queue.production_id)
        if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
            raise HTTPException(status_code=403, detail="Not your batch")

    result = await db.execute(
        select(BatchDocument).where(
            BatchDocument.batch_id == batch_id,
            BatchDocument.document_id == doc_id,
        )
    )
    bd = result.scalar_one_or_none()
    if not bd:
        raise HTTPException(status_code=404, detail="Document not in this batch")

    was_pending = bd.reviewed == "pending"
    bd.reviewed = body.reviewed
    bd.reviewed_at = datetime.utcnow()

    # Update batch reviewed_count
    next_batch_id = None
    if was_pending and body.reviewed in ("reviewed", "skipped"):
        batch.reviewed_count += 1
        if batch.reviewed_count >= batch.size:
            batch.status = "completed"
            batch.completed_at = datetime.utcnow()

            # Auto-assign next pending batch in same queue to this reviewer
            next_result = await db.execute(
                select(ReviewBatch)
                .where(ReviewBatch.queue_id == batch.queue_id)
                .where(ReviewBatch.status == "pending")
                .where(ReviewBatch.reviewer_id.is_(None))
                .order_by(ReviewBatch.created_at)
                .limit(1)
            )
            next_batch = next_result.scalar_one_or_none()
            if next_batch and batch.reviewer_id:
                next_batch.reviewer_id = batch.reviewer_id
                next_batch.status = "in_progress"
                next_batch.assigned_at = func.now()
                next_batch_id = next_batch.id

    queue = await db.get(ReviewQueue, batch.queue_id)
    await log_action(db, user, "document_reviewed", "batch_document", str(bd.id),
                     production_id=queue.production_id,
                     details={"batch_id": batch_id, "status": body.reviewed})
    await db.commit()

    doc = await db.get(Document, bd.document_id) if bd.document_id else None
    return {
        "id": bd.id, "batch_id": bd.batch_id, "document_id": str(bd.document_id),
        "position": bd.position, "reviewed": bd.reviewed,
        "reviewed_at": bd.reviewed_at.isoformat() if bd.reviewed_at else None,
        "bates_begin": doc.bates_begin if doc else "", "title": doc.title if doc else None,
        "next_batch_id": next_batch_id,  # non-null if auto-assigned
    }
```

- [ ] **Step 2: Register in main.py**

Add `batches` to the imports and include the router.

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "feat: add batch assignment, document list, and review status endpoints"
```

---

## Task 6: Create Dashboard Router

**Files:**
- Create: `backend/app/routers/dashboard.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Create dashboard router**

Create `backend/app/routers/dashboard.py`:

```python
from fastapi import APIRouter, Depends
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_user_role_for_production
from app.models import (
    BatchDocument, Document, DocumentTag, QCDecision, ReviewBatch, ReviewQueue, Tag, User,
)
from app.routers.auth import get_current_user
from app.schemas import DashboardStats, QCStats

router = APIRouter(prefix="/api/productions/{production_id}/dashboard", tags=["dashboard"])


@router.get("", response_model=DashboardStats)
async def get_dashboard(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await get_user_role_for_production(db, user, production_id)

    # Total documents
    total = (await db.execute(
        select(func.count(Document.id)).where(Document.production_id == production_id)
    )).scalar() or 0

    # Reviewed documents (documents in completed batches)
    reviewed = (await db.execute(
        select(func.count(func.distinct(BatchDocument.document_id)))
        .join(ReviewBatch, BatchDocument.batch_id == ReviewBatch.id)
        .join(ReviewQueue, ReviewBatch.queue_id == ReviewQueue.id)
        .where(ReviewQueue.production_id == production_id)
        .where(BatchDocument.reviewed == "reviewed")
    )).scalar() or 0

    # Tag breakdown
    tag_result = await db.execute(
        select(Tag.category, Tag.name, func.count(DocumentTag.id))
        .join(DocumentTag, Tag.id == DocumentTag.tag_id)
        .join(Document, DocumentTag.document_id == Document.id)
        .where(Document.production_id == production_id)
        .group_by(Tag.category, Tag.name)
    )
    tag_breakdown: dict[str, dict[str, int]] = {}
    for category, name, count in tag_result.all():
        if category not in tag_breakdown:
            tag_breakdown[category] = {}
        tag_breakdown[category][name] = count

    # Reviewer stats
    reviewer_result = await db.execute(
        select(
            ReviewBatch.reviewer_id,
            User.email,
            func.sum(ReviewBatch.reviewed_count),
        )
        .join(User, ReviewBatch.reviewer_id == User.id)
        .join(ReviewQueue, ReviewBatch.queue_id == ReviewQueue.id)
        .where(ReviewQueue.production_id == production_id)
        .where(ReviewBatch.reviewer_id.isnot(None))
        .group_by(ReviewBatch.reviewer_id, User.email)
    )
    reviewer_stats = [
        {"user_id": uid, "email": email, "reviewed_count": int(count or 0)}
        for uid, email, count in reviewer_result.all()
    ]

    # Queue stats
    queue_result = await db.execute(
        select(
            ReviewQueue.id, ReviewQueue.name,
            func.coalesce(func.sum(ReviewBatch.size), 0),
            func.coalesce(func.sum(ReviewBatch.reviewed_count), 0),
            func.count(ReviewBatch.id),
        )
        .outerjoin(ReviewBatch, ReviewQueue.id == ReviewBatch.queue_id)
        .where(ReviewQueue.production_id == production_id)
        .group_by(ReviewQueue.id, ReviewQueue.name)
    )
    queue_stats = [
        {"queue_id": qid, "name": name, "total": int(total_q), "reviewed": int(rev), "batch_count": int(bc)}
        for qid, name, total_q, rev, bc in queue_result.all()
    ]

    return DashboardStats(
        total_documents=total,
        reviewed_documents=reviewed,
        pending_documents=total - reviewed,
        percent_complete=round(reviewed / total * 100, 1) if total > 0 else 0,
        tag_breakdown=tag_breakdown,
        reviewer_stats=reviewer_stats,
        queue_stats=queue_stats,
    )


@router.get("/qc", response_model=QCStats)
async def get_qc_stats(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await get_user_role_for_production(db, user, production_id)

    result = await db.execute(
        select(
            func.count(QCDecision.id),
            func.sum(case((QCDecision.decision == "agree", 1), else_=0)),
            func.sum(case((QCDecision.decision == "overturn", 1), else_=0)),
        )
        .join(BatchDocument, QCDecision.batch_document_id == BatchDocument.id)
        .join(ReviewBatch, BatchDocument.batch_id == ReviewBatch.id)
        .join(ReviewQueue, ReviewBatch.queue_id == ReviewQueue.id)
        .where(ReviewQueue.production_id == production_id)
    )
    total_qc, agree, overturn = result.one()
    total_qc = int(total_qc or 0)
    agree = int(agree or 0)
    overturn = int(overturn or 0)

    # By original reviewer
    by_reviewer_result = await db.execute(
        select(
            QCDecision.original_reviewer_id,
            User.email,
            func.count(QCDecision.id),
            func.sum(case((QCDecision.decision == "overturn", 1), else_=0)),
        )
        .join(User, QCDecision.original_reviewer_id == User.id)
        .join(BatchDocument, QCDecision.batch_document_id == BatchDocument.id)
        .join(ReviewBatch, BatchDocument.batch_id == ReviewBatch.id)
        .join(ReviewQueue, ReviewBatch.queue_id == ReviewQueue.id)
        .where(ReviewQueue.production_id == production_id)
        .group_by(QCDecision.original_reviewer_id, User.email)
    )
    by_reviewer = [
        {
            "reviewer_id": uid, "email": email,
            "total": int(t), "overturns": int(o or 0),
            "overturn_rate": round(int(o or 0) / int(t) * 100, 1) if int(t) > 0 else 0,
        }
        for uid, email, t, o in by_reviewer_result.all()
    ]

    return QCStats(
        total_decisions=total_qc,
        agree_count=agree,
        overturn_count=overturn,
        overturn_rate=round(overturn / total_qc * 100, 1) if total_qc > 0 else 0,
        by_reviewer=by_reviewer,
    )
```

- [ ] **Step 2: Add reviewer agreement endpoint**

Add to `backend/app/routers/dashboard.py` — this compares coding decisions between two reviewers on documents they both reviewed (simple agreement rate):

```python
@router.get("/agreement")
async def get_reviewer_agreement(
    production_id: int,
    reviewer_a: str,
    reviewer_b: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Compare tag agreement between two reviewers on overlapping documents."""
    role = await get_user_role_for_production(db, user, production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or admin role required")

    # Find documents reviewed by both reviewers (via their batches)
    from sqlalchemy import alias
    bd_a = alias(BatchDocument.__table__, name="bd_a")
    bd_b = alias(BatchDocument.__table__, name="bd_b")
    batch_a = alias(ReviewBatch.__table__, name="batch_a")
    batch_b = alias(ReviewBatch.__table__, name="batch_b")

    # Get doc IDs reviewed by reviewer_a
    docs_a = await db.execute(
        select(BatchDocument.document_id)
        .join(ReviewBatch, BatchDocument.batch_id == ReviewBatch.id)
        .join(ReviewQueue, ReviewBatch.queue_id == ReviewQueue.id)
        .where(ReviewQueue.production_id == production_id)
        .where(ReviewBatch.reviewer_id == reviewer_a)
        .where(BatchDocument.reviewed == "reviewed")
    )
    a_doc_ids = {row[0] for row in docs_a.all()}

    docs_b = await db.execute(
        select(BatchDocument.document_id)
        .join(ReviewBatch, BatchDocument.batch_id == ReviewBatch.id)
        .join(ReviewQueue, ReviewBatch.queue_id == ReviewQueue.id)
        .where(ReviewQueue.production_id == production_id)
        .where(ReviewBatch.reviewer_id == reviewer_b)
        .where(BatchDocument.reviewed == "reviewed")
    )
    b_doc_ids = {row[0] for row in docs_b.all()}

    overlap = a_doc_ids & b_doc_ids
    if not overlap:
        return {"overlap_count": 0, "agreement_rate": None, "details": []}

    # Compare tags on overlapping documents
    agree = 0
    details = []
    for doc_id in overlap:
        tags_a = await db.execute(
            select(DocumentTag.tag_id).where(DocumentTag.document_id == doc_id)
            .where(DocumentTag.applied_by == reviewer_a)
        )
        tags_b = await db.execute(
            select(DocumentTag.tag_id).where(DocumentTag.document_id == doc_id)
            .where(DocumentTag.applied_by == reviewer_b)
        )
        set_a = {r[0] for r in tags_a.all()}
        set_b = {r[0] for r in tags_b.all()}
        match = set_a == set_b
        if match:
            agree += 1
        details.append({"document_id": str(doc_id), "agree": match})

    return {
        "overlap_count": len(overlap),
        "agreement_rate": round(agree / len(overlap) * 100, 1),
        "agree_count": agree,
        "disagree_count": len(overlap) - agree,
        "details": details[:100],  # cap detail output
    }
```

Import `HTTPException` from fastapi and `ROLE_RANK` from dependencies (should already be imported).

- [ ] **Step 3: Register in main.py**

Add `dashboard` to imports and include the router.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "feat: add review progress dashboard, QC stats, and reviewer agreement endpoints"
```

---

## Task 7: Create QC Router

**Files:**
- Create: `backend/app/routers/qc.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Create QC router**

Create `backend/app/routers/qc.py`:

```python
import random

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_user_role_for_production, ROLE_RANK
from app.models import (
    BatchDocument, Document, DocumentTag, QCDecision, ReviewBatch, ReviewQueue, Tag, User,
)
from app.routers.auth import get_current_user
from app.schemas import QCDecisionCreate, QCDecisionOut, QCSampleRequest
from app.services.audit import log_action

router = APIRouter(prefix="/api/qc", tags=["qc"])


@router.post("/sample", response_model=list[int])
async def create_qc_sample(
    body: QCSampleRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Select a random sample of reviewed batch documents for QC."""
    queue = await db.get(ReviewQueue, body.queue_id)
    if not queue:
        raise HTTPException(status_code=404, detail="Queue not found")

    role = await get_user_role_for_production(db, user, queue.production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or admin role required")

    # Get reviewed batch documents not yet QC'd
    query = (
        select(BatchDocument.id)
        .join(ReviewBatch, BatchDocument.batch_id == ReviewBatch.id)
        .where(ReviewBatch.queue_id == body.queue_id)
        .where(BatchDocument.reviewed == "reviewed")
    )
    if body.reviewer_id:
        query = query.where(ReviewBatch.reviewer_id == body.reviewer_id)

    # Exclude already QC'd
    already_qcd = select(QCDecision.batch_document_id)
    query = query.where(BatchDocument.id.notin_(already_qcd))

    result = await db.execute(query)
    eligible_ids = [row[0] for row in result.all()]

    sample_size = max(1, int(len(eligible_ids) * body.sample_percent / 100))
    sample = random.sample(eligible_ids, min(sample_size, len(eligible_ids)))

    await log_action(db, user, "qc_sample_created", "queue", str(body.queue_id),
                     production_id=queue.production_id,
                     details={"sample_size": len(sample), "percent": body.sample_percent})
    await db.commit()

    return sample


@router.get("/batch-document/{bd_id}", response_model=QCDecisionOut | dict)
async def get_qc_context(
    bd_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get QC context for a batch document: original reviewer, their tags, document info."""
    bd = await db.get(BatchDocument, bd_id)
    if not bd:
        raise HTTPException(status_code=404, detail="Batch document not found")

    batch = await db.get(ReviewBatch, bd.batch_id)
    queue = await db.get(ReviewQueue, batch.queue_id)
    await get_user_role_for_production(db, user, queue.production_id)

    doc = await db.get(Document, bd.document_id)
    reviewer = await db.get(User, batch.reviewer_id) if batch.reviewer_id else None

    # Get current tags on the document
    tag_result = await db.execute(
        select(Tag.id, Tag.name, Tag.category)
        .join(DocumentTag, Tag.id == DocumentTag.tag_id)
        .where(DocumentTag.document_id == bd.document_id)
    )
    current_tags = [{"id": t_id, "name": t_name, "category": t_cat} for t_id, t_name, t_cat in tag_result.all()]

    # Check if already QC'd
    existing = await db.execute(
        select(QCDecision).where(QCDecision.batch_document_id == bd_id)
    )
    existing_decision = existing.scalar_one_or_none()

    return {
        "batch_document_id": bd_id,
        "document_id": str(bd.document_id),
        "bates_begin": doc.bates_begin if doc else "",
        "title": doc.title if doc else None,
        "original_reviewer_id": batch.reviewer_id,
        "original_reviewer_email": reviewer.email if reviewer else None,
        "current_tags": current_tags,
        "existing_decision": {
            "id": existing_decision.id,
            "decision": existing_decision.decision,
            "reason": existing_decision.reason,
            "created_at": existing_decision.created_at.isoformat(),
        } if existing_decision else None,
    }


@router.post("/batch-document/{bd_id}/decide", response_model=QCDecisionOut)
async def record_qc_decision(
    bd_id: int,
    body: QCDecisionCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    bd = await db.get(BatchDocument, bd_id)
    if not bd:
        raise HTTPException(status_code=404, detail="Batch document not found")

    batch = await db.get(ReviewBatch, bd.batch_id)
    queue = await db.get(ReviewQueue, batch.queue_id)
    role = await get_user_role_for_production(db, user, queue.production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or admin role required for QC")

    if body.decision not in ("agree", "overturn"):
        raise HTTPException(status_code=400, detail="Decision must be 'agree' or 'overturn'")
    if body.decision == "overturn" and not body.reason:
        raise HTTPException(status_code=400, detail="Reason required for overturn")

    # Get current tags snapshot
    tag_result = await db.execute(
        select(Tag.id, Tag.name, Tag.category)
        .join(DocumentTag, Tag.id == DocumentTag.tag_id)
        .where(DocumentTag.document_id == bd.document_id)
    )
    original_tags = [{"id": t_id, "name": t_name, "category": t_cat} for t_id, t_name, t_cat in tag_result.all()]

    decision = QCDecision(
        batch_document_id=bd_id,
        original_reviewer_id=batch.reviewer_id,
        qc_reviewer_id=user.id,
        decision=body.decision,
        reason=body.reason,
        original_tags=original_tags,
        new_tags=[{"id": tid} for tid in body.new_tag_ids] if body.new_tag_ids else None,
    )
    db.add(decision)

    # If overturn with new tags, update the document's tags
    if body.decision == "overturn" and body.new_tag_ids is not None:
        # Remove existing tags
        await db.execute(
            DocumentTag.__table__.delete().where(DocumentTag.document_id == bd.document_id)
        )
        # Apply new tags
        for tag_id in body.new_tag_ids:
            dt = DocumentTag(
                document_id=bd.document_id,
                tag_id=tag_id,
                applied_by=user.id,
            )
            db.add(dt)

    doc = await db.get(Document, bd.document_id)
    await log_action(db, user, "qc_decision", "batch_document", str(bd_id),
                     production_id=queue.production_id,
                     details={"decision": body.decision, "document_id": str(bd.document_id)})
    await db.commit()
    await db.refresh(decision)

    qc_user = await db.get(User, decision.qc_reviewer_id)
    orig_user = await db.get(User, decision.original_reviewer_id) if decision.original_reviewer_id else None

    return QCDecisionOut(
        id=decision.id, batch_document_id=decision.batch_document_id,
        original_reviewer_id=decision.original_reviewer_id,
        original_reviewer_email=orig_user.email if orig_user else "",
        qc_reviewer_id=decision.qc_reviewer_id,
        qc_reviewer_email=qc_user.email if qc_user else "",
        decision=decision.decision, reason=decision.reason,
        original_tags=decision.original_tags, new_tags=decision.new_tags,
        created_at=decision.created_at,
        bates_begin=doc.bates_begin if doc else "",
    )
```

- [ ] **Step 2: Register in main.py**

Add `qc` to imports and include the router.

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "feat: add QC sampling, context, and decision endpoints"
```

---

## Task 8: Frontend Types and API Client

**Files:**
- Modify: `frontend/src/types/index.ts`
- Modify: `frontend/src/api/client.ts`

- [ ] **Step 1: Add types**

Add to `frontend/src/types/index.ts`:

```typescript
// ── Review Queues & Batches ──

export interface ReviewQueue {
  id: number;
  production_id: number;
  name: string;
  description: string | null;
  query: string;
  filters: Record<string, unknown>;
  status: string;
  created_by: string;
  created_at: string;
  batch_count: number;
  total_documents: number;
  reviewed_documents: number;
}

export interface ReviewBatch {
  id: number;
  queue_id: number;
  queue_name: string;
  reviewer_id: string | null;
  reviewer_email: string | null;
  status: string;
  size: number;
  reviewed_count: number;
  assigned_at: string | null;
  completed_at: string | null;
  created_at: string;
}

export interface BatchDocument {
  id: number;
  batch_id: number;
  document_id: string;
  position: number;
  reviewed: string;
  reviewed_at: string | null;
  bates_begin: string;
  title: string | null;
}

export interface DashboardStats {
  total_documents: number;
  reviewed_documents: number;
  pending_documents: number;
  percent_complete: number;
  tag_breakdown: Record<string, Record<string, number>>;
  reviewer_stats: { user_id: string; email: string; reviewed_count: number }[];
  queue_stats: { queue_id: number; name: string; total: number; reviewed: number; batch_count: number }[];
}

export interface QCStats {
  total_decisions: number;
  agree_count: number;
  overturn_count: number;
  overturn_rate: number;
  by_reviewer: { reviewer_id: string; email: string; total: number; overturns: number; overturn_rate: number }[];
}

export interface QCContext {
  batch_document_id: number;
  document_id: string;
  bates_begin: string;
  title: string | null;
  original_reviewer_id: string;
  original_reviewer_email: string | null;
  current_tags: { id: number; name: string; category: string }[];
  existing_decision: { id: number; decision: string; reason: string | null; created_at: string } | null;
}
```

- [ ] **Step 2: Add API functions**

Add to `frontend/src/api/client.ts`:

```typescript
// ── Review Queues ──

export async function listQueues(productionId: number): Promise<ReviewQueue[]> {
  return request<ReviewQueue[]>(`/api/productions/${productionId}/queues`);
}

export async function createQueue(productionId: number, name: string, description = '', query = '', filters: Record<string, unknown> = {}): Promise<ReviewQueue> {
  return request<ReviewQueue>(`/api/productions/${productionId}/queues`, json({ name, description, query, filters }));
}

export async function deleteQueue(productionId: number, queueId: number): Promise<void> {
  await request(`/api/productions/${productionId}/queues/${queueId}`, { method: 'DELETE' });
}

export async function createBatches(productionId: number, queueId: number, batchSize = 50, reviewerId?: string): Promise<ReviewBatch[]> {
  return request<ReviewBatch[]>(`/api/productions/${productionId}/queues/${queueId}/batches`, json({ batch_size: batchSize, reviewer_id: reviewerId }));
}

// ── Batches ──

export async function getMyBatches(productionId?: number): Promise<ReviewBatch[]> {
  const params = new URLSearchParams();
  if (productionId) params.set('production_id', String(productionId));
  return request<ReviewBatch[]>(`/api/batches/my?${params}`);
}

export async function getBatch(batchId: number): Promise<ReviewBatch> {
  return request<ReviewBatch>(`/api/batches/${batchId}`);
}

export async function assignBatch(batchId: number, reviewerId: string): Promise<ReviewBatch> {
  return request<ReviewBatch>(`/api/batches/${batchId}/assign`, json({ reviewer_id: reviewerId }));
}

export async function listBatchDocuments(batchId: number): Promise<BatchDocument[]> {
  return request<BatchDocument[]>(`/api/batches/${batchId}/documents`);
}

export async function updateBatchDocument(batchId: number, docId: string, reviewed: string): Promise<BatchDocument> {
  return request<BatchDocument>(`/api/batches/${batchId}/documents/${docId}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ reviewed }) });
}

// ── Dashboard ──

export async function getDashboard(productionId: number): Promise<DashboardStats> {
  return request<DashboardStats>(`/api/productions/${productionId}/dashboard`);
}

export async function getQCStats(productionId: number): Promise<QCStats> {
  return request<QCStats>(`/api/productions/${productionId}/dashboard/qc`);
}

// ── QC ──

export async function createQCSample(queueId: number, samplePercent = 10, reviewerId?: string): Promise<number[]> {
  return request<number[]>('/api/qc/sample', json({ queue_id: queueId, sample_percent: samplePercent, reviewer_id: reviewerId }));
}

export async function getQCContext(bdId: number): Promise<QCContext> {
  return request<QCContext>(`/api/qc/batch-document/${bdId}`);
}

export async function recordQCDecision(bdId: number, decision: string, reason?: string, newTagIds?: number[]): Promise<unknown> {
  return request(`/api/qc/batch-document/${bdId}/decide`, json({ decision, reason, new_tag_ids: newTagIds }));
}
```

Add the new types to the import line at the top of `client.ts`. The existing import from `'../types'` should be extended to include:
```typescript
import type { ReviewQueue, ReviewBatch, BatchDocument, DashboardStats, QCStats, QCContext, PaginatedAuditLogs, ... } from '../types';
```
(Add to the existing import statement, don't create a new one.)

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "feat: add frontend types and API client for queues, batches, QC, dashboard"
```

---

## Task 9: Frontend — Queue Manager Component

**Files:**
- Create: `frontend/src/components/QueueManager.tsx`
- Modify: `frontend/src/App.tsx`

- [ ] **Step 1: Create QueueManager component**

Create `frontend/src/components/QueueManager.tsx` — a modal/panel that shows:
- List of queues with name, status, progress bar (reviewed/total), batch count
- "Create Queue" form with name, optional search query, optional metadata filters
- Per-queue actions: "Create Batches" (with batch size input), "Delete"
- Per-queue batch list: show batches with reviewer assignment dropdown and progress
- Assign batch button that calls `assignBatch`

The component takes `productionId: number` and `onClose: () => void` props.

Use the same modal styling pattern as `ManageAccess.tsx` and `AuditLog.tsx` (`.modal-overlay`, `.modal-content`).

Key UI elements:
- Queue list as cards with progress bars
- Inline "Create Queue" form at top
- Expandable batch list per queue
- Reviewer dropdown (from `getProductionAccess` to get user list) for batch assignment
- Batch size input (default 50) when creating batches

- [ ] **Step 2: Wire into App.tsx**

In `Home` component:
- Add state: `const [showQueueManager, setShowQueueManager] = useState(false);`
- Import `QueueManager`
- Add button in the header toolbar: `<button onClick={() => setShowQueueManager(true)}>Review Queues</button>`
- Render: `{showQueueManager && <QueueManager productionId={production.id} onClose={() => setShowQueueManager(false)} />}`

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "feat: add queue manager UI with batch creation and assignment"
```

---

## Task 10: Frontend — Batch Review Component

**Files:**
- Create: `frontend/src/components/BatchReview.tsx`
- Modify: `frontend/src/App.tsx`

- [ ] **Step 1: Create BatchReview component**

Create `frontend/src/components/BatchReview.tsx` — the reviewer's batch view:

Props: `batchId: number`, `onClose: () => void`, `onComplete: () => void`

Features:
- Loads batch info and document list via `getBatch` and `listBatchDocuments`
- Shows progress: "Document X of Y" with progress bar
- Document list sidebar showing all batch documents with reviewed/pending status indicators
- Click a document to view it (renders `DocumentViewer` inline)
- "Mark Reviewed" button calls `updateBatchDocument(batchId, docId, "reviewed")`
- "Skip" button calls `updateBatchDocument(batchId, docId, "skipped")`
- Auto-advances to next pending document after marking reviewed
- When all documents reviewed, shows completion message and calls `onComplete`

- [ ] **Step 2: Add "My Batches" section to App.tsx**

In `Home` component:
- Add state: `const [activeBatchId, setActiveBatchId] = useState<number | null>(null);`
- Add state: `const [myBatches, setMyBatches] = useState<ReviewBatch[]>([]);`
- Load batches on mount: `getMyBatches(production.id).then(setMyBatches)`
- Show a "My Batches" section above the document list when there are assigned batches:
  ```tsx
  {myBatches.length > 0 && (
    <div className="my-batches">
      <h3>My Review Batches</h3>
      {myBatches.map(b => (
        <div key={b.id} className="batch-card" onClick={() => setActiveBatchId(b.id)}>
          <span>{b.queue_name}</span>
          <span>{b.reviewed_count}/{b.size}</span>
          <progress value={b.reviewed_count} max={b.size} />
        </div>
      ))}
    </div>
  )}
  ```
- When `activeBatchId` is set, render `<BatchReview>` instead of the normal view

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "feat: add batch review UI with progress tracking and auto-advance"
```

---

## Task 11: Frontend — Dashboard Component

**Files:**
- Create: `frontend/src/components/Dashboard.tsx`
- Modify: `frontend/src/App.tsx`

- [ ] **Step 1: Create Dashboard component**

Create `frontend/src/components/Dashboard.tsx`:

Props: `productionId: number`, `onClose: () => void`

Features:
- Loads `getDashboard` and `getQCStats` on mount
- Shows:
  - Overall progress: big percentage with progress bar, total/reviewed/pending counts
  - Queue breakdown table: name, total docs, reviewed, batch count, percent
  - Reviewer stats table: email, documents reviewed, (optionally docs/hour if time data available)
  - Tag distribution: for each category, show tag names with counts (bar chart or simple table)
  - QC section: total decisions, agree/overturn counts, overturn rate percentage
  - QC by-reviewer table: reviewer email, total QC'd, overturns, overturn rate
- Use the modal-large pattern
- Refresh button to reload stats

- [ ] **Step 2: Wire into App.tsx**

Add "Dashboard" button in header (for manager+ users — gate on `production.is_owner` for now):
```tsx
<button onClick={() => setShowDashboard(true)}>Dashboard</button>
```

Add state and render the modal.

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "feat: add review progress dashboard with QC stats"
```

---

## Task 12: Frontend — QC Review Component

**Files:**
- Create: `frontend/src/components/QCReview.tsx`
- Modify: `frontend/src/App.tsx`

- [ ] **Step 1: Create QCReview component**

Create `frontend/src/components/QCReview.tsx`:

Props: `sampleIds: number[]`, `productionId: number`, `onClose: () => void`

Features:
- Steps through each batch_document_id in `sampleIds`
- For each, loads `getQCContext` to show:
  - Document info (bates, title)
  - Original reviewer email
  - Current tags applied by the original reviewer
  - The actual document (embed `DocumentViewer` to view the doc)
- Two buttons: "Agree" and "Overturn"
- If "Overturn" is clicked, show a reason text field and tag selector for new tags
- Calls `recordQCDecision`
- Auto-advances to next sample document
- Shows progress: "QC Review: X of Y"

- [ ] **Step 2: Add QC trigger to QueueManager**

In `QueueManager.tsx`, add a "Start QC" button per queue (for manager+) that:
1. Opens a modal to configure sample percent and optional reviewer filter
2. Calls `createQCSample` to get sample IDs
3. Opens `QCReview` with those IDs

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "feat: add QC review interface with agree/overturn workflow"
```

---

## Task 13: Register All Routers and Integration Test

**Files:**
- Modify: `backend/app/main.py`

- [ ] **Step 1: Ensure all routers are registered**

Verify `backend/app/main.py` imports and includes all new routers:
```python
from app.routers import ai, audit, auth, batches, dashboard, documents, export, ingest, notes, productions, qc, queues, saved_searches, search, tags
```

All routers should be included via `app.include_router(...)`.

- [ ] **Step 2: Run backend import check**

```bash
cd backend && ./venv/Scripts/python -c "from app.main import app; print('OK')"
```

- [ ] **Step 3: Run frontend type check**

```bash
cd frontend && npx tsc --noEmit
```

- [ ] **Step 4: Manual verification checklist**

1. Create a review queue → appears in queue list with 0 batches
2. Create batches from queue → batches appear with correct size
3. Assign batch to reviewer → batch shows reviewer email, status "in_progress"
4. Load "My Batches" as the assigned reviewer → see the batch
5. Open batch review → see document list with progress
6. Mark document as reviewed → progress updates, auto-advance works
7. Complete all documents → batch status changes to "completed"
8. Open Dashboard → see overall progress, tag breakdown, reviewer stats
9. Create QC sample → get list of batch document IDs
10. Start QC review → see original reviewer's tags, agree/overturn works
11. Check QC stats → overturn rate displays correctly
12. Readonly user cannot create queues (403)
13. Reviewer cannot create queues (403) — only manager+

- [ ] **Step 5: Commit any fixes**

```bash
git add -A
git commit -m "fix: integration fixes for phase 4 review management"
```
