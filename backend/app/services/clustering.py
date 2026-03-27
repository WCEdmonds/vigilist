"""Topic clustering using k-means on document embeddings with Claude-generated labels."""

import logging

import numpy as np
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, DocumentCluster, DocumentClusterAssignment
from app.services.document_embeddings import get_document_embeddings

logger = logging.getLogger(__name__)


def _silhouette_score(X: np.ndarray, labels: np.ndarray) -> float:
    """Simplified silhouette score computation."""
    n = len(X)
    if n < 3:
        return 0.0

    unique_labels = set(labels)
    if len(unique_labels) < 2:
        return 0.0

    scores = []
    for i in range(n):
        # a(i) = average distance to same-cluster members
        same = [j for j in range(n) if labels[j] == labels[i] and j != i]
        if not same:
            continue
        a = np.mean([np.linalg.norm(X[i] - X[j]) for j in same])

        # b(i) = min average distance to other clusters
        b = float("inf")
        for label in unique_labels:
            if label == labels[i]:
                continue
            others = [j for j in range(n) if labels[j] == label]
            if others:
                avg_dist = np.mean([np.linalg.norm(X[i] - X[j]) for j in others])
                b = min(b, avg_dist)

        if b == float("inf"):
            continue
        scores.append((b - a) / max(a, b))

    return float(np.mean(scores)) if scores else 0.0


def _kmeans(X: np.ndarray, k: int, max_iter: int = 30) -> np.ndarray:
    """Simple k-means returning labels."""
    n = X.shape[0]
    # K-means++ init
    idx = [np.random.randint(n)]
    for _ in range(k - 1):
        dists = np.min(np.linalg.norm(X[:, None, :] - X[idx][None, :, :], axis=2), axis=1)
        idx.append(int(np.argmax(dists)))
    centroids = X[idx].copy()

    for _ in range(max_iter):
        dists = np.linalg.norm(X[:, None, :] - centroids[None, :, :], axis=2)
        labels = np.argmin(dists, axis=1)
        new_centroids = np.zeros_like(centroids)
        for j in range(k):
            mask = labels == j
            if mask.any():
                new_centroids[j] = X[mask].mean(axis=0)
            else:
                new_centroids[j] = centroids[j]
        if np.allclose(centroids, new_centroids, atol=1e-6):
            break
        centroids = new_centroids

    return labels


async def _generate_cluster_label(doc_texts: list[str]) -> str:
    """Use Claude to generate a 3-5 word topic label from representative texts."""
    from app.config import settings
    if not settings.anthropic_api_key:
        return "Unlabeled"

    import anthropic
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    excerpts = "\n---\n".join(text[:500] for text in doc_texts[:3])
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=30,
            messages=[{
                "role": "user",
                "content": f"Generate a concise 3-5 word topic label for these related legal documents. Respond with ONLY the label, no quotes.\n\n{excerpts}",
            }],
        )
        return response.content[0].text.strip()[:100]
    except Exception as e:
        logger.warning("Cluster labeling failed: %s", e)
        return "Unlabeled"


async def cluster_production(
    db: AsyncSession,
    production_id: int,
    num_clusters: int | None = None,
) -> list[dict]:
    """Run topic clustering for a production. Returns list of cluster info dicts."""
    # Clear previous clusters
    old = await db.execute(
        select(DocumentCluster.id).where(DocumentCluster.production_id == production_id)
    )
    old_ids = [row[0] for row in old.all()]
    if old_ids:
        await db.execute(delete(DocumentClusterAssignment).where(DocumentClusterAssignment.cluster_id.in_(old_ids)))
        await db.execute(delete(DocumentCluster).where(DocumentCluster.id.in_(old_ids)))

    embeddings = await get_document_embeddings(db, production_id)
    if len(embeddings) < 3:
        return []

    doc_ids = list(embeddings.keys())
    X = np.array([embeddings[d] for d in doc_ids], dtype=np.float32)

    # Normalize
    norms = np.linalg.norm(X, axis=1, keepdims=True)
    norms[norms == 0] = 1
    X = X / norms

    n = len(doc_ids)

    # Auto-detect k
    if num_clusters is None:
        if n < 50:
            k_range = range(2, min(6, n))
        else:
            k_range = range(5, min(31, n // 10 + 1))

        best_k = 2
        best_score = -1
        for k in k_range:
            labels = _kmeans(X, k)
            score = _silhouette_score(X, labels)
            if score > best_score:
                best_score = score
                best_k = k

        num_clusters = best_k
        logger.info("Auto-detected k=%d (silhouette=%.3f) for %d documents", best_k, best_score, n)

    labels = _kmeans(X, num_clusters)

    # Get doc texts for labeling
    doc_texts = {}
    result = await db.execute(
        select(Document.id, Document.text_content)
        .where(Document.production_id == production_id)
        .where(Document.text_content.isnot(None))
    )
    for doc_id, text in result.all():
        doc_texts[str(doc_id)] = text or ""

    # Create clusters with labels
    clusters_info = []
    for k in range(num_clusters):
        member_ids = [doc_ids[i] for i in range(n) if labels[i] == k]
        if not member_ids:
            continue

        # Get representative texts (closest to centroid)
        mask = labels == k
        centroid = X[mask].mean(axis=0)
        dists = np.linalg.norm(X[mask] - centroid, axis=1)
        sorted_idx = np.argsort(dists)
        rep_ids = [member_ids[i] for i in sorted_idx[:3]]
        rep_texts = [doc_texts.get(d, "")[:500] for d in rep_ids]

        label = await _generate_cluster_label(rep_texts)

        cluster = DocumentCluster(
            production_id=production_id,
            cluster_index=k,
            label=label,
            doc_count=len(member_ids),
        )
        db.add(cluster)
        await db.flush()

        for doc_id in member_ids:
            db.add(DocumentClusterAssignment(document_id=doc_id, cluster_id=cluster.id))

        clusters_info.append({"id": cluster.id, "label": label, "doc_count": len(member_ids)})

    await db.commit()
    logger.info("Clustering complete: %d clusters for production %d", len(clusters_info), production_id)
    return clusters_info
