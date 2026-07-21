"""Unit tests for GET /api/review/estimate and POST /api/review/auto-classify.

Uses a fake session (no database), in the same spirit as
test_results_ownership.py / test_productions_list.py.
"""

import asyncio

import pytest
from fastapi import HTTPException

import app.routers.review as review_router


class FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.email = f"{uid}@thirulaw.com"


class FakeProduction:
    def __init__(self, pid, case_context=None, owner_id="u1"):
        self.id = pid
        self.case_context = case_context
        self.owner_id = owner_id


class FakeOneResult:
    """Answers `result.one()` for the estimate endpoint's aggregate query."""

    def __init__(self, row):
        self._row = row

    def one(self):
        return self._row


class FakeScalarsResult:
    """Answers `result.scalars().first()` for the auto-classify existence check."""

    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None


class FakeBackgroundTasks:
    def add_task(self, *args, **kwargs):
        raise AssertionError("should not have scheduled a background task")


class FakeSession:
    def __init__(self, get_objects=None, exec_results=None):
        self._get_objects = get_objects or {}
        self._exec_results = list(exec_results or [])

    async def get(self, model, key):
        return self._get_objects.get((model.__name__, key))

    async def execute(self, _query):
        return self._exec_results.pop(0)


# ── GET /estimate/{production_id} ──

def test_estimate_returns_zeros_when_average_is_none(monkeypatch):
    """No documents have text_content -> COUNT is 0 and AVG is NULL. The
    endpoint must not crash on float(None); it should report all zeros."""

    async def fake_role(db, user, production_id):
        return "reviewer"

    monkeypatch.setattr(review_router, "get_user_role_for_production", fake_role)

    db = FakeSession(exec_results=[FakeOneResult((0, None))])

    out = asyncio.run(
        review_router.get_classification_estimate(
            production_id=1, db=db, user=FakeUser("u1")
        )
    )

    assert out == {
        "doc_count": 0,
        "est_input_tokens": 0,
        "est_output_tokens": 0,
        "est_usd": 0.0,
    }


# ── POST /auto-classify/{production_id} ──

def test_auto_classify_409s_when_initial_pass_already_exists(monkeypatch):
    async def fake_role(db, user, production_id):
        return "manager"

    monkeypatch.setattr(review_router, "get_user_role_for_production", fake_role)

    production = FakeProduction(1, case_context="Find docs about the incident")
    db = FakeSession(
        get_objects={("Production", 1): production},
        exec_results=[FakeScalarsResult([99])],
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            review_router.auto_classify(
                production_id=1,
                background_tasks=FakeBackgroundTasks(),
                db=db,
                user=FakeUser("u1"),
            )
        )

    assert exc.value.status_code == 409
    assert "Initial relevance pass" in exc.value.detail


def test_auto_classify_400s_when_case_context_is_empty(monkeypatch):
    async def fake_role(db, user, production_id):
        return "admin"

    monkeypatch.setattr(review_router, "get_user_role_for_production", fake_role)

    production = FakeProduction(1, case_context=None)
    db = FakeSession(
        get_objects={("Production", 1): production},
        exec_results=[FakeScalarsResult([])],
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            review_router.auto_classify(
                production_id=1,
                background_tasks=FakeBackgroundTasks(),
                db=db,
                user=FakeUser("u1"),
            )
        )

    assert exc.value.status_code == 400
