"""Unit tests for the agent API: key auth, scoping, and the tool manifest.

No database — SQL-emitting paths are checked by compiling the statement and
asserting on its text, which is what actually enforces the production-set
boundary.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import AgentApiKey, Document
from app.services import agent_keys, agent_manifest, agent_tools
from app.services.agent_auth import extract_token
from app.services.agent_keys import AgentScope
from app.services.search import search_documents
from tests.fakes import FakeResult, FakeSession


def _scope(production_set_id=None, production_id=7):
    return AgentScope(
        key_id=1, name="test-agent", production_id=production_id,
        production_set_id=production_set_id, role="readonly",
    )


# ── Token minting and parsing ───────────────────────────────────────────────


def test_generated_tokens_are_unique_and_parseable():
    seen = set()
    for _ in range(50):
        token, prefix, digest = agent_keys.generate_token()
        assert token.startswith("vgl_")
        assert agent_keys.parse_prefix(token) == prefix
        assert digest == agent_keys.hash_token(token)
        assert len(digest) == 64
        seen.add(token)
    assert len(seen) == 50, "tokens must not repeat"


def test_token_secret_is_not_recoverable_from_stored_fields():
    """Only key_prefix and key_hash are persisted. Neither may carry the
    secret — a database read must not yield a working credential."""
    for _ in range(20):
        token, prefix, digest = agent_keys.generate_token()
        # Bounded split: the secret is everything after the second separator.
        secret = token.split("_", 2)[2]
        assert secret not in prefix
        assert secret not in digest
        assert token not in digest


def test_parse_prefix_rejects_malformed_tokens():
    for bad in ["", "nope", "vgl_only", "vgl__missing", "vgl_a_", "other_a_b", None]:
        assert agent_keys.parse_prefix(bad) is None


def test_parse_prefix_tolerates_underscores_in_the_secret():
    """base64url secrets contain `_`; only the first two separators delimit
    fields, so the prefix is still recovered exactly."""
    assert agent_keys.parse_prefix("vgl_abc123_secret_with_underscores") == "abc123"


def test_generated_prefix_never_contains_a_separator():
    for _ in range(50):
        _, prefix, _ = agent_keys.generate_token()
        assert "_" not in prefix


# ── Header extraction ───────────────────────────────────────────────────────


def test_extract_token_reads_both_header_forms():
    assert extract_token("", "vgl_a_b") == "vgl_a_b"
    assert extract_token("Bearer vgl_a_b", "") == "vgl_a_b"
    assert extract_token("bearer vgl_a_b", "") == "vgl_a_b"
    # X-API-Key wins when both are present.
    assert extract_token("Bearer other", "vgl_a_b") == "vgl_a_b"
    assert extract_token("", "") == ""
    assert extract_token("Basic abc123", "") == ""


# ── authenticate() ──────────────────────────────────────────────────────────


def _key_row(token, **overrides):
    prefix = agent_keys.parse_prefix(token)
    row = AgentApiKey(
        name="agent", key_prefix=prefix, key_hash=agent_keys.hash_token(token),
        production_id=7, production_set_id=3, role="readonly", created_by="u1",
    )
    row.id = 1
    row.revoked_at = None
    row.expires_at = None
    for k, v in overrides.items():
        setattr(row, k, v)
    return row


def _auth(token, rows):
    db = FakeSession(responders=[("agent_api_keys", FakeResult(items=rows))])
    return asyncio.run(agent_keys.authenticate(db, token))


def test_authenticate_returns_scope_for_a_live_key():
    token, _, _ = agent_keys.generate_token()
    scope = _auth(token, [_key_row(token)])
    assert scope is not None
    assert scope.production_id == 7
    assert scope.production_set_id == 3
    assert scope.is_set_scoped


def test_authenticate_rejects_revoked_and_expired_keys():
    token, _, _ = agent_keys.generate_token()
    past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)

    assert _auth(token, [_key_row(token, revoked_at=past)]) is None
    assert _auth(token, [_key_row(token, expires_at=past)]) is None
    # A future expiry is fine.
    future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
    assert _auth(token, [_key_row(token, expires_at=future)]) is not None


def test_authenticate_handles_tz_aware_expiry_without_raising():
    """Comparing an aware expires_at against a naive now() would TypeError on
    the auth path; the value is normalised instead."""
    token, _, _ = agent_keys.generate_token()
    aware_future = datetime.now(timezone.utc) + timedelta(days=1)
    assert _auth(token, [_key_row(token, expires_at=aware_future)]) is not None
    aware_past = datetime.now(timezone.utc) - timedelta(days=1)
    assert _auth(token, [_key_row(token, expires_at=aware_past)]) is None


def test_authenticate_rejects_a_wrong_secret_sharing_a_valid_prefix():
    """The prefix is only a lookup key — the hash is what authenticates."""
    real, prefix, _ = agent_keys.generate_token()
    forged = f"vgl_{prefix}_wrongsecretwrongsecret"
    assert agent_keys.parse_prefix(forged) == prefix
    assert _auth(forged, [_key_row(real)]) is None


def test_authenticate_rejects_unknown_and_malformed_tokens():
    assert _auth("vgl_nope_nope", []) is None
    assert _auth("garbage", []) is None


def test_authenticate_stamps_last_used():
    token, _, _ = agent_keys.generate_token()
    row = _key_row(token)
    assert row.last_used_at is None
    _auth(token, [row])
    assert row.last_used_at is not None


# ── Manifest ────────────────────────────────────────────────────────────────


def test_manifest_tools_are_well_formed():
    assert agent_manifest.TOOLS
    names = [t["name"] for t in agent_manifest.TOOLS]
    assert len(names) == len(set(names)), "tool names must be unique"
    for tool in agent_manifest.TOOLS:
        assert tool["name"]
        assert tool["description"].strip()
        schema = tool["input_schema"]
        assert schema["type"] == "object"
        assert "properties" in schema
        for req in schema.get("required", []):
            assert req in schema["properties"], f"{tool['name']}: {req} not declared"


def test_every_manifest_tool_is_dispatchable_and_vice_versa():
    assert agent_manifest.TOOL_NAMES == set(agent_tools.DISPATCH)


def test_no_tool_accepts_a_production_or_set_id():
    """Scope comes from the key. A tool that took an id would let an agent
    (or an injected instruction) point itself at another matter."""
    for tool in agent_manifest.TOOLS:
        props = set(tool["input_schema"].get("properties", {}))
        assert not props & {"production_id", "production_set_id"}, tool["name"]


# ── Input clamping ──────────────────────────────────────────────────────────


def test_limit_is_clamped_to_the_ceiling():
    assert agent_tools._clamp_limit(10) == 10
    assert agent_tools._clamp_limit(10_000) == agent_tools.MAX_LIMIT
    assert agent_tools._clamp_limit(0) == 1
    assert agent_tools._clamp_limit(-5) == 1
    assert agent_tools._clamp_limit(None) == agent_tools.DEFAULT_LIMIT
    assert agent_tools._clamp_limit("junk") == agent_tools.DEFAULT_LIMIT


def test_page_is_clamped_to_at_least_one():
    assert agent_tools._clamp_page(3) == 3
    assert agent_tools._clamp_page(0) == 1
    assert agent_tools._clamp_page(-2) == 1
    assert agent_tools._clamp_page("junk") == 1


# ── Scoping ─────────────────────────────────────────────────────────────────


def test_scope_filters_pin_a_search_to_the_key():
    filters = agent_tools.scope_filters(_scope(production_set_id=3))
    assert filters == {
        "production_id": 7,
        "production_set_id": 3,
        "accessible_production_ids": [7],
    }


def test_matter_scoped_key_filters_by_production_only():
    sql = str(select(Document.id).where(agent_tools._in_scope_clause(_scope())))
    assert "documents.production_id" in sql
    assert "production_set_items" not in sql


def test_set_scoped_key_filters_through_production_set_items():
    sql = str(
        select(Document.id).where(agent_tools._in_scope_clause(_scope(production_set_id=3)))
    )
    assert "production_set_items" in sql
    assert "documents.id IN" in sql


def test_full_text_search_scopes_to_a_production_set():
    db = FakeSession()
    asyncio.run(search_documents(
        db, "termination", production_id=7, production_set_id=3,
        accessible_production_ids=[7],
    ))
    assert db.executed, "expected the search to reach the database"
    joined = " ".join(db.executed)
    assert "production_set_items" in joined
    assert "production_set_id" in joined


def test_set_scoped_search_with_no_query_lists_the_set():
    """An empty query is still a real search when a set is named — it means
    'everything in this volume'."""
    db = FakeSession()
    asyncio.run(search_documents(db, "", production_set_id=3))
    assert db.executed, "a set-scoped empty query must not short-circuit"


def test_unscoped_empty_query_still_short_circuits():
    db = FakeSession()
    results, total = asyncio.run(search_documents(db, ""))
    assert (results, total) == ([], 0)
    assert not db.executed


# ── Dispatch ────────────────────────────────────────────────────────────────


def test_unknown_tool_raises_tool_error():
    db = FakeSession()
    try:
        asyncio.run(agent_tools.run_tool(db, _scope(), "drop_everything", {}))
    except agent_tools.ToolError as exc:
        assert "drop_everything" in str(exc)
    else:
        raise AssertionError("expected ToolError")


def test_search_tools_require_a_query():
    db = FakeSession()
    for name in ("semantic_search", "keyword_search"):
        try:
            asyncio.run(agent_tools.run_tool(db, _scope(), name, {"query": "  "}))
        except agent_tools.ToolError:
            pass
        else:
            raise AssertionError(f"{name} should reject a blank query")
