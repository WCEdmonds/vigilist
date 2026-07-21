"""Production-grounded chat: the "ask the production" system prompt."""

from types import SimpleNamespace

from app.services.ai import CHAT_SYSTEM_PROMPT, build_production_chat_system_prompt


def _production(**overrides):
    base = dict(
        name="LAKESHORE_MERRITT_001",
        case_context="Contract dispute over a steel supply agreement.",
        brief={"overview": "This production covers the breach claim."},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_prompt_includes_case_context_brief_and_excerpts():
    excerpts = [
        {"bates": "LM 000004", "title": "Internal memo", "content": "slow-walk the July lot"},
        {"bates": "LM 000005", "title": None, "content": "force majeure notice"},
    ]
    prompt = build_production_chat_system_prompt(_production(), excerpts)

    assert CHAT_SYSTEM_PROMPT in prompt
    assert "LAKESHORE_MERRITT_001" in prompt
    assert "steel supply agreement" in prompt
    assert "breach claim" in prompt
    assert "LM 000004 — Internal memo" in prompt
    assert "slow-walk the July lot" in prompt
    assert "LM 000005" in prompt
    assert "Bates" in prompt  # citation instruction


def test_prompt_without_excerpts_says_ungrounded():
    prompt = build_production_chat_system_prompt(_production(), [])
    assert "No excerpts could be retrieved" in prompt
    assert "steel supply agreement" in prompt


def test_prompt_handles_missing_case_context_and_brief():
    prod = _production(case_context=None, brief=None)
    prompt = build_production_chat_system_prompt(prod, [])
    assert "About this case" not in prompt
    assert "Production brief overview" not in prompt
    assert CHAT_SYSTEM_PROMPT in prompt


def test_excerpt_content_is_capped():
    excerpts = [{"bates": "LM 000001", "title": None, "content": "x" * 10000}]
    prompt = build_production_chat_system_prompt(_production(), excerpts)
    assert "x" * 2001 not in prompt
