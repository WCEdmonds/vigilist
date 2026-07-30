# AI merge assistant — design

**Date:** 2026-07-30
**Status:** approved

## Problem

The merge docket fills with obvious duplicates that auto-merge refuses:

| Pair | ratio | auto-merge | suggested |
|---|---|---|---|
| Kennedy Viles / Kennedy Wiles | 0.923 | ✗ | ✓ |
| Very Rawford / Avery Crawford | 0.923 | ✗ | ✓ |
| Tate Streling / Tate Sterling | 0.923 | ✗ | ✓ |

Detection is not the gap — all three are already in the docket. `is_typo_variant`
refuses them for principled reasons: `Viles`/`Wiles` is a substitution (the
`Joan`/`John` class), `Very Rawford`/`Avery Crawford` differs in both tokens
(token similarity 0.000), and `Streling`/`Sterling` is a transposition rather
than an indel.

Nor can the threshold be loosened, because **string similarity cannot separate
the safe cases from the unsafe ones**:

- `Tate Streling` / `Tate Sterling` — 0.93, an OCR corruption, safe to merge
- `Elie Richards` / `Leslie Richards` — 0.93, plausibly two people, not safe

Identical scores, opposite answers. The distinguishing information is not in
the strings; it is in the documents.

## Approach

A batched model pass over pending suggestions that gathers the evidence a
reviewer would gather by hand, and returns one of three verdicts per pair.

Mirrors the AI timeline review — same button idiom, confidence gate, audit
snapshots, all-or-nothing transaction — rather than inventing a second
pattern for the same shape of problem.

### Three verdicts

| Verdict | Action |
|---|---|
| `merge` | Auto-applied when confidence ≥ **0.85**, through the existing merge path so the `EntityMerge` snapshot and `undo_merge` still work |
| `distinct` | Suggestion rejected through the existing reject path. Clears queue noise while merging nothing — no risk, real relief |
| `unclear` | Left pending, with the model's evidence and reasoning written to `rationale` so the decision can be made in the docket instead of by going hunting |

`unclear` is the verdict the `Elie Richards` case demands. That pair should be
neither merged nor dismissed; it should arrive annotated — *"Leslie appears on
40 documents, Elie on 2; they never co-occur; no shared email; plausible OCR
but unconfirmed"* — so a human decides in seconds.

### Evidence per pair

This is what makes the pass more than re-scoring a string:

- **mention and document counts for each side** — the minority-spelling
  signal (`Wiles` ×40 vs `Viles` ×2) that is invisible pair-by-pair, and the
  single most useful positive indicator
- **email addresses for each side.** Shared address ⇒ same person. *Different*
  addresses (`lrichards@` vs `erichards@`) ⇒ different people, and this
  replaces co-occurrence as the primary distinctness signal
- `attributes.role` for each — differing stated roles argue for distinct
  identities
- two mention snippets per side, so the model can see how each name is used

**Co-occurrence in the same document is NOT evidence of distinctness in this
corpus.** An OCR failure yields the correct spelling and the corruption inside
the same document — `Tate Sterling` on one page, `Tate Streling` on another.
Treating co-occurrence as a distinctness signal would reject exactly the
merges this feature exists to make. It is supplied to the model as neutral
context only, explicitly labelled as such, and there is **no code-level guard
on it**.

One call for the whole queue, not per pair: cheaper, and it lets the model see
the full cast, which is what surfaces the minority-spelling pattern.

### Prompt posture

The judgement that actually separates the two 0.93 pairs is **whether the
character difference is a plausible OCR confusion**, which is a genuine model
strength and is why this is not a string-distance problem:

- `Viles`/`Wiles` — `v`/`w` confusion, classic OCR
- `Streling`/`Sterling` — adjacent transposition
- `Rawford`/`Crawford` — dropped leading character
- `Elie`/`Leslie` — *not* a plausible single OCR error; a three-character
  divergence at the head of a given name. Two people until proven otherwise

So: conservative on **given-name** differences (`Elie`/`Leslie`, `Joan`/`John`,
`Erin`/`Erik`) absent corroboration. The safe class is an OCR-plausible
corruption with a matching given name and a lopsided mention count.

Differing email addresses outweigh any string similarity.

## Storage

No migration. `EntityMergeSuggestion` already has `score` (Float) and
`rationale` (Text); the pass overwrites both on pairs it leaves pending.

## API

`POST /api/productions/{id}/merge-review` — manager-gated, 409 while running,
`?force=true` escape, dispatched through Cloud Tasks like the timeline review.
Returns counts: merged, dismissed, left pending.

Audited as `entity_merge_review` with the counts, plus the existing per-merge
audit rows.

## Out of scope

- Widening `is_typo_variant`. Transposition and token-shift classes could be
  added deterministically, but that is a separate change with its own safety
  argument, and this pass covers them.
- Generating *new* candidate pairs. This judges the existing docket only.
- Cross-production merging.

## Testing

- verdict parsing: unknown verdict, missing confidence, malformed JSON all
  degrade to leaving the pair pending rather than raising
- the gate: 0.849 does not merge, 0.85 does
- `distinct` rejects rather than merges
- **co-occurrence does not block a merge** — a pair appearing in the same
  document is still merged at sufficient confidence. This is the regression
  guard for the original design error, where co-occurrence was treated as
  conclusive distinctness and would have rejected every OCR duplicate
- a pair already resolved by a human between dispatch and completion is
  skipped, not re-decided
