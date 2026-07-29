# Entity case roles — design

**Date:** 2026-07-28
**Status:** approved, step 1 in build

## Problem

The entities page picks principals purely by mention frequency:

```js
if (e.mention_count >= Math.max(6, topMentions * 0.3) && e.document_count >= 2)
  return 'principal';
```

On a civil matter built from a criminal case file, that surfaces the criminal
defendant, his defence attorneys, and the State of Maryland — and omits the
civil plaintiffs, who appear in the documents only rarely.

This is not a tuning problem. Frequency measures *who the documents are about*.
The corpus is evidence from one proceeding; the matter is a different
proceeding. No re-weighting of counts bridges that, because the information
that someone is a party to *this* case is not present in the counts.

The brief's `key_players` does not help: its prompt asks for people who
"recur", so it carries the same bias and would nominate the same cast.

## Approach

Introduce a **case role** — an assertion about a person's standing in the
matter, which is not derivable from the documents — and let it drive the
principal tier ahead of frequency.

Built in two steps. Step 1 is declarative and ships alone; step 2 adds an AI
proposer that writes into the same field. The storage, pinning, and tiering
work are identical either way, so step 1 is the substrate rather than a
throwaway. Building the AI first would mean shipping an auto-applied AI
judgment about who someone is, in a legal product, with no correction path.

## Step 1 — declared roles

### Storage

`entities.attributes.case_role`. `attributes` is already `JSONB NOT NULL
DEFAULT '{}'`, so **no migration**.

Vocabulary (fixed; changing these later requires migrating stored data):

| Value | Meaning |
|---|---|
| `plaintiff` | Party bringing this matter |
| `defendant` | Party defending this matter |
| `plaintiff_counsel` | Counsel of record for plaintiffs |
| `defense_counsel` | Counsel of record for defendants |
| `witness` | Testifying or deposed, not a party |
| *(absent)* | Unassigned — the state of every entity today |

Counsel is side-specific deliberately. The reported symptom is the defendant
*and his attorneys* occupying the tier; a flat `counsel` value could not later
distinguish them from plaintiff's counsel.

### Tiering

`tierOf` gains a check ahead of the frequency test: **any entity with a
`case_role` is a principal.** Frequency then fills the remaining slots.

Role-assigned entities are **exempt from `PRINCIPAL_CAP`** (currently 8). The
cap exists to stop frequency noise from flooding the tier, not to limit
deliberate designations — nine designated parties should yield nine.

Ordering within principals: plaintiff → defendant → plaintiff_counsel →
defense_counsel → witness → frequency-derived. The page opens on the caption.

Note this demotes nobody by rule. The criminal defendant is a genuine party
and keeps his card. The State of Maryland falls out naturally once real
parties occupy slots, because it has no role and must compete on frequency.

### Assignment

Two existing idioms, no new surface:

- the hover verbs on a cast row, beside rename/delete
- `EntityPanel`, so a role can be set while reading a profile

The picker offers `documents.source_party` values first. Those are curated,
human-verified party names (550 documents are designated to Matthew Schlegel)
that the entities page currently ignores. Suggestion only — never
auto-assigned.

### API

`PATCH /api/entities/{id}/case-role`, body `{"case_role": "plaintiff" | null}`.
Null clears. Validates against the vocabulary; audited as
`entity_case_role_set` with before/after, matching how rename is audited.
Gated the same way the other curation verbs are.

## Step 2 — AI proposer (not in this build)

A model reads `case_context` plus the entity list plus caption-bearing
documents, and proposes role assignments. Proposals surface in the review
docket beside merge suggestions — the same accept/reject interaction already
on this page — and write into `case_role` on acceptance. Nothing from step 1
is rewritten.

## Out of scope

- Changing how `mention_count` or the frequency thresholds work.
- Roles on the timeline, graph, or brief. `case_role` is available to them
  later, but this design only claims the entities page.
- Inferring roles from `source_party` automatically. It seeds the picker; a
  document producer is not necessarily a party.

## Testing

- `tierOf`: a role-assigned entity with 1 mention is a principal; an
  unassigned high-frequency entity still is; role-assigned entities exceed
  the cap; ordering is by role then frequency.
- Endpoint: valid value persists, invalid value rejected, null clears, audit
  row written, access gated.
