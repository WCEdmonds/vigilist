# Agent API

A read-only HTTP API that lets an autonomous agent search and reason over the
documents in one production set (or one matter), authenticated by a scoped API
key rather than a user login.

It exists because the existing surfaces don't fit an agent. `/api/search` is
Firebase-authenticated — tokens are user-bound and expire hourly, so nothing
can run unattended — and its semantic mode filters by *production* (the
matter), with no way to say "only the documents in this deliverable volume."

Everything here is read-only. There is no write path.

## Scope is a property of the key

An agent key is minted for exactly one production, optionally narrowed to one
production set. That scope lives in the `agent_api_keys` row, and **no endpoint
accepts a production or production set id**. An agent cannot widen its own
reach by asking, so the worst a confused — or prompt-injected — agent can do is
read documents the key was already issued for.

Two consequences worth knowing:

- A document outside the scope returns **404, not 403**. The API never
  distinguishes "exists but not yours" from "does not exist."
- Matter-level intelligence is filtered back down to the scope. A set-scoped
  key asking for duplicates of a document will not learn about copies that were
  left out of its volume, and entities and clusters with no in-scope documents
  are omitted.

`production_set_id` is null for a matter-wide key, in which case the scope is
every document in the production.

## Issuing a key

Manager role or higher on the matter, using a normal Firebase session:

```http
POST /api/productions/{production_id}/agent-keys
{ "name": "privilege-review-agent", "production_set_id": 12, "expires_in_days": 30 }
```

```json
{
  "id": 3,
  "name": "privilege-review-agent",
  "key_prefix": "a1b2c3d4",
  "production_id": 7,
  "production_set_id": 12,
  "token": "vgl_a1b2c3d4_xY7...",
  "...": "..."
}
```

`token` is returned **once, here, and never again** — only its SHA-256 is
stored. Omit `production_set_id` for a matter-wide key; omit `expires_in_days`
for a key that never expires.

| Endpoint | Purpose |
| --- | --- |
| `POST /api/productions/{id}/agent-keys` | Mint a key |
| `GET /api/productions/{id}/agent-keys` | List keys (live and revoked; never tokens) |
| `DELETE /api/agent-keys/{key_id}` | Revoke, effective on the agent's next request |

Creation and revocation are both written to the audit log.

## Authenticating

Either header — agent frameworks differ on which they can set, and `X-API-Key`
wins if both are present:

```http
X-API-Key: vgl_a1b2c3d4_xY7...
Authorization: Bearer vgl_a1b2c3d4_xY7...
```

Every failure — unknown, revoked, expired, malformed — is an identical `401`,
so the endpoint can't be used to confirm that a key prefix exists.

## Two ways in

Both run the same service layer; pick whichever suits the client.

### 1. REST

```
GET  /api/agent/scope                              what this key can see, plus corpus stats
GET  /api/agent/search?q=...&mode=semantic         semantic (default) or keyword
GET  /api/agent/documents                          page the scope in Bates order
GET  /api/agent/documents/{bates_or_id}            full text + metadata + tags
GET  /api/agent/documents/{bates_or_id}/similar    semantically similar documents
GET  /api/agent/documents/{bates_or_id}/duplicates exact and near-duplicates
GET  /api/agent/entities                           list, or ?name=... to look one up
GET  /api/agent/clusters                           topic clusters
GET  /api/agent/clusters/{id}/documents            documents in one cluster
```

Documents are addressable by Bates number *or* UUID, wherever `bates_or_id`
appears.

### 2. Manifest + invoke

For agents that load their toolset at runtime instead of carrying hand-written
bindings that drift from the API:

```
GET  /api/agent/manifest          tool schemas (Anthropic tool-use / MCP shape) + scope
POST /api/agent/tools/{name}      body is the tool's input object
```

`GET /api/agent/manifest` returns the tools and the key's scope together, so an
agent gets its capabilities and its boundaries in one startup round trip. The
schemas drop straight into an Anthropic `tools=[...]` parameter:

```python
manifest = httpx.get(f"{BASE}/api/agent/manifest", headers=H).json()

response = anthropic.messages.create(
    model="claude-opus-5",
    tools=manifest["tools"],
    messages=[{"role": "user", "content": "Who negotiated the settlement?"}],
)

# On a tool_use block:
result = httpx.post(
    f"{BASE}/api/agent/tools/{block.name}", headers=H, json=block.input
).json()
```

A test asserts the manifest and the dispatch table stay in sync, so a tool can
never be advertised without an implementation or vice versa.

## The tools

| Tool | What it's for |
| --- | --- |
| `semantic_search` | Meaning-based search. Finds "had a few" from a query about drinking. |
| `keyword_search` | Literal full-text. Quoted `"phrases"`, `AND`/`OR`/`NOT`, `wildcard*`. |
| `get_document` | One document's full text, metadata, tags, email headers. |
| `list_documents` | Page the whole scope in Bates order. |
| `find_similar_documents` | Expand from one relevant hit to the rest of its kind. |
| `get_duplicates` | Exact and near-duplicates, so the same document isn't reported twice. |
| `lookup_entity` | A person or org: profile, aliases, case role, relationships, mentions. |
| `list_entities` | The cast of characters, most-mentioned first. |
| `list_clusters` | Topic map of the matter. |
| `get_cluster_documents` | Documents in one topic cluster. |
| `get_scope_stats` | Document/page counts, tag breakdown, top custodians. |

`semantic_search` vs `keyword_search` is the choice that matters most: semantic
finds documents whose *meaning* matches even when the words differ, keyword
finds literal terms. Reach for keyword when you need a specific name, case
number, or phrase, where paraphrasing would be a liability.

## Limits

Results are capped at **100 per call** (default 25) and document text at
**20,000 characters**, with `text_truncated` and `text_length` on the response
so an agent knows when it is seeing a partial document. The caps exist so one
call can't return a whole volume and exhaust the agent's context window.

## Degradation

Semantic search needs embeddings, which need a Voyage API key
(`VIGILIST_VOYAGE_API_KEY`) and an ingest that has generated chunks. Without
them `semantic_search` returns no results rather than failing — but unlike
`/api/search`, it does **not** silently fall back to full-text, because an
agent that believes it ran a semantic search over a set and got nothing should
see that, not a keyword result set wearing a semantic label. Use
`keyword_search` explicitly.

Duplicates, clusters, and entities each depend on their analysis having been
run over the matter; each returns an empty list when it hasn't.
