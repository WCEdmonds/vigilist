from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Document, Note, User
from app.routers.auth import get_current_user
from app.dependencies import get_accessible_production_ids
from app.schemas import NoteCreate, NoteOut, NoteUpdate

router = APIRouter(prefix="/api", tags=["notes"])


@router.get("/documents/{doc_id}/notes", response_model=list[NoteOut])
async def list_notes(
    doc_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    doc = await db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    query = select(Note).where(Note.document_id == doc_id).order_by(Note.created_at.desc())
    result = await db.execute(query)
    return result.scalars().all()


@router.post("/documents/{doc_id}/notes", response_model=NoteOut)
async def create_note(
    doc_id: UUID,
    body: NoteCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    doc = await db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")

    note = Note(document_id=doc_id, content=body.content, created_by=user.id)
    db.add(note)
    await db.commit()
    await db.refresh(note)
    return note


@router.put("/notes/{note_id}", response_model=NoteOut)
async def update_note(
    note_id: int,
    body: NoteUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    note = await db.get(Note, note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    doc = await db.get(Document, note.document_id)
    if doc and doc.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    note.content = body.content
    await db.commit()
    await db.refresh(note)
    return note


@router.delete("/notes/{note_id}")
async def delete_note(
    note_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    note = await db.get(Note, note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    doc = await db.get(Document, note.document_id)
    if doc and doc.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")
    await db.delete(note)
    await db.commit()
    return {"ok": True}
