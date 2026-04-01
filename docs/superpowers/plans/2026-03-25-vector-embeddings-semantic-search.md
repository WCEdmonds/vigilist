# Vector Embeddings & Semantic Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add vector embeddings to the document review platform, enabling semantic search, improved find-similar, and NL search via Voyage AI + pgvector.

**Architecture:** Documents are chunked into ~500-word segments with 50-word overlap. Each chunk is embedded via Voyage AI `voyage-3-lite` (1024 dims) and stored in a pgvector-indexed `document_chunks` table. Semantic search embeds the query and finds nearest-neighbor chunks, grouping by document. Existing find-similar and NL search are upgraded to use embeddings with fallback to current behavior.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy async, pgvector, Voyage AI, PostgreSQL (Neon), React 18 + TypeScript

**Spec:** `docs/superpowers/specs/2026-03-25-vector-embeddings-semantic-search-design.md`

---

## File Structure

| Action | Path | Responsibility |
|--------|------|---------------|
| Create | `backend/app/services/chunking.py` | Text chunking (pure function) |
| Create | `backend/app/services/embeddings.py` | Voyage API wrapper + chunk-and-embed pipeline |
| Create | `backend/app/services/semantic_search.py` | Semantic search query logic |
| Create | `backend/tests/test_chunking.py` | Chunking unit tests |
| Create | `backend/tests/test_embeddings.py` | Embedding service unit tests |
| Create | `backend/tests/test_semantic_search.py` | Semantic search tests |
| Create | `backend/alembic/versions/g9b5c4d03e26_add_document_chunks.py` | Migration: pgvector + document_chunks table |
| Modify | `backend/app/models.py` | Add `DocumentChunk` model, `chunks` relationship on `Document` |
| Modify | `backend/app/config.py` | Add `voyage_api_key` setting |
| Modify | `backend/requirements.txt` | Add `voyageai`, `pgvector` |
| Modify | `backend/app/routers/search.py` | Add `mode` parameter, semantic search path |
| Modify | `backend/app/routers/ai.py` | Upgrade find-similar and NL search |
| Modify | `backend/app/routers/ingest.py` | Add embed-document handler, backfill endpoint, Phase B integration |
| Modify | `backend/app/services/tasks.py` | Add embed task enqueue helper |
| Modify | `frontend/src/api/client.ts` | Add `mode` parameter to `searchDocuments` |
| Modify | `frontend/src/App.tsx` | Add search mode toggle |

---

### Task 1: Dependencies and Config

**Files:**
- Modify: `backend/requirements.txt`
- Modify: `backend/app/config.py`

- [ ] **Step 1: Add Python dependencies**

In `backend/requirements.txt`, add:
```
voyageai>=0.3
pgvector>=0.3
```

- [ ] **Step 2: Add config setting**

In `backend/app/config.py`, add to the `Settings` class after `anthropic_api_key`:
```python
    # Voyage AI for vector embeddings
    voyage_api_key: str = ""
```

- [ ] **Step 3: Install locally**

Run: `cd backend && pip install voyageai pgvector`

- [ ] **Step 4: Commit**

```bash
git add backend/requirements.txt backend/app/config.py
git commit -m "feat: add voyageai and pgvector dependencies"
```

---

### Task 2: DocumentChunk Model and Migration

**Files:**
- Modify: `backend/app/models.py`
- Create: `backend/alembic/versions/g9b5c4d03e26_add_document_chunks.py`

- [ ] **Step 1: Add DocumentChunk model to models.py**

Add import at top:
```python
from pgvector.sqlalchemy import Vector
```

Add model class after `Document`:
```python
class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_chunk_doc_idx"),
        Index("ix_chunks_document_id", "document_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    chunk_index = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    embedding = Column(Vector(1024), nullable=False)

    document = relationship("Document", back_populates="chunks")
```

Add relationship on `Document` class:
```python
    chunks = relationship("DocumentChunk", back_populates="document", cascade="all, delete-orphan")
```

- [ ] **Step 2: Create migration file**

Create `backend/alembic/versions/g9b5c4d03e26_add_document_chunks.py`:
```python
"""add document_chunks table with pgvector

Revision ID: g9b5c4d03e26
Revises: f8a4b3c92d15
Create Date: 2026-03-25 21:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision: str = 'g9b5c4d03e26'
down_revision: Union[str, None] = 'f8a4b3c92d15'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS vector')
    op.create_table(
        'document_chunks',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('document_id', sa.UUID(as_uuid=True), sa.ForeignKey('documents.id', ondelete='CASCADE'), nullable=False),
        sa.Column('chunk_index', sa.Integer, nullable=False),
        sa.Column('content', sa.Text, nullable=False),
        sa.Column('embedding', Vector(1024), nullable=False),
        sa.UniqueConstraint('document_id', 'chunk_index', name='uq_chunk_doc_idx'),
    )
    op.create_index('ix_chunks_document_id', 'document_chunks', ['document_id'])
    op.execute("""
        CREATE INDEX ix_chunks_embedding_hnsw ON document_chunks
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)


def downgrade() -> None:
    op.drop_table('document_chunks')
```

- [ ] **Step 3: Run migration against Neon**

```bash
cd backend
VIGILIST_DATABASE_URL="postgresql+asyncpg://neondb_owner:REDACTED-DB-PASSWORD@ep-noisy-frog-a8h520r3-pooler.eastus2.azure.neon.tech/neondb" python -m alembic upgrade head
```

Note: The alembic env.py already handles SSL for Neon URLs.

- [ ] **Step 4: Commit**

```bash
git add backend/app/models.py backend/alembic/versions/g9b5c4d03e26_add_document_chunks.py
git commit -m "feat: add DocumentChunk model with pgvector"
```

---

### Task 3: Chunking Service

**Files:**
- Create: `backend/app/services/chunking.py`
- Create: `backend/tests/test_chunking.py`

- [ ] **Step 1: Write tests**

Create `backend/tests/__init__.py` (empty) and `backend/tests/test_chunking.py`:
```python
from app.services.chunking import chunk_text


def test_short_text_single_chunk():
    """Text shorter than chunk_size produces one chunk."""
    text = "Hello world this is a short document."
    chunks = chunk_text(text, chunk_size=500, overlap=50)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_empty_text():
    """Empty or whitespace-only text produces no chunks."""
    assert chunk_text("") == []
    assert chunk_text("   ") == []
    assert chunk_text(None) == []


def test_chunking_with_overlap():
    """Text is split into overlapping chunks."""
    words = [f"word{i}" for i in range(100)]
    text = " ".join(words)
    chunks = chunk_text(text, chunk_size=30, overlap=5)
    assert len(chunks) > 1
    # Each chunk should be <= chunk_size words (except possibly the last)
    for chunk in chunks[:-1]:
        assert len(chunk.split()) == 30
    # Overlap: end of chunk N overlaps with start of chunk N+1
    first_chunk_words = chunks[0].split()
    second_chunk_words = chunks[1].split()
    assert first_chunk_words[-5:] == second_chunk_words[:5]


def test_chunk_size_boundary():
    """Text exactly at chunk_size produces one chunk."""
    words = ["word"] * 500
    text = " ".join(words)
    chunks = chunk_text(text, chunk_size=500, overlap=50)
    assert len(chunks) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && python -m pytest tests/test_chunking.py -v
```
Expected: ImportError — `chunking` module doesn't exist yet.

- [ ] **Step 3: Implement chunking service**

Create `backend/app/services/chunking.py`:
```python
"""Text chunking for vector embeddings."""


def chunk_text(
    text: str | None,
    chunk_size: int = 500,
    overlap: int = 50,
) -> list[str]:
    """Split text into word-based chunks with overlap.

    Args:
        text: Document text to chunk.
        chunk_size: Target words per chunk.
        overlap: Words of overlap between consecutive chunks.

    Returns:
        List of chunk strings.
    """
    if not text or not text.strip():
        return []

    words = text.split()
    if len(words) <= chunk_size:
        return [text]

    chunks = []
    start = 0
    while start < len(words):
        end = start + chunk_size
        chunk = " ".join(words[start:end])
        chunks.append(chunk)
        if end >= len(words):
            break
        start = end - overlap

    return chunks
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd backend && python -m pytest tests/test_chunking.py -v
```
Expected: All 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/chunking.py backend/tests/
git commit -m "feat: add text chunking service with tests"
```

---

### Task 4: Embedding Service

**Files:**
- Create: `backend/app/services/embeddings.py`
- Create: `backend/tests/test_embeddings.py`

- [ ] **Step 1: Write tests**

Create `backend/tests/test_embeddings.py`:
```python
"""Tests for embedding service — uses mocks to avoid Voyage API calls."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.embeddings import embed_texts, embed_query


@pytest.fixture
def mock_voyage():
    with patch("app.services.embeddings._get_client") as mock:
        client = MagicMock()
        mock.return_value = client
        yield client


def test_embed_texts_batching(mock_voyage):
    """Texts are batched at 128 per API call."""
    # 200 texts should produce 2 batches (128 + 72)
    fake_embeddings = [[0.1] * 1024] * 200
    mock_voyage.embed.return_value = MagicMock(embeddings=fake_embeddings[:128])
    # Second call returns remaining
    mock_voyage.embed.side_effect = [
        MagicMock(embeddings=fake_embeddings[:128]),
        MagicMock(embeddings=fake_embeddings[:72]),
    ]
    texts = [f"text {i}" for i in range(200)]
    result = embed_texts(texts)
    assert len(result) == 200
    assert mock_voyage.embed.call_count == 2


def test_embed_texts_no_api_key():
    """Returns empty list when no API key configured."""
    with patch("app.services.embeddings._get_client", return_value=None):
        result = embed_texts(["hello"])
        assert result == []


def test_embed_query_no_api_key():
    """Returns empty list when no API key configured."""
    with patch("app.services.embeddings._get_client", return_value=None):
        result = embed_query("hello")
        assert result == []


def test_embed_query_uses_query_input_type(mock_voyage):
    """embed_query passes input_type='query'."""
    mock_voyage.embed.return_value = MagicMock(embeddings=[[0.1] * 1024])
    embed_query("test query")
    call_kwargs = mock_voyage.embed.call_args
    assert call_kwargs[1].get("input_type") == "query" or call_kwargs.kwargs.get("input_type") == "query"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && python -m pytest tests/test_embeddings.py -v
```
Expected: ImportError — `embeddings` module doesn't exist.

- [ ] **Step 3: Implement embedding service**

Create `backend/app/services/embeddings.py`:
```python
"""Voyage AI embedding service for vector search."""

import logging
import time

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.chunking import chunk_text

logger = logging.getLogger(__name__)

BATCH_SIZE = 128
MAX_RETRIES = 3


def _get_client():
    """Get Voyage AI client, or None if not configured."""
    if not settings.voyage_api_key:
        return None
    import voyageai
    return voyageai.Client(api_key=settings.voyage_api_key)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed document texts using Voyage AI.

    Batches at 128 texts per API call. Uses input_type='document'.
    Retries transient failures with exponential backoff.
    """
    client = _get_client()
    if not client or not texts:
        return []

    all_embeddings = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i:i + BATCH_SIZE]
        for attempt in range(MAX_RETRIES):
            try:
                result = client.embed(
                    batch,
                    model="voyage-3-lite",
                    input_type="document",
                )
                all_embeddings.extend(result.embeddings)
                break
            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    wait = 2 ** attempt
                    logger.warning("Voyage API error (attempt %d/%d), retrying in %ds: %s", attempt + 1, MAX_RETRIES, wait, e)
                    time.sleep(wait)
                else:
                    logger.error("Voyage API failed after %d attempts: %s", MAX_RETRIES, e)
                    raise

    return all_embeddings


def embed_query(query: str) -> list[float]:
    """Embed a search query using Voyage AI.

    Uses input_type='query' for asymmetric search.
    """
    client = _get_client()
    if not client or not query.strip():
        return []

    result = client.embed(
        [query],
        model="voyage-3-lite",
        input_type="query",
    )
    return result.embeddings[0]


async def chunk_and_embed_document(db: AsyncSession, doc_id: str) -> int:
    """Chunk a document's text and store embeddings.

    Idempotent: deletes existing chunks first.
    Returns number of chunks created.
    """
    from app.models import Document, DocumentChunk

    doc = await db.get(Document, doc_id)
    if not doc or not doc.text_content:
        return 0

    chunks = chunk_text(doc.text_content)
    if not chunks:
        return 0

    embeddings = embed_texts(chunks)
    if len(embeddings) != len(chunks):
        logger.error("Embedding count mismatch for doc %s: %d chunks, %d embeddings", doc_id, len(chunks), len(embeddings))
        return 0

    # Delete existing chunks (idempotent)
    await db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc.id))

    # Insert new chunks
    for i, (text, embedding) in enumerate(zip(chunks, embeddings)):
        chunk = DocumentChunk(
            document_id=doc.id,
            chunk_index=i,
            content=text,
            embedding=embedding,
        )
        db.add(chunk)

    await db.flush()
    logger.info("Embedded doc %s: %d chunks", doc_id, len(chunks))
    return len(chunks)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd backend && python -m pytest tests/test_embeddings.py -v
```
Expected: All 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/embeddings.py backend/tests/test_embeddings.py
git commit -m "feat: add Voyage AI embedding service with retry and batching"
```

---

### Task 5: Semantic Search Service

**Files:**
- Create: `backend/app/services/semantic_search.py`

- [ ] **Step 1: Implement semantic search**

Create `backend/app/services/semantic_search.py`:
```python
"""Semantic search using pgvector."""

import logging

from sqlalchemy import func, select, text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, DocumentChunk, DocumentTag
from app.services.embeddings import embed_query

logger = logging.getLogger(__name__)


async def semantic_search(
    db: AsyncSession,
    query: str,
    production_id: int | None = None,
    tag_ids: list[int] | None = None,
    page: int = 1,
    per_page: int = 50,
    accessible_production_ids: list[int] | None = None,
) -> tuple[list[dict], int]:
    """Search documents by semantic similarity.

    Embeds the query, finds nearest-neighbor chunks via pgvector,
    groups by document, and returns ranked results.
    """
    query_embedding = embed_query(query)
    if not query_embedding:
        return [], 0

    # Over-fetch chunks to ensure enough documents after grouping
    chunk_limit = per_page * 5

    # Build subquery: nearest chunks with cosine distance
    distance = DocumentChunk.embedding.cosine_distance(query_embedding).label("distance")

    chunk_q = (
        select(
            DocumentChunk.document_id,
            DocumentChunk.content,
            distance,
        )
        .join(Document, Document.id == DocumentChunk.document_id)
    )

    # Apply filters
    if accessible_production_ids is not None:
        chunk_q = chunk_q.where(Document.production_id.in_(accessible_production_ids))
    if production_id is not None:
        chunk_q = chunk_q.where(Document.production_id == production_id)
    if tag_ids:
        chunk_q = chunk_q.where(
            Document.id.in_(
                select(DocumentTag.document_id).where(DocumentTag.tag_id.in_(tag_ids))
            )
        )

    chunk_q = chunk_q.order_by(distance).limit(chunk_limit)

    rows = (await db.execute(chunk_q)).all()

    if not rows:
        return [], 0

    # Group by document, keep best (lowest distance) chunk per doc
    doc_best: dict[str, tuple[str, float]] = {}
    for doc_id, content, dist in rows:
        if doc_id not in doc_best or dist < doc_best[doc_id][1]:
            doc_best[doc_id] = (content, dist)

    # Sort by similarity (lowest distance first)
    sorted_docs = sorted(doc_best.items(), key=lambda x: x[1][1])

    total_approx = len(sorted_docs)

    # Paginate
    start = (page - 1) * per_page
    page_docs = sorted_docs[start:start + per_page]

    if not page_docs:
        return [], total_approx

    # Load document details
    doc_ids = [doc_id for doc_id, _ in page_docs]
    docs_result = await db.execute(
        select(Document).where(Document.id.in_(doc_ids))
    )
    docs_map = {str(d.id): d for d in docs_result.scalars().all()}

    results = []
    for doc_id, (snippet, dist) in page_docs:
        doc = docs_map.get(str(doc_id))
        if not doc:
            continue
        results.append({
            "id": doc.id,
            "production_id": doc.production_id,
            "bates_begin": doc.bates_begin,
            "bates_end": doc.bates_end,
            "page_count": doc.page_count,
            "title": doc.title,
            "snippet": snippet[:300],
            "rank": round(1.0 - dist, 4),  # Convert distance to similarity
        })

    return results, total_approx
```

- [ ] **Step 2: Commit**

```bash
git add backend/app/services/semantic_search.py
git commit -m "feat: add semantic search service using pgvector"
```

---

### Task 6: Search Router — Add Semantic Mode

**Files:**
- Modify: `backend/app/routers/search.py`

- [ ] **Step 1: Add mode parameter and semantic path**

Replace the search router at `backend/app/routers/search.py`:
```python
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User
from app.routers.auth import get_current_user
from app.dependencies import get_accessible_production_ids
from app.services.audit import log_action
from app.schemas import SearchResponse, SearchResult
from app.services.search import search_documents

router = APIRouter(prefix="/api", tags=["search"])


@router.get("/search", response_model=SearchResponse)
async def search(
    q: str = "",
    production_id: int | None = None,
    tag_ids: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    sort: str = Query("relevance", pattern="^(relevance|bates)$"),
    mode: str = Query("fulltext", pattern="^(fulltext|semantic)$"),
    metadata: str | None = Query(None, description="JSON object of metadata key-value filters"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    metadata_filters = None
    if metadata:
        try:
            metadata_filters = json.loads(metadata)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid metadata JSON")

    parsed_tag_ids = None
    if tag_ids:
        try:
            parsed_tag_ids = [int(t) for t in tag_ids.split(",")]
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid tag_ids")

    accessible = await get_accessible_production_ids(db, user)

    if mode == "semantic" and q:
        from app.services.semantic_search import semantic_search
        results, total = await semantic_search(
            db, q,
            production_id=production_id,
            tag_ids=parsed_tag_ids,
            page=page,
            per_page=per_page,
            accessible_production_ids=accessible,
        )
    else:
        results, total = await search_documents(
            db, q, production_id=production_id, page=page, per_page=per_page, sort=sort,
            accessible_production_ids=accessible,
            metadata_filters=metadata_filters,
        )

    await log_action(db, user, "search_executed", "search", None,
                     details={"query": q, "mode": mode, "result_count": total})
    await db.commit()
    return SearchResponse(
        results=[SearchResult(**r) for r in results],
        total=total,
        page=page,
        per_page=per_page,
    )
```

- [ ] **Step 2: Commit**

```bash
git add backend/app/routers/search.py
git commit -m "feat: add semantic search mode to search endpoint"
```

---

### Task 7: Upgrade Find Similar and NL Search

**Files:**
- Modify: `backend/app/routers/ai.py`

- [ ] **Step 1: Update find-similar endpoint**

In `backend/app/routers/ai.py`, replace the `find_similar_docs` endpoint implementation to try embeddings first, falling back to current behavior:

After the existing imports, add:
```python
from app.models import DocumentChunk
```

Replace the find-similar endpoint body with:
```python
@router.post("/ai/find-similar/{doc_id}")
async def find_similar_docs(
    doc_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Find semantically similar documents. Uses embeddings if available, falls back to Claude."""
    doc = await db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    # Try embedding-based similarity first
    from sqlalchemy import select
    chunk_count = (await db.execute(
        select(func.count(DocumentChunk.id)).where(DocumentChunk.document_id == doc.id)
    )).scalar() or 0

    if chunk_count > 0:
        from app.services.semantic_search import semantic_search
        from app.dependencies import get_accessible_production_ids
        accessible = await get_accessible_production_ids(db, user)
        results, total = await semantic_search(
            db, doc.text_content[:2000] if doc.text_content else "",
            production_id=doc.production_id,
            page=1, per_page=10,
            accessible_production_ids=accessible,
        )
        # Filter out the source document
        results = [r for r in results if str(r["id"]) != doc_id]
        return {
            "source_id": doc_id,
            "search_terms": "(embedding-based)",
            "results": results[:10],
            "total": len(results),
        }

    # Fallback: Claude-based term extraction
    terms = await extract_similar_terms(doc.text_content or "")
    if not terms:
        return {"source_id": doc_id, "search_terms": "", "results": [], "total": 0}

    from app.services.search import search_documents
    from app.dependencies import get_accessible_production_ids
    accessible = await get_accessible_production_ids(db, user)
    results, total = await search_documents(db, terms, production_id=doc.production_id, page=1, per_page=11, accessible_production_ids=accessible)
    results = [r for r in results if str(r["id"]) != doc_id]
    return {
        "source_id": doc_id,
        "search_terms": terms,
        "results": results[:10],
        "total": len(results),
    }
```

- [ ] **Step 2: Update NL search endpoint**

Replace the NL search endpoint body:
```python
@router.post("/ai/nl-search")
async def natural_language_search(
    body: dict,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Natural language search. Uses embeddings if available, falls back to Claude → FTS."""
    query = body.get("query", "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Query is required")

    # Try semantic search first (if Voyage configured)
    from app.config import settings
    if settings.voyage_api_key:
        from app.services.semantic_search import semantic_search
        from app.dependencies import get_accessible_production_ids
        accessible = await get_accessible_production_ids(db, user)
        results, total = await semantic_search(db, query, page=1, per_page=20, accessible_production_ids=accessible)
        return {
            "original_query": query,
            "structured_query": "(semantic search)",
            "results": results,
            "total": total,
        }

    # Fallback: Claude translation → FTS
    structured = await nl_to_search_query(query)
    if not structured:
        return {"original_query": query, "structured_query": "", "results": [], "total": 0}

    from app.services.search import search_documents
    from app.dependencies import get_accessible_production_ids
    accessible = await get_accessible_production_ids(db, user)
    results, total = await search_documents(db, structured, page=1, per_page=20, accessible_production_ids=accessible)
    return {
        "original_query": query,
        "structured_query": structured,
        "results": results,
        "total": total,
    }
```

- [ ] **Step 3: Commit**

```bash
git add backend/app/routers/ai.py
git commit -m "feat: upgrade find-similar and NL search to use embeddings"
```

---

### Task 8: Ingest Integration — Phase B + Backfill

**Files:**
- Modify: `backend/app/routers/ingest.py`
- Modify: `backend/app/services/tasks.py`

- [ ] **Step 1: Add embedding to process-document handler**

In `backend/app/routers/ingest.py`, in the `process_document_task` function, after the AI title generation block and before `doc.processing_status = "complete"`, add:

```python
        # Chunk + embed for vector search
        if settings.voyage_api_key and doc.text_content:
            try:
                from app.services.embeddings import chunk_and_embed_document
                import asyncio
                chunk_count = await asyncio.to_thread(chunk_and_embed_document_sync, db, str(doc.id))
                logger.info("Phase B task: embedded %s (%d chunks)", doc.bates_begin, chunk_count)
            except Exception as e:
                logger.warning("Phase B task: embedding failed for %s: %s", doc.bates_begin, e)
```

Note: Since `chunk_and_embed_document` calls the sync Voyage API, we need to handle the async/sync boundary. Simplest approach: call `embed_texts` (sync) inside `chunk_and_embed_document` which is already async (it does `await db.flush()`). The Voyage API call is sync but fast (~100ms per batch), so it's acceptable to block briefly.

- [ ] **Step 2: Add embed-document task handler**

Add new endpoint in `backend/app/routers/ingest.py`:
```python
@router.post("/ingest/embed-document")
async def embed_document_task(request: Request, db: AsyncSession = Depends(get_db)):
    """Cloud Tasks handler: chunk and embed a single document."""
    from app.services.embeddings import chunk_and_embed_document

    body = await request.json()
    doc_id = body.get("doc_id")
    if not doc_id:
        raise HTTPException(status_code=400, detail="doc_id required")

    try:
        count = await chunk_and_embed_document(db, doc_id)
        await db.commit()
        return {"status": "complete", "doc_id": doc_id, "chunks": count}
    except Exception as e:
        logger.exception("Embed task failed for %s: %s", doc_id, e)
        raise HTTPException(status_code=500, detail=str(e))
```

- [ ] **Step 3: Add backfill endpoint**

Add new endpoint in `backend/app/routers/ingest.py`:
```python
@router.post("/ingest/embed-production/{production_id}")
async def embed_production(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Backfill embeddings for all documents in a production that don't have chunks."""
    from sqlalchemy import select, func
    from app.models import Document, DocumentChunk
    from app.services.tasks import enqueue_embed_tasks

    production = await db.get(Production, production_id)
    if not production:
        raise HTTPException(status_code=404, detail="Production not found")

    # Find docs with text but no chunks
    docs_with_chunks = select(DocumentChunk.document_id).distinct()
    result = await db.execute(
        select(Document.id)
        .where(Document.production_id == production_id)
        .where(Document.text_content.isnot(None))
        .where(Document.id.notin_(docs_with_chunks))
    )
    doc_ids = [str(row[0]) for row in result.all()]

    if not doc_ids:
        return {"enqueued": 0, "message": "All documents already embedded"}

    import asyncio
    count = await asyncio.to_thread(enqueue_embed_tasks, doc_ids, production_id)
    return {"enqueued": count}
```

- [ ] **Step 4: Add embed task enqueue helper**

In `backend/app/services/tasks.py`, add:
```python
def enqueue_embed_tasks(doc_ids: list[str], production_id: int) -> int:
    """Enqueue Cloud Tasks for embedding documents."""
    if not settings.cloud_run_service_url:
        logger.warning("VIGILIST_CLOUD_RUN_SERVICE_URL not set — skipping embed tasks")
        return 0

    client = tasks_v2.CloudTasksClient()
    queue_path = client.queue_path(
        settings.gcp_project_id,
        settings.gcp_location,
        settings.cloud_tasks_queue,
    )

    handler_url = f"{settings.cloud_run_service_url}/api/ingest/embed-document"
    created = 0

    for doc_id in doc_ids:
        payload = json.dumps({
            "doc_id": doc_id,
            "production_id": production_id,
        }).encode()

        task = tasks_v2.Task(
            http_request=tasks_v2.HttpRequest(
                http_method=tasks_v2.HttpMethod.POST,
                url=handler_url,
                headers={"Content-Type": "application/json"},
                body=payload,
                oidc_token=tasks_v2.OidcToken(
                    service_account_email=f"{settings.gcp_project_id}@appspot.gserviceaccount.com",
                    audience=settings.cloud_run_service_url,
                ),
            ),
        )

        try:
            client.create_task(parent=queue_path, task=task)
            created += 1
        except Exception:
            logger.warning("Failed to enqueue embed task for doc %s", doc_id, exc_info=True)

    logger.info("Enqueued %d embed tasks for production %d", created, production_id)
    return created
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/ingest.py backend/app/services/tasks.py
git commit -m "feat: add embedding to Phase B ingest + backfill endpoint"
```

---

### Task 9: Frontend — Search Mode Toggle

**Files:**
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/App.tsx`

- [ ] **Step 1: Add mode parameter to searchDocuments**

In `frontend/src/api/client.ts`, update the `searchDocuments` function signature and body:

```typescript
export async function searchDocuments(
  q: string,
  page = 1,
  perPage = 50,
  sort = 'relevance',
  productionId?: number,
  tagIds?: number[],
  metadata?: Record<string, string>,
  mode?: 'fulltext' | 'semantic',
): Promise<SearchResponse> {
  const params = new URLSearchParams({ q, page: String(page), per_page: String(perPage), sort });
  if (productionId) params.set('production_id', String(productionId));
  if (tagIds?.length) params.set('tag_ids', tagIds.join(','));
  if (metadata && Object.keys(metadata).length > 0) {
    params.set('metadata', JSON.stringify(metadata));
  }
  if (mode) params.set('mode', mode);
  return request<SearchResponse>(`/api/search?${params}`);
}
```

- [ ] **Step 2: Add search mode toggle to App.tsx**

In `frontend/src/App.tsx`, add state for search mode near the other search state:
```typescript
const [searchMode, setSearchMode] = useState<'fulltext' | 'semantic'>('fulltext');
```

Update the `handleSearch` function to pass `searchMode`:
```typescript
// In the searchDocuments call, add searchMode as the last argument
const res = await searchDocuments(query, 1, 50, 'relevance', selectedProduction || undefined, selectedTagIds.length ? selectedTagIds : undefined, undefined, searchMode);
```

Add the toggle next to the search input (inside the search bar area):
```tsx
<div style={{ display: 'flex', gap: 2, fontSize: 'var(--text-xs)', background: 'var(--color-neutral-100)', borderRadius: 'var(--radius-md)', padding: 2 }}>
  {(['fulltext', 'semantic'] as const).map(m => (
    <button
      key={m}
      onClick={() => setSearchMode(m)}
      style={{
        padding: '4px 10px', borderRadius: 'var(--radius-sm)', border: 'none', cursor: 'pointer',
        background: searchMode === m ? 'var(--color-neutral-900)' : 'transparent',
        color: searchMode === m ? '#fff' : 'var(--color-neutral-500)',
        fontWeight: searchMode === m ? 600 : 400,
        fontSize: 'var(--text-xs)',
      }}
    >
      {m === 'fulltext' ? 'Full-text' : 'Semantic'}
    </button>
  ))}
</div>
```

- [ ] **Step 3: Build and verify**

```bash
cd frontend && npm run build
```
Expected: Build succeeds with no TypeScript errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/client.ts frontend/src/App.tsx
git commit -m "feat: add search mode toggle (fulltext/semantic)"
```

---

### Task 10: Deploy and Test End-to-End

**Files:** None (deployment + verification)

- [ ] **Step 1: Set Voyage API key on Cloud Run**

```bash
gcloud run services update vigilist-api --region us-central1 --update-env-vars "VIGILIST_VOYAGE_API_KEY=<your-voyage-api-key>"
```

- [ ] **Step 2: Deploy backend**

```bash
cd backend && gcloud run deploy vigilist-api --source . --region us-central1
```

- [ ] **Step 3: Deploy frontend**

```bash
cd .. && npx firebase deploy --only hosting
```

- [ ] **Step 4: Trigger embedding backfill**

After Phase A completes for the production, call the backfill endpoint:
```bash
curl -X POST "https://ediscover.web.app/api/ingest/embed-production/1" \
  -H "Authorization: Bearer <token>"
```

- [ ] **Step 5: Verify semantic search**

1. Open the app, switch to "Semantic" mode
2. Search for a natural language query like "documents about use of force"
3. Verify results are returned with snippets
4. Switch back to "Full-text" and verify existing search still works

- [ ] **Step 6: Verify Find Similar**

1. Open a document
2. Click "Find Similar" in the AI tools sidebar
3. Verify results are returned faster than before (no Claude API delay)
