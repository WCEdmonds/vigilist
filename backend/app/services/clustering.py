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


def _sanitize_label(raw: str) -> str:
    """Keep only responses that look like actual labels.

    On OCR-poor documents the model sometimes replies with an apology
    sentence ("I cannot reliably extract legible document titles…") instead
    of a label; stored verbatim, that sentence rendered inside theme chips
    in prod. Anything sentence-shaped falls back to "Unlabeled" — the UI
    then shows its generic "Cluster N" name.
    """
    label = raw.strip().strip('"').strip()
    refusal_markers = (
        "i cannot", "i can't", "i am unable", "i'm unable", "unable to",
        "sorry", "illegible", "not legible", "no legible", "cannot determine",
    )
    lowered = label.lower()
    if (
        not label
        or len(label) > 40
        or len(label.split()) > 6
        or label.endswith(".")
        or any(m in lowered for m in refusal_markers)
    ):
        return "Unlabeled"
    return label


def _spread_sample(ordered: list, k: int) -> list:
    """Pick up to k items evenly spread across an ordered list (first and
    last always included). Labeling from a spread, not just the centroid,
    keeps the label honest about the whole cluster's breadth."""
    n = len(ordered)
    if n <= k:
        return list(ordered)
    if k == 1:
        return [ordered[0]]
    idx = sorted({round(i * (n - 1) / (k - 1)) for i in range(k)})
    return [ordered[i] for i in idx]


_LABEL_SAMPLE_SIZE = 8


async def _generate_cluster_label(doc_lines: list[str]) -> str:
    """Use Claude to generate a 2-4 word topic label covering all sampled docs.

    `doc_lines` are per-document "Title — snippet" strings sampled from
    across the cluster (centroid to edge), so the model labels the common
    denominator rather than the flavor of the centroid documents.
    """
    from app.config import settings
    if not settings.anthropic_api_key:
        return "Unlabeled"

    import anthropic
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    excerpts = "\n---\n".join(doc_lines[:_LABEL_SAMPLE_SIZE])
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=30,
            messages=[{
                "role": "user",
                # Labels render as compact chips in the document list — long
                # ones truncate, so brevity is a display constraint.
                "content": (
                    "These documents were sampled from across ONE cluster of "
                    "related legal documents. Generate a topic label of 2-4 "
                    "short words (under 30 characters) describing what ALL of "
                    "them have in common — prefer the general category over "
                    "the specifics of any single document. If they share no "
                    "discernible topic, or the text is illegible, respond "
                    "with exactly: Unlabeled. Respond with ONLY the label, "
                    f"no quotes.\n\n{excerpts}"
                ),
            }],
        )
        return _sanitize_label(response.content[0].text)
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

    # Titles + text snippets for labeling. Titles (AI-generated at ingest)
    # are a much cleaner topic signal than raw leading characters,
    # especially for OCR-heavy tabular documents.
    doc_texts: dict[str, str] = {}
    doc_titles: dict[str, str] = {}
    result = await db.execute(
        select(Document.id, Document.title, Document.text_content)
        .where(Document.production_id == production_id)
    )
    for doc_id, title, text in result.all():
        doc_texts[str(doc_id)] = text or ""
        doc_titles[str(doc_id)] = title or ""

    # Create clusters with labels
    clusters_info = []
    for k in range(num_clusters):
        member_ids = [doc_ids[i] for i in range(n) if labels[i] == k]
        if not member_ids:
            continue

        # Sample documents across the whole cluster, centroid to edge, so
        # the label reflects the common denominator rather than whatever
        # flavor happens to sit at the center.
        mask = labels == k
        centroid = X[mask].mean(axis=0)
        dists = np.linalg.norm(X[mask] - centroid, axis=1)
        sorted_idx = np.argsort(dists)
        ordered_ids = [member_ids[i] for i in sorted_idx]
        rep_ids = _spread_sample(ordered_ids, _LABEL_SAMPLE_SIZE)
        rep_lines = []
        for d in rep_ids:
            title = doc_titles.get(d, "")
            snippet = doc_texts.get(d, "")[:200].replace("\n", " ").strip()
            rep_lines.append(f"{title} — {snippet}" if title else snippet)

        label = await _generate_cluster_label(rep_lines)

        cluster = DocumentCluster(
            production_id=production_id,
            cluster_index=k,
            # NULL rather than "Unlabeled": the UI falls back to "Cluster N",
            # which stays distinguishable when several clusters lack labels.
            label=None if label == "Unlabeled" else label,
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
