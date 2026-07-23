"""Unit tests for AI Agent tool definitions and pure helpers."""

import asyncio
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
        "semantic_search",
        "get_document",
        "list_productions",
        "find_similar_documents",
        "get_duplicates",
        "get_corpus_stats",
        "lookup_entity",
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


def test_run_tool_routes_semantic_search(monkeypatch):
    calls = {}

    async def fake_semantic(db, query, **kwargs):
        calls["query"] = query
        calls["accessible"] = kwargs.get("accessible_production_ids")
        return ([{"id": "d1", "bates_begin": "ABC-1", "bates_end": "ABC-1",
                  "title": "T", "snippet": "snip", "page_count": 1,
                  "production_id": 1, "rank": 0.9}], 1)

    monkeypatch.setattr(ai_tools, "_semantic_search", fake_semantic)
    run = asyncio.run(ai_tools.run_tool(
        db=object(), user=_FakeUser(), accessible_ids=[1, 2],
        name="semantic_search", tool_input={"query": "drinking"},
    ))
    assert run.ok is True
    assert calls["query"] == "drinking"
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


class _FakeDB:
    def __init__(self, doc=None):
        self._doc = doc

    async def get(self, model, key):
        return self._doc

    async def execute(self, *a, **k):
        raise AssertionError("execute should not be called on the UUID path")


class _FakeDoc:
    def __init__(self, production_id, text="hello"):
        self.id = uuid.uuid4()
        self.production_id = production_id
        self.bates_begin = "ABC-1"
        self.bates_end = "ABC-1"
        self.title = "T"
        self.page_count = 1
        self.summary = None
        self.text_content = text


def test_get_document_denies_out_of_scope_uuid():
    doc = _FakeDoc(production_id=999)  # not in accessible [1, 2]
    run = asyncio.run(ai_tools.run_tool(
        db=_FakeDB(doc=doc), user=_FakeUser(), accessible_ids=[1, 2],
        name="get_document", tool_input={"bates_or_id": str(doc.id)},
    ))
    assert run.ok is False
    assert "no accessible" in run.result.lower()


def test_get_document_allows_in_scope_uuid():
    doc = _FakeDoc(production_id=1)  # in accessible [1, 2]
    run = asyncio.run(ai_tools.run_tool(
        db=_FakeDB(doc=doc), user=_FakeUser(), accessible_ids=[1, 2],
        name="get_document", tool_input={"bates_or_id": str(doc.id)},
    ))
    assert run.ok is True
    assert "ABC-1" in run.result_summary


def test_lookup_entity_registered():
    from app.services.ai_tools import TOOLS, _DISPATCH, tool_use_summary
    assert any(t["name"] == "lookup_entity" for t in TOOLS)
    assert "lookup_entity" in _DISPATCH
    assert "Jorge" in tool_use_summary("lookup_entity", {"name": "Jorge"})


def test_lookup_entity_out_of_scope_production_returns_empty():
    """Scope check must short-circuit before any DB use."""
    run = asyncio.run(ai_tools.run_tool(
        db=object(), user=_FakeUser(), accessible_ids=[1, 2],
        name="lookup_entity", tool_input={"name": "test", "production_id": 999},
    ))
    assert run.ok is True
    import json
    result = json.loads(run.result)
    assert result["matches"] == []
