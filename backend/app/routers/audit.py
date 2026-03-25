import csv
import io
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_accessible_production_ids, get_user_role_for_production, ROLE_RANK
from app.models import AuditLog, User
from app.routers.auth import get_current_user
from app.schemas import AuditLogOut, PaginatedAuditLogs

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("", response_model=PaginatedAuditLogs)
async def list_audit_logs(
    production_id: int | None = None,
    user_id: str | None = None,
    action: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Audit logs require at least manager role on the production
    accessible = await get_accessible_production_ids(db, user)
    if production_id is not None:
        role = await get_user_role_for_production(db, user, production_id)
        if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
            raise HTTPException(status_code=403, detail="Manager or admin role required to view audit logs")

    query = select(AuditLog).where(
        AuditLog.production_id.in_(accessible) | AuditLog.production_id.is_(None)
    )
    count_query = select(func.count(AuditLog.id)).where(
        AuditLog.production_id.in_(accessible) | AuditLog.production_id.is_(None)
    )

    if production_id is not None:
        query = query.where(AuditLog.production_id == production_id)
        count_query = count_query.where(AuditLog.production_id == production_id)
    if user_id is not None:
        query = query.where(AuditLog.user_id == user_id)
        count_query = count_query.where(AuditLog.user_id == user_id)
    if action is not None:
        query = query.where(AuditLog.action == action)
        count_query = count_query.where(AuditLog.action == action)
    if date_from is not None:
        query = query.where(AuditLog.created_at >= date_from)
        count_query = count_query.where(AuditLog.created_at >= date_from)
    if date_to is not None:
        query = query.where(AuditLog.created_at <= date_to)
        count_query = count_query.where(AuditLog.created_at <= date_to)

    total = (await db.execute(count_query)).scalar() or 0

    query = query.order_by(AuditLog.created_at.desc())
    query = query.offset((page - 1) * per_page).limit(per_page)

    result = await db.execute(query)
    logs = result.scalars().all()

    return PaginatedAuditLogs(
        logs=[AuditLogOut.model_validate(log) for log in logs],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.get("/export/csv")
async def export_audit_csv(
    production_id: int | None = None,
    user_id: str | None = None,
    action: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    if production_id is not None:
        role = await get_user_role_for_production(db, user, production_id)
        if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
            raise HTTPException(status_code=403, detail="Manager or admin role required")

    query = select(AuditLog).where(
        AuditLog.production_id.in_(accessible) | AuditLog.production_id.is_(None)
    )

    if production_id is not None:
        query = query.where(AuditLog.production_id == production_id)
    if user_id is not None:
        query = query.where(AuditLog.user_id == user_id)
    if action is not None:
        query = query.where(AuditLog.action == action)
    if date_from is not None:
        query = query.where(AuditLog.created_at >= date_from)
    if date_to is not None:
        query = query.where(AuditLog.created_at <= date_to)

    query = query.order_by(AuditLog.created_at.desc())
    result = await db.execute(query)
    logs = result.scalars().all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Timestamp", "User", "Email", "Action", "Resource Type", "Resource ID", "Production ID", "Details"])
    for log in logs:
        writer.writerow([
            log.created_at.isoformat(),
            log.user_id,
            log.user_email,
            log.action,
            log.resource_type,
            log.resource_id or "",
            log.production_id or "",
            str(log.details),
        ])

    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit_log.csv"},
    )
