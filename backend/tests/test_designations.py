"""Confidentiality designations come from tags (customer: some documents are
Confidential, some Attorneys' Eyes Only, some both)."""

from app.services.designations import combine_designations

CONF, AEO = (1, "CONFIDENTIAL"), (2, "ATTORNEYS' EYES ONLY")


def test_single_designation():
    assert combine_designations([CONF]) == "CONFIDENTIAL"


def test_both_join_in_tag_creation_order():
    assert combine_designations([AEO, CONF]) == "CONFIDENTIAL – ATTORNEYS' EYES ONLY"


def test_none_without_designation_tags():
    assert combine_designations([]) is None


def test_duplicates_and_blanks_ignored():
    assert combine_designations([CONF, (3, "CONFIDENTIAL"), (4, "  ")]) == "CONFIDENTIAL"


def test_fits_the_item_column():
    long = [(i, "X" * 40) for i in range(5)]
    assert len(combine_designations(long)) <= 100


def test_tag_update_sets_and_clears_designation(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import app.routers.tags as rt
    from app.schemas import TagPrivilegeUpdate
    from tests.fakes import FakeSession, FakeUser

    audit = []

    async def fake_accessible(db, user):
        return [1]

    async def fake_role(db, user, pid):
        return "manager"

    async def fake_log(db, user, action, *a, **kw):
        audit.append((action, kw.get("details")))

    monkeypatch.setattr(rt, "get_accessible_production_ids", fake_accessible)
    monkeypatch.setattr(rt, "get_user_role_for_production", fake_role)
    monkeypatch.setattr(rt, "log_action", fake_log)
    tag = SimpleNamespace(id=7, name="Confidential", category="confidentiality", color="yellow",
                          keyboard_shortcut=None, production_id=None, is_privilege=False,
                          designation=None)
    db = FakeSession(get_objects={("Tag", 7): tag})

    out = asyncio.run(rt.update_tag_privilege(7, TagPrivilegeUpdate(designation=" CONFIDENTIAL "),
                                              db=db, user=FakeUser()))
    assert out.designation == "CONFIDENTIAL" and tag.is_privilege is False
    assert audit == [("tag_designation_set", {"designation": "CONFIDENTIAL"})]

    asyncio.run(rt.update_tag_privilege(7, TagPrivilegeUpdate(designation=None), db=db, user=FakeUser()))
    assert tag.designation is None


def test_manifest_counts_documents_per_legend():
    from types import SimpleNamespace

    from app.services.production_export import compute_manifest

    ps = SimpleNamespace(id=1, name="Vol 1", prefix="ACME", designation="CONFIDENTIAL",
                         image_format="pdf", locked_at=None, rendered_at=None, start_number=1)
    items = [SimpleNamespace(bates_begin=f"ACME{n:06d}", bates_end=f"ACME{n:06d}", pages=1,
                             disposition="produce", produce_native=False, output_path=None,
                             designation=d)
             for n, d in enumerate([None, "CONFIDENTIAL – ATTORNEYS' EYES ONLY", None], start=1)]
    counts = compute_manifest(ps, items)["counts"]
    assert counts["designations"] == {"CONFIDENTIAL": 2, "CONFIDENTIAL – ATTORNEYS' EYES ONLY": 1}
