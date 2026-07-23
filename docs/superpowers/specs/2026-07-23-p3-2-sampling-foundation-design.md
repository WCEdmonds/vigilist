# P3-2 — Statistical Sampling Foundation

**Date:** 2026-07-23
**Phase:** 3, sub-project 2 of 4
**Depends on:** tags (coding vehicle), P0-SP5 source scope
**Consumed by:** P3-3 TAR validation (control sets reuse the sample table +
stats module); QC acceptance testing.

## Decision context (approved 2026-07-23)

Defensibility claims about unreviewed documents are sampling claims. This
sub-project ships the math and the frozen-random-sample machinery; humans
code samples with ordinary tags; estimates read the coding back.

- **Stats are dependency-free** (no scipy): Wilson score intervals (the
  defensible standard for proportions, well-behaved at small p) with z for
  90/95/99% confidence; sample-size via normal approximation with finite
  population correction; acceptance = "upper Wilson bound of observed
  defect rate ≤ tolerable rate".
- **A sample is a frozen artifact**: a persisted random draw (doc-id list +
  draw parameters + purpose) — never a live query, so the denominator can't
  drift after coding starts. Purposes: `richness` | `acceptance` |
  `control` (P3-3 uses `control`).
- **Coding = tagging.** Estimates take a `tag_id` meaning "positive"
  (responsive for richness; DEFECT for acceptance) and count sample members
  bearing it. No new review UI.

## 1. Stats module — `app/services/sampling_stats.py` (pure)

```python
Z = {90: 1.6449, 95: 1.9599, 99: 2.5758}

def sample_size(population: int, confidence: int = 95,
                margin: float = 0.05, expected_rate: float = 0.5) -> int
# n0 = z^2 * p(1-p) / e^2, finite-population-corrected, ceil, min(population).

def wilson_ci(positives: int, n: int, confidence: int = 95)
    -> tuple[float, float, float]   # (rate, low, high); (0,0,0) when n == 0

def acceptance(defects: int, n: int, tolerable_rate: float,
               confidence: int = 95) -> dict
# {"accept": upper <= tolerable_rate, "rate", "upper_bound", "tolerable_rate"}
```

## 2. Data model (one migration `c5d6e7f8a9b0`, down_revision `a3b4c5d6e7f8`)

`samples`: `id` int PK; `production_id` FK CASCADE indexed; `name`
String(255); `purpose` String(20) (`richness|acceptance|control`); `params`
JSONB (size requested, confidence, margin, expected_rate, source_type,
population at draw); `document_ids` JSONB (the frozen draw); `created_by`;
`created_at`.

## 3. Endpoints — new `app/routers/sampling.py` (`/api`), registered in main

- `GET /sampling/sample-size?population&confidence&margin&expected_rate` —
  pure calculator (auth only; 422 on bad params: confidence not in
  90/95/99, margin/expected_rate out of (0,1), population < 1).
- `POST /productions/{pid}/samples` `{name, purpose, confidence?, margin?,
  expected_rate?, size?, source_type?}` — manager+, audited. Size = explicit
  `size` or computed from the calculator against the scoped population.
  Draws `ORDER BY random() LIMIT n` under the scope (source_type semantics
  as everywhere) and freezes ids+params. 422: bad purpose/params, empty
  population.
- `GET /productions/{pid}/samples` — list (any access); `GET /samples/{id}`
  — detail incl. ids count (not the raw id list in list view).
- `GET /samples/{id}/estimate?tag_id&confidence?` — richness/precision
  estimate: n, positives (members bearing tag), Wilson (rate, low, high),
  and extrapolation to the draw population from params.
- `GET /samples/{id}/acceptance?tag_id&tolerable&confidence?` — acceptance
  verdict per §1 (tag marks defects). 422 tolerable out of (0,1).
- `DELETE /samples/{id}` — manager+, audited.

## 4. Testing

Pure: sample_size vs known values (e.g. N=100000, 95/5 → 383; small-N
correction caps at population; expected_rate shrinks n), Wilson bounds vs
hand-checked values (incl. 0 and n positives, n=0), acceptance boundary
(upper == tolerable accepts; just over rejects). Endpoints: calculator
validation, draw freezes ids + params (fake responder for random query +
count), estimate counts tag intersection (responder for tag query),
acceptance verdict, role gates. Migration purity/head; full suite green.

## Out of scope

- Control-set workflow, recall/elusion (P3-3, reusing this table/module).
- Review-queue integration for coding samples (tags suffice; queue
  convenience can come with P3-3's UI).
- UI (follow-up panel alongside P3-3's).
