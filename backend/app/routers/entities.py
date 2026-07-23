"""Ontology read API: document entities, profiles, mentions, connections."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_accessible_production_ids
from app.models import (
    Document, Entity, EntityMention, EntityRelationship,
    EventParticipant, OntologyEvent, User,
)
from app.routers.auth import get_current_user
from app.schemas import (
    DocEntityOut, DocumentEntitiesOut, EntityConnectionOut, EntityConnectionsOut,
    EntityDocMentionOut, EntityDocumentMentionsOut, EntityListItemOut,
    EntityListPageOut, EntityMentionsPageOut, EntityProfileOut, MentionSpanOut,
    SharedEventOut,
)
from app.services.entity_profile import generate_entity_overview, is_overview_stale

router = APIRouter(prefix="/api", tags=["entities"])


async def _get_scoped_entity(db: AsyncSession, user: User, entity_id: UUID) -> Entity:
    accessible = await get_accessible_production_ids(db, user)
    entity = await db.get(Entity, entity_id)
    if entity is None or entity.production_id not in accessible:
        raise HTTPException(status_code=404, detail="Entity not found")
    return entity


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
