"""Brief prompt construction and response parsing (pure functions)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.brief import build_brief_prompt, generate_brief, parse_brief_response


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


# ── generate_brief branch coverage (SDK + DB faked) ──


def test_generate_brief_returns_none_without_api_key():
    with patch("app.services.brief._get_client", return_value=None):
        db = MagicMock()
        out = asyncio.run(generate_brief(db, 1))
    assert out is None
    db.get.assert_not_called()


def test_generate_brief_returns_none_for_missing_production():
    with patch("app.services.brief._get_client") as mock_get:
        mock_get.return_value = MagicMock()
        db = MagicMock()
        db.get = AsyncMock(return_value=None)
        out = asyncio.run(generate_brief(db, 999))
    assert out is None


def test_generate_brief_returns_none_on_unparseable_model_output():
    block = MagicMock()
    block.text = "this is not json"
    response = MagicMock()
    response.content = [block]

    client = MagicMock()
    client.messages.create = AsyncMock(return_value=response)

    prod = MagicMock()
    prod.case_context = "ctx"

    scalar_result = MagicMock()
    scalar_result.scalar.return_value = 0
    scalar_result.one.return_value = (None, None)
    scalars = MagicMock()
    scalars.all.return_value = []
    scalar_result.scalars.return_value = scalars
    scalar_result.all.return_value = []

    db = MagicMock()
    db.get = AsyncMock(return_value=prod)
    db.execute = AsyncMock(return_value=scalar_result)

    with patch("app.services.brief._get_client", return_value=client):
        out = asyncio.run(generate_brief(db, 1))
    assert out is None
    assert client.messages.create.await_count == 1


def test_generate_brief_stamps_metadata_on_success():
    block = MagicMock()
    block.text = '{"overview": "O.", "key_players": [], "date_range": null, "notable_documents": []}'
    response = MagicMock()
    response.content = [block]

    client = MagicMock()
    client.messages.create = AsyncMock(return_value=response)

    prod = MagicMock()
    prod.case_context = None

    scalar_result = MagicMock()
    scalar_result.scalar.return_value = 3
    scalar_result.one.return_value = ("A-1", "A-3")
    scalars = MagicMock()
    scalars.all.return_value = []
    scalar_result.scalars.return_value = scalars
    scalar_result.all.return_value = []

    db = MagicMock()
    db.get = AsyncMock(return_value=prod)
    db.execute = AsyncMock(return_value=scalar_result)

    with patch("app.services.brief._get_client", return_value=client):
        out = asyncio.run(generate_brief(db, 1))
    assert out is not None
    assert out["overview"] == "O."
    assert out["model"] == "claude-sonnet-4-6"
    assert "generated_at" in out
