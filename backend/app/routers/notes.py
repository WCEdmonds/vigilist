from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Document, Note, User
from app.routers.auth import get_current_user
from app.dependencies import get_accessible_production_ids, get_user_role_for_production
from app.services.audit import log_action
from app.schemas import NoteCreate, NoteOut, NoteUpdate

router = APIRouter(prefix="/api", tags=["notes"])


def _build_note_out(note: Note, email: str, display_name: str | None) -> NoteOut:
    return NoteOut(
        id=note.id,
        document_id=note.document_id,
        content=note.content,
        timestamp=note.timestamp,
        created_by=note.created_by,
        created_by_email=email,
        created_by_display_name=display_name,
        created_at=note.created_at,
        updated_at=note.updated_at,
    )


async def _note_out_for(db: AsyncSession, note: Note) -> NoteOut:
    creator = await db.get(User, note.created_by)
    email = creator.email if creator else note.created_by
    display_name = creator.display_name if creator else None
    return _build_note_out(note, email, display_name)


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
    notes = result.scalars().all()

    # Resolve unique user emails/display_names in one pass (mirrors annotations router).
    user_cache: dict[str, User | None] = {}
    out: list[NoteOut] = []
    for note in notes:
        if note.created_by not in user_cache:
            user_cache[note.created_by] = await db.get(User, note.created_by)
        creator = user_cache[note.created_by]
        email = creator.email if creator else note.created_by
        display_name = creator.display_name if creator else None
        out.append(_build_note_out(note, email, display_name))
    return out


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
    role = await get_user_role_for_production(db, user, doc.production_id)
    if role == "readonly":
        raise HTTPException(status_code=403, detail="Read-only access")

    note = Note(document_id=doc_id, content=body.content, created_by=user.id)
    db.add(note)
    await db.flush()
    await log_action(db, user, "note_created", "note", str(note.id),
                     production_id=doc.production_id, details={"document_id": str(doc_id)})
    await db.commit()
    await db.refresh(note)
    return _build_note_out(note, user.email, user.display_name)


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
    if doc:
        role = await get_user_role_for_production(db, user, doc.production_id)
        if role == "readonly":
            raise HTTPException(status_code=403, detail="Read-only access")
    note.content = body.content
    await log_action(db, user, "note_updated", "note", str(note_id),
                     details={"document_id": str(note.document_id)})
    await db.commit()
    await db.refresh(note)
    return await _note_out_for(db, note)


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
    if doc:
        role = await get_user_role_for_production(db, user, doc.production_id)
        if role == "readonly":
            raise HTTPException(status_code=403, detail="Read-only access")
    await db.delete(note)
    await log_action(db, user, "note_deleted", "note", str(note_id),
                     details={"document_id": str(note.document_id)})
    await db.commit()
    return {"ok": True}
