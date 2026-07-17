# Vigilist → E-Discovery Platform Parity Roadmap

**Goal:** Close the gap between Vigilist (a strong AI-forward *review* tool) and the
enterprise incumbents (Relativity, Everlaw, DISCO, Logikcull) so it can run a live matter
end-to-end and be sold to law firms.

**Method:** Each phase below is a *program*, not a single PR. Before building a phase, it gets
its own spec → implementation plan (brainstorming → writing-plans). Effort figures are
senior full-stack estimates and carry wide error bars (±50%).

## Current state (verified against the codebase, 2026-07-16)

**Strong today:** dual search (Postgres FTS + pgvector semantic), LLM review/classification,
clustering, near-dup (MinHash), corpus analysis, review workflow (queues/batches/QC/sampling),
tags/notes/annotations, audit log, orgs + role-based access, ingest of Relativity load files
(DAT/OPT), document/native/media viewers, CSV/ZIP export.

**Confirmed gaps (0 or stub-only in code):**
- Redaction — none.
- Privilege log generation — none ("privilege" is only a tag).
- Production/deliverable output (Bates endorsement, slip-sheets, load-file export) — none; DAT/OPT is import-only.
- Email family/threading — schema fields exist (`family_id`, `thread_id`, `is_inclusive`) but are never populated.
- Metadata model — no first-class custodian, sent/received dates, or file hash; only a generic `metadata_` JSONB.
- Hash-based dedup (MD5/SHA) — none (only MinHash near-dup).
- Search-term hit reports — none.
- Defensible TAR validation (control sets, recall/precision, elusion) — none; AI review is unvalidated LLM classification.
- Legal hold / custodian management / collection — none (entire left side of EDRM).
- Enterprise SSO/SAML — none (user auth is Firebase; OIDC is Cloud-Tasks-internal only).
- Formal security/compliance posture (SOC 2 / ISO 27001, retention, data residency) — not established.

---

## Phase 0 — Processing & Metadata Foundation
*The unglamorous plumbing everything else depends on. Do this first.*

**Why first:** Redaction logs, privilege logs, production sets, dedup, and defensibility all
require accurate per-document metadata and family relationships. Building them on the current
thin model would mean reworking them later.

**Scope**
- First-class `Document` fields (migration): `custodian`, `date_sent`, `date_received`,
  `date_created`, `date_modified` (timezone-normalized, stored UTC + original tz),
  `file_hash_md5`, `file_hash_sha256`, `source_path`, `file_type`, `extraction_status`,
  `extraction_error`.
- **Email container parsing**: PST/OST → messages; MSG/EML → message + attachments. Populate
  `family_id` (parent-child: email + its attachments) and `thread_id` (conversation), and
  compute `is_inclusive` (most-complete message in a thread).
- **Broaden extraction**: Apache Tika (or `textract`/`unstructured`) for 500+ file types,
  spreadsheets (incl. hidden content), embedded objects; capture extraction exceptions.
- **Exception handling**: corrupt/encrypted/password-protected files recorded with status +
  surfaced in an exceptions report rather than silently dropped.
- **Hash dedup**: exact dedup by SHA-256, both global and per-custodian; mark duplicates,
  keep a primary, preserve custodian list per hash.

**Depends on:** nothing (foundational).
**Rough effort:** 2–4 months (migration + ingest pipeline rewrite + Tika/PST integration).
**Defensibility:** hash + custodian + dates are the backbone of every downstream report.

---

## Phase 1 — Redaction & Privilege
*Gates the ability to withhold/redact before producing.*

**Scope**
- **Redaction**: draw redaction boxes on the image/PDF rendition; burn them into produced
  renditions (never expose underlying text/OCR for redacted regions); redaction reason codes.
- **Redaction QC** workflow (second-pass review of redactions before production).
- **Privilege log generation**: auto-build a privilege log from privilege tags + Phase-0
  metadata (author/recipients, date, doc type, privilege basis, description), exportable to
  PDF/CSV/DAT.
- Withhold vs. redact-in-part handling and tracking.

**Depends on:** Phase 0 (metadata for log fields; image renditions).
**Rough effort:** 1.5–3 months (image redaction burn-in is fiddly and high-stakes).
**Defensibility:** producing un-redacted privileged text is a malpractice-level failure —
this must be airtight, with tests proving redacted text never leaks via OCR/native/text export.

---

## Phase 2 — Production / Deliverable Output
*The actual "produce documents to opposing counsel" capability.*

**Scope**
- **Production set builder**: select documents (by tag/search/saved set), apply dedup,
  ordering, and numbering scheme.
- **Bates endorsement**: stamp sequential Bates numbers + confidentiality designations onto
  produced images; **slip-sheets** for withheld/redacted/technical-issue documents.
- **Format options**: native, image (TIFF/PDF), searchable PDF, text; per-field production.
- **Load-file export**: Concordance **DAT** (metadata) + **OPT** (image cross-reference),
  Relativity-compatible, so opposing counsel can load the production.
- Production manifest + validation (counts, gaps, Bates continuity).

**Depends on:** Phase 1 (redactions must be applied), Phase 0 (metadata for load files).
**Rough effort:** 1.5–3 months.
**Defensibility:** Bates continuity, correct redaction application, and load-file correctness
are checked by the receiving party — errors are visible and embarrassing.

---

## Phase 3 — Defensibility & Analytics
*Turns the AI review from "nice" into "defensible in court."*

**Scope**
- **Search-term hit reports**: per-term document/family counts across the corpus (for
  meet-and-confer and negotiated search-term lists).
- **Defensible TAR/CAL**: wrap the existing LLM review with control sets, richness
  estimation, recall/precision with confidence intervals, and elusion testing on the
  null set — the statistics courts expect to validate a predictive-coding process.
- **Statistical sampling** upgrades: defensible sample sizes, acceptance testing.
- **Chain of custody & processing reports**: ingest → processing → review → production
  lineage per document; hash verification; exceptions report.

**Depends on:** Phase 0 (hash/metadata), existing review workflow.
**Rough effort:** 1–2 months eng + statistical rigor/validation.
**Defensibility:** this *is* the defensibility layer; pairs the modern AI with the math that
makes it admissible.

---

## Phase 4 — Enterprise Trust (Security, Compliance, Admin)
*Gates selling to law firms. Mostly process, some engineering. Run in parallel from now.*

**Scope**
- **SOC 2 Type II** (and/or ISO 27001) readiness: control framework, policies, evidence
  collection, audit-log hardening (build on existing audit log), access reviews.
- **Data handling**: documented encryption at rest/in transit, data residency options,
  retention + legal-defensible deletion, tenant data isolation guarantees.
- **Enterprise SSO/SAML** (Okta/Azure AD/Google), SCIM provisioning, granular RBAC beyond the
  four current roles, IP allowlisting, session/MFA policies.
- Pen-test remediation, vulnerability management.

**Depends on:** nothing technical; gates sales, not features.
**Rough effort:** SOC 2 is a 6–12 month *calendar* effort (largely non-engineering);
SSO/SAML ~1 month eng.
**Note:** you can match every feature and still lose deals without this — for legal data it is
often the deciding factor.

---

## Phase 5 — Scale & Left-Side EDRM (Growth / Optional)
*Do as demand warrants; consider partnering instead of building.*

**Scope**
- **Scale**: dedicated search tier (OpenSearch/Elasticsearch) for multi-million-doc matters,
  ingest throughput, high reviewer concurrency, sharding/partitioning. Load-test before
  committing to the current Postgres+pgvector path at 10M+ docs.
- **Left side of EDRM** (optional / integrate): legal hold notices + acknowledgment tracking,
  custodian management, collection connectors (M365, Google Workspace, Slack, forensic).
- **Review polish**: customizable coding layouts/panels, persistent search-term highlighting
  in the viewer, more native file types, communication analysis (who-talked-to-whom),
  timelines, client-facing reporting dashboards.
- Load-file **overlay imports**, public API/integrations.

**Depends on:** product-market signal.
**Rough effort:** large and open-ended; scope to demand.

---

## Recommended sequencing

```
Phase 0 (foundation) ─┬─> Phase 1 (redaction/priv) ──> Phase 2 (production out)
                      └─> Phase 3 (defensibility)
Phase 4 (compliance) ──── run in PARALLEL, starting now (long calendar lead time)
Phase 5 (scale/left-side) ── as demand warrants
```

- **To run a real matter end-to-end:** Phases 0 → 1 → 2 are the hard gate. Phase 3 makes it
  *defensible*.
- **To sell to firms:** Phase 4 (start immediately — SOC 2 lead time is long).
- **Rough critical-path effort to "can replace incumbents on a mid-size matter":** ~6–11
  months of focused senior work for Phases 0–3, with Phase 4 running alongside.

## Next step
Pick the first phase to build (recommended: **Phase 0**). It then gets a full spec and a
task-by-task implementation plan before any code, decomposed as needed (Phase 0 alone is
likely 3–4 sub-projects: metadata model + migration, email/PST parsing, extraction/Tika,
hash dedup).
