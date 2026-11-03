"""Stale-"running" detection for the pipeline 409 guard (pure function)."""

from datetime import datetime, timedelta, timezone

from app.routers.productions import _is_actively_running


def test_no_status_is_not_running():
    assert _is_actively_running(None) is False


def test_running_fresh_is_running():
    status = {"clustering": "running", "updated_at": datetime.now(timezone.utc).isoformat()}
    assert _is_actively_running(status) is True


def test_running_stale_is_not_running():
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    status = {"clustering": "running", "updated_at": old.isoformat()}
    assert _is_actively_running(status) is False


def test_running_unparseable_timestamp_is_running():
    status = {"clustering": "running", "updated_at": "not-a-timestamp"}
    assert _is_actively_running(status) is True


# m2: a stage waiting on a Message Batch (or applying its results) is live.


def test_waiting_batch_stage_is_running():
    now = datetime.now(timezone.utc).isoformat()
    status = {"summaries": {"status": "waiting", "batch_id": "b1", "submitted_at": now}}
    assert _is_actively_running(status) is True


def test_applying_batch_stage_is_running():
    now = datetime.now(timezone.utc).isoformat()
    status = {"entities": {"status": "applying", "batch_id": "b1", "submitted_at": now,
                           "applying_at": now}}
    assert _is_actively_running(status) is True


def test_expired_waiting_batch_stage_is_not_running():
    old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    status = {"summaries": {"status": "waiting", "batch_id": "b1", "submitted_at": old}}
    assert _is_actively_running(status) is False
