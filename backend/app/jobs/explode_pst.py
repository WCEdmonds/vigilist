"""Cloud Run Job ``vigilist-pst-explode``: unpack one large PST once.

    python -m app.jobs.explode_pst --job-id J --storage-path P

Downloads the PST into in-memory /tmp, surveys it and explodes every item to
Storage (``pst_explode.unpack_container``), then records the survey on the job
through ``large_ingest.run_survey`` (same attempt counter and failure state as
before). A failure exits non-zero so Cloud Run retries; the last run
(``CLOUD_RUN_TASK_ATTEMPT`` >= the Job's max retries) gives the container up,
records the error on the job and still exits non-zero.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import logging
import os
import sys

logger = logging.getLogger("app.jobs.explode_pst")

# Progress is written at start and then at most this often while items
# advance; the status self-heal gives a container up after 4.5 h without it.
HEARTBEAT_S = 300


async def _heartbeat(factory, job_id: str, storage_path: str, progress, stop: asyncio.Event) -> None:
    from app.services import large_ingest

    last = None
    while True:
        if progress.value != last:
            last = progress.value
            try:
                async with factory() as db:
                    await large_ingest.record_survey_progress(db, job_id, storage_path)
            except Exception:
                logger.exception("Could not record unpack progress for %s", storage_path)
        try:
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_S)
            return
        except asyncio.TimeoutError:
            continue


async def _record_final_failure(factory, job_id: str, storage_path: str, error: str) -> None:
    """Best-effort: give the container up so the job does not wait forever."""
    from app.services import large_ingest

    try:
        async with factory() as db:
            await large_ingest._give_up_survey(db, job_id, storage_path, 0, error,
                                               reason="the unpack job failed")
    except Exception:
        logger.exception("Could not record the unpack failure of %s on job %s", storage_path, job_id)


async def run(job_id: str, storage_path: str, *, final_attempt: bool,
              session_factory=None, work=None) -> int:
    """One Job run. Returns the process exit code."""
    from app.models import IngestJob
    from app.services import large_ingest, pst_explode
    from app.services.pst_reader import short_error

    progress = pst_explode.Progress()

    if session_factory is None:
        from app.database import async_session_factory as session_factory
    try:
        async with session_factory() as db:
            job = await db.get(IngestJob, job_id)
            if job is None:
                logger.warning("Ingest job %s not found; nothing to unpack", job_id)
                return 0
            production_id = job.production_id
            await db.commit()
            if work is None:
                def work(path, progress):
                    return pst_explode.unpack_container(production_id, job_id, path,
                                                        progress=progress)
            if len(inspect.signature(work).parameters) >= 2:
                run_work = lambda path, _w=work: _w(path, progress)  # noqa: E731
            else:
                run_work = work
            stop = asyncio.Event()
            beat = asyncio.create_task(_heartbeat(session_factory, job_id, storage_path, progress, stop))
            try:
                outcome = await large_ingest.run_survey(
                    db, job_id, storage_path, run_work, give_up_on_failure=final_attempt)
            finally:
                stop.set()
                await beat
            if outcome == "discarded":
                # Given up on while this run worked: drop what it wrote.
                await asyncio.to_thread(pst_explode.delete_exploded,
                                        pst_explode.explode_prefix(production_id, job_id, storage_path))
    except Exception as e:
        logger.exception("Unpacking %s for job %s failed", storage_path, job_id)
        if final_attempt:
            # Includes failures before the attempt counter was written (e.g.
            # the database was unreachable): there is no later run to retry.
            await _record_final_failure(session_factory, job_id, storage_path, short_error(e))
        return 1
    logger.info("Unpack of %s for job %s: %s", storage_path, job_id, outcome)
    # "refused" is permanent (e.g. too large): exit 0 so Cloud Run does not retry.
    return 1 if outcome == "gave_up" else 0


def main(argv: list[str] | None = None) -> None:
    from app.services.pst_explode import EXPLODE_JOB_MAX_RETRIES

    parser = argparse.ArgumentParser(prog="python -m app.jobs.explode_pst")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--storage-path", required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        attempt = int(os.environ.get("CLOUD_RUN_TASK_ATTEMPT", "0"))
    except ValueError:
        attempt = 0
    final = attempt >= EXPLODE_JOB_MAX_RETRIES
    sys.exit(asyncio.run(run(args.job_id, args.storage_path, final_attempt=final)))


if __name__ == "__main__":
    main()
