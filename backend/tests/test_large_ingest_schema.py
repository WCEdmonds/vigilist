import importlib.util
import pathlib


def test_migration_is_import_safe_and_chained():
    p = next(pathlib.Path("alembic/versions").glob("e3n4o5p6q7r8_*.py"))
    spec = importlib.util.spec_from_file_location("m", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.down_revision == "d2l3m4n5o6p7"
    src = p.read_text()
    assert "from app" not in src and "import app" not in src


def test_models_have_new_columns():
    from app.models import IngestChunk, IngestJob, Production

    for c in ("survey", "selection", "items_total", "items_done", "items_failed"):
        assert c in IngestJob.__table__.c
    assert "ai_hold" in Production.__table__.c
    assert {
        "kind", "slices", "doc_ids", "type_filter", "status", "attempts", "parent_id",
    } <= set(IngestChunk.__table__.c.keys())
