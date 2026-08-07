"""Tool schemas for the agent API, in Anthropic tool-use / MCP shape.

Served from `GET /api/agent/manifest` so a deep agent can discover the toolset
at runtime instead of carrying hand-written bindings that drift from the API.
Each entry's `name` matches a key in `agent_tools.DISPATCH`, and every tool is
invoked the same way: `POST /api/agent/tools/{name}` with the input object as
the body.

No tool takes a production or production set id. Scope comes from the API key,
so there is nothing for the model to get wrong and nothing for an injected
instruction to redirect.
"""

from __future__ import annotations

from app.services.agent_tools import DEFAULT_LIMIT, MAX_LIMIT

_LIMIT_PROP = {
    "type": "integer",
    "description": f"Max results, 1-{MAX_LIMIT}. Default {DEFAULT_LIMIT}.",
}
_PAGE_PROP = {"type": "integer", "description": "1-based page number. Default 1."}


TOOLS: list[dict] = [
    {
        "name": "semantic_search",
        "description": (
            "Meaning-based search over the documents in scope, using embeddings. "
            "Finds documents by concept even when they use different words, "
            "euphemisms, or indirect phrasing — a query about 'drinking' can "
            "surface text that says 'had a few', 'intoxicated', or 'DUI'. Prefer "
            "this over keyword_search for topics that may be described obliquely, "
            "and when you do not know the exact terms the documents use."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "A natural-language description of what to find.",
                },
                "limit": _LIMIT_PROP,
                "page": _PAGE_PROP,
            },
            "required": ["query"],
        },
    },
    {
        "name": "keyword_search",
        "description": (
            "Exact full-text search over the documents in scope. Supports quoted "
            '"phrases", AND/OR/NOT, and wildcard*. Use this when you need literal '
            "term matching — a name, a case number, a specific phrase — where "
            "semantic_search's paraphrasing would be a liability."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms."},
                "file_type": {
                    "type": "string",
                    "description": (
                        "Optional filter: video, audio, pdf, office, image, email, "
                        "native, or images_only."
                    ),
                },
                "sort": {
                    "type": "string",
                    "enum": ["relevance", "bates"],
                    "description": "Result ordering. Default relevance.",
                },
                "limit": _LIMIT_PROP,
                "page": _PAGE_PROP,
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_document",
        "description": (
            "Fetch one document's full extracted text, metadata, tags, and email "
            "headers, by Bates number or document id. Use this after a search to "
            "read a hit in full — search results carry only a snippet."
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
        "name": "list_documents",
        "description": (
            "Page through every document in scope, in Bates order. Use this to see "
            "what you are working with before searching, or to walk a small set "
            "exhaustively."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"limit": _LIMIT_PROP, "page": _PAGE_PROP},
        },
    },
    {
        "name": "find_similar_documents",
        "description": (
            "Find documents in scope that are semantically similar to a given "
            "document. Use this to expand from one relevant hit to the rest of its "
            "kind — other copies of a contract, other emails in a discussion."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "bates_or_id": {
                    "type": "string",
                    "description": "A Bates number or document UUID.",
                },
                "limit": _LIMIT_PROP,
            },
            "required": ["bates_or_id"],
        },
    },
    {
        "name": "get_duplicates",
        "description": (
            "List exact and near-duplicate copies of a document, if duplicate "
            "detection has been run on the matter. Use this to avoid reporting the "
            "same document several times under different Bates numbers."
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
        "name": "lookup_entity",
        "description": (
            "Look up a person or organization in the case ontology by name. Returns "
            "their profile, aliases, case role, stated relationships, in-scope "
            "mention counts, and sample mention snippets. Use this to answer "
            "'who is X' or 'how is X connected to Y'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Person or organization name. Partial names match.",
                },
                "limit": _LIMIT_PROP,
            },
            "required": ["name"],
        },
    },
    {
        "name": "list_entities",
        "description": (
            "List the people and organizations in the matter, most-mentioned first. "
            "Use this to orient yourself in an unfamiliar matter before you know "
            "any names to look up."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_type": {
                    "type": "string",
                    "enum": ["person", "org"],
                    "description": "Optional: restrict to people or organizations.",
                },
                "limit": _LIMIT_PROP,
            },
        },
    },
    {
        "name": "list_clusters",
        "description": (
            "List topic clusters over the matter, with the number of in-scope "
            "documents in each. Use this to map the subject matter before drilling "
            "into any one topic."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"limit": _LIMIT_PROP},
        },
    },
    {
        "name": "get_cluster_documents",
        "description": "List the in-scope documents belonging to one topic cluster.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {
                    "type": "integer",
                    "description": "Cluster id from list_clusters.",
                },
                "limit": _LIMIT_PROP,
                "page": _PAGE_PROP,
            },
            "required": ["cluster_id"],
        },
    },
    {
        "name": "get_scope_stats",
        "description": (
            "Size and composition of what this key can see: document and page "
            "counts, tag breakdown, and top custodians. Cheap; call it first to "
            "size the corpus before planning a search strategy."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]

TOOL_NAMES: set[str] = {t["name"] for t in TOOLS}
