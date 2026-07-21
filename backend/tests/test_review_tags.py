"""Category->tag resolution for accepted/overridden AI decisions."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from app.services.review_tags import (
    CATEGORY_TAG_MAP,
    decision_to_category,
    resolve_tag_for_category,
)


def test_decision_to_category():
    assert decision_to_category("agree") is None
    assert decision_to_category("override_key_document") == "key_document"
    assert decision_to_category("override_custom_cat") == "custom_cat"
    assert decision_to_category("something_else") is None


def test_map_covers_default_categories():
    assert CATEGORY_TAG_MAP["relevant"] == ("Responsive", "responsiveness")
    assert CATEGORY_TAG_MAP["key_document"] == ("Key Document", "issues")


def _db_returning(tag):
    result = MagicMock()
    result.scalar_one_or_none.return_value = tag
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


def test_resolve_finds_seeded_tag():
    seeded = MagicMock()
    db = _db_returning(seeded)
    out = asyncio.run(resolve_tag_for_category(db, "relevant", []))
    assert out is seeded
    db.add.assert_not_called()


def test_resolve_creates_custom_tag_when_missing():
    db = _db_returning(None)
    db.add = MagicMock()
    db.flush = AsyncMock()
    out = asyncio.run(
        resolve_tag_for_category(db, "hot_topic", [{"name": "hot_topic", "color": "red"}])
    )
    db.add.assert_called_once()
    created = db.add.call_args[0][0]
    assert created.name == "Hot Topic"
    assert created.category == "custom"
    assert created.color == "red"
    assert out is created
