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


def test_two_comma_person_name_normalizes():
    assert normalize_name("Rivera, Jorge, Jr.") == "jorge rivera"


def test_org_comma_suffix_normalizes():
    assert normalize_name("Acme Corp, Inc.") == "acme corp inc" == normalize_name("Acme Corp Inc")


def test_empty_or_honorific_only_candidate_creates():
    e = E("", aliases=[""])
    kind, ent = match_entity({"name": "", "type": "person", "surface_forms": [], "emails": []}, [e])
    assert (kind, ent) == ("create", None)
    kind, ent = match_entity({"name": "Esq.", "type": "person", "surface_forms": [], "emails": []}, [e])
    assert (kind, ent) == ("create", None)


def test_blank_email_never_attaches():
    e = E("Someone Else", emails=[""])
    kind, *_ = match_entity(
        {"name": "Totally Unrelated Name", "type": "person", "surface_forms": [], "emails": [""]}, [e])
    assert kind == "create"


def test_exact_name_beats_earlier_degenerate_match():
    e1 = E("Totally Different", emails=[""])
    e2 = E("Jorge Rivera")
    kind, ent = match_entity(
        {"name": "Jorge Rivera", "type": "person", "surface_forms": [], "emails": [""]}, [e1, e2])
    assert (kind, ent) == ("attach", e2)


def test_org_comma_form_attaches_to_noncomma_canonical():
    e = E("Acme Corp Inc", etype="org")
    kind, ent = match_entity(
        {"name": "Acme Corp, Inc.", "type": "org", "surface_forms": [], "emails": []}, [e])
    assert (kind, ent) == ("attach", e)


# ── Token-aware fuzzy suggestions (transcription/OCR variants) ──

def _cand(name, etype="org"):
    return {"name": name, "type": etype, "surface_forms": [], "emails": []}


def test_suggests_merge_for_transcribed_org_variants():
    school = E("Severna Park Elementary School", etype="org")
    for variant in ("Smyrna Park Elementary", "Smarter Park Elementary",
                    "Silverman Park School", "Elementary Park Elementary School"):
        decision = match_entity(_cand(variant), [school])
        assert decision[0] == "suggest", f"{variant!r} should suggest, got {decision[0]}"
        assert decision[1] is school


def test_suggests_merge_for_fuzzy_person_variant():
    katie = E("Katie Swistak")
    decision = match_entity(_cand("Catherine Swistak", "person"), [katie])
    assert decision[0] == "suggest"


def test_suggests_merge_for_bare_first_name_subset():
    perry = E("Perry Priem")
    decision = match_entity(_cand("Perry", "person"), [perry])
    assert decision[0] == "suggest"


def test_no_suggestion_when_only_generic_token_shared():
    smith = E("John Smith")
    assert match_entity(_cand("John Doe", "person"), [smith])[0] == "create"


def test_no_suggestion_without_any_shared_token():
    acme = E("Acme Corporation", etype="org")
    assert match_entity(_cand("Meridian Holdings"), [acme])[0] == "create"
