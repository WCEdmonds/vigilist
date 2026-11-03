"""Ambient AI pipeline: clustering -> summaries -> brief.

Runs after ingest completes (or on demand). Owns its DB sessions so it can be
invoked from a Cloud Tasks worker, a background task, or an endpoint without
holding a request session across long model calls. Every stage is wrapped:
a failure marks that stage "failed" and the pipeline moves on — ingest and
document availability are never affected.
"""

import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text

from app.database import async_session
from app.models import Document, Production
from app.services import claude_batch
from app.services import tasks as task_service
from app.services.ai import generate_summaries_batch, is_refusal, mark_ai_refused, summary_request_params
from app.services.audit import log_action, resolve_audit_actor
from app.services.brief import generate_brief
from app.services.clustering import cluster_production

logger = logging.getLogger(__name__)

STAGES = ("clustering", "summaries", "entities", "timeline_review", "brief")
SUMMARY_BATCH_SIZE = 25
ENTITY_BATCH_SIZE = 10

# ── Message Batches for large per-document runs ──
# A summaries/entities run with more pending documents than this goes through
# the Message Batches API (half price) instead of synchronous calls.
BATCH_THRESHOLD_DOCS = 500
# Delay between polls of a submitted batch.
BATCH_POLL_SECONDS = 300
# One batch round holds at most this many requests / this much request JSON
# (the API allows 100,000 / 256 MB; these keep the submit and the apply pass
# well inside one Cloud Tasks request). A run with more pending documents
# continues in further rounds.
BATCH_MAX_REQUESTS = 10_000
BATCH_MAX_BYTES = 64_000_000
BATCH_LOAD_PAGE = 500
# Batches expire after 24 h. A waiting stage older than this lost its poll
# chain; the next run resubmits instead of waiting forever.
BATCH_STALE_HOURS = 30
# Applying results stops here and re-polls immediately to continue, so one
# poll request stays inside the dispatch deadline.
BATCH_APPLY_BUDGET_S = 1200
BATCH_APPLY_GROUP = 100
# A poll applying results claims the stage ("applying"); a claim this old
# (past the 30 min dispatch deadline) belongs to a dead worker and may be
# taken over.
BATCH_APPLY_STALE_MINUTES = 40
BATCHED_STAGES = ("summaries", "entities")
WAITING = "waiting"
APPLYING = "applying"
# ai_pipeline_status[NEXT_ATTEMPT_KEY][stage] == "sync": the stage's last
# batch left documents unprocessed, so its next attempt runs synchronously
# (once) instead of resubmitting them.
NEXT_ATTEMPT_KEY = "next_attempt"


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


def _age(raw) -> timedelta | None:
    try:
        when = datetime.fromisoformat(raw or "")
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - when


def _is_stale_apply(entry) -> bool:
    age = _age(entry.get("applying_at"))
    return age is None or age >= timedelta(minutes=BATCH_APPLY_STALE_MINUTES)


def _is_live_wait(entry) -> bool:
    """True for a stage waiting on a Message Batch that can still finish, or
    whose results a live poll is applying."""
    if not isinstance(entry, dict):
        return False
    if entry.get("status") == APPLYING:
        return not _is_stale_apply(entry)
    if entry.get("status") != WAITING:
        return False
    age = _age(entry.get("submitted_at"))
    return age is not None and age < timedelta(hours=BATCH_STALE_HOURS)


# Compare-and-set of one stage entry: succeeds only if the stage still holds
# exactly ``prev``, so of two concurrent polls only one claims the batch.
_CAS_STAGE_SQL = (
    "UPDATE productions SET ai_pipeline_status = jsonb_set(jsonb_set("
    "coalesce(ai_pipeline_status, cast('{}' as jsonb)), "
    "ARRAY[cast(:stage as text)], cast(:new as jsonb)), "
    "'{updated_at}', to_jsonb(cast(:now as text))) "
    "WHERE id = :pid AND ai_pipeline_status -> cast(:stage as text) = cast(:prev as jsonb) "
    "RETURNING id"
)


async def _cas_stage(production_id: int, stage: str, prev: dict, new: dict) -> bool:
    """Atomically replace the stage entry ``prev`` with ``new``. False when
    another writer changed it first."""
    async with async_session() as db:
        row = (await db.execute(text(_CAS_STAGE_SQL), {
            "pid": production_id, "stage": stage,
            "prev": json.dumps(prev), "new": json.dumps(new),
            "now": datetime.now(timezone.utc).isoformat(),
        })).first()
        await db.commit()
        return row is not None


async def _take_sync_marker(production_id: int, stage: str) -> bool:
    """True (and clear it) if the stage's next attempt must be synchronous."""
    async with async_session() as db:
        prod = await db.get(Production, production_id)
        if prod is None:
            return False
        status = dict(prod.ai_pipeline_status or {})
        marks = dict(status.get(NEXT_ATTEMPT_KEY) or {})
        if marks.pop(stage, None) != "sync":
            return False
        status[NEXT_ATTEMPT_KEY] = marks
        prod.ai_pipeline_status = status
        await db.commit()
        return True


async def _finish_batch(production_id: int, stage: str, state: str,
                        error: str | None, sync_next: bool) -> None:
    """Record the stage's state after its batch, plus the sync-next marker."""
    async with async_session() as db:
        prod = await db.get(Production, production_id)
        if prod is None:
            return
        out = merge_stage(prod.ai_pipeline_status, stage, state, error)
        marks = dict(out.get(NEXT_ATTEMPT_KEY) or {})
        if sync_next:
            marks[stage] = "sync"
        else:
            marks.pop(stage, None)
        out[NEXT_ATTEMPT_KEY] = marks
        prod.ai_pipeline_status = out
        await db.commit()


async def _set_stage_waiting(production_id: int, stage: str, batch_id: str, more: bool) -> None:
    """Record that ``stage`` waits on Message Batch ``batch_id``."""
    async with async_session() as db:
        prod = await db.get(Production, production_id)
        if prod is None:
            return
        out = merge_stage(prod.ai_pipeline_status, stage, WAITING)
        out[stage] = {"status": WAITING, "batch_id": batch_id, "more": more,
                      "submitted_at": datetime.now(timezone.utc).isoformat()}
        prod.ai_pipeline_status = out
        await db.commit()


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


def _apply_summary(doc, summary: str | None) -> bool:
    """Persist one document's summary (sync and batch paths). True if written.

    A refusal stores an empty summary (so ``summary IS NULL`` no longer picks
    the document up) and records the refusal on the document."""
    if is_refusal(summary):
        doc.summary = ""
        mark_ai_refused(doc, "summaries")
        return True
    if not summary:
        return False
    doc.summary = summary
    return True


async def _run_summaries(production_id: int):
    """Summaries stage: a large run is submitted as a Message Batch (returns
    WAITING); otherwise documents are summarized synchronously."""
    if await _maybe_submit_batch(production_id, "summaries"):
        return WAITING
    await _run_summaries_sync(production_id)


async def _run_summaries_sync(production_id: int) -> None:
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
                if summary or is_refusal(summary):
                    doc = await db.get(Document, doc_id)
                    if doc is not None and _apply_summary(doc, summary):
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


async def _persist_document_entities(db, doc, parsed: dict) -> None:
    """Persist one document's parsed extraction and mark it done (sync and
    batch paths). Caller commits."""
    from app.services import entity_extraction as ee

    headers = ee.header_candidates(doc)
    if headers:
        parsed = ee.merge_parsed([parsed, {"entities": headers, "events": [], "relationships": []}])
    await ee.persist_extraction(db, doc.production_id, doc.id, doc.text_content or "", parsed)
    doc.entities_extracted_at = datetime.now(timezone.utc).replace(tzinfo=None)


async def _extract_one_document(db, doc) -> bool:
    """Extract + persist one document. True = marked done. False = left
    unmarked for a later pipeline run (LLM failure). Never raises."""
    from app.services.entity_extraction import extract_document_entities
    try:
        parsed = await extract_document_entities(doc.text_content or "")
        if parsed is None:
            return False
        refused = parsed.pop("refused", False)
        await _persist_document_entities(db, doc, parsed)
        if refused:
            mark_ai_refused(doc, "entities")
        return True
    except Exception:
        logger.exception("Entity extraction failed for document %s", doc.id)
        return False


async def _run_entities(production_id: int):
    """Entities stage: a large run is submitted as a Message Batch (returns
    WAITING); otherwise documents are extracted synchronously."""
    if await _maybe_submit_batch(production_id, "entities"):
        return WAITING
    await _run_entities_sync(production_id)


async def _run_entities_sync(production_id: int) -> None:
    """Extract entities for documents not yet processed, in small batches,
    one commit per document. A batch where every document fails aborts the
    stage (rather than spinning); unmarked docs retry on the next run."""
    while True:
        pending = await _pending_extraction_docs(production_id, ENTITY_BATCH_SIZE)
        if not pending:
            return
        any_ok = False
        attempted = 0
        for doc in pending:
            async with async_session() as db:
                live = await db.get(Document, doc.id)
                if live is None or live.entities_extracted_at is not None:
                    continue
                attempted += 1
                if await _extract_one_document(db, live):
                    any_ok = True
                    await db.commit()
        if attempted == 0:
            # Every doc in this batch was already handled elsewhere (deleted,
            # or marked done by a concurrent worker) — nothing failed. The
            # next _pending_extraction_docs select won't return them, so
            # looping can't spin.
            continue
        if not any_ok:
            raise RuntimeError("entity extraction: entire batch failed (no API key or persistent errors)")


def _pending_condition(stage: str):
    if stage == "summaries":
        return Document.summary.is_(None)
    return Document.entities_extracted_at.is_(None)


async def _count_pending(production_id: int, stage: str) -> int:
    async with async_session() as db:
        return int((await db.execute(
            select(func.count()).select_from(Document).where(
                Document.production_id == production_id,
                Document.text_content.isnot(None),
                _pending_condition(stage),
            )
        )).scalar() or 0)


async def _load_batch_docs(production_id: int, stage: str, offset: int, limit: int):
    async with async_session() as db:
        return (await db.execute(
            select(Document).where(
                Document.production_id == production_id,
                Document.text_content.isnot(None),
                _pending_condition(stage),
            ).order_by(Document.bates_begin, Document.id).offset(offset).limit(limit)
        )).scalars().all()


def _batch_requests_for(stage: str, doc) -> list[tuple[str, dict]]:
    """Requests for one document, built by the same helpers as the sync
    calls. Summaries use the document id as ``custom_id``; entity slices use
    ``{document id}_{slice index}``."""
    if stage == "summaries":
        params = summary_request_params(doc.text_content)
        return [(str(doc.id), params)] if params else []
    from app.services.entity_extraction import extraction_request_params, slice_text
    return [(f"{doc.id}_{i}", extraction_request_params(chunk))
            for i, chunk in enumerate(slice_text(doc.text_content or ""))]


async def _maybe_submit_batch(production_id: int, stage: str) -> bool:
    """Submit this stage's pending documents as one Message Batch when the
    run is large. True = submitted (stage now waiting, poll enqueued).

    Needs Cloud Tasks: without it nothing could poll, so the run stays
    synchronous. A stage whose last batch left documents unprocessed runs
    synchronously once (the marker is consumed), so failing items are not
    resubmitted in a loop.
    """
    if stage not in BATCHED_STAGES or not task_service.is_configured():
        return False
    if await _take_sync_marker(production_id, stage):
        logger.info("Pipeline %s for production %s: synchronous retry after a batch",
                    stage, production_id)
        return False
    if await _count_pending(production_id, stage) <= BATCH_THRESHOLD_DOCS:
        return False

    requests: list[tuple[str, dict]] = []
    size = 0
    offset = 0
    more = False
    while not more:
        page = await _load_batch_docs(production_id, stage, offset, BATCH_LOAD_PAGE)
        if not page:
            break
        offset += len(page)
        for doc in page:
            reqs = _batch_requests_for(stage, doc)
            req_size = sum(len(json.dumps(params)) for _, params in reqs)
            if requests and (len(requests) + len(reqs) > BATCH_MAX_REQUESTS
                             or size + req_size > BATCH_MAX_BYTES):
                more = True
                break
            requests.extend(reqs)
            size += req_size
    if not requests:
        return False

    batch_id = await claude_batch.submit(requests)
    logger.info("Pipeline %s for production %s: batch %s (%d requests, more=%s)",
                stage, production_id, batch_id, len(requests), more)
    await _set_stage_waiting(production_id, stage, batch_id, more)
    task_service.enqueue_batch_poll(production_id, stage, BATCH_POLL_SECONDS)
    return True


def _as_uuid(value: str):
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


async def _apply_summary_results(production_id: int, results: dict,
                                 deadline: float) -> tuple[int, int, bool]:
    """Apply summary results. Returns (usable, unprocessed, finished)."""
    ids = sorted(cid for cid, text in results.items() if text or is_refusal(text))
    unprocessed = len(results) - len(ids)
    for start in range(0, len(ids), BATCH_APPLY_GROUP):
        if time.monotonic() > deadline:
            return len(ids), unprocessed, False
        async with async_session() as db:
            for cid in ids[start:start + BATCH_APPLY_GROUP]:
                key = _as_uuid(cid)
                doc = await db.get(Document, key) if key else None
                # Skip documents summarized since (or by an earlier pass).
                if doc is None or doc.production_id != production_id or doc.summary is not None:
                    continue
                _apply_summary(doc, results[cid])
            await db.commit()
    return len(ids), unprocessed, True


async def _apply_entity_results(production_id: int, results: dict,
                                deadline: float) -> tuple[int, int, bool]:
    """Apply entity results; a document with any failed slice is left
    unprocessed. Returns (usable documents, unprocessed documents, finished)."""
    from app.services.entity_extraction import merge_parsed, parse_extraction_response, slice_text

    by_doc: dict[str, dict[int, str | None]] = {}
    for cid, text in results.items():
        doc_id, _, idx = cid.rpartition("_")
        if not doc_id or not idx.isdigit():
            continue
        by_doc.setdefault(doc_id, {})[int(idx)] = text
    usable = sorted(d for d, slices in by_doc.items()
                    if all(t or is_refusal(t) for t in slices.values()))
    unprocessed = len(by_doc) - len(usable)
    for doc_id in usable:
        if time.monotonic() > deadline:
            return len(usable), unprocessed, False
        key = _as_uuid(doc_id)
        if key is None:
            continue
        async with async_session() as db:
            doc = await db.get(Document, key)
            if doc is None or doc.production_id != production_id or doc.entities_extracted_at is not None:
                continue
            slices = by_doc[doc_id]
            if sorted(slices) != list(range(len(slice_text(doc.text_content or "")))):
                continue  # text changed since submit: re-extract on a later run
            try:
                # Refused slices are not retried: merge the rest and record it.
                parsed = merge_parsed([parse_extraction_response(slices[i]) for i in sorted(slices)
                                       if not is_refusal(slices[i])])
                await _persist_document_entities(db, doc, parsed)
                if any(is_refusal(t) for t in slices.values()):
                    mark_ai_refused(doc, "entities")
                await db.commit()
            except Exception:
                logger.exception("Applying batched extraction failed for document %s", doc_id)
    return len(usable), unprocessed, True


async def _continue_pipeline(production_id: int) -> None:
    if task_service.is_configured():
        task_service.enqueue_pipeline(production_id)
    else:
        await run_ambient_pipeline(production_id)


class BatchPollClaimError(Exception):
    """A batch poll failed before claiming the stage (database or API
    unavailable): nothing was applied."""


async def poll_batch_stage(production_id: int, stage: str) -> None:
    """Cloud Tasks poll of a waiting stage's Message Batch.

    Still processing: poll again in BATCH_POLL_SECONDS. Ended: claim the
    stage ("applying", compare-and-set, so a duplicate poll backs off), apply
    the results through the same persistence code as the synchronous path,
    mark the stage done (or pending, when more documents remain for another
    round), and continue the pipeline.

    Errored items stay unprocessed and the stage's next attempt runs
    synchronously. A batch where nothing succeeded fails the stage and does
    not continue the pipeline (resubmitting would fail the same way).
    """
    # Only the database reads and the claim are pre-claim failures (capped by
    # the handler). A Batches API or enqueue failure propagates as itself, so
    # Cloud Tasks retries it normally.
    def claim_error(e):
        return BatchPollClaimError(f"batch poll for production {production_id} {stage} "
                                   f"failed before its claim: {e}")

    try:
        async with async_session() as db:
            prod = await db.get(Production, production_id)
            entry = (prod.ai_pipeline_status or {}).get(stage) if prod is not None else None
    except Exception as e:
        raise claim_error(e) from e
    if not (isinstance(entry, dict) and entry.get("batch_id")):
        logger.info("Batch poll for production %s %s: stage not waiting, nothing to do",
                    production_id, stage)
        return
    state = entry.get("status")
    if not (state == WAITING or (state == APPLYING and _is_stale_apply(entry))):
        logger.info("Batch poll for production %s %s: stage is %s, nothing to do",
                    production_id, stage, state)
        return

    results = await claude_batch.fetch_if_done(entry["batch_id"])
    if results is None:
        task_service.enqueue_batch_poll(production_id, stage, BATCH_POLL_SECONDS)
        return

    claimed = {**entry, "status": APPLYING,
               "applying_at": datetime.now(timezone.utc).isoformat()}
    try:
        won = await _cas_stage(production_id, stage, entry, claimed)
    except Exception as e:
        raise claim_error(e) from e
    if not won:
        logger.info("Batch poll for production %s %s: another poll claimed batch %s",
                    production_id, stage, entry["batch_id"])
        return

    deadline = time.monotonic() + BATCH_APPLY_BUDGET_S
    apply = _apply_summary_results if stage == "summaries" else _apply_entity_results
    usable, unprocessed, finished = await apply(production_id, results, deadline)
    if not finished:
        # Out of time: release the claim and re-poll now; already-applied
        # documents are skipped.
        released = {k: v for k, v in claimed.items() if k != "applying_at"}
        released["status"] = WAITING
        await _cas_stage(production_id, stage, claimed, released)
        task_service.enqueue_batch_poll(production_id, stage, 0)
        return

    if results and usable == 0:
        await _finish_batch(production_id, stage, "failed",
                            f"batch {entry['batch_id']}: no request succeeded; "
                            "the next run retries synchronously", sync_next=True)
        return
    if entry.get("more"):
        # The next round's batch includes this round's failures again, and
        # always carries new documents, so it cannot loop without progress.
        await _finish_batch(production_id, stage, "pending", None, sync_next=False)
    else:
        await _finish_batch(production_id, stage, "done", None, sync_next=unprocessed > 0)
        if stage == "summaries":
            async with async_session() as db:
                await _log_summary_batch_completed(db, production_id, usable)
    await _continue_pipeline(production_id)


async def _run_timeline_review(production_id: int) -> None:
    """AI review of the extracted timeline (spec:
    docs/superpowers/specs/2026-07-24-timeline-review-design.md). Runs after
    entities so the review sees the full extraction, before brief so the
    brief regenerates from the cleaned chronology."""
    from app.services.audit import resolve_audit_actor
    from app.services.timeline_review import run_timeline_review
    async with async_session() as db:
        prod = await db.get(Production, production_id)
        if prod is None:
            return
        actor = await resolve_audit_actor(db, prod)
        if actor is None:
            logger.warning("Timeline review skipped for production %s: "
                           "no owner to attribute audit rows to", production_id)
            return
        summary = await run_timeline_review(db, production_id, actor)
        if isinstance(summary, dict) and "sampled" in summary:
            # Record the cap in the stage status, next to the stage state.
            prod.ai_pipeline_status = {
                **(prod.ai_pipeline_status or {}),
                "timeline_review_sampling": {
                    k: summary[k] for k in ("sampled", "events_total", "events_reviewed")},
            }
        await db.commit()


async def _run_merge_review(production_id: int) -> None:
    """AI review of the entity merge docket (spec:
    docs/superpowers/specs/2026-07-30-ai-merge-assistant-design.md).

    On-demand only — it is not a pipeline stage, because unlike the timeline
    review there is nothing downstream waiting on it and a merge is a
    judgement the reviewer may want to make themselves.
    """
    from app.services.audit import resolve_audit_actor
    from app.services.merge_review import run_merge_review
    async with async_session() as db:
        prod = await db.get(Production, production_id)
        if prod is None:
            return
        actor = await resolve_audit_actor(db, prod)
        if actor is None:
            logger.warning("Merge review skipped for production %s: "
                           "no owner to attribute audit rows to", production_id)
            return
        await run_merge_review(db, production_id, actor)
        await db.commit()


async def run_merge_review_stage(production_id: int) -> None:
    """Standalone merge-review run for the manager endpoint — same stage-status
    writes as the pipeline loop so one status surface serves both. Never raises."""
    await _set_stage(production_id, "merge_review", "running")
    try:
        await _run_merge_review(production_id)
    except Exception as exc:
        logger.exception("Merge review failed for production %s", production_id)
        await _set_stage(production_id, "merge_review", "failed", error=str(exc)[:300])
    else:
        await _set_stage(production_id, "merge_review", "done")


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
    "timeline_review": _run_timeline_review,
    "brief": _run_brief,
}


async def run_ambient_pipeline(production_id: int, force: bool = False) -> None:
    """Run the ambient pipeline's pending stages for a production.

    Callers must serialize invocations per production: each stage's status
    write is read-modify-write against `Production.ai_pipeline_status`, so
    two concurrent runs for the same production can race and clobber state.

    A stage waiting on a Message Batch stops the run (later stages depend on
    it); the batch poll resumes the pipeline once the results are applied.
    """
    async with async_session() as db:
        prod = await db.get(Production, production_id)
        if prod is None:
            return
        status = dict(prod.ai_pipeline_status or {})
        pending = stages_to_run(status, force)

    for stage in pending:
        if _is_live_wait(status.get(stage)):
            logger.info("Pipeline for production %s waits on the %s batch", production_id, stage)
            return
        await _set_stage(production_id, stage, "running")
        try:
            outcome = await _STAGE_RUNNERS[stage](production_id)
        except Exception as exc:  # never let one stage kill the rest
            logger.exception("Pipeline stage %s failed for production %s", stage, production_id)
            await _set_stage(production_id, stage, "failed", error=str(exc)[:300])
        else:
            if outcome == WAITING:
                return
            await _set_stage(production_id, stage, "done")


async def run_timeline_review_stage(production_id: int) -> None:
    """Standalone timeline-review run for the manager endpoint — same
    stage-status writes as the pipeline loop, so one status surface serves
    both triggers. Never raises."""
    await _set_stage(production_id, "timeline_review", "running")
    try:
        await _run_timeline_review(production_id)
    except Exception as exc:
        logger.exception("Timeline review failed for production %s", production_id)
        await _set_stage(production_id, "timeline_review", "failed",
                         error=str(exc)[:300])
    else:
        await _set_stage(production_id, "timeline_review", "done")
