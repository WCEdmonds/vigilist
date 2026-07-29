"""Persistence for saved AI chat threads.

The two writes are deliberately split around the model call. The user turn is
written and committed *before* streaming starts — the request's session is torn
down once StreamingResponse takes over the connection, so anything uncommitted
by then never lands. The assistant turn is written afterwards on a *fresh*
session.

That split is not incidental. Holding a pooled connection across a minutes-long
model call is exactly what Neon killed in the timeline review (#87) and the
ingest loop (#88); this module must never be "simplified" into doing both
writes on one long-lived session.
"""

import logging
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.models import ChatConversation, ChatMessage, User

logger = logging.getLogger(__name__)

_TITLE_MAX = 60


def derive_title(text: str) -> str:
    """A short, human-readable label from the opening question."""
    flat = " ".join(text.split())
    if len(flat) <= _TITLE_MAX:
        return flat
    # Prefer a word boundary so the label doesn't end mid-word.
    clipped = flat[:_TITLE_MAX].rsplit(" ", 1)[0] or flat[:_TITLE_MAX]
    return f"{clipped}…"


async def start_turn(
    db: AsyncSession,
    user: User,
    production_id: int | None,
    conversation_id: str | None,
    content: str,
    attachments: list[str],
) -> UUID | None:
    """Create or continue a conversation and append the user turn.

    Returns the conversation id, or None when there is nothing to attach the
    history to (an ungrounded chat with no production). The caller commits —
    this shares the transaction that also writes the audit entry.

    History is shared per matter, so continuing someone else's thread is
    allowed; only the production has to match, which prevents a conversation
    id from one matter being used to write into another.
    """
    if production_id is None:
        return None

    convo: ChatConversation | None = None
    if conversation_id:
        try:
            convo = await db.get(ChatConversation, UUID(str(conversation_id)))
        except (ValueError, AttributeError):
            convo = None
        # Silently start a new thread rather than 400 on a stale or foreign id:
        # a failed save must never cost the user their actual answer.
        if convo is not None and convo.production_id != production_id:
            convo = None

    if convo is None:
        convo = ChatConversation(
            production_id=production_id,
            created_by=user.id,
            title=derive_title(content),
        )
        db.add(convo)
        await db.flush()
    elif not convo.title:
        convo.title = derive_title(content)

    db.add(
        ChatMessage(
            conversation_id=convo.id,
            role="user",
            user_id=user.id,
            content=content,
            attachments=attachments,
        )
    )
    return convo.id


async def finish_turn(conversation_id: UUID, content: str) -> None:
    """Append the assistant turn on a fresh session.

    Best-effort: a history write must never surface as a chat failure, since
    the user has already received the answer by the time this runs.
    """
    if not content:
        return
    try:
        async with async_session_factory() as session:
            convo = await session.get(ChatConversation, conversation_id)
            if convo is None:  # deleted mid-stream
                return
            session.add(
                ChatMessage(
                    conversation_id=conversation_id,
                    role="assistant",
                    content=content,
                )
            )
            # Touch the parent so the history list sorts by real activity.
            convo.updated_at = func.now()
            await session.commit()
    except Exception:
        logger.warning(
            "Failed to persist assistant turn for conversation %s",
            conversation_id,
            exc_info=True,
        )
