"""Corpus analysis: cluster documents, label topics, identify key documents."""

import logging
import random
from collections import defaultdict
from uuid import UUID

import numpy as np
from sqlalchemy import func, select, text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Document, DocumentChunk

logger = logging.getLogger(__name__)


async def get_document_embeddings(db: AsyncSession, production_id: int) -> tuple[list[UUID], np.ndarray]:
    """Get average embedding per document for a production.

    Returns (doc_ids, embedding_matrix) where embedding_matrix is (N, dim).
    """
    result = await db.execute(
        select(DocumentChunk.document_id, func.avg(DocumentChunk.embedding))
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(Document.production_id == production_id)
        .group_by(DocumentChunk.document_id)
    )

    doc_ids = []
    embeddings = []
    for doc_id, avg_emb in result.all():
        if avg_emb is None:
            continue
        doc_ids.append(doc_id)
        if isinstance(avg_emb, str):
            emb = [float(x) for x in avg_emb.strip("[]").split(",")]
        else:
            emb = list(avg_emb)
        embeddings.append(emb)

    if not embeddings:
        return [], np.array([])

    X = np.array(embeddings, dtype=np.float32)
    # L2 normalize
    norms = np.linalg.norm(X, axis=1, keepdims=True)
    norms[norms == 0] = 1
    X = X / norms

    return doc_ids, X


def cluster_documents(X: np.ndarray, n_clusters: int = 25, max_iter: int = 30) -> np.ndarray:
    """K-means clustering on normalized embeddings.

    Returns cluster assignments array of shape (N,).
    """
    n = X.shape[0]
    if n <= n_clusters:
        return np.arange(n)

    # K-means++ initialization
    centroids_idx = [random.randint(0, n - 1)]
    for _ in range(n_clusters - 1):
        dists = np.min(
            np.linalg.norm(X[:, None, :] - X[centroids_idx][None, :, :], axis=2),
            axis=1,
        )
        # Weighted random selection (proportional to distance squared)
        probs = dists ** 2
        probs = probs / probs.sum()
        centroids_idx.append(int(np.random.choice(n, p=probs)))

    centroids = X[centroids_idx].copy()

    for _ in range(max_iter):
        dists = np.linalg.norm(X[:, None, :] - centroids[None, :, :], axis=2)
        assignments = np.argmin(dists, axis=1)

        new_centroids = np.zeros_like(centroids)
        for j in range(n_clusters):
            mask = assignments == j
            if mask.any():
                new_centroids[j] = X[mask].mean(axis=0)
            else:
                new_centroids[j] = centroids[j]

        if np.allclose(centroids, new_centroids, atol=1e-6):
            break
        centroids = new_centroids

    # Final assignment
    dists = np.linalg.norm(X[:, None, :] - centroids[None, :, :], axis=2)
    assignments = np.argmin(dists, axis=1)

    return assignments


async def label_cluster(docs_text: list[str], doc_titles: list[str]) -> dict:
    """Use Claude to generate a topic label and summary for a cluster of documents."""
    if not settings.anthropic_api_key:
        return {"label": "Unlabeled", "summary": "", "themes": []}

    import anthropic
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    # Build context from top docs
    context_parts = []
    for i, (title, text) in enumerate(zip(doc_titles, docs_text)):
        snippet = text[:1500] if text else "(no text)"
        context_parts.append(f"Document {i+1}: {title}\n{snippet}")

    context = "\n\n---\n\n".join(context_parts[:5])  # Max 5 docs

    try:
        response = await client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": f"""These documents are grouped together by semantic similarity in a litigation production.
Analyze them and respond with JSON only:
{{
  "label": "Short topic label (3-6 words)",
  "summary": "1-2 sentence description of what these documents cover",
  "themes": ["theme1", "theme2", "theme3"]
}}

Documents:
{context}"""
            }],
        )

        import json
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

        return json.loads(raw)
    except Exception as e:
        logger.warning("Cluster labeling failed: %s", e)
        # Fallback: use most common words from titles
        words = " ".join(doc_titles).split()
        common = sorted(set(words), key=lambda w: -words.count(w))[:3]
        return {"label": " / ".join(common), "summary": "", "themes": []}


async def analyze_production(
    db: AsyncSession,
    production_id: int,
    n_clusters: int = 25,
    on_progress: callable = None,
) -> dict:
    """Full corpus analysis: cluster, label, identify key documents.

    Returns a comprehensive analysis report.
    on_progress(step, percent) is called with progress updates.
    """
    def progress(step: str, pct: float):
        if on_progress:
            on_progress(step, pct)

    logger.info("Starting corpus analysis for production %d with %d clusters", production_id, n_clusters)

    # Get embeddings
    progress("Loading document embeddings...", 5)
    doc_ids, X = await get_document_embeddings(db, production_id)

    if len(doc_ids) == 0:
        return {"error": "No embedded documents found. Run embedding backfill first."}

    # Adjust cluster count if fewer docs
    actual_clusters = min(n_clusters, len(doc_ids) // 2, len(doc_ids))
    if actual_clusters < 2:
        actual_clusters = 2

    # Cluster
    progress("Clustering documents...", 15)
    assignments = cluster_documents(X, n_clusters=actual_clusters)

    # Load document metadata
    progress("Loading document metadata...", 25)
    result = await db.execute(
        select(Document.id, Document.bates_begin, Document.title, Document.text_content,
               Document.native_path, Document.page_count)
        .where(Document.id.in_(doc_ids))
    )
    doc_map = {}
    for row in result.all():
        doc_map[row[0]] = {
            "id": str(row[0]),
            "bates_begin": row[1],
            "title": row[2],
            "text_content": row[3],
            "native_path": row[4],
            "page_count": row[5],
        }

    # Build clusters
    clusters_data = defaultdict(list)
    for i, doc_id in enumerate(doc_ids):
        cluster_id = int(assignments[i])
        doc = doc_map.get(doc_id, {})
        doc["cluster"] = cluster_id
        # Compute distance to cluster centroid for importance scoring
        clusters_data[cluster_id].append((doc_id, doc, X[i]))

    # Compute centroids and score documents
    cluster_centroids = {}
    for cluster_id, members in clusters_data.items():
        member_embeddings = np.array([m[2] for m in members])
        centroid = member_embeddings.mean(axis=0)
        centroid = centroid / (np.linalg.norm(centroid) + 1e-8)
        cluster_centroids[cluster_id] = centroid

        # Score each doc: closeness to centroid (centrality) + text length (substance)
        for doc_id, doc, emb in members:
            similarity = float(np.dot(emb, centroid))
            text_len = len(doc.get("text_content") or "")
            # Importance score: centrality (0-1) weighted with substance
            doc["centrality"] = round(similarity, 4)
            doc["text_length"] = text_len
            doc["importance_score"] = round(similarity * 0.6 + min(text_len / 10000, 1.0) * 0.4, 4)

    # Label each cluster using Claude
    import asyncio

    progress("Labeling topic clusters with AI...", 35)
    sorted_cluster_ids = sorted(clusters_data.keys())
    total_clusters = len(sorted_cluster_ids)
    clusters_report = []
    for idx, cluster_id in enumerate(sorted_cluster_ids):
        members = clusters_data[cluster_id]
        # Sort by importance for labeling
        members.sort(key=lambda m: m[1].get("importance_score", 0), reverse=True)

        top_docs_text = [m[1].get("text_content", "")[:2000] for m in members[:5]]
        top_docs_titles = [m[1].get("title", m[1].get("bates_begin", "")) for m in members[:5]]

        pct = 35 + (idx / total_clusters) * 55  # 35% to 90%
        progress(f"Labeling cluster {idx + 1}/{total_clusters}...", pct)

        label_data = await label_cluster(top_docs_text, top_docs_titles)
        await asyncio.sleep(0.3)  # Rate limit

        # Build cluster report
        doc_summaries = []
        for doc_id, doc, _ in members:
            doc_summaries.append({
                "id": doc["id"],
                "bates_begin": doc["bates_begin"],
                "title": doc["title"],
                "page_count": doc["page_count"],
                "importance_score": doc.get("importance_score", 0),
                "has_native": bool(doc.get("native_path")),
            })

        # Sort by importance within cluster
        doc_summaries.sort(key=lambda d: d["importance_score"], reverse=True)

        clusters_report.append({
            "cluster_id": cluster_id,
            "label": label_data.get("label", "Unlabeled"),
            "summary": label_data.get("summary", ""),
            "themes": label_data.get("themes", []),
            "document_count": len(members),
            "key_documents": doc_summaries[:5],  # Top 5 per cluster
            "all_document_ids": [d["id"] for d in doc_summaries],
        })

    # Sort clusters by size (largest first)
    clusters_report.sort(key=lambda c: c["document_count"], reverse=True)

    # Overall stats
    total_docs = len(doc_ids)
    total_pages = sum(doc_map[d].get("page_count", 0) for d in doc_ids if d in doc_map)
    docs_with_natives = sum(1 for d in doc_ids if doc_map.get(d, {}).get("native_path"))

    # Find globally most important documents
    all_scored = []
    for cluster_id, members in clusters_data.items():
        for doc_id, doc, _ in members:
            all_scored.append(doc)
    all_scored.sort(key=lambda d: d.get("importance_score", 0), reverse=True)

    key_documents = [{
        "id": d["id"],
        "bates_begin": d["bates_begin"],
        "title": d["title"],
        "importance_score": d.get("importance_score", 0),
        "cluster": d.get("cluster"),
    } for d in all_scored[:20]]

    report = {
        "production_id": production_id,
        "total_documents_analyzed": total_docs,
        "total_pages": total_pages,
        "documents_with_native_files": docs_with_natives,
        "cluster_count": len(clusters_report),
        "clusters": clusters_report,
        "key_documents": key_documents,
    }

    progress("Finalizing report...", 95)
    logger.info("Corpus analysis complete: %d docs, %d clusters", total_docs, len(clusters_report))
    return report
