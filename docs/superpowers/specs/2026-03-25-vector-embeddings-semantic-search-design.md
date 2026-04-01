# Vector Embeddings & Semantic Search — Design Spec

**Date:** 2026-03-25
**Phase:** 7A (AI-Powered Review — Foundation)
**Status:** Approved

---

## Goal

Add vector embedding infrastructure to the document review platform, enabling semantic search, improved find-similar, and laying the foundation for clustering, TAR, and the structured AI review workflow.

## Architecture

### Embedding Model

- **Model:** Voyage AI `voyage-3-lite`
- **Dimensions:** 1024
- **Cost:** ~$0.02 per 1M tokens
- **Batch limit:** 128 texts per API call
- **Dependency:** `voyageai` Python package
- **Config:** `VIGILIST_VOYAGE_API_KEY` env var

### Storage: pgvector on Neon Postgres

Enable the `pgvector` extension on the existing Neon database. New table:

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE document_chunks (
    id SERIAL PRIMARY KEY,
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    embedding vector(1024) NOT NULL,
    UNIQUE(document_id, chunk_index)
);

CREATE INDEX ix_chunks_document_id ON document_chunks(document_id);
CREATE INDEX ix_chunks_embedding_hnsw ON document_chunks
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
```

### SQLAlchemy Model

```python
from pgvector.sqlalchemy import Vector

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

Add `chunks` relationship to `Document` model:
```python
chunks = relationship("DocumentChunk", back_populates="document", cascade="all, delete-orphan")
```

### Chunking Strategy

- Split each document's `text_content` into chunks of ~500 whitespace-delimited words (a reasonable proxy for model tokens; actual Voyage tokens may be ~20% higher, which is within the model's input limit)
- 50-word overlap between consecutive chunks
- Documents with no text content get no chunks
- Store chunk text alongside embedding for snippet display

---

## Components

### 1. Chunking Service

**File:** `backend/app/services/chunking.py`

```
chunk_text(text: str, chunk_size=500, overlap=50) -> list[str]
```

- Splits text into token-based chunks with overlap
- Returns list of chunk strings
- Pure function, no I/O

### 2. Embedding Service

**File:** `backend/app/services/embeddings.py`

```
embed_texts(texts: list[str]) -> list[list[float]]
embed_query(query: str) -> list[float]
```

- Wraps Voyage API client
- `embed_texts`: batches inputs (max 128 per call), uses `input_type="document"` for corpus embeddings, returns embeddings. Retries transient failures with exponential backoff (3 attempts, 1s/2s/4s).
- `embed_query`: single query embedding, uses `input_type="query"` for asymmetric search
- Returns empty list if Voyage API key not configured (graceful degradation)

### 3. Chunk + Embed Pipeline

**File:** `backend/app/services/embeddings.py`

```
async chunk_and_embed_document(db, doc_id: str) -> int
```

- Loads document text from DB
- Chunks the text
- Embeds all chunks in one batch call
- Inserts into `document_chunks` table
- Idempotent: deletes existing chunks for the document first
- Returns number of chunks created

### 4. Phase B Integration

**File:** `backend/app/routers/ingest.py` (existing endpoint)

In the `/api/ingest/process-document` Cloud Task handler, after image conversion and title generation:

```python
# Chunk + embed (if Voyage API key configured)
if settings.voyage_api_key and doc.text_content:
    from app.services.embeddings import chunk_and_embed_document
    await chunk_and_embed_document(db, str(doc.id))
```

### 5. Embed Task Handler

**File:** `backend/app/routers/ingest.py`

New endpoint for Cloud Tasks to call for embedding-only work:

```
POST /api/ingest/embed-document
```

- Accepts `{"doc_id": "...", "production_id": N}` (same shape as `process-document`)
- Calls `chunk_and_embed_document(db, doc_id)`
- Idempotent — safe to retry
- Authenticated via Cloud Tasks OIDC (no Firebase user auth)
- Returns 200 on success, 500 triggers Cloud Tasks retry

### 6. Backfill Endpoint

**File:** `backend/app/routers/ingest.py`

```
POST /api/ingest/embed-production/{production_id}
```

- Queries documents in the production that have text but no chunks in `document_chunks`
- Fans out one Cloud Task per document to `/api/ingest/embed-document` (reuses `ingest-phase-b` queue)
- Returns count of tasks enqueued
- Manager+ role required

### 7. Semantic Search

**File:** `backend/app/routers/search.py` (modify existing)

Add `mode` query parameter to `GET /api/search`:

- `mode=fulltext` (default): current PostgreSQL tsvector behavior
- `mode=semantic`: embed query → pgvector ANN search → group by document

Semantic search flow:
1. Call `embed_query(q)` to get query vector
2. Query `document_chunks` ordered by `embedding <=> query_vector` with LIMIT of `per_page * 5` (over-fetch to account for grouping)
3. JOIN `documents` for production_id filter; JOIN `document_tags` for tag_id filter (same filter logic as fulltext search)
4. Group by `document_id`, take best similarity score per document as `rank` (use `1 - cosine_distance` so higher = more similar, matching FTS rank semantics)
5. Return `SearchResponse` with chunk content as snippet, `total` as approximate count
6. Pagination: `page` and `per_page` apply after grouping. `total` is approximate (based on chunk LIMIT, not exact count)

### 8. Upgraded Find Similar

**File:** `backend/app/routers/ai.py` (modify existing)

Replace current `POST /api/ai/find-similar/{doc_id}` implementation:

1. Load all chunks for the source document
2. Query pgvector for nearest neighbor chunks using each source chunk, excluding the source doc (max-similarity approach — better than averaging for multi-topic documents)
3. Merge results, deduplicate by document, take best similarity score per document
4. Return top-N documents

Falls back to current Claude-based approach if no chunks exist for the document.

### 9. Upgraded NL Search

**File:** `backend/app/routers/ai.py` (modify existing)

Replace current `POST /api/ai/nl-search` implementation:

1. Embed the NL query directly with Voyage (no Claude translation step)
2. Run vector search (same as semantic search)
3. Falls back to current Claude → FTS approach if Voyage API key not configured

---

## Frontend Changes

### Search Mode Toggle

**File:** `frontend/src/App.tsx`

Add a toggle next to the search input:

```
[Full-text | Semantic]
```

- Passes `mode` parameter to `searchDocuments()` API call
- Default: `fulltext`
- Results render identically (same `SearchResponse` shape)
- Toggle state persists during session (not across refreshes)

**File:** `frontend/src/api/client.ts`

Add `mode` parameter to `searchDocuments()`:

```typescript
export async function searchDocuments(
  q: string, page = 1, perPage = 50, sort = 'relevance',
  productionId?: number, tagIds?: number[],
  metadata?: Record<string, string>,
  mode?: 'fulltext' | 'semantic',
): Promise<SearchResponse>
```

No other frontend changes needed.

---

## Dependencies

### Python packages (add to requirements.txt)
- `voyageai>=0.3` — Voyage AI embedding client
- `pgvector>=0.3` — SQLAlchemy pgvector integration

### Environment variables (Cloud Run)
- `VIGILIST_VOYAGE_API_KEY` — Voyage AI API key

### Database
- Enable pgvector extension: `CREATE EXTENSION IF NOT EXISTS vector;`
- Alembic migration for `document_chunks` table + indexes

### Config additions (backend/app/config.py)
- `voyage_api_key: str = ""` — Voyage AI API key

---

## Data Flow

```
Ingest Phase B (per document):
  text_content → chunk_text() → embed_texts() → INSERT document_chunks

Semantic Search:
  query → embed_query() → pgvector ANN → group by doc → SearchResponse

Find Similar:
  doc.chunks → max-similarity ANN per chunk → merge + dedup → top-N docs

NL Search:
  nl_query → embed_query() → pgvector ANN → group by doc → results
```

---

## Error Handling & Graceful Degradation

- If `VIGILIST_VOYAGE_API_KEY` is not set, all embedding features are disabled silently
- Semantic search returns empty results with a message if no chunks exist
- Find Similar falls back to current Claude-based term extraction
- NL Search falls back to current Claude → FTS translation
- Embedding failures during ingest are logged but don't block document processing
- Backfill endpoint can be re-run safely (idempotent)

---

## Performance Targets

- Embedding 550 documents (~3,000 chunks): < 2 minutes with Cloud Tasks parallelism
- Semantic search query: < 500ms (HNSW index)
- Find Similar: < 500ms (no Claude API call needed)

---

## Out of Scope (deferred to later specs)

- Hybrid FTS + vector search with RRF fusion
- Clustering & conceptual grouping (requires embeddings — builds on this)
- Active learning / TAR (requires embeddings + review workflow)
- Structured AI review workflow (separate spec, builds on this)
- Communication analysis (depends on email metadata, not embeddings)
