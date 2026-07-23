"""Defensible sampling: calculator, frozen draws, estimates (P3-2)."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import ROLE_RANK, get_accessible_production_ids, get_user_role_for_production
from app.models import Document, DocumentTag, Sample, User
from app.routers.auth import get_current_user
from app.schemas import SampleCreate, SampleOut
from app.services.audit import log_action
from app.services.sampling_stats import Z, acceptance, sample_size, wilson_ci

router = APIRouter(prefix="/api", tags=["sampling"])

PURPOSES = ("richness", "acceptance", "control")


def _validate_stats_params(confidence: int, margin: float, expected_rate: float):
    if confidence not in Z:
        raise HTTPException(status_code=422, detail=f"confidence must be one of {sorted(Z)}")
    if not (0 < margin < 1):
        raise HTTPException(status_code=422, detail="margin must be in (0, 1)")
    if not (0 < expected_rate < 1):
        raise HTTPException(status_code=422, detail="expected_rate must be in (0, 1)")


async def _load_sample(db: AsyncSession, user: User, sample_id: int,
                       require_manager: bool = False) -> Sample:
    smp = await db.get(Sample, sample_id)
    if not smp:
        raise HTTPException(status_code=404, detail="Sample not found")
    accessible = await get_accessible_production_ids(db, user)
    if smp.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    if require_manager:
        role = await get_user_role_for_production(db, user, smp.production_id)
        if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
            raise HTTPException(status_code=403, detail="Manager or higher role required")
    return smp


def _scope_conditions(production_id: int, source_type: str | None) -> list:
    conditions = [Document.production_id == production_id]
    if source_type == "received":
        conditions.append(Document.source_type == "received")
    elif source_type == "collection":
        conditions.append(Document.source_type.is_distinct_from("received"))
    return conditions


@router.get("/sampling/sample-size")
async def calculate_sample_size(
    population: int = Query(..., ge=1),
    confidence: int = 95,
    margin: float = 0.05,
    expected_rate: float = 0.5,
    user: User = Depends(get_current_user),
):
    _validate_stats_params(confidence, margin, expected_rate)
    n = sample_size(population, confidence, margin, expected_rate)
    return {"population": population, "confidence": confidence, "margin": margin,
            "expected_rate": expected_rate, "sample_size": n}


@router.post("/productions/{production_id}/samples",
             response_model=SampleOut, status_code=201)
async def draw_sample(
    production_id: int,
    body: SampleCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    role = await get_user_role_for_production(db, user, production_id)
    if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="Manager or higher role required")
    if not body.name.strip():
        raise HTTPException(status_code=422, detail="name is required")
    if body.purpose not in PURPOSES:
        raise HTTPException(status_code=422, detail=f"purpose must be one of {PURPOSES}")
    if body.source_type not in (None, "collection", "received"):
        raise HTTPException(status_code=422, detail="source_type must be 'collection' or 'received'")
    _validate_stats_params(body.confidence, body.margin, body.expected_rate)

    scope = _scope_conditions(production_id, body.source_type)
    population = (await db.execute(
        select(func.count(Document.id)).where(*scope)
    )).scalar() or 0
    if population == 0:
        raise HTTPException(status_code=422, detail="No documents in scope to sample")

    n = body.size or sample_size(population, body.confidence, body.margin,
                                 body.expected_rate)
    n = min(n, population)
    rows = (await db.execute(
        select(Document.id).where(*scope).order_by(func.random()).limit(n)
    )).all()
    doc_ids = [str(r[0]) for r in rows]

    smp = Sample(
        production_id=production_id, name=body.name.strip(), purpose=body.purpose,
        params={"population": population, "confidence": body.confidence,
                "margin": body.margin, "expected_rate": body.expected_rate,
                "source_type": body.source_type, "requested_size": body.size,
                "size": len(doc_ids)},
        document_ids=doc_ids, created_by=user.id,
    )
    db.add(smp)
    await db.flush()
    await log_action(db, user, "sample_drawn", "sample", str(smp.id),
                     production_id=production_id,
                     details={"purpose": body.purpose, "size": len(doc_ids),
                              "population": population})
    await db.commit()
    await db.refresh(smp)
    return SampleOut.model_validate(smp)


@router.get("/productions/{production_id}/samples", response_model=list[SampleOut])
async def list_samples(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    rows = (await db.execute(
        select(Sample).where(Sample.production_id == production_id)
        .order_by(Sample.created_at, Sample.id)
    )).scalars().all()
    return [SampleOut.model_validate(s) for s in rows]


@router.get("/samples/{sample_id}", response_model=SampleOut)
async def get_sample(
    sample_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    smp = await _load_sample(db, user, sample_id)
    return SampleOut.model_validate(smp)


async def _tagged_in_sample(db: AsyncSession, smp: Sample, tag_id: int) -> int:
    rows = (await db.execute(
        select(DocumentTag.document_id).where(
            DocumentTag.tag_id == tag_id,
            DocumentTag.document_id.in_(list(smp.document_ids or [])),
        )
    )).all()
    sample_ids = set(smp.document_ids or [])
    return sum(1 for (did,) in rows if str(did) in sample_ids)


@router.get("/samples/{sample_id}/estimate")
async def sample_estimate(
    sample_id: int,
    tag_id: int,
    confidence: int | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    smp = await _load_sample(db, user, sample_id)
    conf = confidence or smp.params.get("confidence", 95)
    if conf not in Z:
        raise HTTPException(status_code=422, detail=f"confidence must be one of {sorted(Z)}")
    n = len(smp.document_ids or [])
    positives = await _tagged_in_sample(db, smp, tag_id)
    rate, low, high = wilson_ci(positives, n, conf)
    population = smp.params.get("population", 0)
    return {
        "sample_id": sample_id, "tag_id": tag_id, "n": n,
        "positives": positives, "confidence": conf,
        "rate": rate, "ci_low": low, "ci_high": high,
        "population": population,
        "estimated_low": int(low * population),
        "estimated_high": int(high * population),
    }


@router.get("/samples/{sample_id}/acceptance")
async def sample_acceptance(
    sample_id: int,
    tag_id: int,
    tolerable: float = Query(..., description="tolerable defect rate, e.g. 0.05"),
    confidence: int | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    smp = await _load_sample(db, user, sample_id)
    if not (0 < tolerable < 1):
        raise HTTPException(status_code=422, detail="tolerable must be in (0, 1)")
    conf = confidence or smp.params.get("confidence", 95)
    if conf not in Z:
        raise HTTPException(status_code=422, detail=f"confidence must be one of {sorted(Z)}")
    n = len(smp.document_ids or [])
    defects = await _tagged_in_sample(db, smp, tag_id)
    return {"sample_id": sample_id, "tag_id": tag_id,
            **acceptance(defects, n, tolerable, conf)}


@router.delete("/samples/{sample_id}")
async def delete_sample(
    sample_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    smp = await _load_sample(db, user, sample_id, require_manager=True)
    await log_action(db, user, "sample_deleted", "sample", str(sample_id),
                     production_id=smp.production_id,
                     details={"name": smp.name, "purpose": smp.purpose})
    await db.delete(smp)
    await db.commit()
    return {"ok": True}
