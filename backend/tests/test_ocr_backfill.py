"""Backfill OCR of image documents the old inline-skip rule left blank."""

import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from PIL import Image

import app.routers.ingest as ri
from app.services import ocr_backfill as ob
from tests.fakes import FakeResult, FakeSession, FakeUser


def _img(side, pad=0):
    buf = io.BytesIO()
    Image.effect_noise((side, side), 90).save(buf, "PNG")
    return buf.getvalue() + b"\0" * pad


BIG = _img(400, 30_000)   # >= 20 KB, side 400
TINY = _img(40)           # small file and short side


def _doc(i, path, text=None):
    return SimpleNamespace(id=f"00000000-0000-0000-0000-{i:012d}", production_id=7,
                           native_path=path, text_content=text, file_type="image",
                           bates_begin=f"ACME{i:06d}")


class _Db(FakeSession):
    def __init__(self, docs):
        super().__init__()
        self.docs = docs
        self.rollbacks = 0

    async def execute(self, stmt, params=None):
        await super().execute(stmt, params)
        sql = str(stmt)
        if sql.lstrip().upper().startswith("SELECT") and "count(" not in sql.lower():
            return FakeResult(items=list(self.docs))
        return FakeResult()

    async def rollback(self):
        self.rollbacks += 1


def _patch(monkeypatch, blobs, ocr_text="Invoice 42"):
    calls = {"ocr": [], "embed": []}
    monkeypatch.setattr(ob, "download_bytes", lambda p: blobs[p])

    def fake_ocr(data):
        calls["ocr"].append(len(data))
        return ocr_text
    monkeypatch.setattr(ob, "ocr_bytes", fake_ocr)

    async def fake_embed(db, ids):
        calls["embed"].append(list(ids))
        return len(ids)
    monkeypatch.setattr(ob, "embed_documents", fake_embed)
    return calls


def test_batch_ocrs_large_skips_tiny_and_already_texted(monkeypatch):
    big, tiny, texted = _doc(1, "p/big.png"), _doc(2, "p/tiny.png"), _doc(3, "p/t.png", "has text")
    calls = _patch(monkeypatch, {"p/big.png": BIG, "p/tiny.png": TINY, "p/t.png": BIG})
    db = _Db([big, tiny, texted])
    res = asyncio.run(ob.run_batch(db, 7, None, 50))
    assert big.text_content == "Invoice 42"
    assert tiny.text_content in (None, "")
    assert texted.text_content == "has text"
    assert calls["ocr"] == [len(BIG)]            # Vision called once
    assert calls["embed"] == [[big.id]]
    assert any("text_search_vector" in s for s in db.executed)
    assert (res["ocrd"], res["skipped_tiny"], res["skipped_has_text"]) == (1, 1, 1)
    assert res["next_after"] == texted.id


def test_batch_failure_does_not_abort(monkeypatch):
    a, b = _doc(1, "p/a.png"), _doc(2, "p/b.png")
    calls = _patch(monkeypatch, {"p/b.png": BIG})   # a missing -> KeyError
    db = _Db([a, b])
    res = asyncio.run(ob.run_batch(db, 7, None, 50))
    assert res["failed"] == 1 and res["ocrd"] == 1
    assert b.text_content == "Invoice 42"


def test_empty_batch_is_done(monkeypatch):
    _patch(monkeypatch, {})
    res = asyncio.run(ob.run_batch(_Db([]), 7, None, 50))
    assert res["done"] is True


def _endpoint_env(monkeypatch, role):
    import app.dependencies as dep

    async def acc(db, user):
        return [7]

    async def rl(db, user, pid):
        return role
    monkeypatch.setattr(dep, "get_accessible_production_ids", acc)
    monkeypatch.setattr(dep, "get_user_role_for_production", rl)
    async def cnt(db, pid):
        return 200
    monkeypatch.setattr(ob, "count_candidates", cnt)
    enq = MagicMock()
    monkeypatch.setattr("app.services.tasks.enqueue_ocr_backfill_batch", enq)
    monkeypatch.setattr("app.services.tasks.is_configured", lambda: True)
    return enq


def test_dry_run_counts_and_estimates(monkeypatch):
    enq = _endpoint_env(monkeypatch, "manager")
    out = asyncio.run(ri.ocr_backfill(7, dry_run=True, db=FakeSession(), user=FakeUser()))
    assert out["candidates"] == 200
    assert out["estimated_usd"] == pytest.approx(0.3)
    assert out["upper_bound"] is True
    enq.assert_not_called()


def test_run_enqueues_first_batch(monkeypatch):
    enq = _endpoint_env(monkeypatch, "admin")
    out = asyncio.run(ri.ocr_backfill(7, dry_run=False, db=FakeSession(), user=FakeUser()))
    enq.assert_called_once_with(7, None)
    assert out["candidates"] == 200


def test_reviewer_forbidden(monkeypatch):
    enq = _endpoint_env(monkeypatch, "reviewer")
    with pytest.raises(HTTPException) as e:
        asyncio.run(ri.ocr_backfill(7, dry_run=False, db=FakeSession(), user=FakeUser()))
    assert e.value.status_code == 403
    enq.assert_not_called()


def test_no_access_is_404(monkeypatch):
    import app.dependencies as dep
    async def acc(db, user):
        return []
    monkeypatch.setattr(dep, "get_accessible_production_ids", acc)
    with pytest.raises(HTTPException) as e:
        asyncio.run(ri.ocr_backfill(7, dry_run=True, db=FakeSession(), user=FakeUser()))
    assert e.value.status_code == 404


def test_handler_chains_next_batch(monkeypatch):
    enq = MagicMock()
    monkeypatch.setattr("app.services.tasks.enqueue_ocr_backfill_batch", enq)
    async def rb(db, pid, after, limit=50):
        return {"done": False, "next_after": "abc", "ocrd": 1}
    monkeypatch.setattr(ob, "run_batch", rb)
    asyncio.run(ri.ocr_backfill_batch_handler({"production_id": 7, "after_id": None},
                                              db=FakeSession(), _verified=None))
    enq.assert_called_once_with(7, "abc")


def test_after_id_keyset_binds_as_uuid(monkeypatch):
    _patch(monkeypatch, {})
    db = _Db([])
    asyncio.run(ob.run_batch(db, 7, "00000000-0000-0000-0000-000000000009", 50))
    assert any("documents.id >" in s for s in db.executed)
