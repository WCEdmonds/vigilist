import json
from app.services.ai_review import parse_classification_response, build_classification_prompt


def test_build_classification_prompt():
    prompt = build_classification_prompt(
        review_criteria="Documents about use of force",
        document_text="Officer used a taser during the arrest.",
    )
    assert "use of force" in prompt
    assert "Officer used a taser" in prompt
    assert "responsive" in prompt.lower()


def test_parse_valid_response():
    raw = json.dumps({
        "decision": "responsive",
        "confidence": 92,
        "reasoning": "The document discusses use of a taser, which is a use of force.",
        "key_excerpts": [{"text": "Officer used a taser", "start_offset": 0, "end_offset": 20}],
        "considerations": "Clear use of force reference."
    })
    result = parse_classification_response(raw)
    assert result["decision"] == "responsive"
    assert result["confidence"] == 92
    assert len(result["key_excerpts"]) == 1


def test_parse_invalid_json():
    result = parse_classification_response("not json at all")
    assert result["decision"] == "needs_review"
    assert result["confidence"] == 0


def test_parse_missing_fields():
    raw = json.dumps({"decision": "responsive"})
    result = parse_classification_response(raw)
    assert result["decision"] == "responsive"
    assert result["confidence"] == 50
    assert result["reasoning"] != ""
