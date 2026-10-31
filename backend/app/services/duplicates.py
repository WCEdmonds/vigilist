"""Near-duplicate detection using MinHash (exact) and embedding similarity (conceptual)."""

import asyncio
import logging
from collections import defaultdict

import numpy as np
from datasketch import MinHash, MinHashLSH
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, DocumentDuplicate, DuplicateGroup
from app.services.document_embeddings import get_document_embeddings

logger = logging.getLogger(__name__)


def group_by_hash(rows: list[tuple[str, str]]) -> list[list[str]]:
    """Group document ids by identical SHA-256 hash.

    ``rows`` is a list of ``(doc_id, sha256)``. Returns a list of doc-id
    groups, each of size >= 2 (a hash held by one doc is not a duplicate).
    Rows with an empty/None hash are ignored. First-seen order is preserved
    for both groups and members (deterministic).
    """
    buckets: dict[str, list[str]] = {}
    for doc_id, sha in rows:
        if not sha:
            continue
        buckets.setdefault(sha, []).append(doc_id)
    return [ids for ids in buckets.values() if len(ids) >= 2]


def _compute_minhash(text: str, num_perm: int = 128) -> MinHash:
    """Compute MinHash signature from word 3-grams."""
    m = MinHash(num_perm=num_perm)
    words = text.lower().split()
    for i in range(len(words) - 2):
        gram = " ".join(words[i:i + 3])
        m.update(gram.encode("utf-8"))
    return m


def _find_connected_components(pairs: list[tuple[str, str, float]]) -> list[list[tuple[str, float]]]:
    """Group pairs into connected components. Returns list of groups,
    each group is list of (doc_id, max_similarity)."""
    if not pairs:
        return []

    # Build adjacency list
    adj: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for a, b, sim in pairs:
        adj[a].append((b, sim))
        adj[b].append((a, sim))

    visited = set()
    components = []

    for node in adj:
        if node in visited:
            continue
        # BFS
        component = {}
        queue = [node]
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            # Track max similarity for this node
            max_sim = max((s for _, s in adj[current]), default=0.0)
            component[current] = max_sim
            for neighbor, _ in adj[current]:
                if neighbor not in visited:
                    queue.append(neighbor)
        if len(component) > 1:
            components.append([(doc_id, sim) for doc_id, sim in component.items()])

    return components


def _similar_pairs(ids: list[str], M: np.ndarray) -> list[tuple[str, str, float]]:
    """Pairs with cosine similarity in [0.80, 0.95) (exact dupes are MinHash's
    job). M rows are unit-normalized; row blocks bound memory."""
    pairs = []
    n = len(ids)
    for start in range(0, n, 500):
        sims = M[start:start + 500] @ M.T
        rows, cols = np.nonzero((sims >= 0.80) & (sims < 0.95))
        gi = rows + start
        keep = cols > gi  # each unordered pair once
        for i, j in zip(gi[keep], cols[keep]):
            pairs.append((ids[i], ids[j], float(sims[i - start, j])))
    return pairs


def _compute_groups(hash_rows, docs, embeddings):
    """All the CPU work, with no DB access: callers run it in a thread.
    Returns (hash_components, exact_components, similar_components)."""
    hash_components = group_by_hash(hash_rows)
    logger.info("Hash: found %d byte-identical groups", len(hash_components))
    if len(docs) < 2:
        return hash_components, [], []

    # --- MinHash exact duplicates (95%+) ---
    exact_pairs = []
    lsh = MinHashLSH(threshold=0.95, num_perm=128)
    minhashes = {}

    for doc_id, text in docs:
        if len(text.split()) < 5:
            continue
        mh = _compute_minhash(text)
        minhashes[doc_id] = mh
        # Check for existing similar docs before inserting
        try:
            candidates = lsh.query(mh)
            for cand_id in candidates:
                sim = mh.jaccard(minhashes[cand_id])
                if sim >= 0.95:
                    exact_pairs.append((doc_id, cand_id, sim))
            lsh.insert(doc_id, mh)
        except ValueError:
            pass  # Duplicate key

    exact_components = _find_connected_components(exact_pairs)
    logger.info("MinHash: found %d exact duplicate groups from %d pairs", len(exact_components), len(exact_pairs))

    # --- Embedding similarity (80-95%) ---
    similar_pairs = []
    if embeddings:
        doc_ids_with_emb = list(embeddings.keys())
        emb_matrix = np.array([embeddings[d] for d in doc_ids_with_emb])
        norms = np.linalg.norm(emb_matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1
        similar_pairs = _similar_pairs(doc_ids_with_emb, emb_matrix / norms)

    similar_components = _find_connected_components(similar_pairs)
    logger.info("Embeddings: found %d similar groups from %d pairs", len(similar_components), len(similar_pairs))
    return hash_components, exact_components, similar_components


async def detect_duplicates(
    db: AsyncSession,
    production_id: int,
) -> dict:
    """Run full duplicate detection for a production.

    Returns { hash_groups: int, exact_groups: int, similar_groups: int,
    total_documents_grouped: int }
    """
    hash_result = await db.execute(
        select(Document.id, Document.file_hash_sha256)
        .where(Document.production_id == production_id)
        .where(Document.file_hash_sha256.isnot(None))
        .where(Document.file_hash_sha256 != "")
    )
    hash_rows = [(str(r[0]), r[1]) for r in hash_result.all()]

    result = await db.execute(
        select(Document.id, Document.text_content)
        .where(Document.production_id == production_id)
        .where(Document.text_content.isnot(None))
        .where(Document.text_content != "")
    )
    docs = [(str(row[0]), row[1]) for row in result.all()]
    logger.info("Duplicate detection: %d documents with text", len(docs))
    embeddings = await get_document_embeddings(db, production_id) if len(docs) >= 2 else {}

    # Reads are done: end the transaction so no connection sits idle through
    # the CPU work, which runs in a thread so the instance keeps serving.
    await db.commit()
    hash_components, exact_components, similar_components = await asyncio.to_thread(
        _compute_groups, hash_rows, docs, embeddings)

    # Replace the previous results in one short transaction.
    old_groups = await db.execute(
        select(DuplicateGroup.id).where(DuplicateGroup.production_id == production_id)
    )
    old_ids = [row[0] for row in old_groups.all()]
    if old_ids:
        await db.execute(delete(DocumentDuplicate).where(DocumentDuplicate.group_id.in_(old_ids)))
        await db.execute(delete(DuplicateGroup).where(DuplicateGroup.id.in_(old_ids)))

    counts = {}
    for kind, components in (("hash", hash_components), ("exact", exact_components),
                             ("similar", similar_components)):
        counts[kind] = 0
        for component in components:
            group = DuplicateGroup(production_id=production_id, type=kind)
            db.add(group)
            await db.flush()
            for member in component:
                doc_id, sim = (member, 1.0) if kind == "hash" else member
                db.add(DocumentDuplicate(document_id=doc_id, group_id=group.id, similarity=sim))
                counts[kind] += 1

    await db.commit()

    return {
        "hash_groups": len(hash_components),
        "exact_groups": len(exact_components),
        "similar_groups": len(similar_components),
        "total_documents_grouped": counts["hash"] + counts["exact"] + counts["similar"],
    }
