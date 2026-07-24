"""Ontology read API: document entities, profiles, mentions, connections."""

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import ROLE_RANK, get_accessible_production_ids, get_user_role_for_production
from app.models import (
    Document, Entity, EntityMention, EntityMerge, EntityMergeSuggestion,
    EntityRelationship, EventParticipant, OntologyEvent, User,
)
from app.routers.auth import get_current_user
from app.schemas import (
    ChipEntityOut, DocEntityOut, DocumentEntitiesOut, EntityConnectionOut, EntityConnectionsOut,
    EntityDocMentionOut, EntityDocumentMentionsOut, EntitiesSummaryOut, EntityListItemOut,
    EntityListPageOut, EntityMentionsPageOut, EntityProfileOut, EventEditRequest, MentionSpanOut,
    MergeRequest, MergeResultOut, MergeSuggestionOut, SharedEventOut,
    TimelineEventOut, TimelinePageOut, TimelineParticipantOut,
    GraphNodeOut, GraphEdgeOut, GraphOut,
)
from app.services.audit import log_action
from app.services.entity_extraction import EVENT_TYPES, parse_event_date
from app.services.entity_merge import merge_entities, undo_merge
from app.services.entity_profile import generate_entity_overview, is_overview_stale

router = APIRouter(prefix="/api", tags=["entities"])


def _clamp_per_page(v: int) -> int:
    """Clamp per_page parameter to valid range [1, 100]."""
    return max(1, min(100, v))


async def _get_scoped_entity(db: AsyncSession, user: User, entity_id: UUID) -> Entity:
    accessible = await get_accessible_production_ids(db, user)
    entity = await db.get(Entity, entity_id)
    if entity is None or entity.production_id not in accessible:
        raise HTTPException(status_code=404, detail="Entity not found")
    return entity


async def _get_scoped_event(db: AsyncSession, user: User, event_id: int) -> OntologyEvent:
    """Load an event and 404 if it isn't in a production the user can access.
    Mirrors _get_scoped_entity so out-of-scope events never leak."""
    accessible = await get_accessible_production_ids(db, user)
    event = await db.get(OntologyEvent, event_id)
    if event is None or event.production_id not in accessible:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


async def _require_manager(db: AsyncSession, user: User, production_id: int) -> None:
    role = await get_user_role_for_production(db, user, production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or admin role required")


# Route note: Starlette matches routes in REGISTRATION order (first match wins),
# and matching happens PER ROUTER, not globally by specificity. documents.py
# defines GET /api/documents/{doc_id}, and documents.router is included in
# main.py before entities.router. That means ANY literal path under
# /api/documents/* declared in a DIFFERENT router (like this one used to be:
# /api/documents/entities-summary) is unreachable -- the {doc_id} route in
# documents.py matches first and "entities-summary" gets captured as a doc_id,
# producing a 422 instead of ever reaching this handler. Lesson: routers don't
# get to carve out sub-namespaces of a path prefix another router already owns
# with a catch-all segment; either own the whole prefix or pick a namespace
# no other router has claimed. Hence this lives at /api/entities-summary.
@router.get("/entities-summary", response_model=EntitiesSummaryOut)
async def get_entities_summary(
    ids: str = "",
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Batch: top-3 entities per document for row chips. Scoped; bad ids skipped."""
    raw = [s.strip() for s in ids.split(",") if s.strip()]
    if len(raw) > 100:
        raise HTTPException(status_code=422, detail="Too many ids (max 100)")
    doc_ids = []
    for s in raw:
        try:
            doc_ids.append(UUID(s))
        except ValueError:
            continue
    if not doc_ids:
        return EntitiesSummaryOut(summaries={})

    accessible = await get_accessible_production_ids(db, user)
    rows = (await db.execute(
        select(EntityMention.document_id, Entity,
               func.count(EntityMention.id).label("cnt"))
        .join(Entity, EntityMention.entity_id == Entity.id)
        .where(EntityMention.document_id.in_(doc_ids),
               EntityMention.production_id.in_(accessible))
        .group_by(EntityMention.document_id, Entity.id)
        .order_by(EntityMention.document_id, func.count(EntityMention.id).desc())
    )).all()

    summaries: dict[str, list[ChipEntityOut]] = {}
    for doc_id, ent, _cnt in rows:
        bucket = summaries.setdefault(str(doc_id), [])
        if len(bucket) < 3:
            bucket.append(ChipEntityOut(entity_id=ent.id, canonical_name=ent.canonical_name,
                                        entity_type=ent.entity_type))
    return EntitiesSummaryOut(summaries=summaries)


@router.get("/documents/{doc_id}/entities", response_model=DocumentEntitiesOut)
async def get_document_entities(
    doc_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    doc = await db.get(Document, doc_id)
    if not doc or doc.production_id not in accessible:
        raise HTTPException(status_code=404, detail="Document not found")

    rows = (await db.execute(
        select(EntityMention, Entity)
        .join(Entity, EntityMention.entity_id == Entity.id)
        .where(EntityMention.document_id == doc_id)
        .order_by(EntityMention.start_offset)
        # Pathological-document guard: normal documents have far fewer than
        # 2000 mentions; this just prevents a runaway result set.
        .limit(2000)
    )).all()

    by_entity: dict = {}
    for mention, entity in rows:
        item = by_entity.setdefault(entity.id, DocEntityOut(
            id=entity.id, entity_type=entity.entity_type,
            canonical_name=entity.canonical_name,
            mention_count=entity.mention_count, mentions=[],
        ))
        item.mentions.append(MentionSpanOut(
            surface_text=mention.surface_text,
            start_offset=mention.start_offset, end_offset=mention.end_offset,
        ))
    return DocumentEntitiesOut(entities=list(by_entity.values()))


@router.get("/entities/{entity_id}", response_model=EntityProfileOut)
async def get_entity(
    entity_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    entity = await _get_scoped_entity(db, user, entity_id)

    doc_count = (await db.execute(
        select(func.count(func.distinct(EntityMention.document_id)))
        .where(EntityMention.entity_id == entity_id)
    )).scalar() or 0

    overview = entity.overview
    if is_overview_stale(entity):
        generated = await generate_entity_overview(db, entity)
        if generated is not None:
            overview = generated
            await db.commit()

    return EntityProfileOut(
        id=entity.id, production_id=entity.production_id,
        entity_type=entity.entity_type, canonical_name=entity.canonical_name,
        aliases=list(entity.aliases or []), attributes=dict(entity.attributes or {}),
        overview=overview, mention_count=entity.mention_count, document_count=doc_count,
    )


@router.get("/entities/{entity_id}/mentions", response_model=EntityMentionsPageOut)
async def get_entity_mentions(
    entity_id: UUID,
    page: int = 1,
    per_page: int = 20,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _get_scoped_entity(db, user, entity_id)
    per_page = max(1, min(50, per_page))

    total = (await db.execute(
        select(func.count(func.distinct(EntityMention.document_id)))
        .where(EntityMention.entity_id == entity_id)
    )).scalar() or 0

    doc_ids = (await db.execute(
        select(EntityMention.document_id, Document.bates_begin)
        .join(Document, EntityMention.document_id == Document.id)
        .where(EntityMention.entity_id == entity_id)
        .group_by(EntityMention.document_id, Document.bates_begin)
        .order_by(Document.bates_begin)
        .offset((max(1, page) - 1) * per_page)
        .limit(per_page)
    )).all()

    documents = []
    for document_id, _bates in doc_ids:
        doc = await db.get(Document, document_id)
        mention_rows = (await db.execute(
            select(EntityMention)
            .where(EntityMention.entity_id == entity_id, EntityMention.document_id == document_id)
            .order_by(EntityMention.start_offset)
            .limit(20)
        )).scalars().all()
        documents.append(EntityDocumentMentionsOut(
            document_id=document_id, bates_begin=doc.bates_begin, title=doc.title,
            mentions=[EntityDocMentionOut(surface_text=m.surface_text,
                                          context_snippet=m.context_snippet,
                                          start_offset=m.start_offset) for m in mention_rows],
        ))
    return EntityMentionsPageOut(documents=documents, total=total)


@router.get("/entities/{entity_id}/connections", response_model=EntityConnectionsOut)
async def get_entity_connections(
    entity_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _get_scoped_entity(db, user, entity_id)

    stated = []
    for direction_col, other_col in (
        (EntityRelationship.source_entity_id, EntityRelationship.target_entity_id),
        (EntityRelationship.target_entity_id, EntityRelationship.source_entity_id),
    ):
        rows = (await db.execute(
            select(EntityRelationship, Entity)
            .join(Entity, Entity.id == other_col)
            .where(direction_col == entity_id)
            .limit(50)
        )).all()
        for rel, other in rows:
            stated.append(EntityConnectionOut(
                entity_id=other.id, canonical_name=other.canonical_name,
                entity_type=other.entity_type, relationship_type=rel.relationship_type,
                description=rel.description, document_id=rel.document_id,
            ))

    # Same-production join safety: this joins mentions on document_id alone
    # (no explicit production_id filter) because persist_extraction always
    # stamps every ontology row's production_id from its source document,
    # and resolution only ever matches entities within a single production.
    # That invariant guarantees a shared document_id can't cross tenants.
    em_self = EntityMention.__table__.alias("em_self")
    em_other = EntityMention.__table__.alias("em_other")
    cooc_rows = (await db.execute(
        select(em_other.c.entity_id, func.count(func.distinct(em_other.c.document_id)).label("shared"))
        .select_from(em_self.join(em_other, em_self.c.document_id == em_other.c.document_id))
        .where(em_self.c.entity_id == entity_id, em_other.c.entity_id != entity_id)
        .group_by(em_other.c.entity_id)
        .order_by(func.count(func.distinct(em_other.c.document_id)).desc())
        .limit(10)
    )).all()
    cooccurrence = []
    for other_id, shared in cooc_rows:
        other = await db.get(Entity, other_id)
        if other is not None:
            cooccurrence.append(EntityConnectionOut(
                entity_id=other.id, canonical_name=other.canonical_name,
                entity_type=other.entity_type, shared_doc_count=shared,
            ))

    event_rows = (await db.execute(
        select(OntologyEvent)
        .join(EventParticipant, EventParticipant.event_id == OntologyEvent.id)
        .where(EventParticipant.entity_id == entity_id)
        .order_by(OntologyEvent.event_date.desc().nullslast())
        .limit(20)
    )).scalars().all()
    shared_events = [SharedEventOut(
        event_id=e.id, description=e.description, event_type=e.event_type,
        event_date=e.event_date.isoformat() if e.event_date else None,
        document_id=e.document_id,
    ) for e in event_rows]

    return EntityConnectionsOut(stated=stated, cooccurrence=cooccurrence, shared_events=shared_events)


@router.get("/productions/{production_id}/entities", response_model=EntityListPageOut)
async def list_production_entities(
    production_id: int,
    search: str | None = None,
    entity_type: str | None = None,
    page: int = 1,
    per_page: int = 50,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=404, detail="Production not found")
    per_page = max(1, min(100, per_page))

    query = select(Entity).where(Entity.production_id == production_id)
    count_query = select(func.count(Entity.id)).where(Entity.production_id == production_id)
    if entity_type in ("person", "org"):
        query = query.where(Entity.entity_type == entity_type)
        count_query = count_query.where(Entity.entity_type == entity_type)
    if search:
        pattern = f"%{search}%"
        query = query.where(Entity.canonical_name.ilike(pattern))
        count_query = count_query.where(Entity.canonical_name.ilike(pattern))

    total = (await db.execute(count_query)).scalar() or 0
    rows = (await db.execute(
        query.order_by(Entity.mention_count.desc(), Entity.id)
        .offset((max(1, page) - 1) * per_page).limit(per_page)
    )).scalars().all()

    out = []
    for e in rows:
        doc_count = (await db.execute(
            select(func.count(func.distinct(EntityMention.document_id)))
            .where(EntityMention.entity_id == e.id)
        )).scalar() or 0
        out.append(EntityListItemOut(id=e.id, entity_type=e.entity_type,
                                     canonical_name=e.canonical_name,
                                     mention_count=e.mention_count, document_count=doc_count))
    return EntityListPageOut(entities=out, total=total)


@router.post("/productions/{production_id}/reset-entities")
async def reset_production_entities(
    production_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """DESTRUCTIVE: discard all extracted entities (and their mentions, events,
    participants, relationships, and merge suggestions via FK cascade) plus any
    confirmed merges for this production, clear each document's
    entities_extracted_at, then re-enqueue the extraction pipeline to repopulate
    with the improved extractor. Manager or admin only."""
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=404, detail="Production not found")
    role = await get_user_role_for_production(db, user, production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or admin role required")

    # Delete all entities for this production. The ondelete=CASCADE FKs on
    # entity_mentions, ontology_events (via participants), event_participants,
    # entity_relationships, and entity_merge_suggestions remove dependents.
    # ontology_events also cascades from productions directly, but is emptied
    # here transitively via its participants; delete it explicitly too so events
    # with no participants are cleared.
    await db.execute(delete(EventParticipant).where(
        EventParticipant.event_id.in_(
            select(OntologyEvent.id).where(OntologyEvent.production_id == production_id))))
    await db.execute(delete(OntologyEvent).where(OntologyEvent.production_id == production_id))
    # entity_merges references entities with SET NULL (preserves audit rows) —
    # not deleted here; that's acceptable per spec.
    await db.execute(delete(Entity).where(Entity.production_id == production_id))

    # Clear the per-document extraction watermark so the pipeline reprocesses.
    await db.execute(
        update(Document).where(Document.production_id == production_id)
        .values(entities_extracted_at=None))

    from app.services import tasks as task_service
    mode = "enqueued" if task_service.is_configured() else "background"

    await log_action(db, user, "entities_reset", "production", str(production_id),
                     production_id=production_id, details={"mode": mode})
    await db.commit()

    # Enqueue AFTER commit so the cleared entities_extracted_at watermark is
    # durably visible before the re-extraction worker starts; otherwise the
    # worker can read stale state and skip documents that look already-extracted.
    if mode == "enqueued":
        task_service.enqueue_pipeline(production_id)
    else:
        from app.services.pipeline import run_ambient_pipeline
        background_tasks.add_task(run_ambient_pipeline, production_id)
    return {"reset": True}


async def _require_writer(db: AsyncSession, user: User, production_id: int) -> None:
    role = await get_user_role_for_production(db, user, production_id)
    if role == "readonly":
        raise HTTPException(status_code=403, detail="Read-only access")


async def _entity_list_item(db: AsyncSession, entity_id) -> EntityListItemOut | None:
    e = await db.get(Entity, entity_id)
    if e is None:
        return None
    doc_count = (await db.execute(
        select(func.count(func.distinct(EntityMention.document_id)))
        .where(EntityMention.entity_id == e.id)
    )).scalar() or 0
    return EntityListItemOut(id=e.id, entity_type=e.entity_type,
                             canonical_name=e.canonical_name,
                             mention_count=e.mention_count, document_count=doc_count)


@router.get("/productions/{production_id}/merge-suggestions", response_model=list[MergeSuggestionOut])
async def list_merge_suggestions(
    production_id: int,
    status: str = "pending",
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=404, detail="Production not found")
    rows = (await db.execute(
        select(EntityMergeSuggestion)
        .where(EntityMergeSuggestion.production_id == production_id,
               EntityMergeSuggestion.status == status)
        .order_by(EntityMergeSuggestion.score.desc())
        .limit(100)
    )).scalars().all()
    out = []
    for s in rows:
        a = await _entity_list_item(db, s.entity_a_id)
        b = await _entity_list_item(db, s.entity_b_id)
        if a and b:
            out.append(MergeSuggestionOut(id=s.id, score=s.score, rationale=s.rationale,
                                          status=s.status, entity_a=a, entity_b=b))
    return out


@router.post("/merge-suggestions/{suggestion_id}/accept", response_model=MergeResultOut)
async def accept_merge_suggestion(
    suggestion_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    sugg = await db.get(EntityMergeSuggestion, suggestion_id)
    accessible = await get_accessible_production_ids(db, user)
    if sugg is None or sugg.production_id not in accessible:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    await _require_writer(db, user, sugg.production_id)

    # Atomically claim the suggestion: two concurrent accepts (or an accept
    # racing a reject) on the same pending row must not both proceed.
    claimed = await db.execute(
        update(EntityMergeSuggestion)
        .where(EntityMergeSuggestion.id == suggestion_id, EntityMergeSuggestion.status == "pending")
        .values(status="accepted", resolved_by=user.id, resolved_at=func.now())
    )
    if claimed.rowcount == 0:
        raise HTTPException(status_code=409, detail="Suggestion already resolved")

    a = await db.get(Entity, sugg.entity_a_id)
    b = await db.get(Entity, sugg.entity_b_id)
    if a is None or b is None:
        raise HTTPException(status_code=409, detail="An entity in this pair no longer exists")
    # keep the one with more mentions as the winner
    winner, loser = (a, b) if (a.mention_count or 0) >= (b.mention_count or 0) else (b, a)
    try:
        merge = await merge_entities(db, winner, loser, user.id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    await db.flush()
    await log_action(db, user, "entity_merged", "entity", str(winner.id),
                     production_id=sugg.production_id,
                     details={"suggestion_id": suggestion_id, "loser": str(loser.id)})
    await db.commit()
    return MergeResultOut(merge_id=merge.id, winner_id=winner.id)


@router.post("/merge-suggestions/{suggestion_id}/reject")
async def reject_merge_suggestion(
    suggestion_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    sugg = await db.get(EntityMergeSuggestion, suggestion_id)
    accessible = await get_accessible_production_ids(db, user)
    if sugg is None or sugg.production_id not in accessible:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    await _require_writer(db, user, sugg.production_id)

    # Atomically claim the suggestion: see accept_merge_suggestion.
    claimed = await db.execute(
        update(EntityMergeSuggestion)
        .where(EntityMergeSuggestion.id == suggestion_id, EntityMergeSuggestion.status == "pending")
        .values(status="rejected", resolved_by=user.id, resolved_at=func.now())
    )
    if claimed.rowcount == 0:
        raise HTTPException(status_code=409, detail="Suggestion already resolved")

    await log_action(db, user, "entity_merge_suggestion_rejected", "entity_merge_suggestion",
                     str(suggestion_id), production_id=sugg.production_id, details={})
    await db.commit()
    return {"ok": True}


@router.post("/entities/merge", response_model=MergeResultOut)
async def manual_merge(
    body: MergeRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    winner = await _get_scoped_entity(db, user, body.winner_id)
    loser = await _get_scoped_entity(db, user, body.loser_id)
    await _require_writer(db, user, winner.production_id)
    try:
        merge = await merge_entities(db, winner, loser, user.id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    await db.flush()
    await log_action(db, user, "entity_merged", "entity", str(winner.id),
                     production_id=winner.production_id, details={"loser": str(loser.id), "manual": True})
    await db.commit()
    return MergeResultOut(merge_id=merge.id, winner_id=winner.id)


@router.post("/entity-merges/{merge_id}/undo")
async def undo_entity_merge(
    merge_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    merge = await db.get(EntityMerge, merge_id)
    accessible = await get_accessible_production_ids(db, user)
    if merge is None or merge.production_id not in accessible:
        raise HTTPException(status_code=404, detail="Merge not found")
    await _require_writer(db, user, merge.production_id)
    try:
        restored = await undo_merge(db, merge)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    await log_action(db, user, "entity_merge_undone", "entity", str(restored.id),
                     production_id=merge.production_id, details={"merge_id": merge_id})
    await db.commit()
    return {"ok": True, "restored_entity_id": str(restored.id)}


@router.get("/productions/{production_id}/timeline", response_model=TimelinePageOut)
async def get_production_timeline(
    production_id: int,
    entity_id: UUID | None = None,
    event_type: str | None = None,
    min_significance: int = 3,
    page: int = 1,
    per_page: int = 50,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=404, detail="Production not found")
    per_page = _clamp_per_page(per_page)

    base = select(OntologyEvent).where(OntologyEvent.production_id == production_id)
    count_base = select(func.count(OntologyEvent.id)).where(OntologyEvent.production_id == production_id)
    # Significance filter: treat null (legacy/unrated) as 3 via COALESCE so
    # those rows still surface at the default. min_significance=1 lets all
    # events through (coalesced significance is always >= 1).
    if min_significance > 1:
        sig_filter = func.coalesce(OntologyEvent.significance, 3) >= min_significance
        base = base.where(sig_filter)
        count_base = count_base.where(sig_filter)
    if event_type in EVENT_TYPES:
        base = base.where(OntologyEvent.event_type == event_type)
        count_base = count_base.where(OntologyEvent.event_type == event_type)
    if entity_id is not None:
        base = base.join(EventParticipant, EventParticipant.event_id == OntologyEvent.id).where(
            EventParticipant.entity_id == entity_id)
        count_base = count_base.join(EventParticipant, EventParticipant.event_id == OntologyEvent.id).where(
            EventParticipant.entity_id == entity_id)

    total = (await db.execute(count_base)).scalar() or 0
    undated_count = (await db.execute(
        count_base.where(OntologyEvent.event_date.is_(None)))).scalar() or 0

    rows = (await db.execute(
        base.add_columns(Document.bates_begin, Document.title)
        .join(Document, OntologyEvent.document_id == Document.id)
        .order_by(OntologyEvent.event_date.asc().nullslast(), OntologyEvent.id)
        .offset((max(1, page) - 1) * per_page)
        .limit(per_page)
    )).all()

    event_ids = [ev.id for ev, _b, _t in rows]
    participants_by_event: dict[int, list[TimelineParticipantOut]] = {}
    if event_ids:
        prows = (await db.execute(
            select(EventParticipant.event_id, Entity)
            .join(Entity, EventParticipant.entity_id == Entity.id)
            .where(EventParticipant.event_id.in_(event_ids))
        )).all()
        for eid, ent in prows:
            participants_by_event.setdefault(eid, []).append(TimelineParticipantOut(
                entity_id=ent.id, canonical_name=ent.canonical_name, entity_type=ent.entity_type))

    return TimelinePageOut(
        events=[TimelineEventOut(
            event_id=ev.id, description=ev.description, event_type=ev.event_type,
            event_date=ev.event_date.isoformat() if ev.event_date else None,
            date_precision=ev.date_precision,
            # Null significance (legacy/unrated) presents as the default 3,
            # matching the COALESCE used in the filter above.
            significance=ev.significance if ev.significance is not None else 3,
            date_source_text=ev.date_source_text,
            document_id=ev.document_id,
            bates_begin=bates, title=title,
            participants=participants_by_event.get(ev.id, []),
        ) for ev, bates, title in rows],
        total=total, undated_count=undated_count,
    )


_DATE_PRECISIONS = {"day", "month", "year", "unknown"}


def _event_out(ev: OntologyEvent) -> dict:
    return {
        "event_id": ev.id,
        "event_type": ev.event_type,
        "description": ev.description,
        "event_date": ev.event_date.isoformat() if ev.event_date else None,
        "date_precision": ev.date_precision,
        "significance": ev.significance if ev.significance is not None else 3,
        "date_source_text": ev.date_source_text,
        "document_id": str(ev.document_id),
    }


@router.patch("/events/{event_id}")
async def edit_event(
    event_id: int,
    body: EventEditRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Correct or clear an event's date/precision. Manager+ only, scoped to an
    accessible production (404 otherwise), audit-logged."""
    event = await _get_scoped_event(db, user, event_id)
    await _require_manager(db, user, event.production_id)

    if body.date_precision is not None and body.date_precision not in _DATE_PRECISIONS:
        raise HTTPException(status_code=422, detail="Invalid date_precision")

    date_provided = "event_date" in body.model_fields_set
    if date_provided:
        if body.event_date is None:
            # Explicit null clears both date and precision.
            event.event_date = None
            event.date_precision = "unknown"
        else:
            parsed_date, parsed_precision = parse_event_date(body.event_date)
            if parsed_date is None:
                raise HTTPException(status_code=422, detail="Unparseable event_date (year required)")
            event.event_date = parsed_date
            # Explicit precision wins over the one derived from the date shape.
            event.date_precision = body.date_precision or parsed_precision
    elif body.date_precision is not None:
        event.date_precision = body.date_precision

    await log_action(db, user, "event_edited", "ontology_event", str(event_id),
                     production_id=event.production_id,
                     details={"event_date": event.event_date.isoformat() if event.event_date else None,
                              "date_precision": event.date_precision})
    await db.commit()
    return _event_out(event)


@router.delete("/events/{event_id}")
async def delete_event(
    event_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Delete a spurious event (event_participants cascade via FK). Manager+
    only, scoped to an accessible production (404 otherwise), audit-logged."""
    event = await _get_scoped_event(db, user, event_id)
    await _require_manager(db, user, event.production_id)
    production_id = event.production_id

    await db.delete(event)
    await log_action(db, user, "event_deleted", "ontology_event", str(event_id),
                     production_id=production_id, details={})
    await db.commit()
    return {"ok": True}


@router.get("/productions/{production_id}/graph", response_model=GraphOut)
async def get_production_graph(
    production_id: int,
    max_nodes: int = 75,
    min_shared_docs: int = 2,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=404, detail="Production not found")
    max_nodes = max(1, min(150, max_nodes))
    min_shared_docs = max(1, min_shared_docs)

    total_entities = (await db.execute(
        select(func.count(Entity.id)).where(Entity.production_id == production_id)
    )).scalar() or 0

    node_rows = (await db.execute(
        select(Entity).where(Entity.production_id == production_id)
        .order_by(Entity.mention_count.desc(), Entity.id).limit(max_nodes)
    )).scalars().all()
    node_ids = {e.id for e in node_rows}
    nodes = [GraphNodeOut(id=e.id, canonical_name=e.canonical_name,
                          entity_type=e.entity_type, mention_count=e.mention_count)
             for e in node_rows]

    edges: list[GraphEdgeOut] = []
    stated_pairs: set[frozenset] = set()
    if node_ids:
        srows = (await db.execute(
            select(EntityRelationship.source_entity_id, EntityRelationship.target_entity_id,
                   EntityRelationship.relationship_type,
                   func.count(EntityRelationship.id).label("weight"))
            .where(EntityRelationship.source_entity_id.in_(node_ids),
                   EntityRelationship.target_entity_id.in_(node_ids))
            .group_by(EntityRelationship.source_entity_id, EntityRelationship.target_entity_id,
                      EntityRelationship.relationship_type)
        )).all()
        for src, tgt, rtype, weight in srows:
            stated_pairs.add(frozenset((src, tgt)))
            edges.append(GraphEdgeOut(source=src, target=tgt, kind="stated",
                                      relationship_type=rtype, weight=weight))

        # Co-occurrence among included nodes; a < b ordering avoids duplicate pairs.
        # Same production-invariant note as get_entity_connections: rows for one
        # document always share its production_id (enforced at persist time).
        em_a = EntityMention.__table__.alias("em_a")
        em_b = EntityMention.__table__.alias("em_b")
        crows = (await db.execute(
            select(em_a.c.entity_id, em_b.c.entity_id,
                   func.count(func.distinct(em_a.c.document_id)).label("shared"))
            .select_from(em_a.join(em_b, em_a.c.document_id == em_b.c.document_id))
            .where(em_a.c.entity_id.in_(node_ids), em_b.c.entity_id.in_(node_ids),
                   em_a.c.entity_id < em_b.c.entity_id)
            .group_by(em_a.c.entity_id, em_b.c.entity_id)
            .having(func.count(func.distinct(em_a.c.document_id)) >= min_shared_docs)
        )).all()
        for a, b, shared in crows:
            if frozenset((a, b)) in stated_pairs:
                continue
            edges.append(GraphEdgeOut(source=a, target=b, kind="cooccurrence", weight=shared))

    return GraphOut(nodes=nodes, edges=edges, truncated=total_entities > max_nodes)
