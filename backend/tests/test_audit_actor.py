"""resolve_audit_actor: picks the actor to attribute ambient (no-caller)
pipeline audit entries to.

Uses a fake session (no database), in the same spirit as
test_results_ownership.py / test_productions_list.py.
"""

import asyncio

from app.services.audit import resolve_audit_actor


class FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.email = f"{uid}@thirulaw.com"


class FakeProduction:
    def __init__(self, owner_id):
        self.owner_id = owner_id


class FakeSession:
    """Only needs to answer `db.get(User, owner_id)`."""

    def __init__(self, users):
        self._users = users

    async def get(self, model, key):
        return self._users.get(key)


def test_resolve_audit_actor_returns_the_owner():
    owner = FakeUser("u1")
    db = FakeSession({"u1": owner})
    production = FakeProduction(owner_id="u1")

    actor = asyncio.run(resolve_audit_actor(db, production))

    assert actor is owner


def test_resolve_audit_actor_returns_none_when_owner_id_is_none():
    db = FakeSession({})
    production = FakeProduction(owner_id=None)

    actor = asyncio.run(resolve_audit_actor(db, production))

    assert actor is None


def test_resolve_audit_actor_returns_none_when_owner_row_is_missing():
    # owner_id points at a user row that no longer exists (deleted account).
    db = FakeSession({})
    production = FakeProduction(owner_id="ghost")

    actor = asyncio.run(resolve_audit_actor(db, production))

    assert actor is None
