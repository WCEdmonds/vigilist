"""Production listing and access management."""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import func, select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import ROLE_RANK, get_accessible_production_ids, get_user_role_for_production
from app.models import Document, PendingInvite, Production, ProductionAccess, User
from app.routers.auth import get_current_user
from app.services.audit import log_action
from app.services.claims import sync_user_claims
from app.services.email import send_access_granted_email, send_invite_email
from app.schemas import (
    IntakeSummaryOut,
    InviteRequest,
    KeyPlayerOut,
    PendingInviteOut,
    PipelineStatusOut,
    ProductionAccessOut,
    ProductionUpdate,
    ProductionWithAccess,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/productions", tags=["productions"])

STALE_RUNNING_MINUTES = 45


def _is_actively_running(status: dict | None) -> bool:
    """True if a pipeline stage is "running" and its status write is recent.

    A worker killed mid-stage (e.g. Cloud Run scale-down) leaves a stage
    "running" forever with no further status writes. Treat a "running"
    status whose `updated_at` is older than the Cloud Tasks dispatch
    ceiling (30 min) plus margin as stale rather than actually in-flight,
    so re-runs aren't blocked forever by a dead worker.
    """
    if not status:
        return False
    if not any(status.get(s) == "running" for s in ("clustering", "summaries", "brief")):
        return False
    ts = status.get("updated_at")
    if not ts:
        return True
    try:
        updated = datetime.fromisoformat(ts)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - updated < timedelta(minutes=STALE_RUNNING_MINUTES)


@router.get("", response_model=list[ProductionWithAccess])
async def list_productions(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List productions the user owns or has access to."""
    prod_ids = await get_accessible_production_ids(db, user)
    if not prod_ids:
        return []
    result = await db.execute(
        select(Production)
        .where(Production.id.in_(prod_ids))
        .order_by(Production.created_at.desc())
    )
    prods = result.scalars().all()
    counts_result = await db.execute(
        select(Document.production_id, func.count(Document.id))
        .where(Document.production_id.in_(prod_ids))
        .group_by(Document.production_id)
    )
    counts = dict(counts_result.all())
    return [
        ProductionWithAccess(
            id=p.id,
            name=p.name,
            description=p.description,
            owner_id=p.owner_id,
            is_owner=(p.owner_id == user.id),
            created_at=p.created_at,
            document_count=counts.get(p.id, 0),
            case_context=p.case_context,
            has_brief=bool(p.brief),
        )
        for p in prods
    ]


@router.delete("/{production_id}")
async def delete_production(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Delete a production. Owner only.

    Cascades to all related rows (documents, access, queues, annotations,
    etc.) via ondelete=CASCADE, and also wipes the production's files
    from Firebase Storage (both raw uploads and converted images).
    Finally re-syncs custom claims for the owner and anyone who had
    shared access.
    """
    from app.services.storage import delete_prefix

    prod = await db.get(Production, production_id)
    if not prod:
        raise HTTPException(status_code=404, detail="Production not found")
    if prod.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Only the owner can delete a production")

    production_name = prod.name

    # Collect users who had access so we can re-sync their claims after
    access_result = await db.execute(
        select(ProductionAccess.user_id).where(ProductionAccess.production_id == production_id)
    )
    affected_user_ids = [row[0] for row in access_result.all()]

    # Delete all storage files first (raw uploads + converted images).
    # Swallow errors here — we still want the DB delete to proceed
    # even if storage cleanup is partial.
    try:
        delete_prefix(f"productions/{production_id}/")
    except Exception:
        pass

    await log_action(
        db, user, "production_deleted", "production", str(production_id),
        production_id=production_id,
        details={"name": production_name},
    )

    await db.delete(prod)
    await db.commit()

    # Re-sync claims for the owner and anyone who had access
    await sync_user_claims(db, user)
    for uid in affected_user_ids:
        affected_user = await db.get(User, uid)
        if affected_user:
            await sync_user_claims(db, affected_user)

    return {"ok": True}


@router.get("/{production_id}/pipeline", response_model=PipelineStatusOut)
async def get_pipeline(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Ambient AI pipeline status, brief, and case context. Any accessible role."""
    prod = await db.get(Production, production_id)
    if prod is None:
        raise HTTPException(status_code=404, detail="Production not found")
    await get_user_role_for_production(db, user, production_id)
    counts = (
        await db.execute(
            select(
                func.count(Document.id),
                func.count(Document.summary),
            ).where(Document.production_id == production_id)
        )
    ).one()
    key_players_resolved = None
    if prod.brief and prod.brief.get("key_players"):
        try:
            from app.services.brief import resolve_key_players
            resolved = await resolve_key_players(
                db, production_id, list(prod.brief["key_players"]))
            # Validate here so a malformed id degrades to un-augmented, never a 500.
            key_players_resolved = [KeyPlayerOut(**r) for r in resolved]
        except Exception:
            logger.exception("key player resolution failed for production %s", production_id)
            key_players_resolved = None
    return PipelineStatusOut(
        status=prod.ai_pipeline_status,
        brief=prod.brief,
        case_context=prod.case_context,
        doc_count=counts[0],
        summarized_count=counts[1],
        key_players_resolved=key_players_resolved,
    )


@router.get("/{production_id}/intake-summary", response_model=IntakeSummaryOut)
async def get_intake_summary(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """What intake created for this production — the ingest wizard's receipt.

    Counts are live, so duplicate groups (detected asynchronously by the
    ambient pipeline) may still read 0 right after ingest completes.
    """
    prod = await db.get(Production, production_id)
    if prod is None:
        raise HTTPException(status_code=404, detail="Production not found")
    await get_user_role_for_production(db, user, production_id)

    from app.models import DuplicateGroup

    doc_counts = (
        await db.execute(
            select(
                func.count(Document.id),
                func.count(func.distinct(Document.custodian)),
                func.count(func.distinct(Document.family_id)),
                func.count(Document.family_id),
                func.count(func.distinct(Document.thread_id)),
                func.count(Document.id).filter(Document.is_inclusive.is_(True)),
            ).where(Document.production_id == production_id)
        )
    ).one()
    dup_groups = (
        await db.execute(
            select(func.count(DuplicateGroup.id)).where(
                DuplicateGroup.production_id == production_id
            )
        )
    ).scalar_one()

    return IntakeSummaryOut(
        documents=doc_counts[0],
        custodians=doc_counts[1],
        email_families=doc_counts[2],
        family_documents=doc_counts[3],
        threads=doc_counts[4],
        inclusive_emails=doc_counts[5],
        duplicate_groups=dup_groups,
    )


@router.post("/{production_id}/pipeline/run")
async def run_pipeline(
    production_id: int,
    background_tasks: BackgroundTasks,
    body: dict | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Kick off (or re-run) the ambient AI pipeline. Manager or admin role required.

    409s if any stage is currently "running" — this is what serializes
    invocations per production, since run_ambient_pipeline's per-stage
    status write is read-modify-write and cannot safely run concurrently
    with itself.
    """
    prod = await db.get(Production, production_id)
    if prod is None:
        raise HTTPException(status_code=404, detail="Production not found")
    role = await get_user_role_for_production(db, user, production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or admin role required")
    status = prod.ai_pipeline_status or {}
    if _is_actively_running(status):
        raise HTTPException(status_code=409, detail="Pipeline already running")
    force = bool((body or {}).get("force"))
    await log_action(
        db, user, "pipeline_run_requested", "production", str(production_id),
        production_id=production_id, details={"force": force},
    )
    await db.commit()

    from app.services import tasks as task_service
    from app.services.pipeline import run_ambient_pipeline

    if task_service.is_configured():
        task_service.enqueue_pipeline(production_id, force)
    else:
        background_tasks.add_task(run_ambient_pipeline, production_id, force)
    return {"started": True}


@router.patch("/{production_id}", response_model=ProductionWithAccess)
async def update_production(
    production_id: int,
    body: ProductionUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Update a production's description or case context. Owner only."""
    prod = await db.get(Production, production_id)
    if prod is None:
        raise HTTPException(status_code=404, detail="Production not found")
    if prod.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Owner only")
    if body.description is not None:
        prod.description = body.description.strip() or None
    if body.case_context is not None:
        prod.case_context = body.case_context.strip() or None
    await db.commit()
    await db.refresh(prod)
    doc_count = (
        await db.execute(
            select(func.count(Document.id)).where(Document.production_id == production_id)
        )
    ).scalar() or 0
    return ProductionWithAccess(
        id=prod.id, name=prod.name, description=prod.description,
        owner_id=prod.owner_id, is_owner=True, created_at=prod.created_at,
        document_count=doc_count, case_context=prod.case_context,
        has_brief=bool(prod.brief),
    )


@router.get("/{production_id}/access", response_model=list[ProductionAccessOut])
async def list_access(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List users with access to a production. Owner only."""
    prod = await db.get(Production, production_id)
    if not prod:
        raise HTTPException(status_code=404, detail="Production not found")
    role = await get_user_role_for_production(db, user, production_id)
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    result = await db.execute(
        select(ProductionAccess, User)
        .join(User, ProductionAccess.user_id == User.id)
        .where(ProductionAccess.production_id == production_id)
        .order_by(ProductionAccess.granted_at)
    )
    rows = result.all()
    return [
        ProductionAccessOut(
            id=pa.id,
            user_id=pa.user_id,
            user_email=u.email,
            user_display_name=u.display_name,
            role=pa.role,
            granted_by=pa.granted_by,
            granted_at=pa.granted_at,
        )
        for pa, u in rows
    ]


@router.get("/{production_id}/invites", response_model=list[PendingInviteOut])
async def list_pending_invites(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List pending invites for a production. Owner only."""
    prod = await db.get(Production, production_id)
    if not prod:
        raise HTTPException(status_code=404, detail="Production not found")
    role = await get_user_role_for_production(db, user, production_id)
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    result = await db.execute(
        select(PendingInvite)
        .where(PendingInvite.production_id == production_id)
        .order_by(PendingInvite.created_at)
    )
    return result.scalars().all()


@router.post("/{production_id}/access")
async def invite_user(
    production_id: int,
    body: InviteRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Invite a user by email. Creates access if user exists, pending invite if not."""
    prod = await db.get(Production, production_id)
    if not prod:
        raise HTTPException(status_code=404, detail="Production not found")
    role = await get_user_role_for_production(db, user, production_id)
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    email = body.email.strip().lower()

    # Check if user already exists
    result = await db.execute(select(User).where(User.email == email))
    target_user = result.scalar_one_or_none()

    if target_user:
        # Check if already has access
        existing = await db.execute(
            select(ProductionAccess).where(
                ProductionAccess.production_id == production_id,
                ProductionAccess.user_id == target_user.id,
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="User already has access")

        pa = ProductionAccess(
            production_id=production_id,
            user_id=target_user.id,
            granted_by=user.id,
            role=body.role,
        )
        db.add(pa)
        await log_action(db, user, "user_invited", "production", str(production_id),
                         production_id=production_id, details={"email": body.email, "role": body.role})
        await db.commit()

        # Sync the invited user's Firebase claims
        await sync_user_claims(db, target_user)

        inviter_name = user.display_name or user.email
        send_access_granted_email(email, inviter_name, prod.name, body.role)

        return {"status": "granted", "email": email}
    else:
        # Create pending invite
        existing = await db.execute(
            select(PendingInvite).where(
                PendingInvite.production_id == production_id,
                PendingInvite.email == email,
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Invite already pending")

        invite = PendingInvite(
            production_id=production_id,
            email=email,
            invited_by=user.id,
            role=body.role,
        )
        db.add(invite)
        await log_action(db, user, "user_invited", "production", str(production_id),
                         production_id=production_id, details={"email": body.email, "role": body.role})
        await db.commit()

        inviter_name = user.display_name or user.email
        send_invite_email(email, inviter_name, prod.name, body.role)

        return {"status": "invited", "email": email}


@router.delete("/{production_id}/access/{user_id}")
async def revoke_access(
    production_id: int,
    user_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Revoke a user's access. Owner only."""
    prod = await db.get(Production, production_id)
    if not prod:
        raise HTTPException(status_code=404, detail="Production not found")
    role = await get_user_role_for_production(db, user, production_id)
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    result = await db.execute(
        select(ProductionAccess).where(
            ProductionAccess.production_id == production_id,
            ProductionAccess.user_id == user_id,
        )
    )
    pa = result.scalar_one_or_none()
    if not pa:
        raise HTTPException(status_code=404, detail="Access entry not found")

    await db.delete(pa)
    await log_action(db, user, "access_revoked", "production", str(production_id),
                     production_id=production_id, details={"revoked_user_id": user_id})
    await db.commit()

    # Sync the revoked user's Firebase claims
    revoked_user = await db.get(User, user_id)
    if revoked_user:
        await sync_user_claims(db, revoked_user)

    return {"ok": True}


@router.post("/{production_id}/source-designation")
async def designate_sources(
    production_id: int,
    body: dict,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Bulk-designate documents' source (P0-SP5 backfill for legacy loads)."""
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    role = await get_user_role_for_production(db, user, production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or higher role required")

    source_type = body.get("source_type")
    if source_type not in ("collection", "received"):
        raise HTTPException(status_code=422, detail="source_type must be 'collection' or 'received'")
    source_party = (body.get("source_party") or "").strip() or None
    only_undesignated = bool(body.get("only_undesignated", True))

    stmt = (
        sa_update(Document)
        .where(Document.production_id == production_id)
        .values(source_type=source_type, source_party=source_party)
    )
    if only_undesignated:
        stmt = stmt.where(Document.source_type.is_(None))
    result = await db.execute(stmt)
    updated = getattr(result, "rowcount", 0) or 0
    await log_action(db, user, "source_designation_set", "production", str(production_id),
                     production_id=production_id,
                     details={"source_type": source_type, "source_party": source_party,
                              "only_undesignated": only_undesignated, "updated": updated})
    await db.commit()
    return {"updated": updated}
