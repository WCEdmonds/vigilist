"""Pipeline stage selection and status merging (pure functions)."""

from app.services.pipeline import STAGES, merge_stage, stages_to_run


def test_fresh_status_runs_all_stages():
    assert stages_to_run(None, force=False) == list(STAGES)


def test_done_stages_are_skipped_unless_forced():
    status = {"clustering": "done", "summaries": "failed", "brief": "pending"}
    assert stages_to_run(status, force=False) == ["summaries", "brief"]
    assert stages_to_run(status, force=True) == list(STAGES)


def test_merge_stage_sets_state_and_timestamp():
    out = merge_stage(None, "clustering", "running")
    assert out["clustering"] == "running"
    assert "updated_at" in out
    assert out.get("errors", {}) == {}


def test_merge_stage_records_and_clears_errors():
    failed = merge_stage({}, "brief", "failed", error="model unavailable")
    assert failed["errors"]["brief"] == "model unavailable"
    recovered = merge_stage(failed, "brief", "done")
    assert "brief" not in recovered["errors"]
    # merge is non-destructive to other stages
    assert failed is not recovered
