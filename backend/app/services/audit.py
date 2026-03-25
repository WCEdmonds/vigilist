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
