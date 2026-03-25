from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User
from app.routers.auth import get_current_user
from app.schemas import IngestRequest, IngestResponse
from app.services.ingest import ingest_production

router = APIRouter(prefix="/api", tags=["ingest"])


@router.post("/ingest", response_model=IngestResponse)
async def ingest(
    body: IngestRequest,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    result = await ingest_production(
        db,
        production_name=body.production_name,
        production_root=body.production_root,
        description=body.description,
    )
    return result
