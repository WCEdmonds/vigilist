"""IDOR regression test for list_results.

list_results previously verified only that the caller had access to
`production_id`, never that `project_id` actually belongs to that
production — so any authenticated user with access to *any* production
could read AI review results from a project in a *different* production
by guessing/knowing its ID. This locks in the fix: mismatched
production_id/project_id must 404 before any results are queried.

Uses a fake session (no database), in the same spirit as
test_org_access.py / test_productions_list.py.
"""

import asyncio

import pytest
from fastapi import HTTPException

import app.routers.review as review_router


class FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.email = f"{uid}@thirulaw.com"


class FakeProject:
    def __init__(self, pid, production_id):
        self.id = pid
        self.production_id = production_id


class FakeSession:
    """Only needs to answer `db.get(ReviewProject, project_id)` — the
    ownership check happens before any `db.execute(...)` query is issued."""

    def __init__(self, objects):
        self._objects = objects

    async def get(self, model, key):
        return self._objects.get((model.__name__, key))

    async def execute(self, *_args, **_kwargs):
        raise AssertionError("list_results queried results before checking project ownership")


def test_list_results_404s_when_project_belongs_to_another_production(monkeypatch):
    async def fake_role(db, user, production_id):
        return "admin"

    monkeypatch.setattr(review_router, "get_user_role_for_production", fake_role)

    # Project 99 belongs to production 2, but the caller asks for it under
    # production 1 (a production they do have access to).
    project = FakeProject(99, production_id=2)
    db = FakeSession({("ReviewProject", 99): project})

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            review_router.list_results(
                production_id=1,
                project_id=99,
                db=db,
                user=FakeUser("u1"),
            )
        )
    assert exc.value.status_code == 404


def test_list_results_404s_when_project_missing(monkeypatch):
    async def fake_role(db, user, production_id):
        return "admin"

    monkeypatch.setattr(review_router, "get_user_role_for_production", fake_role)

    db = FakeSession({})

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            review_router.list_results(
                production_id=1,
                project_id=404,
                db=db,
                user=FakeUser("u1"),
            )
        )
    assert exc.value.status_code == 404
