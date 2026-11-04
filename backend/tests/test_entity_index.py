"""EntityIndex: parity with match_entity, tie-breaking, index maintenance,
persist_extraction with a shared index, and per-document cost at scale."""

import asyncio
import statistics
import time
import uuid
from types import SimpleNamespace

from app.models import Entity, EntityMention
from app.services.entity_extraction import persist_extraction
from app.services.entity_index import EntityIndex
from app.services.entity_resolution import match_entity, normalize_name
from tests.entity_corpus import make_candidates, make_entities
from tests.fakes import FakeResult, FakeSession


def _ns(**kw):
    return SimpleNamespace(**kw)


def _cand(name, etype="person", emails=None, forms=None):
    return {"name": name, "type": etype, "surface_forms": forms or [name], "role": None,
            "emails": emails or []}


def _apply_attach(entity, cand):
    """persist_extraction's attach mutation, on a plain object."""
    new_aliases = [f for f in cand["surface_forms"]
                   if normalize_name(f) != normalize_name(entity.canonical_name)
                   and f not in (entity.aliases or [])]
    if new_aliases:
        entity.aliases = list(entity.aliases or []) + new_aliases
    if cand["emails"]:
        attrs = dict(entity.attributes or {})
        attrs["emails"] = list(dict.fromkeys((attrs.get("emails") or []) + cand["emails"]))
        entity.attributes = attrs
    return new_aliases


# ── parity ──

def test_parity_with_match_entity_on_synthetic_corpus():
    existing = make_entities(3000, seed=7, factory=_ns)
    candidates = make_candidates(existing, 500, seed=11)
    old_list = list(existing)
    index = EntityIndex(existing)

    kinds = {"attach": 0, "suggest": 0, "create": 0}
    old_suggests = reproduced = 0
    for cand in candidates:
        old = match_entity(cand, old_list)
        new = index.match(cand)
        kinds[old[0]] += 1
        # Attach decisions (and which entity) must be identical.
        assert (old[0] == "attach") == (new[0] == "attach"), (cand, old, new)
        if old[0] == "attach":
            assert new[1].obj is old[1], (cand, old[1].canonical_name, new[1].canonical_name)
            entity = old[1]
            before = list(entity.aliases or [])
            new_aliases = _apply_attach(entity, cand)
            assert entity.aliases == before + new_aliases
            index.add_aliases(new[1], new_aliases)
            if cand["emails"]:
                index.set_emails(new[1], entity.attributes["emails"])
            continue
        if old[0] == "suggest":
            old_suggests += 1
            if new[0] == "suggest" and new[1].obj is old[1] and new[2] == old[2] and new[3] == old[3]:
                reproduced += 1
        else:
            assert new[0] == "create", (cand, new)  # never a suggestion old code wouldn't make
        created = _ns(id=uuid.uuid4(), entity_type=cand["type"], canonical_name=cand["name"],
                      aliases=list(cand["surface_forms"]),
                      attributes={"emails": cand["emails"]} if cand["emails"] else {})
        old_list.append(created)
        index.add(created)

    # The corpus must actually exercise every tier.
    print(f"\nparity: {kinds}; suggestions reproduced {reproduced}/{old_suggests}")
    assert kinds["attach"] > 100 and kinds["suggest"] > 30 and kinds["create"] > 5, kinds
    assert old_suggests and reproduced / old_suggests >= 0.95, (reproduced, old_suggests)


def test_exact_tie_returns_first_in_order():
    a = _ns(id=1, entity_type="person", canonical_name="Jorge Rivera", aliases=[], attributes={})
    b = _ns(id=2, entity_type="person", canonical_name="RIVERA, Jorge", aliases=[], attributes={})
    idx = EntityIndex([a, b])
    assert match_entity(_cand("jorge rivera"), [a, b])[1] is a
    assert idx.match(_cand("jorge rivera"))[1].obj is a


def test_alias_added_to_earlier_entity_wins_alias_tie():
    a = _ns(id=1, entity_type="person", canonical_name="Alan Barlow", aliases=[], attributes={})
    b = _ns(id=2, entity_type="person", canonical_name="Beth Barlow", aliases=["Barlow"], attributes={})
    idx = EntityIndex([a, b])
    assert idx.match(_cand("Barlow"))[1].obj is b
    a.aliases = ["Barlow"]
    idx.add_aliases(idx.get(1), ["Barlow"])
    assert match_entity(_cand("Barlow"), [a, b])[1] is a
    assert idx.match(_cand("Barlow"))[1].obj is a


def test_email_match_picks_earliest_entity_across_candidate_emails():
    a = _ns(id=1, entity_type="person", canonical_name="Alan Barlow", aliases=[],
            attributes={"emails": ["ab@acme.example"]})
    b = _ns(id=2, entity_type="person", canonical_name="Beth Draper", aliases=[],
            attributes={"emails": ["bd@acme.example"]})
    idx = EntityIndex([a, b])
    cand = _cand("Someone", emails=["BD@acme.example", "ab@ACME.example"])
    assert match_entity(cand, [a, b])[1] is a
    assert idx.match(cand)[1].obj is a


def test_typo_attach_picks_earliest_variant_and_respects_type():
    a = _ns(id=1, entity_type="person", canonical_name="Greta Holloway", aliases=[], attributes={})
    b = _ns(id=2, entity_type="person", canonical_name="Gretta Holloway", aliases=[], attributes={})
    org = _ns(id=3, entity_type="org", canonical_name="Greta Hollowway", aliases=[], attributes={})
    idx = EntityIndex([org, b, a])
    cand = _cand("Greta Hollowway")  # one indel from a; two edits from b
    assert match_entity(cand, [org, b, a])[1] is a
    assert idx.match(cand)[1].obj is a
    cand2 = _cand("Grett Holloway")  # one indel from both a and b: earliest (b) wins
    assert match_entity(cand2, [org, b, a])[1] is b
    assert idx.match(cand2)[1].obj is b


def test_add_makes_new_entity_matchable():
    idx = EntityIndex([])
    assert idx.match(_cand("Kara Lockhart"))[0] == "create"
    idx.add(_ns(id=9, entity_type="person", canonical_name="Kara Lockhart", aliases=["K. Lockhart"],
                attributes={"emails": ["kl@acme.example"]}))
    assert idx.match(_cand("kara lockhart"))[0] == "attach"
    assert idx.match(_cand("K. Lockhart"))[0] == "attach"
    assert idx.match(_cand("X", emails=["KL@acme.example"]))[0] == "attach"


# ── persist_extraction with a shared index ──

class _ByIdSession(FakeSession):
    """FakeSession whose entity SELECTs honour ``Entity.id IN (...)``:
    the index load returns every row, the by-id load only the asked rows."""

    def __init__(self, rows):
        super().__init__()
        self.rows = {e.id: e for e in rows}
        self.entity_selects = []

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if "FROM entities" not in sql:
            return await super().execute(stmt, params)
        wanted = None
        for v in stmt.compile().params.values():
            if isinstance(v, (list, tuple)):
                wanted = set(v)
        self.entity_selects.append(sql)
        if wanted is None:
            return FakeResult(items=list(self.rows.values()))
        return FakeResult(items=[self.rows[i] for i in wanted if i in self.rows])


def _entity(name, etype="person", aliases=None, mention_count=0):
    return Entity(id=uuid.uuid4(), production_id=1, entity_type=etype, canonical_name=name,
                  aliases=aliases or [], attributes={}, mention_count=mention_count)


TEXT = "Jorge Rivera emailed Ana Cruz about the Acme Corp merger. Rivera signed."


def test_shared_index_spans_sessions_and_loads_attached_rows_by_id():
    db_row = _entity("Jorge Rivera", mention_count=5)
    s1 = _ByIdSession([db_row])
    index = asyncio.run(EntityIndex.load(s1, 1))

    # Doc 1 (session 1): creates Ana Cruz, attaches Jorge Rivera.
    parsed = {"entities": [_cand("Jorge Rivera", forms=["Jorge Rivera"]), _cand("Ana Cruz")],
              "events": [], "relationships": []}
    asyncio.run(persist_extraction(s1, 1, uuid.uuid4(), TEXT, parsed, index=index))
    ana = next(o for o in s1.added if isinstance(o, Entity))
    assert db_row.mention_count == 6
    assert len(s1.entity_selects) == 1  # same session: no by-id reload

    # Doc 2 (a new session, as each apply document gets): fresh DB copies.
    row2 = Entity(id=db_row.id, production_id=1, entity_type="person", canonical_name="Jorge Rivera",
                  aliases=[], attributes={}, mention_count=6)
    ana2 = Entity(id=ana.id, production_id=1, entity_type="person", canonical_name="Ana Cruz",
                  aliases=["Ana Cruz"], attributes={}, mention_count=1)
    s2 = _ByIdSession([row2, ana2])
    parsed2 = {"entities": [_cand("ana cruz", forms=["Ana Cruz"]),
                            _cand("Jorge Rivera", forms=["Rivera"])],
               "events": [], "relationships": []}
    stats = asyncio.run(persist_extraction(s2, 1, uuid.uuid4(), TEXT, parsed2, index=index))
    assert stats["entities"] == 0  # both attached through the shared index
    assert len(s2.entity_selects) == 1 and "IN" in s2.entity_selects[0]
    assert ana2.mention_count == 2
    assert row2.aliases == ["Rivera"]  # mutation lands on this session's row
    assert index.get(db_row.id).aliases == ["Rivera"]  # and in the index
    # A bare "Rivera" now alias-attaches through the index.
    assert index.match(_cand("Rivera"))[0] == "attach"
    mentions = [o for o in s2.added if isinstance(o, EntityMention)]
    assert {m.entity_id for m in mentions} == {ana.id, db_row.id}


def test_shared_index_rebuilds_when_an_attached_entity_vanished():
    gone = _entity("Jorge Rivera")
    s1 = _ByIdSession([gone])
    index = asyncio.run(EntityIndex.load(s1, 1))
    s2 = _ByIdSession([])  # merged away by a reviewer since the index was built
    parsed = {"entities": [_cand("Jorge Rivera")], "events": [], "relationships": []}
    stats = asyncio.run(persist_extraction(s2, 1, uuid.uuid4(), TEXT, parsed, index=index))
    assert stats["entities"] == 1  # re-decided against the fresh index: created
    assert index.get(gone.id) is None


# ── performance ──

def _persist_timings(n_entities, n_docs, cands_per_doc, shared):
    rows = make_entities(n_entities, seed=3)
    pool = make_candidates(rows, n_docs * cands_per_doc, seed=5)
    db = _ByIdSession(rows)

    def add(obj, _orig=db.add):
        _orig(obj)
        if isinstance(obj, Entity):
            db.rows[obj.id] = obj
    db.add = add
    db.added = []
    index = asyncio.run(EntityIndex.load(db, 1)) if shared else None
    timings = []
    for d in range(n_docs):
        parsed = {"entities": pool[d * cands_per_doc:(d + 1) * cands_per_doc],
                  "events": [], "relationships": []}
        db.added.clear()
        t0 = time.perf_counter()
        asyncio.run(persist_extraction(db, 1, uuid.uuid4(), "", parsed, index=index))
        timings.append(time.perf_counter() - t0)
    return timings


def test_shared_index_per_document_cost_stays_flat_at_10k_entities():
    timings = _persist_timings(10_000, 500, 15, shared=True)
    first = statistics.median(timings[:50])
    last = statistics.median(timings[-50:])
    print(f"\nshared index: first-50 median {first * 1000:.2f} ms, last-50 median "
          f"{last * 1000:.2f} ms, overall median {statistics.median(timings) * 1000:.2f} ms")
    assert last <= 2 * first + 0.002, (first, last)
    assert statistics.median(timings) < 0.050
