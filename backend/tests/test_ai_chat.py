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
