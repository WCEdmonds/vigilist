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
