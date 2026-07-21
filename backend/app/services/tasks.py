"""Cloud Tasks helpers for ingest fan-out.

Ingest processes one batch of records per Cloud Task. Each task is a
separate HTTP POST to /api/ingest/process-batch, so every batch runs in
its own Cloud Run request and can't be killed by container scale-down.
"""

import json
import logging

from google.cloud import tasks_v2
from google.protobuf import duration_pb2

from app.config import settings

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    """True if Cloud Tasks is fully configured."""
    return bool(
        settings.cloud_run_service_url
        and settings.gcp_project_id
        and settings.cloud_tasks_service_account
    )


def enqueue_ingest_batch(
    job_id: str,
    production_id: int,
    start_idx: int,
    end_idx: int,
) -> None:
    """Enqueue a Cloud Task to process records[start_idx:end_idx] of an ingest job."""
    if not is_configured():
        raise RuntimeError(
            "Cloud Tasks not configured — set VIGILIST_CLOUD_RUN_SERVICE_URL, "
            "VIGILIST_GCP_PROJECT_ID, VIGILIST_CLOUD_TASKS_SERVICE_ACCOUNT"
        )

    client = tasks_v2.CloudTasksClient()
    queue_path = client.queue_path(
        settings.gcp_project_id,
        settings.gcp_location,
        settings.cloud_tasks_queue,
    )

    handler_url = f"{settings.cloud_run_service_url}/api/ingest/process-batch"
    payload = json.dumps({
        "job_id": job_id,
        "production_id": production_id,
        "start_idx": start_idx,
        "end_idx": end_idx,
    }).encode()

    task = tasks_v2.Task(
        http_request=tasks_v2.HttpRequest(
            http_method=tasks_v2.HttpMethod.POST,
            url=handler_url,
            headers={"Content-Type": "application/json"},
            body=payload,
            oidc_token=tasks_v2.OidcToken(
                service_account_email=settings.cloud_tasks_service_account,
                audience=settings.cloud_run_service_url,
            ),
        ),
    )

    client.create_task(parent=queue_path, task=task)
    logger.info(
        "Enqueued ingest batch for job %s production %d: records %d-%d",
        job_id, production_id, start_idx, end_idx,
    )


def enqueue_pipeline(production_id: int, force: bool = False) -> None:
    """Enqueue one ambient-pipeline run (clustering -> summaries -> brief) for a production."""
    if not is_configured():
        raise RuntimeError("Cloud Tasks not configured")

    client = tasks_v2.CloudTasksClient()
    queue_path = client.queue_path(
        settings.gcp_project_id,
        settings.gcp_location,
        settings.cloud_tasks_queue,
    )

    handler_url = f"{settings.cloud_run_service_url}/api/ingest/run-pipeline"
    payload = json.dumps({"production_id": production_id, "force": force}).encode()

    task = tasks_v2.Task(
        http_request=tasks_v2.HttpRequest(
            http_method=tasks_v2.HttpMethod.POST,
            url=handler_url,
            headers={"Content-Type": "application/json"},
            body=payload,
            oidc_token=tasks_v2.OidcToken(
                service_account_email=settings.cloud_tasks_service_account,
                audience=settings.cloud_run_service_url,
            ),
        ),
        # Pipeline can run long (clustering + batched summaries + brief
        # generation); use the Cloud Tasks maximum dispatch deadline so it
        # isn't killed mid-run.
        dispatch_deadline=duration_pb2.Duration(seconds=1800),
    )

    client.create_task(parent=queue_path, task=task)
    logger.info("Enqueued ambient pipeline run for production %d", production_id)


def enqueue_reocr_batch(
    production_id: int,
    offset: int,
    limit: int,
) -> None:
    """Enqueue a Cloud Task to re-OCR a batch of documents."""
    if not is_configured():
        raise RuntimeError("Cloud Tasks not configured")

    client = tasks_v2.CloudTasksClient()
    queue_path = client.queue_path(
        settings.gcp_project_id,
        settings.gcp_location,
        settings.cloud_tasks_queue,
    )

    handler_url = f"{settings.cloud_run_service_url}/api/ingest/reocr-batch"
    payload = json.dumps({
        "production_id": production_id,
        "offset": offset,
        "limit": limit,
    }).encode()

    task = tasks_v2.Task(
        http_request=tasks_v2.HttpRequest(
            http_method=tasks_v2.HttpMethod.POST,
            url=handler_url,
            headers={"Content-Type": "application/json"},
            body=payload,
            oidc_token=tasks_v2.OidcToken(
                service_account_email=settings.cloud_tasks_service_account,
                audience=settings.cloud_run_service_url,
            ),
        ),
    )

    client.create_task(parent=queue_path, task=task)
    logger.info(
        "Enqueued re-OCR batch for production %d: offset %d limit %d",
        production_id, offset, limit,
    )
