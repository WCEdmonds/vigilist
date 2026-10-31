"""Topic clustering using k-means on document embeddings with Claude-generated labels."""

import asyncio
import logging

import numpy as np
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, DocumentCluster, DocumentClusterAssignment
from app.services.document_embeddings import get_document_embeddings

logger = logging.getLogger(__name__)


# Silhouette is O(n^2): score k on a fixed sample, as sklearn's sample_size
# does, so a 3,000-doc production costs the same as a 2,000-doc one.
_SILHOUETTE_SAMPLE = 2000


def _sq_dists(X: np.ndarray, C: np.ndarray) -> np.ndarray:
    """Pairwise squared Euclidean distances via one matmul (no n*k*d array)."""
    xx = np.einsum("ij,ij->i", X, X)[:, None]
    cc = np.einsum("ij,ij->i", C, C)[None, :]
    return np.maximum(xx + cc - 2.0 * (X @ C.T), 0.0)


def _distance_matrix(X: np.ndarray) -> np.ndarray:
    X64 = X.astype(np.float64)
    D = np.sqrt(_sq_dists(X64, X64))
    np.fill_diagonal(D, 0.0)
    return D


def _silhouette_from_distances(D: np.ndarray, labels: np.ndarray) -> float:
    """Mean silhouette given a precomputed distance matrix. Points alone in
    their cluster are skipped, matching the original implementation."""
    n = len(labels)
    if n < 3:
        return 0.0
    uniq, inv = np.unique(labels, return_inverse=True)
    if len(uniq) < 2:
        return 0.0
    onehot = np.zeros((n, len(uniq)))
    onehot[np.arange(n), inv] = 1.0
    sums = D @ onehot                      # distance from each point to each cluster
    counts = onehot.sum(axis=0)
    own = counts[inv]
    a = sums[np.arange(n), inv] / np.maximum(own - 1, 1)   # D[i,i] == 0
    mean_other = sums / counts
    mean_other[np.arange(n), inv] = np.inf
    b = mean_other.min(axis=1)
    valid = (own > 1) & np.isfinite(b)
    if not valid.any():
        return 0.0
    denom = np.maximum(a, b)[valid]
    s = np.divide(b[valid] - a[valid], denom, out=np.zeros_like(denom), where=denom > 0)
    return float(s.mean())


def _silhouette_sample(n: int) -> np.ndarray:
    if n <= _SILHOUETTE_SAMPLE:
        return np.arange(n)
    return np.sort(np.random.default_rng(0).choice(n, _SILHOUETTE_SAMPLE, replace=False))


def _silhouette_score(X: np.ndarray, labels: np.ndarray) -> float:
    """Silhouette score (on a fixed sample for large inputs)."""
    n = len(X)
    if n < 3:
        return 0.0
    idx = _silhouette_sample(n)
    return _silhouette_from_distances(_distance_matrix(X[idx]), np.asarray(labels)[idx])


def _kmeans(X: np.ndarray, k: int, max_iter: int = 30) -> np.ndarray:
    """Simple k-means returning labels (k-means++-style farthest-point init)."""
    n = X.shape[0]
    idx = [np.random.randint(n)]
    min_d = _sq_dists(X, X[idx[-1]:idx[-1] + 1])[:, 0]
    for _ in range(k - 1):
        nxt = int(np.argmax(min_d))
        idx.append(nxt)
        min_d = np.minimum(min_d, _sq_dists(X, X[nxt:nxt + 1])[:, 0])
    centroids = X[idx].copy()

    for _ in range(max_iter):
        labels = np.argmin(_sq_dists(X, centroids), axis=1)
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


def choose_clusters(X: np.ndarray, num_clusters: int | None = None) -> tuple[np.ndarray, int, float | None]:
    """Pick k by silhouette (unless given) and cluster. Pure CPU: callers run
    it off the event loop. Returns (labels, k, best_silhouette_or_None)."""
    n = len(X)
    best_score = None
    if num_clusters is None:
        if n < 50:
            k_range = range(2, min(6, n))
        else:
            k_range = range(5, min(31, n // 10 + 1))
        idx = _silhouette_sample(n)
        D = _distance_matrix(X[idx])  # computed once, reused for every k
        best_k, best_score = 2, -1.0
        for k in k_range:
            score = _silhouette_from_distances(D, _kmeans(X, k)[idx])
            if score > best_score:
                best_score, best_k = score, k
        num_clusters = best_k
    return _kmeans(X, num_clusters), num_clusters, best_score


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
            model="claude-haiku-4-5",
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

    # Reads are done: end the transaction so no connection sits idle through
    # the CPU work and the LLM calls below (it was dropped mid-stage before).
    await db.commit()

    # CPU-bound: run in a thread so the instance keeps serving requests.
    auto = num_clusters is None
    labels, num_clusters, best_score = await asyncio.to_thread(choose_clusters, X, num_clusters)
    if auto:
        logger.info("Auto-detected k=%d (silhouette=%.3f) for %d documents", num_clusters, best_score, n)

    # Labels next (one LLM call per cluster), still with no transaction open.
    pending = []
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

        pending.append((k, label, member_ids))

    # Replace the old clusters only now that the new ones are ready, so the
    # transaction is short and a failure leaves the previous clustering intact.
    old = await db.execute(
        select(DocumentCluster.id).where(DocumentCluster.production_id == production_id)
    )
    old_ids = [row[0] for row in old.all()]
    if old_ids:
        await db.execute(delete(DocumentClusterAssignment).where(DocumentClusterAssignment.cluster_id.in_(old_ids)))
        await db.execute(delete(DocumentCluster).where(DocumentCluster.id.in_(old_ids)))

    clusters_info = []
    for k, label, member_ids in pending:
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
