"""Ambient AI pipeline: clustering -> (summaries || entities) -> timeline
review -> brief.

Runs after ingest completes (or on demand). Owns its DB sessions so it can be
invoked from a Cloud Tasks worker, a background task, or an endpoint without
holding a request session across long model calls. Every stage is wrapped:
a failure marks that stage "failed" and the pipeline moves on — ingest and
document availability are never affected.
"""

import contextvars
import copy
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import String, func, literal, select, text, tuple_

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
# (the API allows 100,000 / 256 MB; these keep each submit and each apply
# pass well inside one Cloud Tasks request). A run with more pending
# documents is split into several rounds, all submitted at once.
BATCH_MAX_REQUESTS = 10_000
BATCH_MAX_BYTES = 64_000_000
BATCH_LOAD_PAGE = 500
# At most this many of one stage's batches are in flight; further rounds go
# out as earlier ones finish. Anthropic also caps the requests queued per
# organization (by tier); a submit refused for that is retried by the poll.
# Four per stage keeps both stages together near 80k queued requests (the
# account's batch queue tier is unconfirmed).
MAX_INFLIGHT_BATCHES = 4
# One deadline per Cloud Tasks request, shared by everything it does: a
# pipeline run (both stages' submits) gets RUN_BUDGET_S from its start, a
# poll POLL_BUDGET_S (apply first, then top-up submits with what is left).
# No new round is built with less than BATCH_SUBMIT_MIN_REMAINING_S left; the
# rest go out from the stage's poll. Well inside the 30 min dispatch deadline.
RUN_BUDGET_S = 1200
POLL_BUDGET_S = 1500
BATCH_SUBMIT_MIN_REMAINING_S = 120
# The current request's deadline (time.monotonic()), set by the run / poll.
_REQUEST_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "pipeline_request_deadline", default=None)
# A submitter claim older than this belongs to a dead worker.
BATCH_SUBMIT_STALE_MINUTES = 20
# A scheduled poll counts as pending until this long past its due time.
BATCH_POLL_SLACK_S = 60
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
# Run after both per-document stages, claimed once per completion (see
# _claim_final_phase).
FINAL_STAGES = ("timeline_review", "brief")
FINAL_PHASE_KEY = "final_phase"
FINAL_PHASE_STALE_MINUTES = 45
WAITING = "waiting"
APPLYING = "applying"
# Per-batch states inside a parallel stage entry.
B_PROCESSING = "processing"
B_APPLYING = "applying"
B_APPLIED = "applied"
B_FAILED = "failed"
# A stage "running"/"queued" whose status hasn't been written for this long
# is presumed dead (same ceiling as the run endpoint's 409 guard).
ANALYSIS_STALE_MINUTES = 45
ANALYSIS_KEYS = STAGES + ("merge_review",)
AI_RUNNING_INFO = ("AI analysis already running; run analysis again after it finishes "
                   "to include these new documents")
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


def _parse_ts(raw) -> datetime | None:
    try:
        when = datetime.fromisoformat(raw or "")
    except (TypeError, ValueError):
        return None
    return when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when


def _age(raw) -> timedelta | None:
    when = _parse_ts(raw)
    return None if when is None else datetime.now(timezone.utc) - when


def _is_stale_apply(entry) -> bool:
    age = _age(entry.get("applying_at"))
    return age is None or age >= timedelta(minutes=BATCH_APPLY_STALE_MINUTES)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_parallel(entry) -> bool:
    """A stage entry in the parallel shape: ``{"status", "batches": [...]}``.
    The legacy single-batch shape carries ``batch_id`` + ``more`` instead."""
    return isinstance(entry, dict) and isinstance(entry.get("batches"), list)


def _unfinished(entry) -> list[dict]:
    return [b for b in entry.get("batches") or [] if b.get("state") in (B_PROCESSING, B_APPLYING)]


def _normalized(entry: dict) -> dict:
    """Top-level status of a parallel entry: applying while any batch is."""
    entry["status"] = APPLYING if any(b.get("state") == B_APPLYING
                                      for b in entry["batches"]) else WAITING
    return entry


def _is_live_wait(entry) -> bool:
    """True for a stage waiting on a Message Batch that can still finish, or
    whose results a live poll is applying."""
    if not isinstance(entry, dict):
        return False
    if _is_parallel(entry):
        if any(b.get("state") == B_APPLYING and not _is_stale_apply(b) for b in entry["batches"]):
            return True
        if entry.get("status") not in (WAITING, APPLYING):
            return False
        # The stage's poll chain owns it until it finalizes; submitted_at is
        # refreshed by every submission.
        age = _age(entry.get("submitted_at"))
        return age is not None and age < timedelta(hours=BATCH_STALE_HOURS)
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
    "WHERE id = :pid AND coalesce(ai_pipeline_status -> cast(:stage as text), cast('null' as jsonb)) "
    "= cast(:prev as jsonb) "
    "RETURNING id"
)


async def _cas_stage(production_id: int, stage: str, prev, new) -> bool:
    """Atomically replace the stage entry ``prev`` with ``new``. False when
    another writer changed it first. ``prev=None`` matches a missing key."""
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
        prod = await db.get(Production, production_id, with_for_update=True)
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
        prod = await db.get(Production, production_id, with_for_update=True)
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


async def _set_stage(production_id: int, stage: str, state: str, error: str | None = None) -> None:
    """Persist one stage transition in its own short transaction.

    Read-modify-write of the whole status under a row lock (FOR UPDATE), so it
    serializes with the compare-and-set writers of concurrent batch polls and
    never writes back a stale copy of their entries."""
    async with async_session() as db:
        prod = await db.get(Production, production_id, with_for_update=True)
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


async def _persist_document_entities(db, doc, parsed: dict, index=None) -> None:
    """Persist one document's parsed extraction and mark it done (sync and
    batch paths). Caller commits. ``index``: an EntityIndex shared across
    the documents of one apply / sync chunk (see persist_extraction)."""
    from app.services import entity_extraction as ee

    headers = ee.header_candidates(doc)
    if headers:
        parsed = ee.merge_parsed([parsed, {"entities": headers, "events": [], "relationships": []}])
    await ee.persist_extraction(db, doc.production_id, doc.id, doc.text_content or "", parsed,
                                index=index)
    doc.entities_extracted_at = datetime.now(timezone.utc).replace(tzinfo=None)


async def _extract_one_document(db, doc, index=None) -> bool:
    """Extract + persist one document. True = marked done. False = left
    unmarked for a later pipeline run (LLM failure). Never raises."""
    from app.services.entity_extraction import extract_document_entities
    try:
        parsed = await extract_document_entities(doc.text_content or "")
        if parsed is None:
            return False
        refused = parsed.pop("refused", False)
        await _persist_document_entities(db, doc, parsed, index=index)
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
        # One entity index per chunk, shared by its documents. Consistent
        # because the entities stage runs alone for a production (the stage
        # start is a compare-and-set; batch applies are serial). Dropped
        # whenever a document is not committed: it already holds that
        # document's rolled-back entities/aliases.
        index = None
        for doc in pending:
            async with async_session() as db:
                live = await db.get(Document, doc.id)
                if live is None or live.entities_extracted_at is not None:
                    continue
                attempted += 1
                if index is None:
                    from app.services.entity_index import EntityIndex
                    index = EntityIndex.deferred()  # loads on first persist
                if await _extract_one_document(db, live, index=index):
                    try:
                        await db.commit()
                    except Exception:
                        index = None
                        raise
                    any_ok = True
                else:
                    index = None
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


async def _load_batch_docs(production_id: int, stage: str, after, limit: int):
    """One keyset page of the stage's pending documents in (bates_begin, id)
    order, strictly after ``after`` (a ``[bates_begin, id]`` pair) or from the
    start. Keyset rather than offset: documents applied meanwhile drop out of
    the pending set without shifting later pages."""
    async with async_session() as db:
        q = select(Document).where(
            Document.production_id == production_id,
            Document.text_content.isnot(None),
            _pending_condition(stage),
        )
        if after:
            q = q.where(tuple_(Document.bates_begin, Document.id) > tuple_(
                literal(after[0], String), literal(uuid.UUID(str(after[1])), Document.id.type)))
        return (await db.execute(
            q.order_by(Document.bates_begin, Document.id).limit(limit)
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


async def _read_stage(production_id: int, stage: str):
    async with async_session() as db:
        prod = await db.get(Production, production_id)
        return (prod.ai_pipeline_status or {}).get(stage) if prod is not None else None


class StageContention(RuntimeError):
    """A stage entry kept changing under a compare-and-set update."""


async def _update_entry(production_id: int, stage: str, fn, attempts: int = 25):
    """Compare-and-set update of one stage entry: ``fn`` gets a private copy of
    the current entry and returns the new one, or None to leave it alone.
    Retries when another writer got in first. Returns the written entry, or
    None when ``fn`` declined."""
    for _ in range(attempts):
        cur = await _read_stage(production_id, stage)
        new = fn(copy.deepcopy(cur))
        if new is None:
            return None
        if await _cas_stage(production_id, stage, cur, new):
            return new
    raise StageContention(f"production {production_id} {stage}: stage entry kept changing")


def _batch(entry: dict, batch_id: str) -> dict | None:
    return next((b for b in entry["batches"] if b.get("id") == batch_id), None)


def _submitter_is_live(entry: dict) -> bool:
    sub = entry.get("submitter")
    if not isinstance(sub, dict):
        return False
    age = _age(sub.get("at"))
    return age is not None and age < timedelta(minutes=BATCH_SUBMIT_STALE_MINUTES)


async def _build_round(production_id: int, stage: str, cursor):
    """The next round after ``cursor``: (requests, new cursor, documents,
    exhausted). A round stops before the document that would push it past the
    request or size cap (a single oversized document still goes alone)."""
    requests: list[tuple[str, dict]] = []
    size = 0
    docs = 0
    after = cursor
    while True:
        page = await _load_batch_docs(production_id, stage, after, BATCH_LOAD_PAGE)
        if not page:
            return requests, after, docs, True
        for doc in page:
            reqs = _batch_requests_for(stage, doc)
            req_size = sum(len(json.dumps(params)) for _, params in reqs)
            if requests and (len(requests) + len(reqs) > BATCH_MAX_REQUESTS
                             or size + req_size > BATCH_MAX_BYTES):
                return requests, after, docs, False
            requests.extend(reqs)
            size += req_size
            docs += 1 if reqs else 0
            after = [doc.bates_begin, str(doc.id)]
        if len(page) < BATCH_LOAD_PAGE:
            return requests, after, docs, True


_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


def _is_retryable_submit_error(exc: Exception) -> bool:
    """A refusal worth retrying later: rate / queue limit, overload, server or
    connection trouble. Anything else (bad request, auth, ...) is not."""
    try:
        import anthropic
    except ImportError:  # pragma: no cover - the SDK is a hard dependency
        return False
    if isinstance(exc, anthropic.APIConnectionError):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return getattr(exc, "status_code", None) in _RETRYABLE_STATUS
    return False


def _request_deadline() -> float:
    deadline = _REQUEST_DEADLINE.get()
    return deadline if deadline is not None else time.monotonic() + RUN_BUDGET_S


async def _submit_rounds(production_id: int, stage: str, token: str, *,
                         raise_first: bool, start_poll: bool) -> int:
    """Submit rounds from the stage's cursor while this caller holds the
    submitter claim ``token``, up to MAX_INFLIGHT_BATCHES in flight, until
    the request's shared deadline has less than BATCH_SUBMIT_MIN_REMAINING_S
    left. Each batch is recorded right after create(), before the next round
    is built, so a crash loses at most the round being submitted. A refused
    submit (rate / queue limit, overload) stops the loop on any round, the
    first included: the stage's poll retries it. With ``raise_first`` a
    non-retryable failure before any batch went out propagates (the stage
    fails). Always releases the claim. Returns the number submitted."""
    deadline = _request_deadline()
    submitted = 0
    try:
        while deadline - time.monotonic() >= BATCH_SUBMIT_MIN_REMAINING_S:
            entry = await _read_stage(production_id, stage)
            if not (_is_parallel(entry) and (entry.get("submitter") or {}).get("token") == token):
                break  # claim lost (stage finalized or taken over)
            if entry.get("exhausted") or len(_unfinished(entry)) >= MAX_INFLIGHT_BATCHES:
                break
            if any(b.get("state") == B_FAILED for b in entry["batches"]):
                break  # a batch where nothing succeeded: don't spend more
            requests, cursor, docs, exhausted = await _build_round(
                production_id, stage, entry.get("cursor"))
            if not requests:
                def mark_exhausted(cur, _c=cursor):
                    if not (_is_parallel(cur) and (cur.get("submitter") or {}).get("token") == token):
                        return None
                    cur["cursor"], cur["exhausted"] = _c, True
                    return cur
                await _update_entry(production_id, stage, mark_exhausted)
                break
            try:
                batch_id = await claude_batch.submit(requests)
            except Exception as exc:
                if raise_first and submitted == 0 and not _is_retryable_submit_error(exc):
                    raise
                logger.warning("Pipeline %s for production %s: batch submit refused (%s: %s); "
                               "the stage's poll retries it", stage, production_id,
                               type(exc).__name__, str(exc)[:200])
                break
            now = _now_iso()
            record = {"id": batch_id, "submitted_at": now, "doc_count": docs, "state": B_PROCESSING}

            def add_round(cur, _r=record, _c=cursor, _x=exhausted, _now=now):
                if not _is_parallel(cur):
                    return None
                cur["batches"].append(_r)
                cur["cursor"], cur["exhausted"], cur["submitted_at"] = _c, _x, _now
                return _normalized(cur)

            if await _update_entry(production_id, stage, add_round) is None:
                logger.error("Pipeline %s for production %s: batch %s submitted but the stage "
                             "entry is gone; its results are not applied", stage, production_id, batch_id)
                break
            submitted += 1
            logger.info("Pipeline %s for production %s: batch %s (%d requests, %d documents)",
                        stage, production_id, batch_id, len(requests), docs)
            if submitted == 1 and start_poll:
                await _schedule_poll(production_id, stage, BATCH_POLL_SECONDS)
            if exhausted:
                break
    finally:
        def release(cur):
            if not (_is_parallel(cur) and (cur.get("submitter") or {}).get("token") == token):
                return None
            cur.pop("submitter", None)
            return cur
        await _update_entry(production_id, stage, release)
    return submitted


async def _claim_submitter(production_id: int, stage: str) -> str | None:
    """Take the stage's submitter claim (only one caller builds and submits
    rounds at a time, or two would submit the same documents)."""
    token = uuid.uuid4().hex

    def claim(cur):
        if not _is_parallel(cur) or _submitter_is_live(cur) or cur.get("exhausted"):
            return None
        cur["submitter"] = {"token": token, "at": _now_iso()}
        return cur

    return token if await _update_entry(production_id, stage, claim) is not None else None


async def _schedule_poll(production_id: int, stage: str, delay_s: int) -> None:
    """Enqueue the stage's next poll unless one is already scheduled, so
    duplicate polls never fork the chain. ``next_poll_at`` in the entry is the
    due time of the pending poll."""
    due = datetime.now(timezone.utc) + timedelta(seconds=delay_s)

    def mark(cur):
        if not _is_parallel(cur):
            return None
        pending = cur.get("next_poll_at")
        if pending:
            try:
                when = datetime.fromisoformat(pending)
            except ValueError:
                when = None
            if when is not None and when > datetime.now(timezone.utc) + timedelta(seconds=BATCH_POLL_SLACK_S):
                return None
        cur["next_poll_at"] = due.isoformat()
        return cur

    if await _update_entry(production_id, stage, mark) is None:
        return
    try:
        task_service.enqueue_batch_poll(production_id, stage, delay_s)
    except Exception:
        def unmark(cur):
            if not _is_parallel(cur) or cur.get("next_poll_at") != due.isoformat():
                return None
            cur.pop("next_poll_at", None)
            return cur
        try:
            await _update_entry(production_id, stage, unmark)
        except Exception:
            logger.exception("Could not clear next_poll_at for production %s %s", production_id, stage)
        raise


async def _maybe_submit_batch(production_id: int, stage: str) -> bool:
    """Submit this stage's pending documents as Message Batches when the run
    is large: split into rounds by the per-batch caps and submit them all at
    once (up to MAX_INFLIGHT_BATCHES; the poll submits the rest as earlier
    ones finish). True = the stage is now waiting with a poll enqueued.

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

    token = uuid.uuid4().hex
    now = _now_iso()
    start = {"status": WAITING, "batches": [], "cursor": None, "exhausted": False,
             "submitted_at": now, "submitter": {"token": token, "at": now}}

    def create(cur):
        if _is_live_wait(cur):
            return None  # another run already owns this stage
        return copy.deepcopy(start)

    if await _update_entry(production_id, stage, create) is None:
        logger.info("Pipeline %s for production %s: already waiting on batches", stage, production_id)
        return True
    submitted = await _submit_rounds(production_id, stage, token, raise_first=True, start_poll=True)
    if submitted:
        return True
    entry = await _read_stage(production_id, stage)
    if _is_parallel(entry) and not entry.get("exhausted"):
        # Refused (rate / queue limit) or out of time before the first round:
        # stay waiting with no batches; the poll submits the rounds.
        await _schedule_poll(production_id, stage, BATCH_POLL_SECONDS)
        return True
    # Nothing to send after all (e.g. every pending text is blank): run the
    # stage synchronously instead.
    await _set_stage(production_id, stage, "running")
    return False


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
    # One in-memory entity index for the whole apply instead of loading and
    # scanning every entity per document. Consistent only because entity
    # batches apply one at a time per production (``serial`` in
    # _poll_parallel_inner; the legacy shape holds one batch). Dropped and
    # rebuilt after any document that fails, since it already reflects that
    # document's rolled-back changes.
    from app.services.entity_index import EntityIndex
    index = None
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
                if index is None:
                    index = EntityIndex.deferred()  # loads on first persist
                # Refused slices are not retried: merge the rest and record it.
                parsed = merge_parsed([parse_extraction_response(slices[i]) for i in sorted(slices)
                                       if not is_refusal(slices[i])])
                await _persist_document_entities(db, doc, parsed, index=index)
                if any(is_refusal(t) for t in slices.values()):
                    mark_ai_refused(doc, "entities")
                await db.commit()
            except Exception:
                index = None
                logger.exception("Applying batched extraction failed for document %s", doc_id)
    return len(usable), unprocessed, True


async def _continue_pipeline(production_id: int) -> None:
    """Resume the pipeline after a batched stage finished. A resume run never
    restarts a failed per-document stage (that waits for an explicit run)."""
    if task_service.is_configured():
        task_service.enqueue_pipeline(production_id, resume=True)
    else:
        await run_ambient_pipeline(production_id, resume=True)


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
    if _is_parallel(entry):
        await _poll_parallel(production_id, stage, entry)
        return
    # Legacy single-batch entry (``batch_id`` + ``more``), written before
    # parallel rounds: finish it as before. With ``more`` the stage goes back
    # to pending and the continuation submits the rest in the parallel shape.
    if not (isinstance(entry, dict) and entry.get("batch_id")):
        logger.info("Batch poll for production %s %s: stage not waiting, nothing to do",
                    production_id, stage)
        return
    state = entry.get("status")
    if state == APPLYING and not _is_stale_apply(entry):
        # Another request holds the apply claim. If that request was killed
        # (a deploy), this is Cloud Tasks retrying it seconds later: ending
        # here would end the legacy chain for good, since nothing else polls
        # a legacy entry. Re-arm a poll for when the claim goes stale; a
        # live applier finishes first and that poll then finds nothing to do.
        age = _age(entry.get("applying_at")) or timedelta(0)
        delay = max(0, int((timedelta(minutes=BATCH_APPLY_STALE_MINUTES) - age).total_seconds()))
        logger.info("Batch poll for production %s %s: apply claimed %s ago; re-polling in %ds",
                    production_id, stage, age, delay + BATCH_POLL_SLACK_S)
        task_service.enqueue_batch_poll(production_id, stage, delay + BATCH_POLL_SLACK_S)
        return
    if state != WAITING and state != APPLYING:
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


# The status endpoint's self-heal re-enqueues a stalled legacy apply's poll
# at most this often.
BATCH_HEAL_INTERVAL_S = BATCH_POLL_SECONDS


def _stalled_legacy_apply(entry) -> bool:
    """A legacy single-batch entry whose applying request died (claim past
    BATCH_APPLY_STALE_MINUTES) and has not been re-polled recently."""
    if not (isinstance(entry, dict) and not _is_parallel(entry) and entry.get("batch_id")
            and entry.get("status") == APPLYING and _is_stale_apply(entry)):
        return False
    healed = _age(entry.get("heal_poll_at"))
    return healed is None or healed >= timedelta(seconds=BATCH_HEAL_INTERVAL_S)


def needs_batch_heal(status: dict | None) -> bool:
    """Cheap check for the status endpoint: is any batched stage a stalled
    legacy apply that heal_stalled_batch_applies would re-poll?"""
    return any(_stalled_legacy_apply((status or {}).get(s)) for s in BATCHED_STAGES)


async def heal_stalled_batch_applies(production_id: int) -> list[str]:
    """Re-enqueue the poll of every stalled legacy apply (status endpoint
    self-heal). Normally a killed apply is picked up by the Cloud Tasks retry
    (which re-arms a poll, see poll_batch_stage); this covers a chain that
    lost every task anyway. The parallel shape needs none: its poll chain
    re-polls an applying batch every BATCH_POLL_SECONDS until it is stale.

    Marked with ``heal_poll_at`` (compare-and-set) so concurrent status
    reads enqueue one poll per BATCH_HEAL_INTERVAL_S. The poll itself then
    re-claims the stale apply by compare-and-set as usual."""
    healed = []
    for stage in BATCHED_STAGES:
        entry = await _read_stage(production_id, stage)
        if not _stalled_legacy_apply(entry):
            continue
        marked = {**entry, "heal_poll_at": _now_iso()}
        if not await _cas_stage(production_id, stage, entry, marked):
            continue
        logger.warning("Production %s %s: legacy batch %s apply stalled since %s; re-enqueuing its poll",
                       production_id, stage, entry.get("batch_id"), entry.get("applying_at"))
        task_service.enqueue_batch_poll(production_id, stage, 0)
        healed.append(stage)
    return healed


async def _poll_parallel(production_id: int, stage: str, entry: dict) -> None:
    """Poll every unapplied batch of a parallel stage entry.

    Each ended batch is claimed on its own (compare-and-set on the entry: the
    batch goes processing -> applying), applied through the shared helpers and
    marked applied, or failed when no request in it succeeded. Two polls may
    apply different batches; never the same one. Then more rounds are
    submitted if any are waiting for an in-flight slot. The stage finalizes
    once nothing is in flight and nothing is left to submit: done (with the
    sync marker if any item failed) and the pipeline continues, or failed if a
    batch had no success at all (no auto-continue).
    """
    started = time.monotonic()
    request_deadline = started + POLL_BUDGET_S
    deadline = started + min(BATCH_APPLY_BUDGET_S, POLL_BUDGET_S)
    token_deadline = _REQUEST_DEADLINE.set(request_deadline)
    try:
        await _poll_parallel_inner(production_id, stage, entry, deadline)
    finally:
        _REQUEST_DEADLINE.reset(token_deadline)


async def _poll_parallel_inner(production_id: int, stage: str, entry: dict, deadline: float) -> None:
    apply = _apply_summary_results if stage == "summaries" else _apply_entity_results
    # persist_extraction has no unique constraint: two entity batches applied
    # at once could duplicate entities or lose mention counts. Entities apply
    # one batch at a time per stage; summaries may run in parallel.
    serial = stage == "entities"
    for rec in list(entry["batches"]):
        state = rec.get("state")
        if not (state == B_PROCESSING or (state == B_APPLYING and _is_stale_apply(rec))):
            continue
        if serial:
            live = await _read_stage(production_id, stage)
            if _is_parallel(live) and any(
                    o.get("id") != rec["id"] and o.get("state") == B_APPLYING
                    and not _is_stale_apply(o) for o in live["batches"]):
                continue
        results = await claude_batch.fetch_if_done(rec["id"])
        if results is None:
            continue
        stamp = _now_iso()

        def claim(cur, _id=rec["id"], _stamp=stamp):
            b = _batch(cur, _id) if _is_parallel(cur) else None
            if b is None or not (b.get("state") == B_PROCESSING
                                 or (b.get("state") == B_APPLYING and _is_stale_apply(b))):
                return None
            if serial and any(o is not b and o.get("state") == B_APPLYING and not _is_stale_apply(o)
                              for o in cur["batches"]):
                return None  # another entity batch is being applied
            b["state"], b["applying_at"] = B_APPLYING, _stamp
            return _normalized(cur)

        if await _update_entry(production_id, stage, claim) is None:
            logger.info("Batch poll for production %s %s: batch %s claimed elsewhere",
                        production_id, stage, rec["id"])
            continue
        usable, unprocessed, finished = await apply(production_id, results, deadline)

        def settle(cur, _id=rec["id"], _stamp=stamp, _u=usable, _n=unprocessed,
                   _fin=finished, _failed=bool(results) and usable == 0):
            b = _batch(cur, _id) if _is_parallel(cur) else None
            if b is None or b.get("applying_at") != _stamp:
                return None
            b.pop("applying_at", None)
            if not _fin:
                b["state"] = B_PROCESSING  # applied documents are skipped next time
            else:
                b["state"] = B_FAILED if _failed else B_APPLIED
                b["usable"], b["unprocessed"] = _u, _n
            return _normalized(cur)

        await _update_entry(production_id, stage, settle)
        if not finished or time.monotonic() > deadline:
            await _schedule_poll(production_id, stage, 0)
            return

    entry = await _read_stage(production_id, stage)
    if (_is_parallel(entry) and not entry.get("exhausted")
            and _request_deadline() - time.monotonic() >= BATCH_SUBMIT_MIN_REMAINING_S
            and not any(b.get("state") == B_FAILED for b in entry["batches"])
            and len(_unfinished(entry)) < MAX_INFLIGHT_BATCHES):
        token = await _claim_submitter(production_id, stage)
        if token:
            await _submit_rounds(production_id, stage, token, raise_first=False, start_poll=False)

    entry = await _read_stage(production_id, stage)
    if not _is_parallel(entry):
        return
    if _unfinished(entry):
        await _schedule_poll(production_id, stage, BATCH_POLL_SECONDS)
        return
    any_failed = any(b.get("state") == B_FAILED for b in entry["batches"])
    if not entry.get("exhausted") and not any_failed:
        age = _age(entry.get("submitted_at"))
        if age is None or age < timedelta(hours=BATCH_STALE_HOURS):
            # Rounds still to submit (the last submit was refused): retry later.
            await _schedule_poll(production_id, stage, BATCH_POLL_SECONDS)
            return
        if await _cas_stage(production_id, stage, entry, "failed"):
            await _finish_batch(production_id, stage, "failed",
                                "the remaining documents could not be submitted as batches; "
                                "the next run retries", sync_next=False)
        return

    # Finalize exactly once: only the poll whose compare-and-set wins.
    final = "failed" if any_failed else "done"
    if not await _cas_stage(production_id, stage, entry, final):
        return
    if any_failed:
        ids = ", ".join(b["id"] for b in entry["batches"] if b.get("state") == B_FAILED)
        await _finish_batch(production_id, stage, "failed",
                            f"batch {ids}: no request succeeded; the next run retries synchronously",
                            sync_next=True)
        return
    unprocessed = sum(int(b.get("unprocessed") or 0) for b in entry["batches"])
    await _finish_batch(production_id, stage, "done", None, sync_next=unprocessed > 0)
    if stage == "summaries":
        async with async_session() as db:
            await _log_summary_batch_completed(
                db, production_id, sum(int(b.get("usable") or 0) for b in entry["batches"]))
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
        await db.commit()
    if isinstance(summary, dict) and "sampled" in summary:
        # Record the cap in the stage status, next to the stage state. Fresh
        # locked read: the copy loaded before the review is stale by now.
        async with async_session() as db:
            prod = await db.get(Production, production_id, with_for_update=True)
            if prod is not None:
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


def analysis_in_progress(status: dict | None) -> bool:
    """True while an AI analysis is under way on the production: a stage
    waiting on (or applying) Message Batches, or a stage running or queued
    whose status was written recently (a dead worker's stale "running" does
    not count)."""
    if not status:
        return False
    if any(_is_live_wait(status.get(s)) for s in STAGES):
        return True
    if not any(status.get(k) in ("running", "queued") for k in ANALYSIS_KEYS):
        return False
    age = _age(status.get("updated_at"))
    return age is None or age < timedelta(minutes=ANALYSIS_STALE_MINUTES)


async def _run_stage(production_id: int, stage: str) -> str | None:
    """Run one stage with its status writes. Returns the runner's outcome
    (WAITING for a batched stage), or "failed"."""
    await _set_stage(production_id, stage, "running")
    try:
        outcome = await _STAGE_RUNNERS[stage](production_id)
    except Exception as exc:  # never let one stage kill the rest
        logger.exception("Pipeline stage %s failed for production %s", stage, production_id)
        await _set_stage(production_id, stage, "failed", error=str(exc)[:300])
        return "failed"
    if outcome != WAITING:
        await _set_stage(production_id, stage, "done")
    return outcome


async def _start_per_doc_stage(production_id: int, stage: str, resume: bool) -> None:
    """Start summaries or entities unless it is already in hand.

    Skipped when it waits on live batches (never resubmitted while its poll
    chain owns it). A resume run (a batch poll's continuation) also leaves a
    failed or running stage alone: a failed stage waits for an explicit run,
    and a running one belongs to another run. The start itself is a
    compare-and-set from the state observed, so of two runs that both saw the
    stage pending only one starts it."""
    cur = await _read_stage(production_id, stage)
    if _is_live_wait(cur):
        logger.info("Pipeline for production %s: %s waits on its batches", production_id, stage)
        return
    if resume and cur in ("failed", "running"):
        logger.info("Pipeline resume for production %s: leaving %s %s", production_id, stage, cur)
        return
    if not await _cas_stage(production_id, stage, cur, "running"):
        logger.info("Pipeline for production %s: %s was started elsewhere", production_id, stage)
        return
    await _run_stage(production_id, stage)


async def _claim_final_phase(production_id: int, run_started: datetime) -> dict | None:
    """Claim the final phase (timeline review, brief) for this run, or None.

    None when another run holds a live claim, or when the final phase was
    completed after this run started (a duplicate continuation). The claim
    is a compare-and-set on ``ai_pipeline_status["final_phase"]``, so of two
    concurrent continuations exactly one wins."""
    cur = await _read_stage(production_id, FINAL_PHASE_KEY)
    if isinstance(cur, dict):
        if cur.get("token"):
            age = _age(cur.get("at"))
            if age is not None and age < timedelta(minutes=FINAL_PHASE_STALE_MINUTES):
                return None
        done_at = _parse_ts(cur.get("done_at"))
        if done_at is not None and done_at >= run_started:
            return None
    claim = {"token": uuid.uuid4().hex, "at": _now_iso()}
    return claim if await _cas_stage(production_id, FINAL_PHASE_KEY, cur, claim) else None


async def run_ambient_pipeline(production_id: int, force: bool = False,
                               resume: bool = False) -> None:
    """Run the ambient pipeline's pending stages for a production.

    Clustering first; then summaries and entities are both started (neither
    reads the other's output), so when both go through Message Batches both
    wait concurrently; then timeline review and brief, once both per-document
    stages are finished. A stage waiting on batches is never resubmitted
    here: its poll applies the results and calls the pipeline again
    (``resume=True``).

    Stage transitions that can race with a concurrent run or poll are
    compare-and-set (per-document stage start, final-phase claim) or written
    under a row lock; explicit runs are still serialized by the run endpoint's
    409 guard.
    """
    token = _REQUEST_DEADLINE.set(time.monotonic() + RUN_BUDGET_S)
    try:
        await _run_ambient_pipeline(production_id, force, resume)
    finally:
        _REQUEST_DEADLINE.reset(token)


async def _run_ambient_pipeline(production_id: int, force: bool, resume: bool) -> None:
    run_started = datetime.now(timezone.utc)
    async with async_session() as db:
        prod = await db.get(Production, production_id)
        if prod is None:
            return
        status = dict(prod.ai_pipeline_status or {})
        pending = stages_to_run(status, force)

    if "clustering" in pending:
        if resume and status.get("clustering") in ("failed", "running"):
            # A batch poll's continuation never reruns clustering (a full
            # re-cluster could eat the request and strand the batched
            # stages); only an explicit or forced run retries it.
            logger.info("Pipeline resume for production %s: leaving clustering %s",
                        production_id, status.get("clustering"))
        else:
            await _run_stage(production_id, "clustering")
    for stage in BATCHED_STAGES:
        if stage in pending:
            await _start_per_doc_stage(production_id, stage, resume)

    async with async_session() as db:
        prod = await db.get(Production, production_id)
        status = dict((prod.ai_pipeline_status if prod is not None else None) or {})
    marks = status.get(NEXT_ATTEMPT_KEY) or {}
    for stage in BATCHED_STAGES:
        state = status.get(stage)
        if state not in ("done", "failed"):
            # Waiting on batches, or started by another run: whoever finishes
            # it continues the pipeline.
            logger.info("Pipeline for production %s waits on %s (%s)", production_id, stage,
                        state.get("status") if isinstance(state, dict) else state)
            return
        if resume and state == "failed" and marks.get(stage) == "sync":
            # A batch where nothing succeeded: as before parallel stages, the
            # pipeline does not continue past it on its own.
            logger.info("Pipeline resume for production %s stops: %s batch failed",
                        production_id, stage)
            return

    final = [s for s in FINAL_STAGES if s in pending]
    if not final:
        return
    claim = await _claim_final_phase(production_id, run_started)
    if claim is None:
        logger.info("Pipeline for production %s: final stages handled by another run", production_id)
        return
    try:
        for stage in final:
            await _run_stage(production_id, stage)
    finally:
        await _cas_stage(production_id, FINAL_PHASE_KEY, claim, {"done_at": _now_iso()})


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
