"""Semantic search using pgvector."""

import logging

from sqlalchemy import select, func
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
    groups by document, returns ranked results.
    """
    query_embedding = embed_query(query)
    if not query_embedding:
        return [], 0

    chunk_limit = per_page * 5

    distance = DocumentChunk.embedding.cosine_distance(query_embedding).label("distance")

    chunk_q = (
        select(
            DocumentChunk.document_id,
            DocumentChunk.content,
            distance,
        )
        .join(Document, Document.id == DocumentChunk.document_id)
    )

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

    # Group by document, keep best (lowest distance) chunk
    doc_best: dict = {}
    for doc_id, content, dist in rows:
        if doc_id not in doc_best or dist < doc_best[doc_id][1]:
            doc_best[doc_id] = (content, dist)

    sorted_docs = sorted(doc_best.items(), key=lambda x: x[1][1])
    total_approx = len(sorted_docs)

    start = (page - 1) * per_page
    page_docs = sorted_docs[start:start + per_page]

    if not page_docs:
        return [], total_approx

    doc_ids = [doc_id for doc_id, _ in page_docs]
    docs_result = await db.execute(
        select(Document).where(Document.id.in_(doc_ids))
    )
    docs_map = {d.id: d for d in docs_result.scalars().all()}

    results = []
    for doc_id, (snippet, dist) in page_docs:
        doc = docs_map.get(doc_id)
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
            "rank": round(1.0 - float(dist), 4),
        })

    return results, total_approx
