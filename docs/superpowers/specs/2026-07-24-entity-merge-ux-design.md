# Entity Merge UX — Smart Typo Auto-Merge + Bulk Confirm + Winner Choice

**Date:** 2026-07-24
**Status:** Approved (decisions captured in conversation)
**Branch:** `feat/entity-merge-ux` (off origin/main). No migrations.

## Decisions

1. **Smart typo auto-merge** — auto-merge only the safe typo class (single inserted/deleted character within one token, rest of the name identical): "Lynell Lyles" ↔ "Lynelle Lyles". Substitutions stay queued (Joan/John, Andersen/Anderson could be genuinely different).
2. **Bulk confirm** — select multiple suggestions in the queue and merge/dismiss them at once.
3. **Winner choice** — the reviewer picks which spelling becomes canonical (default = more frequent, retained loser spelling becomes an alias). All merges reversible via existing undo.
4. **Clear the current queue** — a retroactive action to auto-merge the safe-typo pairs already sitting as pending suggestions (SCHLEGEL is already extracted, so the extraction-time rule alone won't clear them).

## Task M1 — Backend: typo rule + retroactive auto-resolve

**Files:** `backend/app/services/entity_resolution.py`, `backend/app/routers/entities.py`, tests.

Add to `entity_resolution.py`:

```python
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
    if len(ta) != len(tb):
        return False
    diffs = [(x, y) for x, y in zip(ta, tb) if x != y]
    if len(diffs) != 1:
        return False
    x, y = diffs[0]
    if max(len(x), len(y)) < min_token_len:
        return False
    return _is_single_indel(x, y)
```

In `match_entity`, after the tier-1 attach checks (exact/alias/email) and BEFORE the tier-2 suggest loop, add a typo-attach pass over same-type existing entities: if `is_typo_variant(cand_norm, normalize_name(e.canonical_name))` → return `("attach", e)`. (New surface form is appended as an alias by persist, same as other attaches.) Tests: lynell/lynelle → attach; joan/john → NOT attach (falls to suggest/create); andersen/anderson → NOT attach; short tokens (jon/jan) → NOT attach; cross-type never attaches.

**Retroactive endpoint** `POST /api/productions/{id}/merge-suggestions/auto-resolve-typos` (manager+): for each pending suggestion in the production, load both entities; if `is_typo_variant(normalize_name(a.canonical_name), normalize_name(b.canonical_name))` → `merge_entities` (winner = higher mention_count) and the suggestion resolves accepted; return `{merged: n}`. Reuse the atomic/adversarially-reviewed merge path; never raise on one bad pair (skip + continue). Fake-session tests: a typo pair auto-merges, a substitution pair is left pending, scoping 404.

## Task M2 — Frontend: bulk confirm + winner choice + clear-typos button

**Files:** `frontend/src/components/EntitiesView.tsx`, `frontend/src/api/client.ts` (+ `autoResolveTypos(productionId)`).

Suggestion queue changes (the "Possible duplicates" card):
- **Per-row keeper choice:** each row shows both names; the more-frequent one is pre-selected as keeper (radio/highlight); clicking the other flips it. Determines winner for that row's merge.
- **Row checkbox** + header "select all"; a sticky action bar: **"Merge selected (N)"** (merges each selected row via `mergeEntities(chosenWinner, chosenLoser)`, Promise.all, partial-failure surfaced) and **"Dismiss selected (N)"** (rejects each). Refresh after.
- **"Auto-merge obvious typos (N)"** button calling the retroactive endpoint, then refresh — one click clears the safe-typo backlog.
- Keep single-row "Same — merge"/"Different" as the default action; bulk is additive.
- Errors surfaced inline (reuse `resolveError`).

## Task M3 — Verify + PR

Backend suite green (known pre-existing failure only); frontend build clean + lint baseline. Merge latest main. PR "feat(ontology): smart typo auto-merge + bulk/winner merge UX". Note: extraction-time rule + retroactive cleanup; all reversible.

## Notes / self-review

- Auto-merge picks the FIRST-seen entity as canonical at extraction time (candidate attaches to it); the retroactive path and manual path pick by frequency; both reversible + renamable. Acceptable; canonical-rename UI is a separate future nicety.
- `is_typo_variant` is intentionally conservative (indel-only). If real duplicates slip through as substitutions, they still reach the queue — no correctness loss, just a click.
