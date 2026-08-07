"""Read-only tools an autonomous agent can run over one production set.

Every tool derives its filters from the `AgentScope` attached to the API key,
never from caller input. There is no production_id or production_set_id
parameter anywhere in these schemas on purpose: an agent cannot widen its own
reach by asking, so the worst a confused (or prompt-injected) agent can do is
read documents its key was already minted for.

Mirrors `app.services.ai_tools`, which serves the in-app chat under a *user's*
accessible-production list. The two differ in what they trust — a user's
scope is a set of productions resolved per request, an agent's is one
immutable row — so they stay separate rather than sharing a dispatcher.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.models import (
    Document,
    DocumentCluster,
    DocumentClusterAssignment,
    DocumentDuplicate,
    DocumentTag,
    DuplicateGroup,
    Entity,
    EntityMention,
    EntityRelationship,
    Tag,
)
from app.services.agent_keys import AgentScope
from app.services.search import search_documents as _search_documents
from app.services.semantic_search import (
    production_set_member_ids,
    semantic_search as _semantic_search,
)

logger = logging.getLogger(__name__)

# Default and ceiling for list-returning tools. The ceiling exists so one call
# can't return a whole volume and blow up an agent's context window.
DEFAULT_LIMIT = 25
MAX_LIMIT = 100
# Full document text is the one unbounded field here; cap it so get_document
# stays a usable tool result rather than a context-window hazard.
DOC_TEXT_CHAR_LIMIT = 20_000


class ToolError(Exception):
    """A tool was called with input it can't act on. Surfaces as HTTP 400."""


def _clamp_limit(value: Any, default: int = DEFAULT_LIMIT) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, MAX_LIMIT))


def _clamp_page(value: Any) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


# ── Scope helpers ───────────────────────────────────────────────────────────


def scope_filters(scope: AgentScope) -> dict:
    """Keyword args that pin a search service call to this key's scope."""
    return {
        "production_id": scope.production_id,
        "production_set_id": scope.production_set_id,
        "accessible_production_ids": [scope.production_id],
    }


def _in_scope_clause(scope: AgentScope):
    """A WHERE clause restricting `Document` rows to the key's scope."""
    if scope.production_set_id is None:
        return Document.production_id == scope.production_id
    return Document.id.in_(production_set_member_ids(scope.production_set_id))


async def _resolve_document(
    db: AsyncSession, scope: AgentScope, ref: str
) -> Document | None:
    """Resolve a Bates-or-UUID reference to a document inside the scope.

    Out-of-scope documents come back as None rather than 403: the agent must
    not be able to tell "exists but not yours" from "does not exist".
    """
    ref = (ref or "").strip()
    if not ref:
        return None
    try:
        doc_uuid = UUID(ref)
    except (ValueError, AttributeError, TypeError):
        doc_uuid = None

    q = select(Document).where(_in_scope_clause(scope))
    if doc_uuid is not None:
        q = q.where(Document.id == doc_uuid)
    else:
        q = q.where(Document.bates_begin == ref)
    return (await db.execute(q.limit(1))).scalars().first()


def _doc_brief(doc: Document) -> dict:
    return {
        "id": str(doc.id),
        "bates_begin": doc.bates_begin,
        "bates_end": doc.bates_end,
        "title": doc.title,
        "page_count": doc.page_count,
        "file_name": doc.file_name,
        "file_type": doc.file_type,
        "custodian": doc.custodian,
        "date_sent": doc.date_sent.isoformat() if doc.date_sent else None,
    }


# ── Tools ───────────────────────────────────────────────────────────────────


async def tool_semantic_search(db: AsyncSession, scope: AgentScope, args: dict) -> dict:
    query = (args.get("query") or "").strip()
    if not query:
        raise ToolError("query is required")
    limit = _clamp_limit(args.get("limit"))
    results, total = await _semantic_search(
        db, query, page=_clamp_page(args.get("page")), per_page=limit,
        **scope_filters(scope),
    )
    return {
        "query": query,
        "mode": "semantic",
        "total": total,
        "returned": len(results),
        "results": [
            {
                "id": str(r["id"]),
                "bates_begin": r["bates_begin"],
                "bates_end": r["bates_end"],
                "title": r["title"],
                "snippet": r["snippet"],
                "similarity": r["rank"],
            }
            for r in results
        ],
    }


async def tool_keyword_search(db: AsyncSession, scope: AgentScope, args: dict) -> dict:
    query = (args.get("query") or "").strip()
    if not query:
        raise ToolError("query is required")
    limit = _clamp_limit(args.get("limit"))
    results, total = await _search_documents(
        db, query, page=_clamp_page(args.get("page")), per_page=limit,
        sort=args.get("sort") if args.get("sort") in ("relevance", "bates") else "relevance",
        file_type=args.get("file_type") or None,
        **scope_filters(scope),
    )
    return {
        "query": query,
        "mode": "keyword",
        "total": total,
        "returned": len(results),
        "results": [
            {
                "id": str(r["id"]),
                "bates_begin": r["bates_begin"],
                "bates_end": r["bates_end"],
                "title": r["title"],
                "snippet": r["snippet"],
                "rank": r["rank"],
            }
            for r in results
        ],
    }


async def tool_get_document(db: AsyncSession, scope: AgentScope, args: dict) -> dict:
    doc = await _resolve_document(db, scope, args.get("bates_or_id", ""))
    if doc is None:
        raise ToolError("No document in scope matches that reference")
    text = (doc.text_content or "").strip()
    truncated = len(text) > DOC_TEXT_CHAR_LIMIT

    tag_rows = (await db.execute(
        select(Tag.category, Tag.name)
        .join(DocumentTag, DocumentTag.tag_id == Tag.id)
        .where(DocumentTag.document_id == doc.id)
    )).all()

    return {
        **_doc_brief(doc),
        "summary": doc.summary,
        "email": {
            "from": doc.email_from,
            "to": doc.email_to,
            "cc": doc.email_cc,
            "subject": doc.email_subject,
        } if doc.email_from or doc.email_subject else None,
        "tags": [{"category": c, "name": n} for c, n in tag_rows],
        "metadata": doc.metadata_ or {},
        "text": text[:DOC_TEXT_CHAR_LIMIT],
        "text_truncated": truncated,
        "text_length": len(text),
    }


async def tool_list_documents(db: AsyncSession, scope: AgentScope, args: dict) -> dict:
    """Paginate the scope's contents in Bates order — the agent's way to see
    what it is working with before searching."""
    limit = _clamp_limit(args.get("limit"))
    page = _clamp_page(args.get("page"))

    total = (await db.execute(
        select(func.count(Document.id)).where(_in_scope_clause(scope))
    )).scalar() or 0

    rows = (await db.execute(
        select(Document)
        # text_content is excluded deliberately: it is the largest column in
        # the table and a listing never shows it.
        .options(load_only(
            Document.id, Document.bates_begin, Document.bates_end, Document.title,
            Document.page_count, Document.file_name, Document.file_type,
            Document.custodian, Document.date_sent,
        ))
        .where(_in_scope_clause(scope))
        .order_by(Document.bates_begin)
        .offset((page - 1) * limit)
        .limit(limit)
    )).scalars().all()

    return {
        "total": total,
        "page": page,
        "per_page": limit,
        "returned": len(rows),
        "documents": [_doc_brief(d) for d in rows],
    }


async def tool_find_similar_documents(
    db: AsyncSession, scope: AgentScope, args: dict
) -> dict:
    doc = await _resolve_document(db, scope, args.get("bates_or_id", ""))
    if doc is None:
        raise ToolError("No document in scope matches that reference")
    text = (doc.text_content or "").strip()
    if not text:
        raise ToolError("That document has no extracted text to compare")
    limit = _clamp_limit(args.get("limit"))

    # Use the document's own opening text as the query. Embedding the whole
    # document would exceed the model's input window on long files, and the
    # opening is where subject/parties/topic live.
    results, _ = await _semantic_search(
        db, text[:2000], per_page=limit + 1, **scope_filters(scope)
    )
    hits = [
        {
            "id": str(r["id"]),
            "bates_begin": r["bates_begin"],
            "title": r["title"],
            "snippet": r["snippet"],
            "similarity": r["rank"],
        }
        for r in results
        if str(r["id"]) != str(doc.id)
    ][:limit]
    return {"source": _doc_brief(doc), "returned": len(hits), "results": hits}


async def tool_get_duplicates(db: AsyncSession, scope: AgentScope, args: dict) -> dict:
    doc = await _resolve_document(db, scope, args.get("bates_or_id", ""))
    if doc is None:
        raise ToolError("No document in scope matches that reference")

    group_ids = [
        g for (g,) in (await db.execute(
            select(DocumentDuplicate.group_id)
            .where(DocumentDuplicate.document_id == doc.id)
        )).all()
    ]
    if not group_ids:
        return {"source": _doc_brief(doc), "returned": 0, "duplicates": []}

    rows = (await db.execute(
        select(Document.id, Document.bates_begin, Document.title,
               DocumentDuplicate.similarity, DuplicateGroup.type)
        .join(DocumentDuplicate, DocumentDuplicate.document_id == Document.id)
        .join(DuplicateGroup, DuplicateGroup.id == DocumentDuplicate.group_id)
        .where(DocumentDuplicate.group_id.in_(group_ids))
        .where(DocumentDuplicate.document_id != doc.id)
        # Duplicate groups span the whole matter, so a set-scoped key must not
        # learn about members that were left out of its volume.
        .where(_in_scope_clause(scope))
        .order_by(DocumentDuplicate.similarity.desc())
        .limit(MAX_LIMIT)
    )).all()

    return {
        "source": _doc_brief(doc),
        "returned": len(rows),
        "duplicates": [
            {
                "id": str(did), "bates_begin": bates, "title": title,
                "similarity": sim, "type": dtype,
            }
            for did, bates, title, sim, dtype in rows
        ],
    }


async def tool_lookup_entity(db: AsyncSession, scope: AgentScope, args: dict) -> dict:
    name = (args.get("name") or "").strip()
    if not name:
        raise ToolError("name is required")
    limit = _clamp_limit(args.get("limit"), default=5)

    entities = (await db.execute(
        select(Entity)
        .where(Entity.production_id == scope.production_id)
        .where(Entity.canonical_name.ilike(f"%{name}%"))
        .order_by(Entity.mention_count.desc())
        .limit(limit)
    )).scalars().all()

    matches = []
    for e in entities:
        matches.append(await _entity_detail(db, scope, e))
    return {"query": name, "returned": len(matches), "matches": matches}


async def tool_list_entities(db: AsyncSession, scope: AgentScope, args: dict) -> dict:
    """The cast of characters, most-mentioned first — how an agent orients
    itself in an unfamiliar matter."""
    limit = _clamp_limit(args.get("limit"))
    entity_type = args.get("entity_type")

    q = select(Entity).where(Entity.production_id == scope.production_id)
    if entity_type in ("person", "org"):
        q = q.where(Entity.entity_type == entity_type)
    entities = (await db.execute(
        q.order_by(Entity.mention_count.desc()).limit(limit)
    )).scalars().all()

    out = []
    for e in entities:
        doc_count = await _entity_doc_count(db, scope, e.id)
        # A set-scoped key sees the matter's ontology, but an entity that no
        # document in its volume mentions is not part of its world.
        if scope.is_set_scoped and doc_count == 0:
            continue
        out.append({
            "entity_id": str(e.id),
            "type": e.entity_type,
            "name": e.canonical_name,
            "aliases": list(e.aliases or [])[:10],
            "case_role": (e.attributes or {}).get("case_role"),
            "mention_count": e.mention_count,
            "document_count": doc_count,
        })
    return {"returned": len(out), "entities": out}


async def _entity_doc_count(db: AsyncSession, scope: AgentScope, entity_id) -> int:
    """How many in-scope documents mention this entity."""
    q = (
        select(func.count(func.distinct(EntityMention.document_id)))
        .where(EntityMention.entity_id == entity_id)
    )
    if scope.is_set_scoped:
        q = q.where(
            EntityMention.document_id.in_(
                production_set_member_ids(scope.production_set_id)
            )
        )
    return (await db.execute(q)).scalar() or 0


async def _entity_detail(db: AsyncSession, scope: AgentScope, e: Entity) -> dict:
    doc_count = await _entity_doc_count(db, scope, e.id)

    edges = (await db.execute(
        select(EntityRelationship.relationship_type, Entity.canonical_name)
        .join(Entity, Entity.id == EntityRelationship.target_entity_id)
        .where(EntityRelationship.source_entity_id == e.id)
        .distinct()
        .limit(20)
    )).all()

    mention_q = (
        select(EntityMention.context_snippet, Document.bates_begin)
        .join(Document, Document.id == EntityMention.document_id)
        .where(EntityMention.entity_id == e.id)
        .where(EntityMention.context_snippet.isnot(None))
        .where(_in_scope_clause(scope))
        .limit(5)
    )
    mentions = (await db.execute(mention_q)).all()

    return {
        "entity_id": str(e.id),
        "type": e.entity_type,
        "name": e.canonical_name,
        "aliases": list(e.aliases or [])[:10],
        "case_role": (e.attributes or {}).get("case_role"),
        "overview": (e.overview or "")[:1500] or None,
        "mention_count": e.mention_count,
        "document_count": doc_count,
        "relationships": [
            {"type": rt, "target": target} for rt, target in edges
        ],
        "sample_mentions": [
            {"bates": bates, "snippet": (snippet or "")[:300]}
            for snippet, bates in mentions
        ],
    }


async def tool_list_clusters(db: AsyncSession, scope: AgentScope, args: dict) -> dict:
    """Topic clusters over the matter, with in-scope document counts."""
    limit = _clamp_limit(args.get("limit"))
    clusters = (await db.execute(
        select(DocumentCluster)
        .where(DocumentCluster.production_id == scope.production_id)
        .order_by(DocumentCluster.doc_count.desc())
        .limit(limit)
    )).scalars().all()

    out = []
    for c in clusters:
        count_q = (
            select(func.count(DocumentClusterAssignment.id))
            .where(DocumentClusterAssignment.cluster_id == c.id)
        )
        if scope.is_set_scoped:
            count_q = count_q.where(
                DocumentClusterAssignment.document_id.in_(
                    production_set_member_ids(scope.production_set_id)
                )
            )
        in_scope = (await db.execute(count_q)).scalar() or 0
        if scope.is_set_scoped and in_scope == 0:
            continue
        out.append({
            "cluster_id": c.id,
            "index": c.cluster_index,
            "label": c.label,
            "document_count": in_scope,
        })
    return {"returned": len(out), "clusters": out}


async def tool_get_cluster_documents(
    db: AsyncSession, scope: AgentScope, args: dict
) -> dict:
    try:
        cluster_id = int(args.get("cluster_id"))
    except (TypeError, ValueError):
        raise ToolError("cluster_id is required and must be an integer")

    cluster = (await db.execute(
        select(DocumentCluster)
        .where(DocumentCluster.id == cluster_id)
        .where(DocumentCluster.production_id == scope.production_id)
    )).scalars().first()
    if cluster is None:
        raise ToolError("No cluster in scope matches that id")

    limit = _clamp_limit(args.get("limit"))
    page = _clamp_page(args.get("page"))
    rows = (await db.execute(
        select(Document)
        .options(load_only(
            Document.id, Document.bates_begin, Document.bates_end, Document.title,
            Document.page_count, Document.file_name, Document.file_type,
            Document.custodian, Document.date_sent,
        ))
        .join(DocumentClusterAssignment,
              DocumentClusterAssignment.document_id == Document.id)
        .where(DocumentClusterAssignment.cluster_id == cluster_id)
        .where(_in_scope_clause(scope))
        .order_by(Document.bates_begin)
        .offset((page - 1) * limit)
        .limit(limit)
    )).scalars().all()

    return {
        "cluster_id": cluster_id,
        "label": cluster.label,
        "page": page,
        "per_page": limit,
        "returned": len(rows),
        "documents": [_doc_brief(d) for d in rows],
    }


async def tool_get_scope_stats(db: AsyncSession, scope: AgentScope, args: dict) -> dict:
    """Size and tag breakdown of what this key can see."""
    total_docs = (await db.execute(
        select(func.count(Document.id)).where(_in_scope_clause(scope))
    )).scalar() or 0
    total_pages = (await db.execute(
        select(func.coalesce(func.sum(Document.page_count), 0))
        .where(_in_scope_clause(scope))
    )).scalar() or 0
    tag_rows = (await db.execute(
        select(Tag.category, Tag.name, func.count(DocumentTag.id))
        .join(DocumentTag, DocumentTag.tag_id == Tag.id)
        .join(Document, Document.id == DocumentTag.document_id)
        .where(_in_scope_clause(scope))
        .group_by(Tag.category, Tag.name)
        .order_by(Tag.category, Tag.name)
    )).all()
    breakdown: dict = {}
    for category, name, count in tag_rows:
        breakdown.setdefault(category or "uncategorized", {})[name] = count

    custodian_rows = (await db.execute(
        select(Document.custodian, func.count(Document.id))
        .where(_in_scope_clause(scope))
        .where(Document.custodian.isnot(None))
        .group_by(Document.custodian)
        .order_by(func.count(Document.id).desc())
        .limit(25)
    )).all()

    return {
        "production_id": scope.production_id,
        "production_set_id": scope.production_set_id,
        "total_documents": total_docs,
        "total_pages": int(total_pages),
        "tag_breakdown": breakdown,
        "custodians": [{"name": c, "document_count": n} for c, n in custodian_rows],
    }


DISPATCH = {
    "semantic_search": tool_semantic_search,
    "keyword_search": tool_keyword_search,
    "get_document": tool_get_document,
    "list_documents": tool_list_documents,
    "find_similar_documents": tool_find_similar_documents,
    "get_duplicates": tool_get_duplicates,
    "lookup_entity": tool_lookup_entity,
    "list_entities": tool_list_entities,
    "list_clusters": tool_list_clusters,
    "get_cluster_documents": tool_get_cluster_documents,
    "get_scope_stats": tool_get_scope_stats,
}


async def run_tool(
    db: AsyncSession, scope: AgentScope, name: str, args: dict | None
) -> dict:
    """Execute one tool by name. Raises ToolError for bad input."""
    impl = DISPATCH.get(name)
    if impl is None:
        raise ToolError(f"Unknown tool '{name}'")
    return await impl(db, scope, args or {})
