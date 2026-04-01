"""Diverse document sampling using vector embeddings.

Selects a semantically diverse sample by clustering document embeddings
and picking one representative per cluster. Falls back to random sampling
if embeddings aren't available.
"""

import logging
import random
from uuid import UUID

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, DocumentChunk

logger = logging.getLogger(__name__)


async def select_diverse_sample(
    db: AsyncSession,
    production_id: int,
    sample_size: int,
) -> list[UUID]:
    """Select a semantically diverse sample of documents.

    Uses k-means clustering on averaged document embeddings to ensure
    the sample covers the full semantic space of the corpus.

    Falls back to random sampling if fewer than sample_size documents
    have embeddings.
    """
    # Get all doc IDs with text
    result = await db.execute(
        select(Document.id)
        .where(Document.production_id == production_id)
        .where(Document.text_content.isnot(None))
    )
    all_doc_ids = [row[0] for row in result.all()]

    if not all_doc_ids or sample_size >= len(all_doc_ids):
        return all_doc_ids

    # Get average embedding per document (average of all chunk embeddings)
    result = await db.execute(
        select(
            DocumentChunk.document_id,
            func.avg(DocumentChunk.embedding).label("avg_embedding"),
        )
        .where(DocumentChunk.document_id.in_(all_doc_ids))
        .group_by(DocumentChunk.document_id)
    )
    rows = result.all()

    if len(rows) < sample_size:
        # Not enough embedded docs — fall back to random
        logger.info("Diverse sampling: only %d/%d docs have embeddings, using random", len(rows), sample_size)
        return random.sample(all_doc_ids, sample_size)

    # Build matrix: doc_ids and their embeddings
    doc_ids = []
    embeddings = []
    for doc_id, avg_emb in rows:
        if avg_emb is not None:
            doc_ids.append(doc_id)
            # avg_emb comes back as a string or list from pgvector
            if isinstance(avg_emb, str):
                emb = [float(x) for x in avg_emb.strip("[]").split(",")]
            else:
                emb = list(avg_emb)
            embeddings.append(emb)

    if len(embeddings) < sample_size:
        return random.sample(all_doc_ids, sample_size)

    X = np.array(embeddings, dtype=np.float32)

    # Normalize for cosine similarity
    norms = np.linalg.norm(X, axis=1, keepdims=True)
    norms[norms == 0] = 1
    X = X / norms

    # Simple k-means clustering
    selected = _kmeans_diverse_select(X, doc_ids, sample_size)

    logger.info("Diverse sampling: selected %d docs across %d clusters from %d total",
                len(selected), sample_size, len(doc_ids))
    return selected


def _kmeans_diverse_select(
    X: np.ndarray,
    doc_ids: list[UUID],
    k: int,
    max_iterations: int = 20,
) -> list[UUID]:
    """Run k-means and return the doc closest to each centroid."""
    n = X.shape[0]

    # Initialize centroids with k-means++ style: pick first randomly, then farthest
    centroids_idx = [random.randint(0, n - 1)]
    for _ in range(k - 1):
        # Distance from each point to nearest centroid
        dists = np.min(
            np.linalg.norm(X[:, None, :] - X[centroids_idx][None, :, :], axis=2),
            axis=1,
        )
        # Pick the farthest point (greedy diverse selection)
        centroids_idx.append(int(np.argmax(dists)))

    centroids = X[centroids_idx].copy()

    # Iterate
    for _ in range(max_iterations):
        # Assign each point to nearest centroid
        dists = np.linalg.norm(X[:, None, :] - centroids[None, :, :], axis=2)
        assignments = np.argmin(dists, axis=1)

        # Update centroids
        new_centroids = np.zeros_like(centroids)
        for j in range(k):
            mask = assignments == j
            if mask.any():
                new_centroids[j] = X[mask].mean(axis=0)
            else:
                new_centroids[j] = centroids[j]

        if np.allclose(centroids, new_centroids, atol=1e-6):
            break
        centroids = new_centroids

    # Pick the doc closest to each centroid
    selected = []
    used = set()
    for j in range(k):
        dists_to_centroid = np.linalg.norm(X - centroids[j], axis=1)
        # Sort by distance, pick first unused
        for idx in np.argsort(dists_to_centroid):
            if idx not in used:
                selected.append(doc_ids[idx])
                used.add(idx)
                break

    return selected
