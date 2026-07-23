"""classify_document_cascade: Haiku screens every document, Sonnet confirms
anything that might matter.

The cascade calls classify_document at most twice: once with SCREEN_MODEL
(cheap, reads everything), and — unless the screen pass returns a confident
screen-out decision — once with CONFIRM_MODEL, whose answer wins. These tests
monkeypatch classify_document itself, so no SDK machinery is involved.
"""

import asyncio
from unittest.mock import AsyncMock

import app.services.ai_review as ai_review


def _result(decision, confidence):
    return {
        "decision": decision,
        "confidence": confidence,
        "reasoning": "r",
        "key_excerpts": [],
        "considerations": None,
    }


def _run_cascade(monkeypatch, side_effect, categories=None):
    fake = AsyncMock(side_effect=side_effect)
    monkeypatch.setattr(ai_review, "classify_document", fake)
    out = asyncio.run(
        ai_review.classify_document_cascade("criteria", "doc text", categories=categories)
    )
    return out, fake


def test_confident_not_relevant_stays_on_screen_model(monkeypatch):
    (result, tokens, model), fake = _run_cascade(
        monkeypatch, [(_result("not_relevant", 92), 150)]
    )

    assert fake.call_count == 1
    assert fake.call_args.kwargs["model"] == ai_review.SCREEN_MODEL
    assert result["decision"] == "not_relevant"
    assert tokens == 150
    assert model == ai_review.SCREEN_MODEL


def test_relevant_screen_decision_escalates_and_confirm_wins(monkeypatch):
    (result, tokens, model), fake = _run_cascade(
        monkeypatch,
        [(_result("relevant", 95), 150), (_result("key_document", 88), 400)],
    )

    assert fake.call_count == 2
    assert fake.call_args_list[0].kwargs["model"] == ai_review.SCREEN_MODEL
    assert fake.call_args_list[1].kwargs["model"] == ai_review.CONFIRM_MODEL
    assert result["decision"] == "key_document"
    assert tokens == 550
    assert model == ai_review.CONFIRM_MODEL


def test_low_confidence_not_relevant_escalates(monkeypatch):
    (result, tokens, model), fake = _run_cascade(
        monkeypatch,
        [(_result("not_relevant", 55), 150), (_result("not_relevant", 90), 400)],
    )

    assert fake.call_count == 2
    assert result["decision"] == "not_relevant"
    assert tokens == 550
    assert model == ai_review.CONFIRM_MODEL


def test_needs_review_escalates(monkeypatch):
    (result, tokens, model), fake = _run_cascade(
        monkeypatch,
        [(_result("needs_review", 90), 150), (_result("relevant", 80), 400)],
    )

    assert fake.call_count == 2
    assert result["decision"] == "relevant"
    assert model == ai_review.CONFIRM_MODEL


def test_custom_categories_without_screen_out_always_escalate(monkeypatch):
    cats = [{"name": "responsive"}, {"name": "privileged"}]
    (result, tokens, model), fake = _run_cascade(
        monkeypatch,
        [(_result("responsive", 99), 150), (_result("responsive", 97), 400)],
        categories=cats,
    )

    assert fake.call_count == 2
    assert fake.call_args_list[0].kwargs["categories"] == cats
    assert fake.call_args_list[1].kwargs["categories"] == cats
    assert model == ai_review.CONFIRM_MODEL


def test_screen_failure_short_circuits_without_confirm_call(monkeypatch):
    # classify_document signals failure with tokens == 0; the cascade must
    # preserve that contract and not burn a confirm call on a doc that will
    # be retried anyway.
    (result, tokens, model), fake = _run_cascade(
        monkeypatch, [(_result("needs_review", 0), 0)]
    )

    assert fake.call_count == 1
    assert tokens == 0


def test_confirm_failure_returns_failure_sentinel(monkeypatch):
    # If the confirm pass fails, the document must stay unclassified (the
    # batch runner skips tokens == 0) rather than shipping a screen-only
    # answer for a doc the screen itself said might matter.
    (result, tokens, model), fake = _run_cascade(
        monkeypatch,
        [(_result("relevant", 90), 150), (_result("needs_review", 0), 0)],
    )

    assert fake.call_count == 2
    assert tokens == 0
