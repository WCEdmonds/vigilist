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
# Voyage rejects a call over 320k tokens (voyage-3). Batches are packed against
# a conservative estimate so one long document can't overflow a call.
TOKEN_BUDGET_PER_CALL = 100_000
CHARS_PER_TOKEN = 3  # conservative: real English averages ~4 chars/token


def _get_client():
    if not settings.voyage_api_key:
        return None
    import voyageai
    return voyageai.Client(api_key=settings.voyage_api_key)


def _est_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN + 1


def _pack_batches(texts: list[str]) -> list[list[int]]:
    """Group text indices into calls of <= BATCH_SIZE items and the token budget."""
    batches: list[list[int]] = []
    current: list[int] = []
    tokens = 0
    for i, t in enumerate(texts):
        cost = _est_tokens(t)
        if current and (len(current) >= BATCH_SIZE or tokens + cost > TOKEN_BUDGET_PER_CALL):
            batches.append(current)
            current, tokens = [], 0
        current.append(i)
        tokens += cost
    if current:
        batches.append(current)
    return batches


def _embed_batch(client, texts: list[str]) -> list[list[float] | None]:
    """Embed one batch. A rejected request is never retried as-is: it is split
    in half until the offending item is isolated, and only that item fails
    (None). Transient errors are retried with backoff."""
    from voyageai.error import InvalidRequestError, MalformedRequestError

    for attempt in range(MAX_RETRIES):
        try:
            return list(client.embed(texts, model="voyage-3", input_type="document").embeddings)
        except (InvalidRequestError, MalformedRequestError) as e:
            if len(texts) == 1:
                logger.warning("Voyage rejected one input (%d chars) — skipping it: %s", len(texts[0]), e)
                return [None]
            mid = len(texts) // 2
            return _embed_batch(client, texts[:mid]) + _embed_batch(client, texts[mid:])
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                wait = 2 ** attempt
                logger.warning("Voyage API error (attempt %d/%d), retrying in %ds: %s", attempt + 1, MAX_RETRIES, wait, e)
                time.sleep(wait)
            else:
                logger.error("Voyage API failed after %d attempts: %s", MAX_RETRIES, e)
                raise
    raise AssertionError("unreachable")


def embed_texts(texts: list[str]) -> list[list[float] | None]:
    """Embed document texts using Voyage AI, one vector per input.

    Inputs Voyage rejects (e.g. over its per-request limit) come back as None
    so the rest of the document still embeds.
    """
    client = _get_client()
    if not client or not texts:
        return []

    out: list[list[float] | None] = [None] * len(texts)
    for idxs in _pack_batches(texts):
        vectors = _embed_batch(client, [texts[i] for i in idxs])
        for i, v in zip(idxs, vectors):
            out[i] = v
    return out


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

    embedded = 0
    for i, (text, embedding) in enumerate(zip(chunks, embeddings)):
        if embedding is None:  # rejected by Voyage; the rest still embed
            continue
        embedded += 1
        chunk = DocumentChunk(
            document_id=doc.id,
            chunk_index=i,
            content=text,
            embedding=embedding,
        )
        db.add(chunk)

    await db.flush()
    logger.info("Embedded doc %s: %d/%d chunks", doc_id, embedded, len(chunks))
    return embedded


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
