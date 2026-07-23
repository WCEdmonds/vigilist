"""Search-term hit reports (P3-1): saved term lists, runs, CSV export."""

import csv
import io
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import ROLE_RANK, get_accessible_production_ids, get_user_role_for_production
from app.models import SearchTermReport, User
from app.routers.auth import get_current_user
from app.schemas import SearchTermReportCreate, SearchTermReportOut
from app.services.audit import log_action
from app.services.search_terms import run_search_term_report

router = APIRouter(prefix="/api", tags=["search-terms"])

MAX_TERMS = 200


async def _load_report(db: AsyncSession, user: User, report_id: int,
                       require_manager: bool = False) -> SearchTermReport:
    rpt = await db.get(SearchTermReport, report_id)
    if not rpt:
        raise HTTPException(status_code=404, detail="Report not found")
    accessible = await get_accessible_production_ids(db, user)
    if rpt.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    if require_manager:
        role = await get_user_role_for_production(db, user, rpt.production_id)
        if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
            raise HTTPException(status_code=403, detail="Manager or higher role required")
    return rpt


@router.post("/productions/{production_id}/search-term-reports",
             response_model=SearchTermReportOut, status_code=201)
async def create_report(
    production_id: int,
    body: SearchTermReportCreate,
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
    terms = [t.strip() for t in body.terms]
    if not terms or len(terms) > MAX_TERMS or any(not t for t in terms):
        raise HTTPException(
            status_code=422,
            detail=f"terms must be 1..{MAX_TERMS} non-blank entries")

    rpt = SearchTermReport(
        production_id=production_id, name=body.name.strip(), terms=terms,
        created_by=user.id,
    )
    db.add(rpt)
    await db.flush()
    await log_action(db, user, "search_term_report_created", "search_term_report",
                     str(rpt.id), production_id=production_id,
                     details={"name": rpt.name, "terms": len(terms)})
    await db.commit()
    await db.refresh(rpt)
    return SearchTermReportOut.model_validate(rpt)


@router.get("/productions/{production_id}/search-term-reports",
            response_model=list[SearchTermReportOut])
async def list_reports(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    rows = (await db.execute(
        select(SearchTermReport)
        .where(SearchTermReport.production_id == production_id)
        .order_by(SearchTermReport.created_at, SearchTermReport.id)
    )).scalars().all()
    return [SearchTermReportOut.model_validate(r) for r in rows]


@router.get("/search-term-reports/{report_id}", response_model=SearchTermReportOut)
async def get_report(
    report_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rpt = await _load_report(db, user, report_id)
    return SearchTermReportOut.model_validate(rpt)


@router.post("/search-term-reports/{report_id}/run")
async def run_report(
    report_id: int,
    body: dict | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rpt = await _load_report(db, user, report_id, require_manager=True)
    source_type = (body or {}).get("source_type")
    if source_type not in (None, "collection", "received"):
        raise HTTPException(status_code=422, detail="source_type must be 'collection' or 'received'")
    results = await run_search_term_report(db, rpt.production_id, rpt.terms, source_type)
    rpt.results = results
    rpt.computed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await log_action(db, user, "search_term_report_run", "search_term_report",
                     str(report_id), production_id=rpt.production_id,
                     details={"terms": len(rpt.terms), "any_hits": results["any_hits"],
                              "source_type": source_type})
    await db.commit()
    return results


@router.delete("/search-term-reports/{report_id}")
async def delete_report(
    report_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rpt = await _load_report(db, user, report_id, require_manager=True)
    await log_action(db, user, "search_term_report_deleted", "search_term_report",
                     str(report_id), production_id=rpt.production_id,
                     details={"name": rpt.name})
    await db.delete(rpt)
    await db.commit()
    return {"ok": True}


@router.get("/search-term-reports/{report_id}/csv")
async def export_report_csv(
    report_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rpt = await _load_report(db, user, report_id)
    if not rpt.results:
        raise HTTPException(status_code=404, detail="Report has not been run yet")
    results = rpt.results

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Term", "Documents with hits", "Docs + families", "Unique hits"])
    for row in results.get("terms", []):
        writer.writerow([row["term"], row["hits"], row["with_families"], row["unique_hits"]])
    writer.writerow(["TOTAL (any term)", results.get("any_hits", 0),
                     results.get("any_with_families", 0),
                     sum(r["unique_hits"] for r in results.get("terms", []))])

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=search_term_report_{report_id}.csv"},
    )
