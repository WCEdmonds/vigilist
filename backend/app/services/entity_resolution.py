"""Deterministic entity resolution tiers. No LLM in the loop.

Tier 1 (attach): normalized-name equality, known alias, or shared email.
Tier 2 (suggest): initial-pattern or high string similarity — creates a
merge suggestion for a human; nothing merges silently.
Tier 3 (create): everything else.
"""

import re
from difflib import SequenceMatcher

_HONORIFICS = {"mr", "mrs", "ms", "dr", "prof", "hon", "esq", "jr", "sr", "ii", "iii"}
_SIMILARITY_THRESHOLD = 0.85


def normalize_name(name: str) -> str:
    """Lowercase, strip punctuation/honorifics, swap 'Last, First' to 'first last'."""
    s = name.strip().lower()
    if s.count(",") == 1:
        last, first = s.split(",")
        s = f"{first.strip()} {last.strip()}"
    s = re.sub(r"[^\w\s]", " ", s)
    tokens = [t for t in s.split() if t not in _HONORIFICS]
    return " ".join(tokens)


def _initial_pattern(a: str, b: str) -> bool:
    """'j rivera' vs 'jorge rivera' — same last token, first tokens agree on initial."""
    ta, tb = a.split(), b.split()
    if len(ta) < 2 or len(tb) < 2 or ta[-1] != tb[-1]:
        return False
    fa, fb = ta[0], tb[0]
    if fa == fb:
        return False  # would already be an exact match
    return (len(fa) == 1 and fb.startswith(fa)) or (len(fb) == 1 and fa.startswith(fb))


def match_entity(candidate: dict, existing: list) -> tuple:
    """Match one extracted candidate against a production's existing entities.

    Returns ("attach", entity) | ("suggest", entity, score, rationale) | ("create", None).
    Only entities of the same type are considered.
    """
    cand_norm = normalize_name(candidate["name"])
    cand_emails = set(candidate.get("emails") or [])
    same_type = [e for e in existing if e.entity_type == candidate["type"]]

    for e in same_type:
        if normalize_name(e.canonical_name) == cand_norm and cand_norm:
            return ("attach", e)
        if cand_norm in {normalize_name(a) for a in (e.aliases or [])}:
            return ("attach", e)
        if cand_emails & {em.lower() for em in (e.attributes or {}).get("emails", [])}:
            return ("attach", e)

    best = None  # (score, entity, rationale)
    for e in same_type:
        e_norm = normalize_name(e.canonical_name)
        if not e_norm or not cand_norm:
            continue
        if _initial_pattern(cand_norm, e_norm):
            score, rationale = 0.9, f'initial pattern: "{candidate["name"]}" ~ "{e.canonical_name}"'
        else:
            ratio = SequenceMatcher(None, cand_norm, e_norm).ratio()
            if ratio < _SIMILARITY_THRESHOLD:
                continue
            score, rationale = ratio, f'name similarity {ratio:.2f}: "{candidate["name"]}" ~ "{e.canonical_name}"'
        if best is None or score > best[0]:
            best = (score, e, rationale)

    if best is not None:
        return ("suggest", best[1], round(best[0], 3), best[2])
    return ("create", None)
