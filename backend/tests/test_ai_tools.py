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
