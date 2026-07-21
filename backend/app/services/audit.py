from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog, User


async def log_action(
    db: AsyncSession,
    user: User,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    production_id: int | None = None,
    details: dict | None = None,
) -> None:
    """Append an immutable audit log entry. Fire-and-forget within the current transaction."""
    entry = AuditLog(
        user_id=user.id,
        user_email=user.email,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        production_id=production_id,
        details=details or {},
    )
    db.add(entry)
    # Don't commit — let the caller's transaction handle it.
    # The audit entry commits with the action it's logging.


async def resolve_audit_actor(db: AsyncSession, production) -> User | None:
    """Resolve the User to attribute an *ambient* audit action to.

    Ambient pipeline stages (clustering, summarization, brief generation) run
    with no authenticated caller — there's no request, no session, nobody who
    clicked a button. But `log_action` requires a `User` to attribute the
    entry to. We attribute these system-initiated actions to the production's
    owner: they're the closest thing to a responsible party for AI work that
    runs against their production. When the production has no owner
    (`owner_id` is null — e.g. an org-only production with no individual
    owner), there's nobody to attribute to; callers should treat `None` as
    "skip logging this action" rather than invent a synthetic actor.
    """
    if production.owner_id is None:
        return None
    return await db.get(User, production.owner_id)
