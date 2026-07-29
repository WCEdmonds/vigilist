"""Saved-chat persistence: title derivation and user-turn writes.

Uses the repo's fake-session pattern — no DB, no network.
"""

import asyncio
import uuid

from app.models import ChatConversation, ChatMessage, User
from app.services.chat_history import derive_title, start_turn


class _FakeSession:
    """Minimal stand-in for AsyncSession covering get/add/flush."""

    def __init__(self, existing: ChatConversation | None = None):
        self.added: list[object] = []
        self._existing = existing

    async def get(self, _model, _pk):
        return self._existing

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        # A real flush INSERTs, which is what assigns the primary key. The
        # fake mirrors that so callers can read convo.id afterwards.
        for obj in self.added:
            if isinstance(obj, ChatConversation) and obj.id is None:
                obj.id = uuid.uuid4()


def _user():
    return User(id="uid-1", email="reviewer@firm.com", display_name="Reviewer")


def _added(session, cls):
    return [o for o in session.added if isinstance(o, cls)]


# ── derive_title ──

def test_derive_title_keeps_short_questions_intact():
    assert derive_title("Who signed the lease?") == "Who signed the lease?"


def test_derive_title_collapses_whitespace():
    assert derive_title("  Who   signed\nthe lease? ") == "Who signed the lease?"


def test_derive_title_truncates_on_a_word_boundary():
    title = derive_title("word " * 40)
    assert len(title) <= 61  # 60 chars plus the ellipsis
    assert title.endswith("…")
    # Truncation must not leave a half-word before the ellipsis.
    assert not title[:-1].endswith("wor")


# ── start_turn ──

def test_start_turn_without_production_saves_nothing():
    session = _FakeSession()
    result = asyncio.run(
        start_turn(session, _user(), None, None, "Anything?", [])
    )
    assert result is None
    assert session.added == []


def test_start_turn_creates_conversation_and_user_message():
    session = _FakeSession()
    convo_id = asyncio.run(
        start_turn(session, _user(), 7, None, "Who signed the lease?", ["doc-1"])
    )

    convos = _added(session, ChatConversation)
    messages = _added(session, ChatMessage)
    assert len(convos) == 1 and len(messages) == 1
    assert convo_id == convos[0].id
    assert convos[0].production_id == 7
    assert convos[0].created_by == "uid-1"
    assert convos[0].title == "Who signed the lease?"

    assert messages[0].role == "user"
    assert messages[0].user_id == "uid-1"       # attribution in a shared thread
    assert messages[0].content == "Who signed the lease?"
    assert messages[0].attachments == ["doc-1"]


def test_start_turn_continues_an_existing_conversation():
    existing = ChatConversation(
        id=uuid.uuid4(), production_id=7, created_by="someone-else", title="Earlier"
    )
    session = _FakeSession(existing=existing)

    convo_id = asyncio.run(
        start_turn(session, _user(), 7, str(existing.id), "Follow-up?", [])
    )

    # Shared history: continuing another user's thread is allowed, and the
    # existing title is left alone.
    assert convo_id == existing.id
    assert _added(session, ChatConversation) == []
    assert existing.title == "Earlier"
    assert _added(session, ChatMessage)[0].user_id == "uid-1"


def test_start_turn_ignores_a_conversation_from_another_production():
    foreign = ChatConversation(
        id=uuid.uuid4(), production_id=99, created_by="uid-1", title="Other matter"
    )
    session = _FakeSession(existing=foreign)

    convo_id = asyncio.run(
        start_turn(session, _user(), 7, str(foreign.id), "Question?", [])
    )

    # A conversation id from another matter must not be written into: start a
    # fresh thread in the requested production instead.
    fresh = _added(session, ChatConversation)
    assert len(fresh) == 1
    assert fresh[0].production_id == 7
    assert convo_id == fresh[0].id
    assert convo_id != foreign.id


def test_start_turn_tolerates_a_malformed_conversation_id():
    session = _FakeSession()
    convo_id = asyncio.run(
        start_turn(session, _user(), 7, "not-a-uuid", "Question?", [])
    )
    # A bad id starts a new thread rather than raising — a failed save must
    # never cost the user their answer.
    assert convo_id is not None
    assert len(_added(session, ChatConversation)) == 1
