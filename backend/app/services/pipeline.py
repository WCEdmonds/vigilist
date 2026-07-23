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
from app.services.audit import log_action, resolve_audit_actor
from app.services.brief import generate_brief
from app.services.clustering import cluster_production

logger = logging.getLogger(__name__)

STAGES = ("clustering", "summaries", "entities", "brief")
SUMMARY_BATCH_SIZE = 25
ENTITY_BATCH_SIZE = 10


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


async def _log_summary_batch_completed(db, production_id: int, written_count: int) -> None:
    """Log summary_batch_completed as the production owner, once, when the
    summaries stage finishes (whether it ran out of rows or gave up after a
    batch that wrote nothing). Ambient action — no owner means no actor to
    attribute it to, so we skip logging entirely rather than log with None.
    Telemetry is best-effort by design and must never affect pipeline outcomes.
    """
    try:
        prod = await db.get(Production, production_id)
        if prod is None:
            return
        actor = await resolve_audit_actor(db, prod)
        if actor is None:
            return
        await log_action(
            db, actor, "summary_batch_completed", "production", str(production_id),
            production_id=production_id,
            details={"summarized": written_count},
        )
        await db.commit()
    except Exception:
        logger.exception("summary_batch_completed audit logging failed for production %s", production_id)


async def _run_summaries(production_id: int) -> None:
    """Summarize documents that don't have a summary yet, in DB batches."""
    written_count = 0
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
                await _log_summary_batch_completed(db, production_id, written_count)
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
                        written_count += 1
            await db.commit()
            if not wrote_any:
                # Model returned nothing for a whole batch (no key / all empty
                # text): stop instead of spinning on the same rows forever.
                await _log_summary_batch_completed(db, production_id, written_count)
                return


async def _pending_extraction_docs(production_id: int, limit: int):
    async with async_session() as db:
        return (
            await db.execute(
                select(Document)
                .where(
                    Document.production_id == production_id,
                    Document.entities_extracted_at.is_(None),
                    Document.text_content.isnot(None),
                )
                .order_by(Document.bates_begin)
                .limit(limit)
            )
        ).scalars().all()


async def _extract_one_document(db, doc) -> bool:
    """Extract + persist one document. True = marked done. False = left
    unmarked for a later pipeline run (LLM failure). Never raises."""
    from app.services.entity_extraction import (
        extract_document_entities, header_candidates, merge_parsed, persist_extraction,
    )
    try:
        parsed = await extract_document_entities(doc.text_content or "")
        if parsed is None:
            return False
        headers = header_candidates(doc)
        if headers:
            parsed = merge_parsed([parsed, {"entities": headers, "events": [], "relationships": []}])
        await persist_extraction(db, doc.production_id, doc.id, doc.text_content or "", parsed)
        doc.entities_extracted_at = datetime.now(timezone.utc).replace(tzinfo=None)
        return True
    except Exception:
        logger.exception("Entity extraction failed for document %s", doc.id)
        return False


async def _run_entities(production_id: int) -> None:
    """Extract entities for documents not yet processed, in small batches,
    one commit per document. A batch where every document fails aborts the
    stage (rather than spinning); unmarked docs retry on the next run."""
    while True:
        pending = await _pending_extraction_docs(production_id, ENTITY_BATCH_SIZE)
        if not pending:
            return
        any_ok = False
        for doc in pending:
            async with async_session() as db:
                live = await db.get(Document, doc.id)
                if live is None or live.entities_extracted_at is not None:
                    continue
                if await _extract_one_document(db, live):
                    any_ok = True
                    await db.commit()
        if not any_ok:
            raise RuntimeError("entity extraction: entire batch failed (no API key or persistent errors)")


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
        # Telemetry is best-effort by design and must never affect pipeline outcomes.
        try:
            actor = await resolve_audit_actor(db, prod)
            if actor is not None:
                await log_action(
                    db, actor, "brief_generated", "production", str(production_id),
                    production_id=production_id,
                    details={"model": brief.get("model")},
                )
                await db.commit()
        except Exception:
            logger.exception("brief_generated audit logging failed for production %s", production_id)


_STAGE_RUNNERS = {
    "clustering": _run_clustering,
    "summaries": _run_summaries,
    "entities": _run_entities,
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
