"""Deterministic entity resolution tiers. No LLM in the loop.

Tier 1 (attach): normalized-name equality, known alias, shared email, or the
safe-typo class (single inserted/deleted character within one token of a
multi-token name — see is_typo_variant). These attach without human review.
Substitutions (Joan/John) and single-token names are NOT in tier 1; they
fall through to tier 2.
Tier 2 (suggest): initial-pattern, high string similarity, or token-aware
fuzzy similarity — creates a merge suggestion for a human; nothing in this
tier merges without review.
Tier 3 (create): everything else.
"""

import re
from difflib import SequenceMatcher

_HONORIFICS = {"mr", "mrs", "ms", "dr", "prof", "hon", "esq", "jr", "sr", "ii", "iii"}
_SUFFIX_TOKENS = _HONORIFICS | {"inc", "llc", "llp", "ltd", "lp", "plc", "pc", "pa", "co", "corp"}
_SIMILARITY_THRESHOLD = 0.85
_TOKEN_SIMILARITY_THRESHOLD = 0.70


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


def _token_similarity(a: str, b: str) -> float:
    """Order-insensitive, token-aware similarity for transcription variants.

    Whole-string SequenceMatcher misses names like "Smyrna Park Elementary"
    vs "Severna Park Elementary School" (~0.78): most tokens agree exactly
    while one is phonetic garble and one is missing. Here each token of the
    shorter name greedily pairs with its most similar counterpart; the mean
    pair score is discounted by how much of the longer name went unmatched.
    Gated on at least one exact shared token so unrelated names ("Meridian
    Holdings" / "Acme Corporation") never score at all.
    """
    ta, tb = a.split(), b.split()
    if not ta or not tb:
        return 0.0
    if len(ta) > len(tb):
        ta, tb = tb, ta
    if not (set(ta) & set(tb)):
        return 0.0
    used: set[int] = set()
    total = 0.0
    for t in ta:
        best, best_j = 0.0, None
        for j, u in enumerate(tb):
            if j in used:
                continue
            r = SequenceMatcher(None, t, u).ratio()
            if r > best:
                best, best_j = r, j
        if best_j is not None:
            used.add(best_j)
        total += best
    coverage = len(used) / len(tb)
    return (total / len(ta)) * (0.5 + 0.5 * coverage)


def _is_single_indel(x: str, y: str) -> bool:
    """True iff x and y differ by exactly one inserted/deleted character."""
    if abs(len(x) - len(y)) != 1:
        return False
    longer, shorter = (x, y) if len(x) > len(y) else (y, x)
    i = j = 0
    skipped = False
    while i < len(longer) and j < len(shorter):
        if longer[i] == shorter[j]:
            i += 1
            j += 1
        elif skipped:
            return False
        else:
            skipped = True
            i += 1
    return True


def is_typo_variant(a_norm: str, b_norm: str, min_token_len: int = 4) -> bool:
    """Safe auto-merge class: exactly one token differs, by a single indel,
    and that token is long enough to be distinctive. Excludes substitutions
    (Joan/John, Andersen/Anderson) which can be genuinely different identities."""
    if not a_norm or not b_norm or a_norm == b_norm:
        return False
    ta, tb = a_norm.split(), b_norm.split()
    if len(ta) < 2:
        # A single-token name has no anchoring token — the entire name IS
        # the "differing token" below, so a single-character indel there
        # (Rogers/Roger, Lyles/Lyle, Michele/Michelle, Grant/Grants) is just
        # as likely to be a different identity as a typo. Require at least
        # one other token to agree before trusting an indel as safe;
        # single-token pairs fall through to tier-2 suggest instead.
        return False
    if len(ta) != len(tb):
        return False
    diffs = [(x, y) for x, y in zip(ta, tb) if x != y]
    if len(diffs) != 1:
        return False
    x, y = diffs[0]
    if max(len(x), len(y)) < min_token_len:
        return False
    return _is_single_indel(x, y)


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

    # Pass 4: safe-typo auto-attach — single inserted/deleted character within
    # one token, rest of the name identical. Deliberately conservative:
    # substitutions (Joan/John, Andersen/Anderson) are excluded and fall
    # through to tier-2 suggest instead of auto-merging.
    if cand_norm:
        for e in same_type:
            if is_typo_variant(cand_norm, normalize_name(e.canonical_name)):
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
            token = _token_similarity(cand_norm, e_norm)
            if ratio >= _SIMILARITY_THRESHOLD:
                score, rationale = ratio, f'name similarity {ratio:.2f}: "{candidate["name"]}" ~ "{e.canonical_name}"'
            elif token >= _TOKEN_SIMILARITY_THRESHOLD:
                score, rationale = token, f'token similarity {token:.2f}: "{candidate["name"]}" ~ "{e.canonical_name}"'
            else:
                continue
        if best is None or score > best[0]:
            best = (score, e, rationale)

    if best is not None:
        return ("suggest", best[1], round(best[0], 3), best[2])
    return ("create", None)
