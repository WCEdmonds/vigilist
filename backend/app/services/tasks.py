"""Cloud Tasks helpers for Phase B ingest fan-out."""

import json
import logging

from google.cloud import tasks_v2

from app.config import settings

logger = logging.getLogger(__name__)


def enqueue_phase_b_tasks(doc_ids: list[str], production_id: int) -> int:
    """Enqueue one Cloud Task per document for Phase B processing.

    Returns the number of tasks created.
    """
    if not settings.cloud_run_service_url:
        logger.warning("VIGILIST_CLOUD_RUN_SERVICE_URL not set — skipping Cloud Tasks fan-out")
        return 0

    client = tasks_v2.CloudTasksClient()
    queue_path = client.queue_path(
        settings.gcp_project_id,
        settings.gcp_location,
        settings.cloud_tasks_queue,
    )

    handler_url = f"{settings.cloud_run_service_url}/api/ingest/process-document"
    created = 0

    for doc_id in doc_ids:
        payload = json.dumps({
            "doc_id": doc_id,
            "production_id": production_id,
        }).encode()

        task = tasks_v2.Task(
            http_request=tasks_v2.HttpRequest(
                http_method=tasks_v2.HttpMethod.POST,
                url=handler_url,
                headers={"Content-Type": "application/json"},
                body=payload,
                oidc_token=tasks_v2.OidcToken(
                    service_account_email=f"{settings.gcp_project_id}@appspot.gserviceaccount.com",
                    audience=settings.cloud_run_service_url,
                ),
            ),
        )

        try:
            client.create_task(parent=queue_path, task=task)
            created += 1
        except Exception:
            logger.warning("Failed to enqueue task for doc %s", doc_id, exc_info=True)

    logger.info("Enqueued %d Phase B tasks for production %d", created, production_id)
    return created


def enqueue_embed_tasks(doc_ids: list[str], production_id: int) -> int:
    """Enqueue Cloud Tasks for embedding documents."""
    if not settings.cloud_run_service_url:
        logger.warning("VIGILIST_CLOUD_RUN_SERVICE_URL not set — skipping embed tasks")
        return 0

    client = tasks_v2.CloudTasksClient()
    queue_path = client.queue_path(
        settings.gcp_project_id,
        settings.gcp_location,
        settings.cloud_tasks_queue,
    )

    handler_url = f"{settings.cloud_run_service_url}/api/ingest/embed-document"
    created = 0

    for doc_id in doc_ids:
        payload = json.dumps({
            "doc_id": doc_id,
            "production_id": production_id,
        }).encode()

        task = tasks_v2.Task(
            http_request=tasks_v2.HttpRequest(
                http_method=tasks_v2.HttpMethod.POST,
                url=handler_url,
                headers={"Content-Type": "application/json"},
                body=payload,
                oidc_token=tasks_v2.OidcToken(
                    service_account_email=f"{settings.gcp_project_id}@appspot.gserviceaccount.com",
                    audience=settings.cloud_run_service_url,
                ),
            ),
        )

        try:
            client.create_task(parent=queue_path, task=task)
            created += 1
        except Exception:
            logger.warning("Failed to enqueue embed task for doc %s", doc_id, exc_info=True)

    logger.info("Enqueued %d embed tasks for production %d", created, production_id)
    return created
