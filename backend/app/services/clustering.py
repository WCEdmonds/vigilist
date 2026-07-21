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


# How many titles to show the labeler at most; beyond this, an even spread.
_TITLE_CAP = 200


async def _generate_cluster_label(titles: list[str], sample_lines: list[str]) -> str:
    """Label one cluster, or decide it has no honest label.

    Primary path: show the labeler the titles of (up to _TITLE_CAP of) ALL
    member documents and ask for a category that genuinely covers them —
    or an explicit "None" verdict, which the caller stores as no label at
    all. Falls back to the title+snippet spread sample when most documents
    have no title to judge by.
    """
    from app.config import settings
    if not settings.anthropic_api_key:
        return "Unlabeled"

    import anthropic
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    titled = [t for t in titles if t]
    # Labels render as compact chips in the document list — long ones
    # truncate, so brevity is a display constraint in both prompts.
    if len(titled) >= max(3, len(titles) // 2):
        listing = "\n".join(f"- {t}" for t in _spread_sample(titled, _TITLE_CAP))
        content = (
            "Below are the titles of the documents in ONE cluster from a "
            "legal document production. Every document in the cluster will "
            "display your answer as its theme badge. If they share a genuine "
            "common category, respond with the most specific 2-4 word label "
            "(under 30 characters) that covers essentially all of them — "
            "widen the category rather than naming the most common type. If "
            "there is NO genuine common thread, respond with exactly: None. "
            f"Respond with ONLY the label or None, no quotes.\n\n{listing}"
        )
    else:
        excerpts = "\n---\n".join(sample_lines[:_LABEL_SAMPLE_SIZE])
        content = (
            "These documents were sampled from across ONE cluster of "
            "related legal documents. Every document in the cluster will "
            "display your answer as its theme badge, so name the most "
            "specific 2-4 word category (under 30 characters) that covers "
            "essentially ALL of the sampled documents — widen the category "
            "rather than naming the most common type. If they share no "
            "genuine common thread, or the text is illegible, respond with "
            "exactly: None. Respond with ONLY the label or None, no "
            f"quotes.\n\n{excerpts}"
        )

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=30,
            messages=[{"role": "user", "content": content}],
        )
        raw = response.content[0].text.strip()
        if raw.strip('."\' ').lower() in ("none", "unlabeled"):
            return "Unlabeled"
        return _sanitize_label(raw)
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

        # Sample documents across the cluster so the label reflects its
        # breadth — but only the closest 80% (the far edge is where k-means
        # dumps outliers, and letting them into the sample made the labeler
        # give up with "Unlabeled"), and only docs with usable signal.
        mask = labels == k
        centroid = X[mask].mean(axis=0)
        dists = np.linalg.norm(X[mask] - centroid, axis=1)
        sorted_idx = np.argsort(dists)
        ordered_ids = [member_ids[i] for i in sorted_idx]
        core_n = max(_LABEL_SAMPLE_SIZE, int(len(ordered_ids) * 0.8))
        core_ids = ordered_ids[:core_n]
        with_signal = [
            d for d in core_ids
            if doc_titles.get(d) or len(doc_texts.get(d, "").strip()) >= 80
        ]
        rep_ids = _spread_sample(with_signal or core_ids, _LABEL_SAMPLE_SIZE)
        rep_lines = []
        for d in rep_ids:
            title = doc_titles.get(d, "")
            snippet = doc_texts.get(d, "")[:200].replace("\n", " ").strip()
            line = f"{title} — {snippet}" if title else snippet
            if line.strip():
                rep_lines.append(line)

        member_titles = [doc_titles.get(d, "") for d in member_ids]
        label = await _generate_cluster_label(member_titles, rep_lines)

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
