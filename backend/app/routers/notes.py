from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Document, Note
from app.routers.auth import get_current_user
from app.schemas import NoteCreate, NoteOut, NoteUpdate

router = APIRouter(prefix="/api", tags=["notes"])


@router.get("/documents/{doc_id}/notes", response_model=list[NoteOut])
async def list_notes(
    doc_id: UUID,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(get_current_user),
):
    query = select(Note).where(Note.document_id == doc_id).order_by(Note.created_at.desc())
    result = await db.execute(query)
    return result.scalars().all()


@router.post("/documents/{doc_id}/notes", response_model=NoteOut)
async def create_note(
    doc_id: UUID,
    body: NoteCreate,
    db: AsyncSession = Depends(get_db),
    user: str = Depends(get_current_user),
):
    doc = await db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    note = Note(document_id=doc_id, content=body.content, created_by=user)
    db.add(note)
    await db.commit()
    await db.refresh(note)
    return note


@router.put("/notes/{note_id}", response_model=NoteOut)
async def update_note(
    note_id: int,
    body: NoteUpdate,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(get_current_user),
):
    note = await db.get(Note, note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    note.content = body.content
    await db.commit()
    await db.refresh(note)
    return note


@router.delete("/notes/{note_id}")
async def delete_note(
    note_id: int,
    db: AsyncSession = Depends(get_db),
    _user: str = Depends(get_current_user),
):
    note = await db.get(Note, note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    await db.delete(note)
    await db.commit()
    return {"ok": True}
