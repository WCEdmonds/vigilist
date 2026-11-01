"""Folders: named, per-matter sets of documents reviewers work from (e.g. one
time period). Membership is a snapshot: it changes only when someone adds or
removes documents. Removing a document from a folder never deletes it."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import ROLE_RANK, get_accessible_production_ids, get_user_role_for_production
from app.models import Folder, FolderDocument, User
from app.routers.auth import get_current_user
from app.schemas import FolderAddDocuments, FolderCreate, FolderOut, FolderRemoveDocuments
from app.services import bulk_tagging as bt
from app.services.audit import log_action
from app.services.sql_arrays import ids_match

router = APIRouter(prefix="/api", tags=["folders"])


async def _require(db, user, production_id: int, write: bool) -> None:
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=404, detail="Production not found")
    if write:
        role = await get_user_role_for_production(db, user, production_id)
        if ROLE_RANK.get(role, 0) < ROLE_RANK["reviewer"]:
            raise HTTPException(status_code=403, detail="Read-only access")


async def _folder(db, user, folder_id: int, write: bool) -> Folder:
    folder = await db.get(Folder, folder_id)
    if folder is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    await _require(db, user, folder.production_id, write)
    return folder


async def _count(db, folder_id: int) -> int:
    return (await db.execute(
        select(func.count()).select_from(FolderDocument).where(FolderDocument.folder_id == folder_id)
    )).scalar() or 0


def _out(folder: Folder, count: int) -> FolderOut:
    return FolderOut(id=folder.id, production_id=folder.production_id, name=folder.name,
                     document_count=count, created_at=folder.created_at)


async def _name_taken(db, production_id: int, name: str, except_id: int | None = None) -> bool:
    q = select(Folder.id).where(Folder.production_id == production_id,
                                func.lower(Folder.name) == name.lower())
    if except_id is not None:
        q = q.where(Folder.id != except_id)
    return (await db.execute(q)).first() is not None


@router.get("/productions/{production_id}/folders", response_model=list[FolderOut])
async def list_folders(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _require(db, user, production_id, write=False)
    rows = (await db.execute(
        select(Folder, func.count(FolderDocument.id))
        .outerjoin(FolderDocument, FolderDocument.folder_id == Folder.id)
        .where(Folder.production_id == production_id)
        .group_by(Folder.id)
        .order_by(Folder.name)
    )).all()
    return [_out(f, n) for f, n in rows]


@router.post("/productions/{production_id}/folders", response_model=FolderOut)
async def create_folder(
    production_id: int,
    body: FolderCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _require(db, user, production_id, write=True)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Folder name is required")
    if await _name_taken(db, production_id, name):
        raise HTTPException(status_code=409, detail="A folder with that name already exists")
    folder = Folder(production_id=production_id, name=name, created_by=user.id)
    db.add(folder)
    await db.flush()
    await log_action(db, user, "folder_created", "folder", str(folder.id),
                     production_id=production_id, details={"name": name})
    await db.commit()
    await db.refresh(folder)
    return _out(folder, 0)


@router.patch("/folders/{folder_id}", response_model=FolderOut)
async def rename_folder(
    folder_id: int,
    body: FolderCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    folder = await _folder(db, user, folder_id, write=True)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Folder name is required")
    if await _name_taken(db, folder.production_id, name, except_id=folder.id):
        raise HTTPException(status_code=409, detail="A folder with that name already exists")
    old = folder.name
    folder.name = name
    await log_action(db, user, "folder_renamed", "folder", str(folder_id),
                     production_id=folder.production_id, details={"from": old, "to": name})
    await db.commit()
    return _out(folder, await _count(db, folder_id))


@router.delete("/folders/{folder_id}")
async def delete_folder(
    folder_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Deletes the folder only; its documents stay in the matter."""
    folder = await _folder(db, user, folder_id, write=True)
    count = await _count(db, folder_id)
    await log_action(db, user, "folder_deleted", "folder", str(folder_id),
                     production_id=folder.production_id,
                     details={"name": folder.name, "document_count": count})
    await db.delete(folder)
    await db.commit()
    return {"ok": True}


@router.post("/folders/{folder_id}/documents")
async def add_folder_documents(
    folder_id: int,
    body: FolderAddDocuments,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Add documents (or a whole search's results) plus their attachments.
    Only documents from the folder's own matter are added."""
    folder = await _folder(db, user, folder_id, write=True)
    if body.query is not None:
        query = body.query.model_copy(update={"production_id": folder.production_id})
        try:
            matched = await bt.resolve_query_docs(db, query, [folder.production_id])
        except bt.NoCriteriaError:
            raise HTTPException(status_code=422, detail="Search has no text or filters")
    else:
        matched = await bt.resolve_explicit_docs(db, body.doc_ids)
        if set(body.doc_ids) - set(matched):
            raise HTTPException(status_code=404, detail="Document not found")
        if any(pid != folder.production_id for pid in matched.values()):
            raise HTTPException(status_code=422, detail="Documents must be from the folder's matter")
    targets = await bt.expand_attachments(db, matched) if body.include_attachments else dict(matched)
    if len(targets) > bt.MAX_BULK_DOCS:
        raise HTTPException(
            status_code=422, detail=f"More than {bt.MAX_BULK_DOCS:,} documents — narrow the search")

    ids = list(targets)
    already = {r[0] for r in (await db.execute(
        select(FolderDocument.document_id)
        .where(FolderDocument.folder_id == folder_id, ids_match(FolderDocument.document_id, ids))
    )).all()} if ids else set()
    new_ids = [d for d in ids if d not in already]
    if new_ids:
        await db.execute(
            pg_insert(FolderDocument).on_conflict_do_nothing(constraint="uq_folder_doc"),
            [{"folder_id": folder_id, "document_id": d, "added_by": user.id} for d in new_ids],
        )
    summary = {"matched": len(matched), "attachments_added": len(targets) - len(matched),
               "added": len(new_ids), "already_in_folder": len(already)}
    await log_action(db, user, "folder_documents_added", "folder", str(folder_id),
                     production_id=folder.production_id, details=summary)
    await db.commit()
    return {**summary, "document_count": await _count(db, folder_id)}


@router.post("/folders/{folder_id}/documents/remove")
async def remove_folder_documents(
    folder_id: int,
    body: FolderRemoveDocuments,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    folder = await _folder(db, user, folder_id, write=True)
    result = await db.execute(
        delete(FolderDocument)
        .where(FolderDocument.folder_id == folder_id, ids_match(FolderDocument.document_id, body.doc_ids))
        .execution_options(synchronize_session=False)
    )
    await log_action(db, user, "folder_documents_removed", "folder", str(folder_id),
                     production_id=folder.production_id,
                     details={"removed": result.rowcount, "requested": len(body.doc_ids)})
    await db.commit()
    return {"removed": result.rowcount, "document_count": await _count(db, folder_id)}
