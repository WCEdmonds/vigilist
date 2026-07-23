"""Reversible entity merge: re-point provenance to the winner, snapshot the
loser, log everything needed for a mechanical undo."""

import uuid as _uuid

from sqlalchemy import func, select

from app.models import (
    Entity, EntityMention, EntityMerge, EntityMergeSuggestion,
    EntityRelationship, EventParticipant,
)


def _snapshot(entity: Entity) -> dict:
    return {
        "id": str(entity.id), "production_id": entity.production_id,
        "entity_type": entity.entity_type, "canonical_name": entity.canonical_name,
        "aliases": list(entity.aliases or []), "attributes": dict(entity.attributes or {}),
        "overview": entity.overview, "mention_count": entity.mention_count,
    }


async def merge_entities(db, winner: Entity, loser: Entity, user_id: str) -> EntityMerge:
    if winner.id == loser.id:
        raise ValueError("Cannot merge an entity into itself")
    if winner.production_id != loser.production_id:
        raise ValueError("Entities belong to different productions")
    if winner.entity_type != loser.entity_type:
        raise ValueError("Entities are of different types")

    merge = EntityMerge(
        production_id=winner.production_id, winner_entity_id=winner.id,
        loser_snapshot=_snapshot(loser),
        winner_prior={"aliases": list(winner.aliases or []), "mention_count": winner.mention_count},
        moved_mention_ids=[], moved_relationship_ids=[], moved_participant_ids=[],
        merged_by=user_id,
    )

    # Preload winner's existing (document_id, start_offset) mention keys so we
    # can detect collisions with uq_mention_doc_entity_offset before
    # re-pointing (NULL offsets never collide — Postgres treats NULLs as
    # distinct under a unique constraint).
    winner_mention_keys = {
        (doc_id, offset) for doc_id, offset in (await db.execute(
            select(EntityMention.document_id, EntityMention.start_offset)
            .where(EntityMention.entity_id == winner.id, EntityMention.start_offset.is_not(None))
        )).all()
    }

    mentions = (await db.execute(
        select(EntityMention).where(EntityMention.entity_id == loser.id)
    )).scalars().all()
    for m in mentions:
        if m.start_offset is not None and (m.document_id, m.start_offset) in winner_mention_keys:
            # Winner already has a mention at this exact (document, offset);
            # re-pointing would violate uq_mention_doc_entity_offset. Drop it
            # — not recorded as moved, so it is permanently lost on undo (same
            # treatment as a self-edge below).
            await db.delete(m)
            continue
        m.entity_id = winner.id
        merge.moved_mention_ids.append(m.id)

    # Preload winner's existing edge keys so we can detect collisions with
    # uq_edge_pair_type_doc before re-pointing.
    winner_edge_keys = {
        (source_id, target_id, rel_type, doc_id)
        for source_id, target_id, rel_type, doc_id in (await db.execute(
            select(
                EntityRelationship.source_entity_id,
                EntityRelationship.target_entity_id,
                EntityRelationship.relationship_type,
                EntityRelationship.document_id
            ).where(
                (EntityRelationship.source_entity_id == winner.id)
                | (EntityRelationship.target_entity_id == winner.id))
        )).all()
    }

    edges = (await db.execute(
        select(EntityRelationship).where(
            (EntityRelationship.source_entity_id == loser.id)
            | (EntityRelationship.target_entity_id == loser.id))
    )).scalars().all()
    for e in edges:
        if e.source_entity_id == loser.id:
            e.source_entity_id = winner.id
        if e.target_entity_id == loser.id:
            e.target_entity_id = winner.id
        if e.source_entity_id == e.target_entity_id:
            await db.delete(e)  # became a self-edge; drop (not restored on undo)
            continue
        if (e.source_entity_id, e.target_entity_id, e.relationship_type, e.document_id) in winner_edge_keys:
            # Winner already has an edge with this key; re-pointing would violate
            # uq_edge_pair_type_doc. Drop it — not recorded as moved, so it is
            # permanently lost on undo (same treatment as a self-edge above).
            await db.delete(e)
            continue
        merge.moved_relationship_ids.append(e.id)

    # Preload winner's existing event_ids so we can detect collisions with
    # uq_event_entity before re-pointing.
    winner_event_ids = {
        event_id for (event_id,) in (await db.execute(
            select(EventParticipant.event_id).where(EventParticipant.entity_id == winner.id)
        )).all()
    }

    participants = (await db.execute(
        select(EventParticipant).where(EventParticipant.entity_id == loser.id)
    )).scalars().all()
    for p in participants:
        if p.event_id in winner_event_ids:
            # Winner already participates in this event; re-pointing would
            # violate uq_event_entity. Drop it — not recorded as moved, so it
            # is permanently lost on undo (same treatment as a self-edge
            # above).
            await db.delete(p)
            continue
        p.entity_id = winner.id
        merge.moved_participant_ids.append(p.id)

    suggestions = (await db.execute(
        select(EntityMergeSuggestion).where(
            ((EntityMergeSuggestion.entity_a_id == loser.id) | (EntityMergeSuggestion.entity_b_id == loser.id))
            & (EntityMergeSuggestion.status == "pending"))
    )).scalars().all()
    for s in suggestions:
        s.status = "accepted" if {s.entity_a_id, s.entity_b_id} == {winner.id, loser.id} else "rejected"
        s.resolved_by = user_id

    new_aliases = [loser.canonical_name] + list(loser.aliases or [])
    winner.aliases = list(winner.aliases or []) + [a for a in new_aliases if a not in (winner.aliases or [])]
    # Only mentions actually moved count toward the winner — collided
    # duplicates were deleted above and must not inflate the total.
    winner.mention_count = (winner.mention_count or 0) + len(merge.moved_mention_ids)

    await db.delete(loser)
    db.add(merge)
    return merge


async def undo_merge(db, merge: EntityMerge) -> Entity:
    if merge.undone:
        raise ValueError("Merge already undone")
    if merge.winner_entity_id is None:
        # winner_entity_id is SET NULL on delete: the winner was itself later
        # merged into something else (chain merge) and no longer exists.
        raise ValueError("Winner entity no longer exists (merged again?) — cannot undo")
    winner = await db.get(Entity, merge.winner_entity_id)
    if winner is None:
        raise ValueError("Winner entity no longer exists (merged again?) — cannot undo")

    snap = merge.loser_snapshot
    restored = Entity(
        id=_uuid.UUID(snap["id"]), production_id=snap["production_id"],
        entity_type=snap["entity_type"], canonical_name=snap["canonical_name"],
        aliases=snap["aliases"], attributes=snap["attributes"],
        overview=snap["overview"], mention_count=snap["mention_count"],
    )
    db.add(restored)

    if merge.moved_mention_ids:
        for m in (await db.execute(
            select(EntityMention).where(EntityMention.id.in_(merge.moved_mention_ids))
        )).scalars().all():
            m.entity_id = restored.id
    if merge.moved_relationship_ids:
        for e in (await db.execute(
            select(EntityRelationship).where(EntityRelationship.id.in_(merge.moved_relationship_ids))
        )).scalars().all():
            # restore whichever side(s) pointed at the loser originally is not
            # recorded per-edge; winner-side occurrences of winner.id that came
            # from this merge are exactly the moved ids, so flip those back.
            if e.source_entity_id == winner.id:
                e.source_entity_id = restored.id
            elif e.target_entity_id == winner.id:
                e.target_entity_id = restored.id
    if merge.moved_participant_ids:
        for p in (await db.execute(
            select(EventParticipant).where(EventParticipant.id.in_(merge.moved_participant_ids))
        )).scalars().all():
            p.entity_id = restored.id

    # Make the re-points above visible before re-deriving counts from live
    # rows.
    await db.flush()

    # Re-derive counts from live rows rather than blindly restoring the
    # pre-merge snapshot: mentions ingested against the winner (or, after
    # re-pointing, the restored loser) since the merge would otherwise be
    # silently wiped out by an undo.
    winner.mention_count = (await db.execute(
        select(func.count()).select_from(EntityMention).where(EntityMention.entity_id == winner.id)
    )).scalar() or 0
    restored.mention_count = (await db.execute(
        select(func.count()).select_from(EntityMention).where(EntityMention.entity_id == restored.id)
    )).scalar() or 0

    # Restore winner's prior aliases, but keep any aliases the winner picked
    # up after the merge that didn't come from the loser (post-merge growth
    # that undo should not clobber).
    loser_derived = {snap["canonical_name"], *snap["aliases"]}
    winner.aliases = merge.winner_prior["aliases"] + [
        a for a in (winner.aliases or [])
        if a not in merge.winner_prior["aliases"] and a not in loser_derived
    ]

    merge.undone = True
    return restored
