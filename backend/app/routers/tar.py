"""TAR validation reports (P3-3): run, list, inspect."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import ROLE_RANK, get_accessible_production_ids, get_user_role_for_production
from app.models import Sample, TarValidationReport, User
from app.models_review import ReviewProject
from app.routers.auth import get_current_user
from app.schemas import TarValidationCreate, TarValidationOut
from app.services.audit import log_action
from app.services.sampling_stats import Z
from app.services.tar_validation import build_validation

router = APIRouter(prefix="/api", tags=["tar-validation"])


@router.post("/productions/{production_id}/tar-validation",
             response_model=TarValidationOut, status_code=201)
async def run_validation(
    production_id: int,
    body: TarValidationCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    role = await get_user_role_for_production(db, user, production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or higher role required")
    if body.confidence not in Z:
        raise HTTPException(status_code=422, detail=f"confidence must be one of {sorted(Z)}")

    project = await db.get(ReviewProject, body.project_id)
    if not project or project.production_id != production_id:
        raise HTTPException(status_code=422, detail="project_id is not a review project of this matter")

    control = await db.get(Sample, body.control_sample_id)
    if not control or control.production_id != production_id:
        raise HTTPException(status_code=422, detail="control_sample_id is not a sample of this matter")
    if control.purpose != "control":
        raise HTTPException(status_code=422, detail="control sample must have purpose 'control'")

    elusion = None
    if body.elusion_sample_id is not None:
        elusion = await db.get(Sample, body.elusion_sample_id)
        if not elusion or elusion.production_id != production_id:
            raise HTTPException(status_code=422, detail="elusion_sample_id is not a sample of this matter")
        if elusion.purpose != "elusion":
            raise HTTPException(status_code=422, detail="elusion sample must have purpose 'elusion'")

    results = await build_validation(
        db, production_id, body.project_id, control,
        body.responsive_tag_id, body.nonresponsive_tag_id, elusion,
        body.confidence,
    )

    report = TarValidationReport(
        production_id=production_id,
        project_id=body.project_id,
        params=body.model_dump(),
        results=results,
        created_by=user.id,
    )
    db.add(report)
    await db.flush()
    recall = (results.get("control") or {}).get("recall")
    await log_action(db, user, "tar_validation_run", "tar_validation",
                     str(report.id), production_id=production_id,
                     details={"project_id": body.project_id,
                              "recall": recall and recall.get("rate"),
                              "elusion": bool(results.get("elusion"))})
    await db.commit()
    await db.refresh(report)
    return TarValidationOut.model_validate(report)


@router.get("/productions/{production_id}/tar-validation",
            response_model=list[TarValidationOut])
async def list_validations(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    rows = (await db.execute(
        select(TarValidationReport)
        .where(TarValidationReport.production_id == production_id)
        .order_by(TarValidationReport.created_at, TarValidationReport.id)
    )).scalars().all()
    return [TarValidationOut.model_validate(r) for r in rows]


@router.get("/tar-validation/{report_id}", response_model=TarValidationOut)
async def get_validation(
    report_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    report = await db.get(TarValidationReport, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    accessible = await get_accessible_production_ids(db, user)
    if report.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    return TarValidationOut.model_validate(report)
