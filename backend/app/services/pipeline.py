"""Ambient AI pipeline: clustering -> summaries -> brief.

Runs after ingest completes (or on demand). Owns its DB sessions so it can be
invoked from a Cloud Tasks worker, a background task, or an endpoint without
holding a request session across long model calls. Every stage is wrapped:
a failure marks that stage "failed" and the pipeline moves on — ingest and
document availability are never affected.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.database import async_session
from app.models import Document, Production
from app.services.ai import generate_summaries_batch
from app.services.brief import generate_brief
from app.services.clustering import cluster_production

logger = logging.getLogger(__name__)

STAGES = ("clustering", "summaries", "brief")
SUMMARY_BATCH_SIZE = 25


def stages_to_run(status: dict | None, force: bool) -> list[str]:
    if force or not status:
        return list(STAGES)
    return [s for s in STAGES if status.get(s) != "done"]


def merge_stage(status: dict | None, stage: str, state: str, error: str | None = None) -> dict:
    out = dict(status or {})
    errors = dict(out.get("errors") or {})
    out[stage] = state
    if error:
        errors[stage] = error
    else:
        errors.pop(stage, None)
    out["errors"] = errors
    out["updated_at"] = datetime.now(timezone.utc).isoformat()
    return out


async def _set_stage(production_id: int, stage: str, state: str, error: str | None = None) -> None:
    """Persist one stage transition in its own short transaction."""
    async with async_session() as db:
        prod = await db.get(Production, production_id)
        if prod is None:
            return
        prod.ai_pipeline_status = merge_stage(prod.ai_pipeline_status, stage, state, error)
        await db.commit()


async def _run_clustering(production_id: int) -> None:
    async with async_session() as db:
        await cluster_production(db, production_id)
        await db.commit()
    # Near-duplicate detection used to ride on the old Corpus Analysis page's
    # cluster button; it now rides the ambient clustering stage. Best-effort —
    # duplicates are an enhancement, not a gate.
    try:
        async with async_session() as db:
            from app.services.duplicates import detect_duplicates
            await detect_duplicates(db, production_id)
            await db.commit()
    except Exception:
        logger.exception("Duplicate detection failed for production %s", production_id)


async def _run_summaries(production_id: int) -> None:
    """Summarize documents that don't have a summary yet, in DB batches."""
    while True:
        async with async_session() as db:
            rows = (
                await db.execute(
                    select(Document.id, Document.text_content)
                    .where(
                        Document.production_id == production_id,
                        Document.summary.is_(None),
                        Document.text_content.isnot(None),
                    )
                    .order_by(Document.bates_begin)
                    .limit(SUMMARY_BATCH_SIZE)
                )
            ).all()
            if not rows:
                return
            results = await generate_summaries_batch(
                [(str(r[0]), r[1]) for r in rows]
            )
            wrote_any = False
            for doc_id, summary in results.items():
                if summary:
                    doc = await db.get(Document, doc_id)
                    if doc is not None:
                        doc.summary = summary
                        wrote_any = True
            await db.commit()
            if not wrote_any:
                # Model returned nothing for a whole batch (no key / all empty
                # text): stop instead of spinning on the same rows forever.
                return


async def _run_brief(production_id: int) -> None:
    async with async_session() as db:
        brief = await generate_brief(db, production_id)
        if brief is None:
            raise RuntimeError("brief generation returned no result")
        prod = await db.get(Production, production_id)
        if prod is None:
            return
        prod.brief = brief
        await db.commit()


_STAGE_RUNNERS = {
    "clustering": _run_clustering,
    "summaries": _run_summaries,
    "brief": _run_brief,
}


async def run_ambient_pipeline(production_id: int, force: bool = False) -> None:
    """Run the ambient pipeline's pending stages for a production.

    Callers must serialize invocations per production: each stage's status
    write is read-modify-write against `Production.ai_pipeline_status`, so
    two concurrent runs for the same production can race and clobber state.
    """
    async with async_session() as db:
        prod = await db.get(Production, production_id)
        if prod is None:
            return
        pending = stages_to_run(prod.ai_pipeline_status, force)

    for stage in pending:
        await _set_stage(production_id, stage, "running")
        try:
            await _STAGE_RUNNERS[stage](production_id)
        except Exception as exc:  # never let one stage kill the rest
            logger.exception("Pipeline stage %s failed for production %s", stage, production_id)
            await _set_stage(production_id, stage, "failed", error=str(exc)[:300])
        else:
            await _set_stage(production_id, stage, "done")
