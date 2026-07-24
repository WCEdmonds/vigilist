"""Pure resolution-tier tests. Wrong merges mislead reviewers — every tier
transition here is a behavioral contract, not an implementation detail."""

from app.services.entity_resolution import is_typo_variant, match_entity, normalize_name


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


# --- is_typo_variant: safe single-indel auto-merge class --------------------

def test_typo_variant_single_indel_attaches():
    assert is_typo_variant("lynell lyles", "lynelle lyles") is True
    assert is_typo_variant("lynelle lyles", "lynell lyles") is True


def test_typo_variant_rejects_substitution_same_length():
    # Joan/John and Andersen/Anderson are equal-length substitutions, not
    # indels — these must stay queued for human review, not auto-merge.
    assert is_typo_variant("joan smith", "john smith") is False
    assert is_typo_variant("andersen law", "anderson law") is False


def test_typo_variant_rejects_short_differing_token():
    assert is_typo_variant("jon doe", "jan doe") is False


def test_typo_variant_rejects_identical_or_empty():
    assert is_typo_variant("jorge rivera", "jorge rivera") is False
    assert is_typo_variant("", "jorge rivera") is False
    assert is_typo_variant("jorge rivera", "") is False


def test_typo_variant_rejects_multiple_differing_tokens():
    assert is_typo_variant("jonn smithh", "john smith") is False


def test_typo_variant_rejects_different_token_counts():
    assert is_typo_variant("lynell lyles jr", "lynelle lyles") is False


def test_match_entity_attaches_on_typo_variant():
    e = E("Lynelle Lyles")
    kind, ent = match_entity(
        {"name": "Lynell Lyles", "type": "person", "surface_forms": [], "emails": []}, [e])
    assert (kind, ent) == ("attach", e)


def test_match_entity_does_not_attach_on_substitution():
    e = E("John Smith")
    kind, *_ = match_entity(
        {"name": "Joan Smith", "type": "person", "surface_forms": [], "emails": []}, [e])
    assert kind != "attach"


def test_match_entity_does_not_attach_on_org_substitution():
    e = E("Anderson Law", etype="org")
    kind, *_ = match_entity(
        {"name": "Andersen Law", "type": "org", "surface_forms": [], "emails": []}, [e])
    assert kind != "attach"


def test_match_entity_typo_variant_never_crosses_type():
    e = E("Lynelle Lyles", etype="org")
    kind, *_ = match_entity(
        {"name": "Lynell Lyles", "type": "person", "surface_forms": [], "emails": []}, [e])
    assert kind == "create"
