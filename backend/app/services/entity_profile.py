"""Lazy, cached entity overview generation (Haiku)."""

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import settings

logger = logging.getLogger(__name__)

PROFILE_MODEL = "claude-haiku-4-5"
_STALE_RATIO = 1.5
_STALE_ABSOLUTE = 10
_MAX_SNIPPETS = 20


def is_overview_stale(entity) -> bool:
    """Spec rule: regenerate when there is no overview, or mention_count has
    reached 1.5x the count at generation time, or grown by >= 10 since."""
    if not entity.overview or entity.overview_mention_count is None:
        return True
    grown = (entity.mention_count or 0) - entity.overview_mention_count
    return (entity.mention_count or 0) >= _STALE_RATIO * entity.overview_mention_count or grown >= _STALE_ABSOLUTE


async def generate_entity_overview(db, entity) -> str | None:
    """Synthesize a short 'who is this' overview from mentions + edges.
    Returns None on failure — the caller renders the profile without one."""
    if not settings.anthropic_api_key:
        return None
    from app.models import Entity, EntityMention, EntityRelationship

    snippets = (await db.execute(
        select(EntityMention.context_snippet)
        .where(EntityMention.entity_id == entity.id, EntityMention.context_snippet.isnot(None))
        .limit(_MAX_SNIPPETS)
    )).scalars().all()

    edge_rows = (await db.execute(
        select(EntityRelationship, Entity.canonical_name)
        .join(Entity, Entity.id == EntityRelationship.target_entity_id)
        .where(EntityRelationship.source_entity_id == entity.id)
        .limit(10)
    )).all()
    edges = [f"{rel.relationship_type} -> {name}: {rel.description or ''}" for rel, name in edge_rows]

    role = (entity.attributes or {}).get("role")
    prompt = f"""Based ONLY on the excerpts below from a legal document collection, write a 2-4 sentence factual overview of {entity.canonical_name} ({entity.entity_type}): who they are, their role, and how they figure in these documents. No speculation; if the excerpts are thin, say what little is known.

Known role: {role or "unknown"}
Known relationships: {"; ".join(edges) or "none recorded"}

Excerpts:
{chr(10).join("- " + (s or "")[:300] for s in snippets)}

Respond with ONLY the overview text."""
    try:
        import anthropic  # lazy
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        response = await client.messages.create(
            model=PROFILE_MODEL, max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        text = next((b.text for b in response.content if b.type == "text"), "").strip()
        if not text:
            return None
        entity.overview = text
        entity.overview_generated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        entity.overview_mention_count = entity.mention_count
        return text
    except Exception as e:
        logger.warning("Overview generation failed for entity %s: %s", entity.id, e)
        return None
