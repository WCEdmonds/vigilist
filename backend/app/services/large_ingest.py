"""Large-container (multi-GB PST/OST) ingest: detection and survey.

Containers at or above ``settings.large_container_bytes`` skip the readpst
batch path. They are surveyed first (cheap, read-only walk of the folder tree)
so the user can see counts and cost before any paid work starts.
"""

import asyncio
import logging
import os

from app.config import settings
from app.models import IngestJob
from app.services import pst_reader

logger = logging.getLogger(__name__)

LARGE_CONTAINER_EXTS = {".pst", ".ost"}


# Import-time snapshot for display; `is_large_container` reads settings live.
LARGE_CONTAINER_BYTES = settings.large_container_bytes


def is_large_container(item: dict) -> bool:
    ext = os.path.splitext(item.get("filename") or "")[1].lower()
    return ext in LARGE_CONTAINER_EXTS and int(item.get("size") or 0) >= settings.large_container_bytes


def large_source_key(storage_path: str) -> str:
    return f"native:{storage_path}"


def _survey_container(storage_path: str) -> dict:
    with pst_reader.open_pst(storage_path) as handle:
        result = pst_reader.survey(handle)
    result.storage_path = storage_path
    return result.to_json()


async def run_survey(db, job_id: str, storage_path: str) -> None:
    """Survey one container and record it on the job.

    When every large container in the job has a survey, the job moves to
    ``awaiting_selection``.
    """
    job = await db.get(IngestJob, job_id)
    if not job:
        return
    # Release the connection for the (minutes-long) container walk.
    await db.commit()

    survey_json = await asyncio.to_thread(_survey_container, storage_path)

    # Re-read under a row lock so two surveys finishing together can't lose
    # each other's write or both miss the "all surveyed" transition.
    # Sessions use expire_on_commit=False, so without populate_existing the
    # identity map would hand back the stale pre-survey object.
    job = await db.get(IngestJob, job_id, with_for_update=True, populate_existing=True)
    surveys = dict(job.survey or {})
    surveys[storage_path] = survey_json
    job.survey = surveys
    expected = (job.field_mapping or {}).get("large_containers") or []
    if all(p in surveys for p in expected) and job.status == "surveying":
        job.status = "awaiting_selection"
    await db.commit()
