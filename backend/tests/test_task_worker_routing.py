"""Cloud Tasks go to the worker service, not the web service.

Incident 2026-10-07: mailbox ingest batches and the ambient pipeline ran on
vigilist-api's own instances and wedged all of them, so sign-in and every
other request hung for hours. Tasks now target VIGILIST_CLOUD_TASKS_WORKER_URL.
"""

import asyncio
import json
from unittest.mock import patch

import pytest
from fastapi import HTTPException

import app.services.oidc as oidc
import app.services.tasks as tasks
from app.config import settings

API = "https://vigilist-api.example"
WORKER = "https://vigilist-worker.example"


class FakeClient:
    def __init__(self, sink):
        self.sink = sink

    def queue_path(self, *a):
        return "q"

    def create_task(self, parent, task):
        self.sink.append(task)


@pytest.fixture
def captured(monkeypatch):
    sink = []
    monkeypatch.setattr(tasks.tasks_v2, "CloudTasksClient", lambda: FakeClient(sink))
    monkeypatch.setattr(tasks, "is_configured", lambda: True)
    monkeypatch.setattr(settings, "cloud_run_service_url", API)
    monkeypatch.setattr(settings, "cloud_tasks_service_account", "sa@example")
    return sink


ENQUEUERS = [
    (lambda: tasks.enqueue_ingest_batch("j", 9, 0, 10), "/api/ingest/process-batch"),
    (lambda: tasks.enqueue_pipeline(9), "/api/ingest/run-pipeline"),
    (lambda: tasks.enqueue_reocr_batch(9, 0, 50), "/api/ingest/reocr-batch"),
    (lambda: tasks.enqueue_render_batch(3, ["d"]), "/api/production-sets/render-batch"),
    (lambda: tasks.enqueue_package(3), "/api/production-sets/package-worker"),
]


@pytest.mark.parametrize("enqueue,path", ENQUEUERS)
def test_every_task_targets_the_worker(captured, monkeypatch, enqueue, path):
    monkeypatch.setattr(settings, "cloud_tasks_worker_url", WORKER)
    enqueue()
    req = captured[0].http_request
    assert req.url == WORKER + path
    assert req.oidc_token.audience == WORKER
    assert captured[0].dispatch_deadline.seconds == tasks.MAX_DISPATCH_DEADLINE_S


def test_without_a_worker_tasks_fall_back_to_the_web_service(captured, monkeypatch):
    monkeypatch.setattr(settings, "cloud_tasks_worker_url", "")
    tasks.enqueue_ingest_batch("j", 9, 0, 10)
    req = captured[0].http_request
    assert req.url == API + "/api/ingest/process-batch"
    assert req.oidc_token.audience == API
    assert json.loads(req.body)["job_id"] == "j"


class FakeRequest:
    headers = {"Authorization": "Bearer tok"}


def _verify(monkeypatch, claim_aud):
    monkeypatch.setattr(settings, "cloud_run_service_url", API)
    monkeypatch.setattr(settings, "cloud_tasks_worker_url", WORKER)
    monkeypatch.setattr(settings, "cloud_tasks_service_account", "sa@example")

    def fake_verify(token, transport, audience):
        if claim_aud not in audience:
            raise ValueError("bad audience")
        return {"email": "sa@example", "aud": claim_aud}

    with patch.object(oidc.id_token, "verify_oauth2_token", fake_verify):
        asyncio.run(oidc.verify_cloud_tasks_request(FakeRequest()))


def test_worker_and_legacy_audiences_both_verify(monkeypatch):
    _verify(monkeypatch, WORKER)
    _verify(monkeypatch, API)  # tasks enqueued before the worker existed


def test_foreign_audience_is_rejected(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        _verify(monkeypatch, "https://evil.example")
    assert exc.value.status_code == 401
