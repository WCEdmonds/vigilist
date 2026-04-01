"""Voyage AI embedding service for vector search."""

import logging
import time

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.chunking import chunk_text

logger = logging.getLogger(__name__)

BATCH_SIZE = 128
MAX_RETRIES = 3


def _get_client():
    if not settings.voyage_api_key:
        return None
    import voyageai
    return voyageai.Client(api_key=settings.voyage_api_key)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed document texts using Voyage AI. Batches at 128 per call."""
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
                    model="voyage-3",
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
    """Embed a search query. Uses input_type='query' for asymmetric search."""
    client = _get_client()
    if not client or not query.strip():
        return []

    result = client.embed(
        [query],
        model="voyage-3",
        input_type="query",
    )
    return result.embeddings[0]


async def chunk_and_embed_document(db: AsyncSession, doc_id: str) -> int:
    """Chunk a document's text and store embeddings. Idempotent."""
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
