"""Ontology read API: document entities, profiles, mentions, connections."""

import re
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_accessible_production_ids, get_user_role_for_production
from app.models import (
    Document, Entity, EntityMention, EntityMerge, EntityMergeSuggestion,
    EntityRelationship, EventParticipant, OntologyEvent, User,
)
from app.routers.auth import get_current_user
from app.schemas import (
    ChipEntityOut, DocEntityOut, DocumentEntitiesOut, EntityConnectionOut, EntityConnectionsOut,
    EntityDocMentionOut, EntityDocumentMentionsOut, EntitiesSummaryOut, EntityListItemOut,
    EntityListPageOut, EntityMentionsPageOut, EntityProfileOut, EntityRenameOut, EntityRenameRequest,
    EventEditRequest, MentionSpanOut,
    MergeRequest, MergeResultOut, MergeSuggestionOut, SharedEventOut,
    TimelineEventOut, TimelinePageOut, TimelineParticipantOut,
    GraphNodeOut, GraphEdgeOut, GraphOut,
)
from app.services.audit import log_action
from app.services.entity_extraction import EVENT_TYPES, parse_event_date
from app.services.entity_merge import merge_entities, undo_merge
from app.services.entity_profile import generate_entity_overview, is_overview_stale
from app.services.entity_resolution import is_typo_variant, normalize_name

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


# The destructive full re-extraction path lives in ingest.py:
# POST /productions/{id}/extract-entities?rebuild=true (the old
# /reset-entities endpoint here was consolidated into it).


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


async def _require_writer(db: AsyncSession, user: User, production_id: int) -> None:
    role = await get_user_role_for_production(db, user, production_id)
    if role == "readonly":
        raise HTTPException(status_code=403, detail="Read-only access")


@router.patch("/entities/{entity_id}", response_model=EntityRenameOut)
async def rename_entity(
    entity_id: UUID,
    body: EntityRenameRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Correct an AI-extracted entity's display name. Needed because a
    misspelling in the source (OCR'd/hand-keyed discovery) can run through
    every document, so the correct spelling may never appear as a merge
    candidate -- merging duplicates can't fix a display name that's wrong
    everywhere. Any writer role (not readonly), scoped to an accessible
    production (404 otherwise, same as timeline event edits). The previous
    canonical_name is preserved as an alias so it stays searchable/matchable
    instead of being silently lost."""
    entity = await _get_scoped_entity(db, user, entity_id)
    await _require_writer(db, user, entity.production_id)

    new_name = body.canonical_name.strip()
    if not new_name:
        raise HTTPException(status_code=422, detail="canonical_name cannot be empty")
    if len(new_name) > 500:
        # canonical_name is String(500); silently truncating a name is worse
        # than refusing it in an e-discovery tool -- reject instead of
        # quietly mangling it, same posture as the empty-name case above.
        raise HTTPException(status_code=422, detail="canonical_name exceeds 500 characters")

    old_name = entity.canonical_name
    aliases = list(entity.aliases or [])
    # Drop any existing alias equal to the new canonical name -- otherwise
    # renaming into an already-known alias (e.g. "Jorge Rivera" -> "J. Rivera"
    # when "J. Rivera" is already aliased) leaves the new canonical name
    # redundantly listed as its own alias.
    aliases = [a for a in aliases if a != new_name]
    if old_name != new_name and old_name not in aliases:
        aliases = aliases + [old_name]
    # Reassign (not in-place mutate) so SQLAlchemy detects the JSONB change --
    # mutating the existing list in place is invisible to the ORM's change
    # tracking and silently fails to persist (this exact trap bit us before,
    # see entity_merge.py's alias handling for the same pattern).
    entity.aliases = aliases
    entity.canonical_name = new_name

    await log_action(db, user, "entity_renamed", "entity", str(entity_id),
                     production_id=entity.production_id,
                     details={"old_name": old_name, "new_name": new_name})
    await db.commit()
    return EntityRenameOut(id=entity.id, canonical_name=entity.canonical_name,
                           aliases=list(entity.aliases or []))


@router.delete("/entities/{entity_id}")
async def delete_entity(
    entity_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Delete a junk or spurious AI-extracted entity (e.g. "Local User",
    litigation-process actors) that merging can't remove since there's no
    duplicate to merge into. Any writer role (not readonly), scoped to an
    accessible production (404 otherwise).

    entity_mentions, entity_relationships (source + target), event_participants,
    and entity_merge_suggestions (entity_a + entity_b) all cascade at the DB
    level via ondelete=CASCADE on their entity FK, so a plain delete leaves no
    orphans; entity_merges.winner_entity_id is SET NULL (not CASCADE) so merge
    history/undo trail survives. ontology_events themselves are NOT deleted --
    they cascade from productions, not entities, so this only removes the
    entity's participation in an event, never the event.

    The entity is hard-deleted, so the audit row's snapshot is the only
    surviving record of what was removed (mirrors delete_event)."""
    entity = await _get_scoped_entity(db, user, entity_id)
    await _require_writer(db, user, entity.production_id)
    production_id = entity.production_id
    snapshot = {
        "canonical_name": entity.canonical_name,
        "entity_type": entity.entity_type,
        "mention_count": entity.mention_count,
        "aliases": list(entity.aliases or [])[:50],
    }

    await db.delete(entity)
    await log_action(db, user, "entity_deleted", "entity", str(entity_id),
                     production_id=production_id, details=snapshot)
    await db.commit()
    return {"ok": True}


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


@router.post("/productions/{production_id}/merge-suggestions/auto-resolve-typos")
async def auto_resolve_typo_suggestions(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Retroactive cleanup: auto-merge pending suggestions whose pair is a
    safe typo variant (see is_typo_variant). Same write-access gate as
    accept/reject/manual-merge (_require_writer) — this is not a more
    dangerous action than a manual merge, so it should not require a higher
    role; a reviewer who can merge by hand should not 403 on this button.
    Bad pairs (missing entity, cross-type, merge_entities ValueError) are
    skipped, not raised — one broken suggestion must not block the rest of
    the batch."""
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=404, detail="Production not found")
    await _require_writer(db, user, production_id)

    rows = (await db.execute(
        select(EntityMergeSuggestion)
        .where(EntityMergeSuggestion.production_id == production_id,
               EntityMergeSuggestion.status == "pending")
    )).scalars().all()

    merged = 0
    consumed: set = set()  # entity ids already merged away (deleted) this run
    for sugg in rows:
        # `consumed` guards this loop against re-targeting an entity this run
        # already deleted. It is belt-and-braces: merge_entities' own cleanup
        # pass (below) also marks any other pending suggestion touching the
        # loser as "rejected" as soon as that merge lands, so an overlapping
        # suggestion (e.g. B~C when A~B just consumed B) disappears from the
        # queue either way — just skipped here vs. rejected there.
        if sugg.entity_a_id in consumed or sugg.entity_b_id in consumed:
            continue
        a = await db.get(Entity, sugg.entity_a_id)
        b = await db.get(Entity, sugg.entity_b_id)
        if a is None or b is None or a.entity_type != b.entity_type:
            continue
        if not is_typo_variant(normalize_name(a.canonical_name), normalize_name(b.canonical_name)):
            continue
        winner, loser = (a, b) if (a.mention_count or 0) >= (b.mention_count or 0) else (b, a)
        try:
            await merge_entities(db, winner, loser, user.id)
        except ValueError:
            continue
        consumed.add(loser.id)
        await db.flush()  # make the delete visible so a later db.get returns None too (belt + braces)
        merged += 1

    if merged:
        await db.flush()
        await log_action(db, user, "entity_merge_auto_resolved_typos", "production", str(production_id),
                         production_id=production_id, details={"merged": merged})
    await db.commit()
    return {"merged": merged}


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


def _normalize_human_event_date(raw: str) -> str:
    """Loosen a human-entered event_date before handing it to the extractor's
    strict parse_event_date. Human date editors commonly send a full ISO
    datetime (e.g. "2021-06-15T00:00:00Z" from Date.toISOString()) or a
    non-padded "2021-6-5" -- both are valid corrections but neither
    fullmatches parse_event_date's YYYY[-MM[-DD]] pattern. Strip any
    time/zone component and zero-pad month/day so the value reaches
    parse_event_date as a clean YYYY[-MM[-DD]] string. This does NOT touch
    parse_event_date itself, which stays strict for LLM-extracted dates
    (T2's guarantee) -- it's only applied to the human-facing PATCH path.
    """
    raw = raw.strip()
    date_part = re.split(r"[T ]", raw, maxsplit=1)[0]
    parts = date_part.split("-")
    padded = [parts[0]] + [p.zfill(2) for p in parts[1:]]
    return "-".join(padded)


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
    """Correct or clear an event's date/precision. Any writer role (not
    readonly), scoped to an accessible production (404 otherwise),
    audit-logged."""
    event = await _get_scoped_event(db, user, event_id)
    await _require_writer(db, user, event.production_id)

    if body.date_precision is not None and body.date_precision not in _DATE_PRECISIONS:
        raise HTTPException(status_code=422, detail="Invalid date_precision")

    date_provided = "event_date" in body.model_fields_set
    if date_provided:
        if body.event_date is None:
            # Explicit null clears both date and precision.
            event.event_date = None
            event.date_precision = "unknown"
        else:
            normalized = _normalize_human_event_date(body.event_date)
            parsed_date, parsed_precision = parse_event_date(normalized)
            if parsed_date is None:
                raise HTTPException(
                    status_code=422,
                    detail="event_date must be YYYY, YYYY-MM, or YYYY-MM-DD (optionally with a time component)",
                )
            event.event_date = parsed_date
            # Derive precision from the parsed date shape and ignore any
            # explicitly-passed date_precision here -- an explicit value can
            # otherwise contradict the date itself (e.g. {"event_date":
            # "2021-06", "date_precision": "day"} would claim a day that was
            # never given; {"event_date": "2021-06-15", "date_precision":
            # "unknown"} would discard real precision). Deriving is simpler
            # and friendlier than validating consistency and 422ing.
            event.date_precision = parsed_precision
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
    """Delete a spurious event (event_participants cascade via FK). Any writer
    role (not readonly), scoped to an accessible production (404 otherwise),
    audit-logged. The event is hard-deleted, so the audit row's details
    snapshot is the only surviving record of what was removed."""
    event = await _get_scoped_event(db, user, event_id)
    await _require_writer(db, user, event.production_id)
    production_id = event.production_id
    snapshot = {
        "event_type": event.event_type,
        "description": (event.description or "")[:200],
        "event_date": event.event_date.isoformat() if event.event_date else None,
        "date_precision": event.date_precision,
        "document_id": str(event.document_id),
        "significance": event.significance,
    }

    await db.delete(event)
    await log_action(db, user, "event_deleted", "ontology_event", str(event_id),
                     production_id=production_id, details=snapshot)
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
