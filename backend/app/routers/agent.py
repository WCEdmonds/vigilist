"""Agent API — read-only access to one production set, for autonomous agents.

Authenticated by an agent API key (`X-API-Key` or `Authorization: Bearer`),
never by a Firebase ID token. Every endpoint is scoped by the key itself, so
no route accepts a production or production set id.

Two ways in, over the same service layer:

  * REST resources (`/search`, `/documents`, `/entities`, …) for anything that
    speaks HTTP.
  * A tool manifest (`/manifest`) plus a generic invoke route
    (`/tools/{name}`) for agents that load their toolset at runtime.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Production, ProductionSet
from app.services import agent_tools
from app.services.agent_auth import get_agent_scope
from app.services.agent_keys import AgentScope
from app.services.agent_manifest import TOOLS
from app.services.agent_tools import ToolError, run_tool

router = APIRouter(prefix="/api/agent", tags=["agent"])


async def _run(db: AsyncSession, scope: AgentScope, name: str, args: dict) -> dict:
    """Run a tool, turning bad input into 400 rather than a 500."""
    try:
        return await run_tool(db, scope, name, args)
    except ToolError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ── Discovery ───────────────────────────────────────────────────────────────


@router.get("/manifest")
async def manifest(
    db: AsyncSession = Depends(get_db),
    scope: AgentScope = Depends(get_agent_scope),
):
    """The toolset this key can call, in Anthropic tool-use / MCP shape.

    Returned together with the scope so an agent gets its tools and its
    boundaries in one round trip at startup.
    """
    return {
        "version": 1,
        "scope": await _scope_payload(db, scope),
        "invoke": {
            "method": "POST",
            "url_template": "/api/agent/tools/{tool_name}",
            "body": "The tool's input object, as described by input_schema.",
        },
        "tools": TOOLS,
    }


async def _scope_payload(db: AsyncSession, scope: AgentScope) -> dict:
    production = await db.get(Production, scope.production_id)
    payload = {
        "key_name": scope.name,
        "role": scope.role,
        "production_id": scope.production_id,
        "production_name": production.name if production else None,
        "production_set_id": scope.production_set_id,
        "production_set_name": None,
        "production_set_status": None,
    }
    if scope.production_set_id is not None:
        ps = await db.get(ProductionSet, scope.production_set_id)
        if ps is not None:
            payload["production_set_name"] = ps.name
            payload["production_set_status"] = ps.status
    return payload


@router.get("/scope")
async def scope_info(
    db: AsyncSession = Depends(get_db),
    scope: AgentScope = Depends(get_agent_scope),
):
    """What this key can see, plus corpus size. The natural first call."""
    stats = await _run(db, scope, "get_scope_stats", {})
    return {**await _scope_payload(db, scope), "stats": stats}


# ── Generic tool invocation ─────────────────────────────────────────────────


@router.post("/tools/{tool_name}")
async def invoke_tool(
    tool_name: str,
    args: dict = Body(default_factory=dict),
    db: AsyncSession = Depends(get_db),
    scope: AgentScope = Depends(get_agent_scope),
):
    """Run any manifest tool by name. The body is the tool's input object."""
    if tool_name not in agent_tools.DISPATCH:
        raise HTTPException(status_code=404, detail=f"Unknown tool '{tool_name}'")
    return await _run(db, scope, tool_name, args)


# ── REST resources ──────────────────────────────────────────────────────────


@router.get("/search")
async def search(
    q: str = Query(..., min_length=1, description="The search query."),
    mode: str = Query("semantic", pattern="^(semantic|keyword)$"),
    limit: int = Query(agent_tools.DEFAULT_LIMIT, ge=1, le=agent_tools.MAX_LIMIT),
    page: int = Query(1, ge=1),
    file_type: str | None = Query(None, description="keyword mode only"),
    sort: str = Query("relevance", pattern="^(relevance|bates)$"),
    db: AsyncSession = Depends(get_db),
    scope: AgentScope = Depends(get_agent_scope),
):
    """Search the documents in scope. Semantic by default."""
    if mode == "semantic":
        return await _run(db, scope, "semantic_search",
                          {"query": q, "limit": limit, "page": page})
    return await _run(db, scope, "keyword_search", {
        "query": q, "limit": limit, "page": page,
        "file_type": file_type, "sort": sort,
    })


@router.get("/documents")
async def list_documents(
    limit: int = Query(agent_tools.DEFAULT_LIMIT, ge=1, le=agent_tools.MAX_LIMIT),
    page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
    scope: AgentScope = Depends(get_agent_scope),
):
    """Page through the documents in scope, in Bates order."""
    return await _run(db, scope, "list_documents", {"limit": limit, "page": page})


@router.get("/documents/{bates_or_id}")
async def get_document(
    bates_or_id: str,
    db: AsyncSession = Depends(get_db),
    scope: AgentScope = Depends(get_agent_scope),
):
    """Full text and metadata for one document."""
    try:
        return await run_tool(db, scope, "get_document", {"bates_or_id": bates_or_id})
    except ToolError:
        # A missing document is 404 here, where the reference is the route,
        # even though the same condition is a 400 through /tools.
        raise HTTPException(status_code=404, detail="Document not found in scope")


@router.get("/documents/{bates_or_id}/similar")
async def similar_documents(
    bates_or_id: str,
    limit: int = Query(agent_tools.DEFAULT_LIMIT, ge=1, le=agent_tools.MAX_LIMIT),
    db: AsyncSession = Depends(get_db),
    scope: AgentScope = Depends(get_agent_scope),
):
    """Documents in scope semantically similar to this one."""
    return await _run(db, scope, "find_similar_documents",
                      {"bates_or_id": bates_or_id, "limit": limit})


@router.get("/documents/{bates_or_id}/duplicates")
async def document_duplicates(
    bates_or_id: str,
    db: AsyncSession = Depends(get_db),
    scope: AgentScope = Depends(get_agent_scope),
):
    """Exact and near-duplicate copies of this document, within scope."""
    return await _run(db, scope, "get_duplicates", {"bates_or_id": bates_or_id})


@router.get("/entities")
async def list_entities(
    name: str | None = Query(None, description="Look up by name instead of listing."),
    entity_type: str | None = Query(None, pattern="^(person|org)$"),
    limit: int = Query(agent_tools.DEFAULT_LIMIT, ge=1, le=agent_tools.MAX_LIMIT),
    db: AsyncSession = Depends(get_db),
    scope: AgentScope = Depends(get_agent_scope),
):
    """The matter's people and organizations, or one looked up by name."""
    if name:
        return await _run(db, scope, "lookup_entity", {"name": name, "limit": limit})
    return await _run(db, scope, "list_entities",
                      {"entity_type": entity_type, "limit": limit})


@router.get("/clusters")
async def list_clusters(
    limit: int = Query(agent_tools.DEFAULT_LIMIT, ge=1, le=agent_tools.MAX_LIMIT),
    db: AsyncSession = Depends(get_db),
    scope: AgentScope = Depends(get_agent_scope),
):
    """Topic clusters, with in-scope document counts."""
    return await _run(db, scope, "list_clusters", {"limit": limit})


@router.get("/clusters/{cluster_id}/documents")
async def cluster_documents(
    cluster_id: int,
    limit: int = Query(agent_tools.DEFAULT_LIMIT, ge=1, le=agent_tools.MAX_LIMIT),
    page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
    scope: AgentScope = Depends(get_agent_scope),
):
    """The in-scope documents in one topic cluster."""
    return await _run(db, scope, "get_cluster_documents",
                      {"cluster_id": cluster_id, "limit": limit, "page": page})
