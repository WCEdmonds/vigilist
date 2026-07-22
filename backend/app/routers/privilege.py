"""Privilege overrides + privilege log (P1-5)."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import ROLE_RANK, get_accessible_production_ids, get_user_role_for_production
from app.models import Document, User
from app.routers.auth import get_current_user
from app.schemas import PrivilegeOverrideUpdate
from app.services.audit import log_action
from app.services.privilege import DISPOSITIONS
from app.services.privilege_log import build_privilege_log_rows

router = APIRouter(prefix="/api", tags=["privilege"])


@router.put("/documents/{doc_id}/privilege")
async def update_privilege(
    doc_id: UUID,
    body: PrivilegeOverrideUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    doc = await db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    role = await get_user_role_for_production(db, user, doc.production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or higher role required")

    fields = body.model_fields_set
    if "disposition" in fields:
        if body.disposition is not None and body.disposition not in DISPOSITIONS:
            raise HTTPException(status_code=422, detail="invalid disposition")
        doc.privilege_disposition = body.disposition
    if "description" in fields:
        doc.privilege_description = body.description

    await log_action(
        db, user, "privilege_override_set", "document", str(doc_id),
        production_id=doc.production_id,
        details={"disposition": doc.privilege_disposition,
                 "has_description": doc.privilege_description is not None},
    )
    await db.commit()
    return {"disposition": doc.privilege_disposition,
            "description": doc.privilege_description}


@router.get("/productions/{production_id}/privilege-log")
async def get_privilege_log(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    return await build_privilege_log_rows(db, production_id)
