"""Voyage AI embedding service for vector search."""

import asyncio
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

    # embed_texts blocks on Voyage HTTP calls (with sleep-based retries) —
    # run it off the event loop.
    embeddings = await asyncio.to_thread(embed_texts, chunks)
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


async def embed_production_documents(db: AsyncSession, production_id: int) -> int:
    """Chunk and embed every document in a production that has text but no
    chunks yet. Called at the end of ingest so semantic search, clustering,
    and near-duplicate detection have vectors to work with.

    Skips silently when no Voyage API key is configured. Returns the number
    of documents embedded. Failures on individual documents are logged and
    skipped — embedding is best-effort and must never fail an ingest.
    """
    from app.models import Document, DocumentChunk

    if not settings.voyage_api_key:
        logger.info("VIGILIST_VOYAGE_API_KEY not set — skipping embedding generation")
        return 0

    result = await db.execute(
        select(Document.id)
        .where(
            Document.production_id == production_id,
            Document.text_content.isnot(None),
            Document.text_content != "",
            ~Document.id.in_(select(DocumentChunk.document_id.distinct())),
        )
    )
    doc_ids = [row[0] for row in result.all()]
    if not doc_ids:
        return 0

    logger.info("Embedding %d documents for production %d...", len(doc_ids), production_id)
    embedded = 0
    for doc_id in doc_ids:
        try:
            if await chunk_and_embed_document(db, doc_id):
                embedded += 1
        except Exception:
            logger.warning("Embedding failed for doc %s — skipping", doc_id, exc_info=True)
    await db.commit()
    logger.info("Embedded %d/%d documents for production %d", embedded, len(doc_ids), production_id)
    return embedded
