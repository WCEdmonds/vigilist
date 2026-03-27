"""Shared utility for getting averaged document embeddings."""

import logging

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, DocumentChunk

logger = logging.getLogger(__name__)


async def get_document_embeddings(
    db: AsyncSession, production_id: int
) -> dict[str, np.ndarray]:
    """Get averaged embeddings for all documents in a production.

    Returns dict mapping document_id (str) -> numpy array of averaged chunk embeddings.
    Only includes documents that have embeddings.
    """
    doc_result = await db.execute(
        select(Document.id)
        .where(Document.production_id == production_id)
        .where(Document.text_content.isnot(None))
    )
    all_doc_ids = [row[0] for row in doc_result.all()]

    if not all_doc_ids:
        return {}

    result = await db.execute(
        select(
            DocumentChunk.document_id,
            func.avg(DocumentChunk.embedding).label("avg_embedding"),
        )
        .where(DocumentChunk.document_id.in_(all_doc_ids))
        .group_by(DocumentChunk.document_id)
    )

    embeddings = {}
    for doc_id, avg_emb in result.all():
        if avg_emb is not None:
            if isinstance(avg_emb, str):
                emb = [float(x) for x in avg_emb.strip("[]").split(",")]
            else:
                emb = list(avg_emb)
            embeddings[str(doc_id)] = np.array(emb, dtype=np.float32)

    logger.info("Loaded %d document embeddings for production %d", len(embeddings), production_id)
    return embeddings
