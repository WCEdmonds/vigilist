# AI Agent: Non-blocking Popup + Read-only DB Tools — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert the AI Agent chat from a blocking modal into a corner-docked, resizable, non-modal popup, and give the agent read-only tool-use so it can query the corpus itself.

**Architecture:** Backend `/api/ai/chat` becomes a bounded tool-use loop (`stream_chat_events`) that streams text, executes read-only tools (`ai_tools.run_tool`), and feeds results back, all over SSE. New SSE event types (`tool_use`, `tool_result`) surface tool activity. Frontend drops the dim overlay so the site stays interactive, adds corner-resize with persisted size, and renders tool-activity rows.

**Tech Stack:** FastAPI + SQLAlchemy async + Anthropic Python SDK (`messages.stream`, tool use) on the backend; React + TypeScript + Vite on the frontend; pytest (async via `asyncio.run` + `FakeSession`) for backend tests.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-16-ai-agent-popup-and-db-tools-design.md`.
- All tools are **read-only**. No writes, no audit-log entries from tools.
- Every tool is filtered to `get_accessible_production_ids(db, user)` — a document/production outside the user's scope must be unreachable and must not leak existence.
- Chat model stays `CHAT_MODEL = "claude-opus-4-8"` (already in `backend/app/services/ai.py`).
- Tool loop bounded by `MAX_TOOL_ROUNDS = 8`.
- Per-tool result token caps: reuse `_CHAT_DOC_CHAR_LIMIT = 12000` for document text; search/similar/duplicate result lists capped to one page (≤ 25 items).
- Preserve existing behavior: streaming, transcript copy/download, attached-doc chips, "Send to AI Agent" flow, session-only persistence (panel stays mounted), `<768px` full-screen fallback.
- Backend tests run **without a database**, following `backend/tests/test_org_access.py` (`FakeSession`, `asyncio.run`). Do not require Postgres.
- Run backend tests from `backend/` with `python -m pytest`. Run frontend checks from `frontend/` with `npm run build`.

---

## File Structure

**Backend**
- `backend/app/services/ai_tools.py` *(new)* — tool JSON-schema definitions, per-tool read-only implementations, `run_tool` dispatch, `tool_use_summary`, and the `ToolRun` dataclass. Single responsibility: "what tools exist and how they run under a user's access scope."
- `backend/app/services/ai_chat.py` *(new)* — `stream_chat_events`, the provider-agnostic streaming tool loop yielding SSE strings. Single responsibility: "drive the model↔tool conversation and serialize it to SSE." Kept separate from `ai_tools` so it is testable with a fake client and fake tool runner.
- `backend/app/routers/ai.py` *(modify `chat`)* — build system prompt + accessible ids, bind a `run_tool` closure, hand off to `stream_chat_events`.
- `backend/tests/test_ai_tools.py` *(new)* — schema integrity, dispatch routing, access-scope enforcement, summaries.
- `backend/tests/test_ai_chat.py` *(new)* — loop behavior with a fake stream (delta/tool/done, MAX_TOOL_ROUNDS).

**Frontend**
- `frontend/src/api/client.ts` *(modify `streamChat`)* — parse `tool_use` / `tool_result` frames; add `onToolUse` / `onToolResult` handlers.
- `frontend/src/components/AIAgent.tsx` *(modify)* — remove overlay wrapper; dock panel; add corner resize + persisted size; scope Escape to panel focus; render tool-activity rows.
- `frontend/src/styles/components.css` *(modify)* — replace `.ai-agent-overlay` / modal `.ai-agent-panel` with docked panel + resize-handle styles; add `.ai-agent-activity` rows; keep the `<768px` fallback.

---

## Task 1: Tool schemas + pure helpers (`ai_tools.py`)

**Files:**
- Create: `backend/app/services/ai_tools.py`
- Test: `backend/tests/test_ai_tools.py`

**Interfaces:**
- Produces:
  - `TOOLS: list[dict]` — Anthropic tool definitions; each has `name`, `description`, `input_schema`.
  - `TOOL_NAMES: set[str]` — the tool names.
  - `tool_use_summary(name: str, tool_input: dict) -> str` — human phrase for a call, e.g. `Searching documents for "termination"`.
  - `@dataclass ToolRun: result: str; result_summary: str; ok: bool = True`.
  - `_parse_doc_ref(ref: str) -> tuple[UUID | None, str | None]` — returns `(uuid, None)` if `ref` parses as a UUID, else `(None, ref)` treating it as a Bates number.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_ai_tools.py`:

```python
"""Unit tests for AI Agent tool definitions and pure helpers."""

import uuid

from app.services import ai_tools


def test_tools_are_well_formed():
    assert ai_tools.TOOLS, "expected at least one tool"
    names = [t["name"] for t in ai_tools.TOOLS]
    assert len(names) == len(set(names)), "tool names must be unique"
    for tool in ai_tools.TOOLS:
        assert tool["name"]
        assert tool["description"].strip()
        schema = tool["input_schema"]
        assert schema["type"] == "object"
        assert "properties" in schema
    assert ai_tools.TOOL_NAMES == set(names)


def test_expected_tool_set():
    assert ai_tools.TOOL_NAMES == {
        "search_documents",
        "get_document",
        "list_productions",
        "find_similar_documents",
        "get_duplicates",
        "get_corpus_stats",
    }


def test_tool_use_summary_reads_naturally():
    s = ai_tools.tool_use_summary("search_documents", {"query": "termination"})
    assert "termination" in s
    assert ai_tools.tool_use_summary("list_productions", {}) == "Listing productions"
    got = ai_tools.tool_use_summary("get_document", {"bates_or_id": "ABC-001"})
    assert "ABC-001" in got


def test_parse_doc_ref_uuid_vs_bates():
    u = uuid.uuid4()
    parsed_uuid, parsed_bates = ai_tools._parse_doc_ref(str(u))
    assert parsed_uuid == u and parsed_bates is None
    parsed_uuid2, parsed_bates2 = ai_tools._parse_doc_ref("ABC-000123")
    assert parsed_uuid2 is None and parsed_bates2 == "ABC-000123"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ai_tools.py -v` (from `backend/`)
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.ai_tools'`.

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/services/ai_tools.py`:

```python
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
```

> Note: `DocumentDuplicate`, `DuplicateGroup`, `Production`, `Tag`, `DocumentTag`, `Document`, `User` are all defined in `backend/app/models.py`. `_CHAT_DOC_CHAR_LIMIT` is defined in `backend/app/services/ai.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ai_tools.py -v` (from `backend/`)
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/ai_tools.py backend/tests/test_ai_tools.py
git commit -m "feat(ai): tool schemas and pure helpers for AI Agent"
```

---

## Task 2: Tool implementations + `run_tool` dispatch (`ai_tools.py`)

**Files:**
- Modify: `backend/app/services/ai_tools.py`
- Test: `backend/tests/test_ai_tools.py`

**Interfaces:**
- Consumes (from Task 1): `ToolRun`, `TOOL_NAMES`, `_parse_doc_ref`, `_TOOL_PAGE_SIZE`.
- Produces:
  - `async def run_tool(db: AsyncSession, user: User, accessible_ids: list[int], name: str, tool_input: dict) -> ToolRun`
  - Per-tool `async def _tool_*` implementations (internal).

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_ai_tools.py`:

```python
import asyncio

import pytest


class _FakeUser:
    def __init__(self, uid="u1", email="a@thirulaw.com"):
        self.id = uid
        self.email = email


async def _run(name, tool_input, monkeypatched):
    """Helper: call run_tool with a dummy db/user and captured monkeypatches."""
    return await ai_tools.run_tool(
        db=object(), user=_FakeUser(), accessible_ids=[1, 2],
        name=name, tool_input=tool_input,
    )


def test_run_tool_unknown_name_is_not_ok():
    run = asyncio.run(_run("nope", {}, None))
    assert run.ok is False
    assert "unknown" in run.result.lower()


def test_run_tool_routes_search(monkeypatch):
    calls = {}

    async def fake_search(db, query, **kwargs):
        calls["query"] = query
        calls["accessible"] = kwargs.get("accessible_production_ids")
        return ([{"id": "d1", "bates_begin": "ABC-1", "bates_end": "ABC-1",
                  "title": "T", "snippet": "snip", "page_count": 1,
                  "production_id": 1, "rank": 0.5}], 1)

    monkeypatch.setattr(ai_tools, "_search_documents", fake_search)
    run = asyncio.run(ai_tools.run_tool(
        db=object(), user=_FakeUser(), accessible_ids=[1, 2],
        name="search_documents", tool_input={"query": "hello"},
    ))
    assert run.ok is True
    assert calls["query"] == "hello"
    assert calls["accessible"] == [1, 2]      # access scope always passed through
    assert "1 document" in run.result_summary


def test_run_tool_search_forces_accessible_scope(monkeypatch):
    """Even if the model asks for a production it cannot see, scope wins."""
    seen = {}

    async def fake_search(db, query, **kwargs):
        seen["production_id"] = kwargs.get("production_id")
        seen["accessible"] = kwargs.get("accessible_production_ids")
        return ([], 0)

    monkeypatch.setattr(ai_tools, "_search_documents", fake_search)
    asyncio.run(ai_tools.run_tool(
        db=object(), user=_FakeUser(), accessible_ids=[1, 2],
        name="search_documents", tool_input={"query": "x", "production_id": 999},
    ))
    # production_id 999 is not accessible -> dropped, only accessible scope applies
    assert seen["production_id"] is None
    assert seen["accessible"] == [1, 2]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ai_tools.py -v`
Expected: FAIL — `AttributeError: module 'app.services.ai_tools' has no attribute 'run_tool'`.

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/services/ai_tools.py`:

```python
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
    } for r in results if str(r["id"]) != str(doc.id)]
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
    pid = _clamp_production(tool_input.get("production_id"), accessible_ids)
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
```

> Confirm `DocumentDuplicate` and `DuplicateGroup` are importable from `app.models` (they are used in `backend/app/routers/intelligence.py`). If either is missing from `app/models.py`, import it from where `intelligence.py` imports it and update the Task-1 import block accordingly.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ai_tools.py -v`
Expected: PASS (all tests, including Task-1 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/ai_tools.py backend/tests/test_ai_tools.py
git commit -m "feat(ai): read-only DB tools with access-scoped dispatch"
```

---

## Task 3: Streaming tool loop (`ai_chat.py`)

**Files:**
- Create: `backend/app/services/ai_chat.py`
- Test: `backend/tests/test_ai_chat.py`

**Interfaces:**
- Consumes (from Task 1/2): `ToolRun` (via the injected `run_tool` callable), `tool_use_summary` (injected as `describe_call`).
- Produces:
  - `async def stream_chat_events(client, system, messages, describe_call, run_tool, *, tools, model=CHAT_MODEL, max_rounds=MAX_TOOL_ROUNDS) -> AsyncIterator[str]`
    - `client`: object with `.messages.stream(...)` returning an async context manager exposing `.text_stream` (async iter of `str`) and `await .get_final_message()` (→ object with `.stop_reason` and `.content` list of blocks; tool_use blocks have `.type == "tool_use"`, `.id`, `.name`, `.input`).
    - `describe_call(name, input) -> str`; `run_tool(name, input) -> ToolRun` (awaitable).
    - Yields SSE strings: `data: {json}\n\n` with `type` in `delta|tool_use|tool_result|done|error`.
  - `MAX_TOOL_ROUNDS = 8`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_ai_chat.py`:

```python
"""Unit tests for the AI Agent streaming tool loop, with a fake client."""

import asyncio
import json

from app.services import ai_chat
from app.services.ai_tools import ToolRun


class _Block:
    def __init__(self, type, id=None, name=None, input=None, text=None):
        self.type = type
        self.id = id
        self.name = name
        self.input = input or {}
        self.text = text


class _FinalMessage:
    def __init__(self, stop_reason, content):
        self.stop_reason = stop_reason
        self.content = content


class _FakeStream:
    """One streamed turn: yields text chunks, then a final message."""
    def __init__(self, texts, final):
        self._texts = texts
        self._final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def text_stream(self):
        async def gen():
            for t in self._texts:
                yield t
        return gen()

    async def get_final_message(self):
        return self._final


class _FakeMessages:
    def __init__(self, turns):
        self._turns = list(turns)

    def stream(self, **kwargs):
        return self._turns.pop(0)


class _FakeClient:
    def __init__(self, turns):
        self.messages = _FakeMessages(turns)


async def _collect(agen):
    out = []
    async for item in agen:
        out.append(item)
    return out


def _events(sse_strings):
    evts = []
    for s in sse_strings:
        assert s.startswith("data: ") and s.endswith("\n\n")
        evts.append(json.loads(s[len("data: "):].strip()))
    return evts


def _describe(name, tool_input):
    return f"calling {name}"


def test_plain_answer_no_tools():
    turn = _FakeStream(["Hello ", "world"], _FinalMessage("end_turn", [_Block("text", text="Hello world")]))
    client = _FakeClient([turn])

    async def run_tool(name, tool_input):
        raise AssertionError("should not be called")

    events = _events(asyncio.run(_collect(
        ai_chat.stream_chat_events(client, "sys", [{"role": "user", "content": "hi"}],
                                   _describe, run_tool, tools=[])
    )))
    types = [e["type"] for e in events]
    assert types == ["delta", "delta", "done"]


def test_one_tool_round_then_answer():
    turn1 = _FakeStream(
        ["Let me look. "],
        _FinalMessage("tool_use", [_Block("tool_use", id="t1", name="search_documents",
                                          input={"query": "x"})]),
    )
    turn2 = _FakeStream(["Found it."], _FinalMessage("end_turn", [_Block("text", text="Found it.")]))
    client = _FakeClient([turn1, turn2])

    async def run_tool(name, tool_input):
        assert name == "search_documents"
        return ToolRun(result='{"total": 3}', result_summary="3 documents found")

    events = _events(asyncio.run(_collect(
        ai_chat.stream_chat_events(client, "sys", [{"role": "user", "content": "find x"}],
                                   _describe, run_tool, tools=[{"name": "search_documents"}])
    )))
    types = [e["type"] for e in events]
    assert "tool_use" in types and "tool_result" in types
    assert types[-1] == "done"
    tr = next(e for e in events if e["type"] == "tool_result")
    assert tr["ok"] is True and tr["summary"] == "3 documents found"


def test_max_rounds_terminates():
    # Every turn asks for a tool -> would loop forever without the cap.
    def make_tool_turn():
        return _FakeStream([], _FinalMessage("tool_use",
            [_Block("tool_use", id="t", name="search_documents", input={"query": "x"})]))
    # max_rounds turns that call tools + 1 final no-tool turn
    turns = [make_tool_turn() for _ in range(2)]
    turns.append(_FakeStream(["done"], _FinalMessage("end_turn", [_Block("text", text="done")])))
    client = _FakeClient(turns)

    async def run_tool(name, tool_input):
        return ToolRun(result="{}", result_summary="ok")

    events = _events(asyncio.run(_collect(
        ai_chat.stream_chat_events(client, "sys", [{"role": "user", "content": "x"}],
                                   _describe, run_tool, tools=[{"name": "search_documents"}],
                                   max_rounds=2)
    )))
    assert events[-1]["type"] == "done"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ai_chat.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.ai_chat'`.

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/services/ai_chat.py`:

```python
"""The AI Agent streaming tool loop: model <-> tools, serialized to SSE.

Provider-agnostic and dependency-injected so it can be tested with a fake
client and fake tool runner (no network, no database).
"""

import json
import logging

from app.services.ai import CHAT_MODEL

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 8

_MAX_TOKENS = 4096


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


async def _stream_text(stream_cm, on_delta):
    """Consume one streamed turn, forwarding text deltas; return final message."""
    async with stream_cm as stream:
        async for text in stream.text_stream:
            on_delta(text)
        return await stream.get_final_message()


async def stream_chat_events(
    client, system, messages, describe_call, run_tool,
    *, tools, model=CHAT_MODEL, max_rounds=MAX_TOOL_ROUNDS,
):
    """Drive the tool loop, yielding SSE frame strings.

    - describe_call(name, input) -> str : human phrase shown before a tool runs.
    - run_tool(name, input) -> ToolRun  : awaitable that executes the tool.
    """
    convo = list(messages)

    for _ in range(max_rounds):
        deltas: list[str] = []
        try:
            final = await _stream_text(
                client.messages.stream(
                    model=model, max_tokens=_MAX_TOKENS, system=system,
                    tools=tools, messages=convo,
                ),
                deltas.append,
            )
        except Exception:
            logger.warning("AI chat stream failed", exc_info=True)
            for d in deltas:
                yield _sse({"type": "delta", "text": d})
            yield _sse({"type": "error", "message": "The AI service failed to respond."})
            return

        for d in deltas:
            yield _sse({"type": "delta", "text": d})

        if final.stop_reason != "tool_use":
            yield _sse({"type": "done"})
            return

        convo.append({"role": "assistant", "content": final.content})
        tool_results = []
        for block in final.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            yield _sse({"type": "tool_use", "name": block.name,
                        "summary": describe_call(block.name, block.input)})
            run = await run_tool(block.name, block.input)
            yield _sse({"type": "tool_result", "name": block.name,
                        "ok": run.ok, "summary": run.result_summary})
            tool_results.append({
                "type": "tool_result", "tool_use_id": block.id, "content": run.result,
            })
        convo.append({"role": "user", "content": tool_results})

    # Reached the round cap: ask once more with no tools for a final answer.
    deltas = []
    try:
        final = await _stream_text(
            client.messages.stream(
                model=model, max_tokens=_MAX_TOKENS, system=system, messages=convo,
            ),
            deltas.append,
        )
    except Exception:
        logger.warning("AI chat final stream failed", exc_info=True)
        for d in deltas:
            yield _sse({"type": "delta", "text": d})
        yield _sse({"type": "error", "message": "The AI service failed to respond."})
        return
    for d in deltas:
        yield _sse({"type": "delta", "text": d})
    yield _sse({"type": "done"})
```

> Note: deltas are buffered per turn then emitted, so a mid-turn exception surfaces as a clean `error` frame rather than a half-streamed turn. This keeps the fake-stream tests deterministic and the real UI robust.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ai_chat.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/ai_chat.py backend/tests/test_ai_chat.py
git commit -m "feat(ai): streaming tool loop with bounded rounds and SSE frames"
```

---

## Task 4: Wire tools into `/api/ai/chat`

**Files:**
- Modify: `backend/app/routers/ai.py:133-205` (the `chat` endpoint body and imports)

**Interfaces:**
- Consumes: `ai_chat.stream_chat_events`, `ai_tools.TOOLS`, `ai_tools.run_tool`, `ai_tools.tool_use_summary`, existing `build_chat_system_prompt`, `get_accessible_production_ids`.
- Produces: unchanged route contract (`POST /api/ai/chat`, SSE), now tool-enabled.

- [ ] **Step 1: Update imports**

In `backend/app/routers/ai.py`, extend the service imports. The current block imports from `app.services.ai` (lines ~17-25) — add the new modules after it:

```python
from app.services.ai_chat import stream_chat_events
from app.services.ai_tools import TOOLS, run_tool, tool_use_summary
```

Remove now-unused imports from the old inline stream if any (`CHAT_MODEL` and `json` may still be used by other parts — leave them if referenced elsewhere; the linter/tests will flag genuinely dead imports).

- [ ] **Step 2: Replace the `event_stream` body with the tool loop**

Replace the `async def event_stream(): ...` block and the `return StreamingResponse(...)` (currently `ai.py:182-204`) with:

```python
    accessible = await get_accessible_production_ids(db, user)  # (already computed above; reuse it)

    import anthropic
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def _run(name: str, tool_input: dict):
        return await run_tool(db, user, accessible, name, tool_input)

    return StreamingResponse(
        stream_chat_events(
            client, system, messages,
            describe_call=tool_use_summary,
            run_tool=_run,
            tools=TOOLS,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

> `accessible` is already computed earlier in `chat()` (the doc-resolution block at `ai.py:169`). Reuse that variable rather than recomputing — delete this duplicate line if it already exists above; the closure just needs `accessible` in scope.

- [ ] **Step 3: Verify the full backend test suite still passes**

Run: `python -m pytest -q` (from `backend/`)
Expected: PASS — existing tests plus `test_ai_tools.py` and `test_ai_chat.py`. No test hits the network (the route itself isn't exercised without a live Anthropic key; the loop is covered by `test_ai_chat.py`).

- [ ] **Step 4: Smoke-check import wiring**

Run: `python -c "import app.routers.ai"` (from `backend/`, with the venv active)
Expected: no ImportError.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/ai.py
git commit -m "feat(ai): enable read-only DB tools in the chat endpoint"
```

---

## Task 5: Frontend — parse tool frames in `streamChat`

**Files:**
- Modify: `frontend/src/api/client.ts:240-308`

**Interfaces:**
- Produces: `streamChat(messages, docIds, handlers, signal)` where `handlers` gains optional `onToolUse(evt: {name: string; summary: string})` and `onToolResult(evt: {name: string; ok: boolean; summary: string})`.

- [ ] **Step 1: Widen the handlers type**

Change the `streamChat` signature's `handlers` parameter type to:

```typescript
  handlers: {
    onDelta: (text: string) => void;
    onError: (message: string) => void;
    onToolUse?: (evt: { name: string; summary: string }) => void;
    onToolResult?: (evt: { name: string; ok: boolean; summary: string }) => void;
  },
```

- [ ] **Step 2: Handle the new frame types**

In the SSE parse loop, extend the `evt.type` dispatch (currently handles `delta` and `error`) to also handle the tool frames:

```typescript
          const evt = JSON.parse(payload);
          if (evt.type === 'delta' && typeof evt.text === 'string') handlers.onDelta(evt.text);
          else if (evt.type === 'tool_use') handlers.onToolUse?.({ name: evt.name, summary: evt.summary });
          else if (evt.type === 'tool_result') handlers.onToolResult?.({ name: evt.name, ok: !!evt.ok, summary: evt.summary });
          else if (evt.type === 'error') handlers.onError(evt.message || 'The AI service failed to respond.');
```

- [ ] **Step 3: Typecheck / build**

Run: `npm run build` (from `frontend/`)
Expected: build succeeds (existing `AIAgent.tsx` still compiles — the new handlers are optional).

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/client.ts
git commit -m "feat(ai): parse tool_use/tool_result SSE frames in streamChat"
```

---

## Task 6: Frontend — non-blocking popup + tool activity rows (`AIAgent.tsx`)

**Files:**
- Modify: `frontend/src/components/AIAgent.tsx`

**Interfaces:**
- Consumes: `streamChat` with `onToolUse` / `onToolResult` (Task 5).
- Produces: unchanged `AIAgent` props (`open`, `onClose`, `attachedDocs`, `onRemoveDoc`).

- [ ] **Step 1: Remove the modal overlay wrapper**

Replace the outer wrapper at the top of the returned JSX (`AIAgent.tsx:138-139`):

```tsx
  return (
    <div className="ai-agent-overlay" onClick={onClose}>
      <div className="ai-agent-panel" onClick={e => e.stopPropagation()} role="dialog" aria-label="AI Agent">
```

with a single docked panel (no backdrop, no outside-click close):

```tsx
  return (
    <div
      className="ai-agent-panel"
      role="dialog"
      aria-label="AI Agent"
      style={{ width: size.w, height: size.h }}
      onMouseDown={() => panelRef.current?.focus?.()}
      ref={panelRef}
      tabIndex={-1}
    >
      <div className="ai-agent-resize-handle" onMouseDown={startResize} aria-hidden="true" />
```

And update the matching closing tags at the end of the component (`AIAgent.tsx:227-228`): remove the extra closing `</div>` that previously closed the overlay — the panel now has one wrapper `<div>` (plus the resize handle self-closes). Ensure the JSX still has balanced tags: header, docs, body, composer, then a single `</div>`.

- [ ] **Step 2: Add size state, persistence, and resize handler**

Add near the other refs/state (after `AIAgent.tsx:30`):

```tsx
  const panelRef = useRef<HTMLDivElement | null>(null);

  const DEFAULT_SIZE = { w: 420, h: 620 };
  const [size, setSize] = useState<{ w: number; h: number }>(() => {
    try {
      const saved = JSON.parse(localStorage.getItem('vigilist.aiAgent.size') || '');
      if (saved && typeof saved.w === 'number' && typeof saved.h === 'number') return saved;
    } catch { /* ignore */ }
    return DEFAULT_SIZE;
  });

  // Drag the top-left handle to resize (panel is anchored bottom-right, so it grows up/left).
  const startResize = (e: React.MouseEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startY = e.clientY;
    const startW = size.w;
    const startH = size.h;
    const onMove = (ev: MouseEvent) => {
      const w = Math.min(Math.max(340, startW + (startX - ev.clientX)), window.innerWidth - 32);
      const h = Math.min(Math.max(360, startH + (startY - ev.clientY)), window.innerHeight - 32);
      setSize({ w, h });
    };
    const onUp = () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      setSize(curr => { localStorage.setItem('vigilist.aiAgent.size', JSON.stringify(curr)); return curr; });
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  };
```

- [ ] **Step 3: Scope Escape-to-close to panel focus**

Replace the global Escape effect (`AIAgent.tsx:42-48`) so Escape only closes when focus is inside the panel (otherwise Escape while working elsewhere on the site would dismiss the chat):

```tsx
  // Close on Escape only when focus is within the panel (it's non-modal now).
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && panelRef.current?.contains(document.activeElement)) onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);
```

- [ ] **Step 4: Track tool activity for the in-flight turn**

Add state near the other chat state (`AIAgent.tsx:24-27`):

```tsx
  const [activity, setActivity] = useState<{ summary: string; ok?: boolean; resultSummary?: string }[]>([]);
```

In `send()`, clear it at the start and wire the new handlers into the `streamChat` call. Replace the handlers object passed to `streamChat` (`AIAgent.tsx:68-71`) with:

```tsx
      {
        onDelta: (delta) => { acc += delta; setStreamingText(acc); },
        onError: (message) => { errored = true; showToast(message, 'error'); },
        onToolUse: (evt) => setActivity(prev => [...prev, { summary: evt.summary }]),
        onToolResult: (evt) => setActivity(prev => {
          const next = [...prev];
          for (let i = next.length - 1; i >= 0; i--) {
            if (next[i].ok === undefined) { next[i] = { ...next[i], ok: evt.ok, resultSummary: evt.summary }; break; }
          }
          return next;
        }),
      },
```

Set `setActivity([])` right after `setStreamingText('')` at the start of `send()` (`AIAgent.tsx:58`), and again in `stop()` and `clearConversation()`.

- [ ] **Step 5: Render the activity rows during streaming**

Inside the streaming block (`AIAgent.tsx:200-207`), render activity above the streaming text:

```tsx
          {streaming && (
            <div className="ai-agent-msg ai-agent-msg-assistant">
              <div className="ai-agent-msg-role">AI Agent</div>
              {activity.length > 0 && (
                <div className="ai-agent-activity">
                  {activity.map((a, i) => (
                    <div key={i} className={`ai-agent-activity-row${a.ok === false ? ' is-error' : ''}`}>
                      <span className="ai-agent-activity-icon">{a.ok === undefined ? '⋯' : a.ok ? '✓' : '✕'}</span>
                      {a.resultSummary || a.summary}
                    </div>
                  ))}
                </div>
              )}
              <div className="ai-agent-msg-content">
                {streamingText || <span className="ai-agent-typing"><span /><span /><span /></span>}
              </div>
            </div>
          )}
```

- [ ] **Step 6: Typecheck / build**

Run: `npm run build` (from `frontend/`)
Expected: build succeeds with no type errors.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/AIAgent.tsx
git commit -m "feat(ai): non-blocking docked AI Agent popup with tool activity rows"
```

---

## Task 7: Frontend — docked panel styling (`components.css`)

**Files:**
- Modify: `frontend/src/styles/components.css:751-773` (remove overlay, restyle panel) and the `<768px` block (`:934-940`)

**Interfaces:** none (pure CSS).

- [ ] **Step 1: Remove the overlay rule and re-anchor the panel**

Delete the `.ai-agent-overlay { ... }` rule (`components.css:751-760`). Replace the `.ai-agent-panel { ... }` rule (`:762-773`) with a fixed, corner-docked panel:

```css
.ai-agent-panel {
  position: fixed;
  bottom: var(--space-6);
  right: var(--space-6);
  z-index: 200;
  background: var(--color-card);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-xl), 0 0 0 1px rgba(44, 62, 107, 0.08);
  max-width: calc(100vw - 32px);
  max-height: calc(100vh - 32px);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  animation: modal-in 200ms cubic-bezier(0.16, 1, 0.3, 1);
}
```

> The inline `width`/`height` from `AIAgent.tsx` (`style={{ width: size.w, height: size.h }}`) drive the actual size; `max-*` here just clamps to the viewport. `z-index: 200` sits above the FAB (`150`) and below full modals (`250`), so existing modals still overlay the chat.

- [ ] **Step 2: Add the resize handle style**

Add after the panel rule:

```css
.ai-agent-resize-handle {
  position: absolute;
  top: 0;
  left: 0;
  width: 16px;
  height: 16px;
  cursor: nwse-resize;
  z-index: 1;
  background:
    linear-gradient(135deg, transparent 50%, rgba(44, 62, 107, 0.25) 50%, rgba(44, 62, 107, 0.25) 60%, transparent 60%);
  border-top-left-radius: var(--radius-xl);
}
```

- [ ] **Step 3: Add activity-row styles**

Add near the other `.ai-agent-*` message rules:

```css
.ai-agent-activity {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-bottom: var(--space-2);
}
.ai-agent-activity-row {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--text-xs);
  color: var(--color-neutral-500);
  font-style: italic;
}
.ai-agent-activity-row.is-error { color: var(--color-danger-600); }
.ai-agent-activity-icon { font-style: normal; }
```

- [ ] **Step 4: Keep the mobile full-screen fallback**

In the `@media (max-width: 768px)` block, update the `.ai-agent-panel` override (`components.css:934-940`) to pin all corners and hide the resize handle (the JS inline width/height still apply, so use `!important` to force full-screen on mobile):

```css
  .ai-agent-panel {
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    width: 100vw !important;
    height: 100vh !important;
    max-width: 100vw;
    max-height: 100vh;
    border-radius: 0;
  }
  .ai-agent-resize-handle { display: none; }
```

- [ ] **Step 5: Build + manual verification**

Run: `npm run build` (from `frontend/`)
Expected: build succeeds.

Manual check (run the app — `docker-compose up` or the project's usual dev command, then open the app and log in):
- Open the AI Agent via the FAB. Confirm **no dim backdrop** and that you can click search, open a document, and scroll the page **while the panel is open**.
- Drag the **top-left corner** to resize; reload the page and confirm the size persisted.
- Type a question that needs data (e.g. *"How many documents mention 'termination'?"*) and confirm **tool-activity rows** appear (⋯ → ✓) during the response.
- Press **Escape** while focused in the composer → panel closes. Press Escape while focused in the main search box → panel stays open.
- Narrow the window below 768px → panel goes full-screen and the resize handle disappears.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/styles/components.css
git commit -m "style(ai): dock the AI Agent panel and style resize + tool activity"
```

---

## Self-Review

**Spec coverage:**
- Popup: no-backdrop docked panel (Task 6/7), resizable + persisted (Task 6/7), scoped Escape (Task 6), mobile fallback kept (Task 7), FAB unchanged (no change needed). ✓
- DB tools: six read-only tools (Task 1/2), access-scoped via `accessible_ids` everywhere (Task 2), tool loop with `MAX_TOOL_ROUNDS=8` (Task 3), wired into `/api/ai/chat` (Task 4). ✓
- Streaming protocol: `tool_use`/`tool_result` frames emitted (Task 3), parsed (Task 5), rendered (Task 6). ✓
- Attached docs preserved: `chat()` still calls `build_chat_system_prompt(documents)`; the tool wiring only adds `tools=` + the loop. ✓
- Testing: backend unit tests for tools (access scope, routing, schemas) and the loop (termination, frames) without a DB; frontend manual verification. ✓
- Out of scope (writes, drag-positioning, cross-session persistence, the parallel branch) — not implemented. ✓

**Placeholder scan:** No `TBD`/`TODO`/"add error handling"/"similar to Task N". Each code step shows complete code. ✓

**Type consistency:**
- `ToolRun(result, result_summary, ok)` defined in Task 1, used consistently in Tasks 2–3 and asserted in tests.
- `run_tool(db, user, accessible_ids, name, tool_input)` signature identical in Task 2 definition, Task 3 test doubles (via the injected `_run`), and Task 4 closure.
- `stream_chat_events(client, system, messages, describe_call, run_tool, *, tools, model, max_rounds)` identical between Task 3 definition, its tests, and the Task 4 call site.
- SSE `type` values (`delta`/`tool_use`/`tool_result`/`done`/`error`) match between backend emit (Task 3) and frontend parse (Task 5).
- `tool_use_summary` (Task 1) passed as `describe_call` (Task 4) — matches the `describe_call(name, input) -> str` contract in Task 3.
✓

**Assumptions to verify during implementation:**
- `DocumentDuplicate` / `DuplicateGroup` importable from `app.models` (used by `routers/intelligence.py`). Task 2 notes the fallback.
- `frontend` build command is `npm run build`. If the project uses a different typecheck script, substitute it.
