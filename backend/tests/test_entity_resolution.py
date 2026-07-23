"""Pure resolution-tier tests. Wrong merges mislead reviewers — every tier
transition here is a behavioral contract, not an implementation detail."""

from app.services.entity_resolution import match_entity, normalize_name


class E:
    def __init__(self, name, etype="person", aliases=None, emails=None):
        self.canonical_name = name
        self.entity_type = etype
        self.aliases = aliases or []
        self.attributes = {"emails": emails or []}


def test_normalize_strips_honorifics_case_punctuation():
    assert normalize_name("Dr. Jorge  Rivera, Esq.") == "jorge rivera"
    assert normalize_name("RIVERA, Jorge") == "jorge rivera"  # comma form swapped


def test_attach_on_exact_normalized_name():
    e = E("Jorge Rivera")
    assert match_entity({"name": "jorge rivera", "type": "person", "surface_forms": [], "emails": []}, [e]) == ("attach", e)


def test_attach_on_alias():
    e = E("Jorge Rivera", aliases=["J. Rivera"])
    assert match_entity({"name": "J. Rivera", "type": "person", "surface_forms": [], "emails": []}, [e]) == ("attach", e)


def test_attach_on_email_even_when_name_differs():
    e = E("Jorge Rivera", emails=["jr@acme.com"])
    assert match_entity({"name": "J.R.", "type": "person", "surface_forms": [], "emails": ["jr@acme.com"]}, [e]) == ("attach", e)


def test_suggest_on_initial_pattern():
    e = E("Jorge Rivera")
    kind, ent, score, rationale = match_entity(
        {"name": "J. Rivera", "type": "person", "surface_forms": [], "emails": []}, [e])
    assert kind == "suggest" and ent is e and score >= 0.8 and "initial" in rationale


def test_suggest_on_high_similarity():
    e = E("Jonathan Smithers")
    kind, ent, score, rationale = match_entity(
        {"name": "Jonathon Smithers", "type": "person", "surface_forms": [], "emails": []}, [e])
    assert kind == "suggest" and ent is e


def test_create_when_no_match():
    kind, ent = match_entity({"name": "Ana Cruz", "type": "person", "surface_forms": [], "emails": []},
                             [E("Jorge Rivera")])
    assert kind == "create" and ent is None


def test_never_matches_across_entity_types():
    e = E("Rivera", etype="org")
    kind, *_ = match_entity({"name": "Rivera", "type": "person", "surface_forms": [], "emails": []}, [e])
    assert kind == "create"
