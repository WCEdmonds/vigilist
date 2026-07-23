"""Deterministic entity resolution tiers. No LLM in the loop.

Tier 1 (attach): normalized-name equality, known alias, or shared email.
Tier 2 (suggest): initial-pattern or high string similarity — creates a
merge suggestion for a human; nothing merges silently.
Tier 3 (create): everything else.
"""

import re
from difflib import SequenceMatcher

_HONORIFICS = {"mr", "mrs", "ms", "dr", "prof", "hon", "esq", "jr", "sr", "ii", "iii"}
_SUFFIX_TOKENS = _HONORIFICS | {"inc", "llc", "llp", "ltd", "lp", "plc", "pc", "pa", "co", "corp"}
_SIMILARITY_THRESHOLD = 0.85


def _clean_tokens(part: str) -> list:
    return re.sub(r"[^\w\s]", " ", part).split()


def normalize_name(name: str) -> str:
    """Lowercase, strip punctuation/honorifics, swap 'Last, First' to 'first last'.

    Trailing comma-parts that are purely suffix tokens (honorifics like "Jr.",
    corporate suffixes like "Inc.") are peeled off before the Last/First swap
    decision, then rejoined afterward (honorifics are stripped by the final
    token filter; corporate suffixes survive it).
    """
    s = name.strip().lower()
    parts = [p.strip() for p in s.split(",") if p.strip()]
    suffix_parts = []
    while len(parts) > 1 and _clean_tokens(parts[-1]) and set(_clean_tokens(parts[-1])) <= _SUFFIX_TOKENS:
        suffix_parts.insert(0, parts.pop())
    if len(parts) == 2:
        s = f"{parts[1]} {parts[0]}"  # "Last, First" -> "first last"
    else:
        s = " ".join(parts)
    if suffix_parts:
        s = s + " " + " ".join(suffix_parts)  # corporate suffixes rejoin (honorifics die below)
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

    # Pass 1: exact normalized name match.
    if cand_norm:
        for e in same_type:
            if normalize_name(e.canonical_name) == cand_norm:
                return ("attach", e)

    # Pass 2: alias match — skip empty/degenerate aliases on both sides.
    if cand_norm:
        for e in same_type:
            alias_norms = {normalize_name(a) for a in (e.aliases or []) if a}
            if cand_norm in {a for a in alias_norms if a}:
                return ("attach", e)

    # Pass 3: shared email — only truthy, "@"-containing addresses count.
    cand_emails_valid = {em.lower() for em in cand_emails if em and "@" in em}
    if cand_emails_valid:
        for e in same_type:
            e_emails_valid = {
                em.lower() for em in (e.attributes or {}).get("emails", []) if em and "@" in em
            }
            if cand_emails_valid & e_emails_valid:
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
