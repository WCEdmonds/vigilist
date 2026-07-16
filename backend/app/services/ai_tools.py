"""Read-only database tools for the AI Agent chat.

Every tool is filtered to the authenticated user's accessible productions.
Tools never write, and never reveal the existence of out-of-scope data.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Document,
    DocumentDuplicate,
    DocumentTag,
    DuplicateGroup,
    Production,
    Tag,
    User,
)
from app.services.ai import _CHAT_DOC_CHAR_LIMIT
from app.services.search import search_documents as _search_documents
from app.services.semantic_search import semantic_search as _semantic_search

logger = logging.getLogger(__name__)

# Cap list-returning tools to a single page so tool results stay bounded.
_TOOL_PAGE_SIZE = 25


@dataclass
class ToolRun:
    """Outcome of running one tool call."""

    result: str          # JSON/text fed back to the model as tool_result content
    result_summary: str  # short human phrase, e.g. "12 documents found"
    ok: bool = True


TOOLS: list[dict] = [
    {
        "name": "search_documents",
        "description": (
            "Full-text search over documents the user can access. Supports quoted "
            "phrases, AND/OR/NOT, and wildcard*. Returns Bates numbers, titles, and "
            "snippets. Use this to find documents by keyword, party, or topic."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms."},
                "production_id": {
                    "type": "integer",
                    "description": "Optional: restrict to one production id.",
                },
                "file_type": {
                    "type": "string",
                    "description": "Optional filter: native, images_only, or an extension group.",
                },
                "page": {"type": "integer", "description": "1-based page. Default 1."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_document",
        "description": (
            "Fetch the full extracted text and metadata for a single document, by its "
            "Bates number or document id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "bates_or_id": {
                    "type": "string",
                    "description": "A Bates number (e.g. ABC-000123) or a document UUID.",
                }
            },
            "required": ["bates_or_id"],
        },
    },
    {
        "name": "list_productions",
        "description": "List the productions the user can access, with document counts.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "find_similar_documents",
        "description": (
            "Find documents semantically similar to a given document (by Bates number "
            "or id), using embeddings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "bates_or_id": {
                    "type": "string",
                    "description": "A Bates number or document UUID.",
                }
            },
            "required": ["bates_or_id"],
        },
    },
    {
        "name": "get_duplicates",
        "description": (
            "List near-duplicate and exact-duplicate documents of a given document "
            "(by Bates number or id), if duplicate detection has been run."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "bates_or_id": {
                    "type": "string",
                    "description": "A Bates number or document UUID.",
                }
            },
            "required": ["bates_or_id"],
        },
    },
    {
        "name": "get_corpus_stats",
        "description": (
            "Summary statistics for one production: total documents, total pages, and "
            "a tag breakdown by category."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "production_id": {"type": "integer", "description": "Production id."}
            },
            "required": ["production_id"],
        },
    },
]

TOOL_NAMES: set[str] = {t["name"] for t in TOOLS}


def tool_use_summary(name: str, tool_input: dict) -> str:
    """A short human-readable phrase describing a tool call, for the UI."""
    if name == "search_documents":
        return f'Searching documents for "{tool_input.get("query", "")}"'
    if name == "get_document":
        return f'Reading document {tool_input.get("bates_or_id", "")}'
    if name == "list_productions":
        return "Listing productions"
    if name == "find_similar_documents":
        return f'Finding documents similar to {tool_input.get("bates_or_id", "")}'
    if name == "get_duplicates":
        return f'Finding duplicates of {tool_input.get("bates_or_id", "")}'
    if name == "get_corpus_stats":
        return f'Gathering stats for production {tool_input.get("production_id", "")}'
    return f"Running {name}"


def _parse_doc_ref(ref: str) -> tuple[UUID | None, str | None]:
    """Interpret a document reference as either a UUID or a Bates string."""
    try:
        return UUID(str(ref).strip()), None
    except (ValueError, AttributeError, TypeError):
        return None, str(ref).strip()


def _clamp_production(production_id, accessible_ids: list[int]) -> int | None:
    """Return production_id only if the user can access it, else None."""
    try:
        pid = int(production_id)
    except (TypeError, ValueError):
        return None
    return pid if pid in accessible_ids else None


async def _resolve_document(db, ref: str, accessible_ids: list[int]) -> "Document | None":
    """Resolve a Bates-or-id ref to a Document the user may see, else None."""
    doc_uuid, bates = _parse_doc_ref(ref)
    if doc_uuid is not None:
        doc = await db.get(Document, doc_uuid)
        if doc and doc.production_id in accessible_ids:
            return doc
        return None
    result = await db.execute(
        select(Document)
        .where(Document.bates_begin == bates)
        .where(Document.production_id.in_(accessible_ids))
        .limit(1)
    )
    return result.scalars().first()


def _doc_brief(doc) -> dict:
    return {
        "id": str(doc.id),
        "bates_begin": doc.bates_begin,
        "bates_end": doc.bates_end,
        "title": doc.title,
        "page_count": doc.page_count,
    }


async def _tool_search_documents(db, user, accessible_ids, tool_input) -> ToolRun:
    query = (tool_input.get("query") or "").strip()
    if not query:
        return ToolRun(result="Error: query is required.", result_summary="No query", ok=False)
    results, total = await _search_documents(
        db, query,
        production_id=_clamp_production(tool_input.get("production_id"), accessible_ids),
        file_type=tool_input.get("file_type"),
        page=int(tool_input.get("page") or 1),
        per_page=_TOOL_PAGE_SIZE,
        accessible_production_ids=accessible_ids,
    )
    hits = [{
        "id": str(r["id"]),
        "bates_begin": r["bates_begin"],
        "title": r["title"],
        "snippet": r["snippet"],
    } for r in results]
    payload = {"total": total, "returned": len(hits), "results": hits}
    plural = "document" if total == 1 else "documents"
    return ToolRun(result=json.dumps(payload), result_summary=f"{total} {plural} found")


async def _tool_get_document(db, user, accessible_ids, tool_input) -> ToolRun:
    doc = await _resolve_document(db, tool_input.get("bates_or_id", ""), accessible_ids)
    if not doc:
        return ToolRun(result="No accessible document matches that reference.",
                       result_summary="Not found", ok=False)
    text = (doc.text_content or "").strip()
    truncated = len(text) > _CHAT_DOC_CHAR_LIMIT
    payload = {
        **_doc_brief(doc),
        "summary": doc.summary,
        "text": text[:_CHAT_DOC_CHAR_LIMIT] + ("\n…[truncated]" if truncated else ""),
    }
    return ToolRun(result=json.dumps(payload), result_summary=f"Read {doc.bates_begin}")


async def _tool_list_productions(db, user, accessible_ids, tool_input) -> ToolRun:
    if not accessible_ids:
        return ToolRun(result=json.dumps({"productions": []}),
                       result_summary="No productions")
    rows = (await db.execute(
        select(Production.id, Production.name, func.count(Document.id))
        .outerjoin(Document, Document.production_id == Production.id)
        .where(Production.id.in_(accessible_ids))
        .group_by(Production.id, Production.name)
        .order_by(Production.name)
    )).all()
    prods = [{"id": pid, "name": name, "doc_count": count} for pid, name, count in rows]
    return ToolRun(result=json.dumps({"productions": prods}),
                   result_summary=f"{len(prods)} productions")


async def _tool_find_similar_documents(db, user, accessible_ids, tool_input) -> ToolRun:
    doc = await _resolve_document(db, tool_input.get("bates_or_id", ""), accessible_ids)
    if not doc:
        return ToolRun(result="No accessible document matches that reference.",
                       result_summary="Not found", ok=False)
    if not (doc.text_content or "").strip():
        return ToolRun(result="That document has no extracted text to compare.",
                       result_summary="No text", ok=False)
    results, _ = await _semantic_search(
        db, doc.text_content[:2000],
        per_page=_TOOL_PAGE_SIZE,
        accessible_production_ids=accessible_ids,
    )
    hits = [{
        "id": str(r["id"]), "bates_begin": r["bates_begin"],
        "title": r["title"], "similarity": r["rank"],
    } for r in results if str(r["id"]) != str(doc.id)][:_TOOL_PAGE_SIZE]
    return ToolRun(result=json.dumps({"results": hits}),
                   result_summary=f"{len(hits)} similar")


async def _tool_get_duplicates(db, user, accessible_ids, tool_input) -> ToolRun:
    doc = await _resolve_document(db, tool_input.get("bates_or_id", ""), accessible_ids)
    if not doc:
        return ToolRun(result="No accessible document matches that reference.",
                       result_summary="Not found", ok=False)
    group_rows = (await db.execute(
        select(DocumentDuplicate.group_id).where(DocumentDuplicate.document_id == doc.id)
    )).all()
    group_ids = [g[0] for g in group_rows]
    if not group_ids:
        return ToolRun(result=json.dumps({"duplicates": []}),
                       result_summary="No duplicates")
    member_rows = (await db.execute(
        select(DocumentDuplicate.document_id, Document.bates_begin,
               Document.title, DocumentDuplicate.similarity, DuplicateGroup.type)
        .join(Document, DocumentDuplicate.document_id == Document.id)
        .join(DuplicateGroup, DocumentDuplicate.group_id == DuplicateGroup.id)
        .where(DocumentDuplicate.group_id.in_(group_ids))
        .where(DocumentDuplicate.document_id != doc.id)
        .where(Document.production_id.in_(accessible_ids))
    )).all()
    dups = [{
        "id": str(did), "bates_begin": bates, "title": title,
        "similarity": sim, "type": dtype,
    } for did, bates, title, sim, dtype in member_rows]
    return ToolRun(result=json.dumps({"duplicates": dups}),
                   result_summary=f"{len(dups)} duplicates")


async def _tool_get_corpus_stats(db, user, accessible_ids, tool_input) -> ToolRun:
    raw_pid = tool_input.get("production_id")
    if raw_pid is None:
        return ToolRun(result="Error: production_id is required.",
                       result_summary="Missing production_id", ok=False)
    pid = _clamp_production(raw_pid, accessible_ids)
    if pid is None:
        return ToolRun(result="That production is not accessible.",
                       result_summary="No access", ok=False)
    total_docs = (await db.execute(
        select(func.count(Document.id)).where(Document.production_id == pid)
    )).scalar() or 0
    total_pages = (await db.execute(
        select(func.coalesce(func.sum(Document.page_count), 0))
        .where(Document.production_id == pid)
    )).scalar() or 0
    tag_rows = (await db.execute(
        select(Tag.category, Tag.name, func.count(DocumentTag.id))
        .join(DocumentTag, Tag.id == DocumentTag.tag_id)
        .join(Document, DocumentTag.document_id == Document.id)
        .where(Document.production_id == pid)
        .group_by(Tag.category, Tag.name)
        .order_by(Tag.category, Tag.name)
    )).all()
    tag_breakdown: dict = {}
    for category, name, count in tag_rows:
        tag_breakdown.setdefault(category, {})[name] = count
    payload = {
        "production_id": pid,
        "total_documents": total_docs,
        "total_pages": int(total_pages),
        "tag_breakdown": tag_breakdown,
    }
    return ToolRun(result=json.dumps(payload),
                   result_summary=f"{total_docs} docs")


_DISPATCH = {
    "search_documents": _tool_search_documents,
    "get_document": _tool_get_document,
    "list_productions": _tool_list_productions,
    "find_similar_documents": _tool_find_similar_documents,
    "get_duplicates": _tool_get_duplicates,
    "get_corpus_stats": _tool_get_corpus_stats,
}


async def run_tool(db, user, accessible_ids: list[int], name: str, tool_input: dict) -> ToolRun:
    """Execute one tool call under the user's access scope. Never raises."""
    impl = _DISPATCH.get(name)
    if impl is None:
        return ToolRun(result=f"Error: unknown tool '{name}'.",
                       result_summary="Unknown tool", ok=False)
    try:
        return await impl(db, user, accessible_ids, tool_input or {})
    except Exception:
        logger.warning("AI tool %s failed", name, exc_info=True)
        return ToolRun(result=f"Error: the '{name}' tool failed.",
                       result_summary="Tool error", ok=False)
