"""Reversible entity merge: re-point provenance to the winner, snapshot the
loser, log everything needed for a mechanical undo."""

import uuid as _uuid

from sqlalchemy import select

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

    mentions = (await db.execute(
        select(EntityMention).where(EntityMention.entity_id == loser.id)
    )).scalars().all()
    for m in mentions:
        m.entity_id = winner.id
        merge.moved_mention_ids.append(m.id)

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
        merge.moved_relationship_ids.append(e.id)

    participants = (await db.execute(
        select(EventParticipant).where(EventParticipant.entity_id == loser.id)
    )).scalars().all()
    for p in participants:
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
    winner.mention_count = (winner.mention_count or 0) + (loser.mention_count or 0)

    await db.delete(loser)
    db.add(merge)
    return merge


async def undo_merge(db, merge: EntityMerge) -> Entity:
    if merge.undone:
        raise ValueError("Merge already undone")
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

    winner.aliases = merge.winner_prior["aliases"]
    winner.mention_count = merge.winner_prior["mention_count"]
    merge.undone = True
    return restored
