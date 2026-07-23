# P2-3.5 — Production Validation: Conflicts + Audited Override

**Date:** 2026-07-22
**Phase:** 2, sub-project P2-3.5 (inserted after Relativity research)
**Depends on:** P2-1 (sets/lock), P1-4/5 (redaction QC, privilege), stacked on P2-3 (PR #40)
**Consumed by:** P2-4 (the wizard's Validate step renders this endpoint's output)

## Decision context (Relativity research, 2026-07-22)

Relativity's production console stages a snapshot, then runs a validation
step: conflicts against "Production Restrictions" (a saved search of
must-never-produce documents) must be explicitly removed or overridden, and
the override is recorded on the production set (who/when). That
surface-and-decide model — not silent derivation — is the defensibility
feature. We adapt it to our existing Phase-1 machinery:

- **QC gating (our strongest restriction):** a `redact_in_part` document
  whose redaction QC is not currently `approved` (P1-4/5 auto-invalidating
  status) is a conflict. Producing unapproved redactions is the
  malpractice-adjacent path this whole phase exists to close.
- **Privilege-produce conflicts:** a privilege-tagged document whose
  effective disposition is `produce` (an override forces it out clean).
- **No-images conflicts:** a non-withheld member with no page renditions —
  it would fail the render loudly; surface it before lock instead.
- **Override is explicit and audited**: recorded on the set
  (`conflicts_overridden_by/at`) AND in the audit log with conflict counts,
  mirroring Relativity's "Restriction override by / on" fields.

## 1. Data model (one migration, import-safe)

`production_sets`: `conflicts_overridden_by` String(128) nullable;
`conflicts_overridden_at` DateTime nullable.
`down_revision = "c9d0e1f2a3b4"`.

## 2. Service — `app/services/production_validation.py` (DB-aware)

```python
async def compute_conflicts(db, ps) -> dict
# Loads the set's member document ids, then per doc: privilege-tag presence,
# redaction count + latest change timestamp (max(coalesce(updated_at,
# created_at))), latest QC decision (decided_at desc, id desc — same
# tie-break as the QC queue), privilege_disposition override, image_paths.
# Classification per member (disposition = effective_disposition(...) or
# "produce" — derived live, so the check works on DRAFT sets before lock):
#   - redact_in_part and qc_status(...) != "approved"  -> "qc_pending"
#     (detail names the current qc status)
#   - privilege-tagged and disposition == "produce"    -> "privilege_produce"
#   - disposition != "withhold" and no image_paths     -> "no_images"
# Returns {"qc_pending": [...], "privilege_produce": [...],
#          "no_images": [...], "total": int}
# where each entry is {"document_id", "control_number", "detail"}.
```

Reuses `effective_disposition` and `qc_status` from `app.services.privilege`
— no duplicated rules. A document can appear in multiple conflict lists.

## 3. Endpoints — extend `app/routers/production_sets.py`

- `GET /production-sets/{set_id}/validation` — any role with access; works
  on draft AND locked sets (draft is the point: validate before lock).
  Returns `compute_conflicts` output.
- `POST /production-sets/{set_id}/lock` — now takes an optional body
  `{"override_conflicts": bool = false}`. Lock computes conflicts first:
  - conflicts and no override → **409** with the per-category counts in the
    error detail (the UI shows the validation panel).
  - conflicts and `override_conflicts: true` → proceed; set
    `conflicts_overridden_by = user.id`, `conflicts_overridden_at = now`;
    audit `production_set_conflicts_overridden` with counts.
  - no conflicts → proceed as before (override fields stay NULL).
- `ProductionSetOut` gains `conflicts_overridden_by: str | None`,
  `conflicts_overridden_at: datetime | None`.

## 4. Error handling

- Validation on a set you can't access → existing 403/404 conventions.
- Lock's existing gates (draft-only, non-empty) unchanged and checked first;
  conflict check runs after them.

## 5. Testing

- Service tests (`test_production_validation.py`, fake-session): each
  conflict category triggers correctly; approved-and-fresh QC produces no
  conflict; stale approval (redaction edited after decision) conflicts;
  withhold docs exempt from no-images; override-produce on privileged doc
  flags; clean set returns total 0.
- Endpoint tests (append to `test_production_set_endpoints.py`): validation
  endpoint returns service output; lock 409 with conflicts and no override
  (counts in detail); lock with `override_conflicts` proceeds and stamps
  `conflicts_overridden_by/at`; clean lock leaves override fields NULL.
  Existing lock tests gain responders for the new queries.
- Migration purity + single head; full suite green.

## Out of scope

- Custom restriction saved-searches (Relativity's general mechanism) — our
  three built-in checks cover the high-stakes cases; arbitrary restrictions
  can layer on later.
- UI (P2-4 renders the validation panel).
