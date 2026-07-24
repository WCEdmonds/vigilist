"""Pipeline wiring for the timeline_review stage."""

import asyncio

import pytest

import app.services.pipeline as pl


def test_stage_registered_between_entities_and_brief():
    assert pl.STAGES == ("clustering", "summaries", "entities",
                         "timeline_review", "brief")
    assert "timeline_review" in pl._STAGE_RUNNERS


def test_stages_to_run_includes_new_stage_for_existing_productions():
    # A production that ran before this feature has no timeline_review key.
    status = {"clustering": "done", "summaries": "done", "entities": "done",
              "brief": "done"}
    assert pl.stages_to_run(status, force=False) == ["timeline_review"]


def test_standalone_stage_runner_marks_failed_without_raising(monkeypatch):
    calls = []

    async def boom(production_id):
        raise RuntimeError("model exploded")

    async def spy_set_stage(production_id, stage, state, error=None):
        calls.append((stage, state, error))

    monkeypatch.setattr(pl, "_run_timeline_review", boom)
    monkeypatch.setattr(pl, "_set_stage", spy_set_stage)
    asyncio.run(pl.run_timeline_review_stage(7))  # must not raise
    assert calls[0] == ("timeline_review", "running", None)
    assert calls[1][0:2] == ("timeline_review", "failed")
    assert "model exploded" in calls[1][2]


def test_standalone_stage_runner_marks_done(monkeypatch):
    calls = []

    async def ok(production_id):
        pass

    async def spy_set_stage(production_id, stage, state, error=None):
        calls.append((stage, state))

    monkeypatch.setattr(pl, "_run_timeline_review", ok)
    monkeypatch.setattr(pl, "_set_stage", spy_set_stage)
    asyncio.run(pl.run_timeline_review_stage(7))
    assert calls == [("timeline_review", "running"), ("timeline_review", "done")]
