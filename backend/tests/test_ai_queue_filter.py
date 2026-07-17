"""Normalization of the AI-slice queue filter."""

import pytest

from app.services.batching import build_ai_filter_conditions


def test_defaults_filled():
    out = build_ai_filter_conditions({"project_id": 3, "decision": "relevant"})
    assert out == {
        "project_id": 3,
        "decision": "relevant",
        "min_confidence": 0,
        "exclude_decided": True,
    }


def test_coercions_and_overrides():
    out = build_ai_filter_conditions(
        {"project_id": "7", "decision": "key_document", "min_confidence": "80", "exclude_decided": False}
    )
    assert out["project_id"] == 7
    assert out["min_confidence"] == 80
    assert out["exclude_decided"] is False


def test_missing_required_raises():
    with pytest.raises(ValueError):
        build_ai_filter_conditions({"decision": "relevant"})
    with pytest.raises(ValueError):
        build_ai_filter_conditions({"project_id": 1})
