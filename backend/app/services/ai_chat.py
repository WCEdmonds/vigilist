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
        if not tool_results:
            yield _sse({"type": "done"})
            return
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
