"""Bridge AI review decisions into the shared tag namespace.

Accepting (or overriding) an AI classification writes a real DocumentTag —
the same tags humans apply — so exports, filters, and queues don't care who
decided. Seeded categories map to the seeded responsiveness/issues tags;
custom review categories get-or-create a 'custom' tag. Nothing here commits.
"""

from sqlalchemy import func, select

from app.models import DocumentTag, Tag, User
from app.models_review import AIReviewResult, ReviewProject
from app.services.audit import log_action

CATEGORY_TAG_MAP: dict[str, tuple[str, str]] = {
    "relevant": ("Responsive", "responsiveness"),
    "not_relevant": ("Not Responsive", "responsiveness"),
    "needs_review": ("Needs Review", "responsiveness"),
    "key_document": ("Key Document", "issues"),
}


def decision_to_category(decision: str) -> str | None:
    if decision.startswith("override_"):
        return decision[len("override_"):] or None
    return None


async def resolve_tag_for_category(db, category_name: str, categories: list[dict]) -> Tag:
    """Resolve the Tag for a review category, creating a custom one if needed.

    Seeded categories (CATEGORY_TAG_MAP) look up their mapped (name, category)
    pair. Anything else is a custom review category: look up an existing
    'custom' tag by case-insensitive name, or create one using the color from
    the project's `categories` list (default 'blue').
    """
    if category_name in CATEGORY_TAG_MAP:
        display_name, tag_category = CATEGORY_TAG_MAP[category_name]
    else:
        display_name = category_name.replace("_", " ").title()
        tag_category = "custom"

    result = await db.execute(
        select(Tag).where(
            func.lower(Tag.name) == display_name.lower(),
            Tag.category == tag_category,
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        return existing

    color = "blue"
    for cat in categories:
        if cat.get("name") == category_name:
            color = cat.get("color", "blue")
            break

    tag = Tag(name=display_name, category=tag_category, color=color)
    db.add(tag)
    await db.flush()
    return tag


async def apply_decision_tag(
    db, user: User, result: AIReviewResult, decision: str, project: ReviewProject
) -> int | None:
    """Write a DocumentTag for an accepted/overridden AI decision and log the audit trail.

    Does NOT commit — the caller's transaction handles that.
    """
    final_category = decision_to_category(decision) or result.ai_decision
    if not final_category:
        return None

    tag = await resolve_tag_for_category(db, final_category, project.categories or [])

    existing = await db.execute(
        select(DocumentTag).where(
            DocumentTag.document_id == result.document_id,
            DocumentTag.tag_id == tag.id,
        )
    )
    if not existing.scalar_one_or_none():
        db.add(DocumentTag(document_id=result.document_id, tag_id=tag.id, applied_by=user.id))

    action = "ai_suggestion_accepted" if decision == "agree" else "ai_suggestion_overridden"
    await log_action(
        db,
        user,
        action,
        "document",
        resource_id=str(result.document_id),
        production_id=project.production_id,
        details={
            "project_id": project.id,
            "result_id": result.id,
            "tag_id": tag.id,
            "category": final_category,
        },
    )

    return tag.id
