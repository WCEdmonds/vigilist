"""Cloud Tasks helpers for ingest fan-out.

Ingest processes one batch of records per Cloud Task. Each task is a
separate HTTP POST to /api/ingest/process-batch, so every batch runs in
its own Cloud Run request and can't be killed by container scale-down.

Every task targets ``settings.tasks_target_url``: the dedicated worker
service when VIGILIST_CLOUD_TASKS_WORKER_URL is set, otherwise the web
service itself.
"""

import json
import logging

from google.cloud import tasks_v2
from google.protobuf import duration_pb2

from app.config import settings

logger = logging.getLogger(__name__)

# Cloud Tasks' maximum dispatch deadline. The 10-minute default re-dispatched
# hour-long mailbox batches while the first attempt was still running, piling
# duplicate work onto the same instances.
MAX_DISPATCH_DEADLINE_S = 1800


def is_configured() -> bool:
    """True if Cloud Tasks is fully configured."""
    return bool(
        settings.cloud_run_service_url
        and settings.gcp_project_id
        and settings.cloud_tasks_service_account
    )


def _enqueue(path: str, body: dict, not_configured: str = "Cloud Tasks not configured") -> None:
    """Create one OIDC-signed POST task for ``path`` on the task target."""
    if not is_configured():
        raise RuntimeError(not_configured)

    client = tasks_v2.CloudTasksClient()
    queue_path = client.queue_path(
        settings.gcp_project_id,
        settings.gcp_location,
        settings.cloud_tasks_queue,
    )

    target = settings.tasks_target_url
    task = tasks_v2.Task(
        http_request=tasks_v2.HttpRequest(
            http_method=tasks_v2.HttpMethod.POST,
            url=f"{target}{path}",
            headers={"Content-Type": "application/json"},
            body=json.dumps(body).encode(),
            oidc_token=tasks_v2.OidcToken(
                service_account_email=settings.cloud_tasks_service_account,
                audience=target,
            ),
        ),
        dispatch_deadline=duration_pb2.Duration(seconds=MAX_DISPATCH_DEADLINE_S),
    )

    client.create_task(parent=queue_path, task=task)


def enqueue_ingest_batch(
    job_id: str,
    production_id: int,
    start_idx: int,
    end_idx: int,
) -> None:
    """Enqueue a Cloud Task to process records[start_idx:end_idx] of an ingest job."""
    _enqueue(
        "/api/ingest/process-batch",
        {
            "job_id": job_id,
            "production_id": production_id,
            "start_idx": start_idx,
            "end_idx": end_idx,
        },
        not_configured=(
            "Cloud Tasks not configured — set VIGILIST_CLOUD_RUN_SERVICE_URL, "
            "VIGILIST_GCP_PROJECT_ID, VIGILIST_CLOUD_TASKS_SERVICE_ACCOUNT"
        ),
    )
    logger.info(
        "Enqueued ingest batch for job %s production %d: records %d-%d",
        job_id, production_id, start_idx, end_idx,
    )


def enqueue_pipeline(production_id: int, force: bool = False,
                     job: str | None = None) -> None:
    """Enqueue one ambient-pipeline run (clustering -> summaries -> brief).

    `job` names a standalone run instead of the stage sequence. Without it the
    handler walks STAGES, so anything not in STAGES silently never executes —
    which is exactly how the merge review shipped enqueuing work that had no
    stage to run it, returned 200, and did nothing.
    """
    body = {"production_id": production_id, "force": force}
    if job:
        body["job"] = job
    _enqueue("/api/ingest/run-pipeline", body)
    logger.info("Enqueued ambient pipeline run for production %d", production_id)


def enqueue_reocr_batch(
    production_id: int,
    offset: int,
    limit: int,
    force: bool = False,
) -> None:
    """Enqueue a Cloud Task to re-OCR a batch of documents."""
    _enqueue("/api/ingest/reocr-batch", {
        "production_id": production_id,
        "offset": offset,
        "limit": limit,
        "force": force,
    })
    logger.info(
        "Enqueued re-OCR batch for production %d: offset %d limit %d",
        production_id, offset, limit,
    )


def enqueue_render_batch(set_id: int, document_ids: list[str]) -> None:
    """Enqueue a Cloud Task to render one batch of production-set documents."""
    _enqueue("/api/production-sets/render-batch",
             {"set_id": set_id, "document_ids": document_ids})
    logger.info("Enqueued render batch for set %d: %d documents",
                set_id, len(document_ids))


def enqueue_package(set_id: int) -> None:
    """Enqueue a Cloud Task to package one production set into its ZIP."""
    _enqueue("/api/production-sets/package-worker", {"set_id": set_id})
    logger.info("Enqueued packaging for production set %d", set_id)
