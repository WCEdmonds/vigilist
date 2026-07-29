"""Saved AI chat history: list, read, and delete conversations.

Conversations belong to a production and are shared by everyone with access to
that matter. Read access is therefore production access — no per-user filter.
Deletion is narrower: the author, or a manager/admin on the matter.

Routes live under a flat `/api/conversations` prefix with `production_id` as a
query parameter rather than nesting under `/api/productions/{id}/...`, so they
cannot be shadowed by another router's path parameter.
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.dependencies import (
    ROLE_RANK,
    get_accessible_production_ids,
    get_user_role_for_production,
)
from app.models import ChatConversation, ChatMessage, User
from app.routers.auth import get_current_user
from app.schemas import ChatMessageOut, ConversationDetail, ConversationSummary
from app.services.audit import log_action

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


async def _emails_for(db: AsyncSession, user_ids: set[str]) -> dict[str, str]:
    """Map user id -> email so shared threads render authors, not raw UIDs."""
    ids = {u for u in user_ids if u}
    if not ids:
        return {}
    rows = await db.execute(select(User.id, User.email).where(User.id.in_(ids)))
    return {uid: email for uid, email in rows.all()}


async def _load_accessible(
    db: AsyncSession, user: User, conversation_id: UUID
) -> ChatConversation:
    """Fetch a conversation the caller may read, or raise.

    404 rather than 403 on an inaccessible matter: whether a conversation
    exists in a production you cannot see is itself not your business.
    """
    convo = await db.get(
        ChatConversation,
        conversation_id,
        options=[selectinload(ChatConversation.messages)],
    )
    if convo is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    accessible = await get_accessible_production_ids(db, user)
    if convo.production_id not in accessible:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return convo


@router.get("", response_model=list[ConversationSummary])
async def list_conversations(
    production_id: int = Query(..., description="Matter whose history to list"),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Conversations in a matter, newest first. Shared across the team."""
    accessible = await get_accessible_production_ids(db, user)
    if production_id not in accessible:
        raise HTTPException(status_code=404, detail="Production not found")

    result = await db.execute(
        select(ChatConversation)
        .where(ChatConversation.production_id == production_id)
        .order_by(ChatConversation.updated_at.desc())
        .limit(limit)
    )
    convos = result.scalars().all()
    if not convos:
        return []

    # Counts in one grouped query rather than per-row lazy loads, mirroring
    # how list_productions counts documents.
    ids = [c.id for c in convos]
    count_rows = await db.execute(
        select(ChatMessage.conversation_id, func.count(ChatMessage.id))
        .where(ChatMessage.conversation_id.in_(ids))
        .group_by(ChatMessage.conversation_id)
    )
    counts = dict(count_rows.all())
    emails = await _emails_for(db, {c.created_by for c in convos})

    return [
        ConversationSummary(
            id=c.id,
            production_id=c.production_id,
            title=c.title,
            created_by=c.created_by,
            created_by_email=emails.get(c.created_by),
            message_count=counts.get(c.id, 0),
            created_at=c.created_at,
            updated_at=c.updated_at,
        )
        for c in convos
    ]


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Full transcript of one conversation."""
    convo = await _load_accessible(db, user, conversation_id)
    authors = {m.user_id for m in convo.messages if m.user_id}
    emails = await _emails_for(db, authors | {convo.created_by})

    return ConversationDetail(
        id=convo.id,
        production_id=convo.production_id,
        title=convo.title,
        created_by=convo.created_by,
        created_by_email=emails.get(convo.created_by),
        message_count=len(convo.messages),
        created_at=convo.created_at,
        updated_at=convo.updated_at,
        messages=[
            ChatMessageOut(
                id=m.id,
                role=m.role,
                content=m.content,
                user_id=m.user_id,
                user_email=emails.get(m.user_id) if m.user_id else None,
                attachments=list(m.attachments or []),
                created_at=m.created_at,
            )
            for m in convo.messages
        ],
    )


@router.delete("/{conversation_id}")
async def delete_conversation(
    conversation_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Permanently delete a conversation and its turns.

    A shared thread is not one person's to erase on a whim, so deletion is the
    author's or a manager's. The delete is real — messages go with it via
    ON DELETE CASCADE — and is audited before it happens.
    """
    convo = await _load_accessible(db, user, conversation_id)

    role = await get_user_role_for_production(db, user, convo.production_id)
    if convo.created_by != user.id and ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
        raise HTTPException(
            status_code=403,
            detail="Only the author or a matter manager can delete a conversation",
        )

    await log_action(
        db, user, "chat_conversation_deleted", "chat_conversation",
        resource_id=str(convo.id), production_id=convo.production_id,
        details={
            "title": convo.title,
            "message_count": len(convo.messages),
            "created_by": convo.created_by,
        },
    )
    await db.delete(convo)
    await db.commit()
    return {"ok": True}
