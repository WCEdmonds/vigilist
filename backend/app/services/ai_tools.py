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
