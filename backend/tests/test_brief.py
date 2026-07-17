"""Brief prompt construction and response parsing (pure functions)."""

from app.services.brief import build_brief_prompt, parse_brief_response


def test_prompt_includes_case_context_themes_and_samples():
    prompt = build_brief_prompt(
        case_context="Product-liability suit over the March recall.",
        doc_count=4218,
        date_hint="ACME-000001 .. ACME-004218",
        themes=[{"label": "Recall timeline", "doc_count": 1204}],
        samples=[{"bates": "ACME-000412", "title": "Board minutes", "snippet": "The board voted"}],
    )
    assert "March recall" in prompt
    assert "4218" in prompt or "4,218" in prompt
    assert "Recall timeline" in prompt
    assert "ACME-000412" in prompt
    assert "JSON" in prompt


def test_prompt_handles_missing_case_context():
    prompt = build_brief_prompt(None, 10, None, [], [])
    assert "No case description was provided" in prompt


def test_parse_accepts_fenced_json():
    raw = '```json\n{"overview": "O.", "key_players": ["A"], "date_range": null, "notable_documents": []}\n```'
    brief = parse_brief_response(raw)
    assert brief is not None
    assert brief["overview"] == "O."
    assert brief["key_players"] == ["A"]


def test_parse_rejects_garbage_and_missing_overview():
    assert parse_brief_response("not json at all") is None
    assert parse_brief_response('{"key_players": []}') is None
