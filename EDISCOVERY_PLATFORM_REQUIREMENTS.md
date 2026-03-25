# Lightweight E-Discovery Viewer & Analyzer

## Requirements Specification

**Project:** Schlegel Document Review Platform
**Author:** Will Edmonds / Law Offices of Thiru Vignarajah
**Date:** March 24, 2026
**Target Builder:** Claude Code

---

## 1. Overview

Build a cloud-deployed, lightweight e-discovery document review platform for ingesting, searching, viewing, tagging, and analyzing litigation document productions. The platform replaces the need for a full Relativity license for a small legal team reviewing productions in active Maryland litigation.

The system must handle an initial 26GB production with additional rolling productions expected. It will serve a small team of 2–5 users with shared authentication.

---

## 2. Production Data Structure

Productions follow a standard Relativity export format with four root directories:

```
PRODUCTION_ROOT/
├── DATA/
│   ├── *.dat          # Delimited metadata load file (format TBD — see Section 2.1)
│   └── *.opt          # Opticon image cross-reference file
├── TEXT/
│   └── {subfolders}/
│       └── *.txt      # Extracted text files, one per document (keyed to Bates number)
├── NATIVES/
│   └── {subfolders}/
│       └── *.mp4      # Native video files
└── IMAGES/
    └── {subfolders}/
        └── *.tif      # TIFF page images (one per page, multi-page documents = multiple TIFs)
```

### 2.1 DAT File Format

**Confirmed: Concordance DAT format.** Analyzed from `SCHLEGEL_PROD001.dat`.

- **Encoding:** UTF-8 with BOM (EF BB BF).
- **Field wrapper:** `þ` (U+00FE, encoded as 0xC3 0xBE in UTF-8). Each field value is wrapped in `þ` on both sides.
- **Field separator:** `DC4` control character (0x14) between wrapped fields.
- **Row terminator:** `\r\n` (CRLF).
- **First row:** Header with field names.

**Fields in this production (5 columns):**

| Index | Field Name   | Description                          | Example Value                              |
|-------|-------------|--------------------------------------|--------------------------------------------|
| 0     | Begin Bates | Starting Bates number                | `SCHLEGEL 000001`                          |
| 1     | End Bates   | Ending Bates number                  | `SCHLEGEL 000006`                          |
| 2     | Page Count  | Number of pages in document          | `5`                                        |
| 3     | Text Link   | Relative path to extracted text file | `TEXT\\TEXT001\\SCHLEGEL 000002.txt`        |
| 4     | Native Link | Relative path to native file (if any)| `NATIVES\\NATIVE001\\SCHLEGEL 000007.MP4`  |

**Bates number format:** `SCHLEGEL NNNNNN` (prefix "SCHLEGEL", space, 6-digit zero-padded number). Note the space separator — not an underscore.

**Production inventory (from SCHLEGEL_PROD001.dat):**

- **Total documents:** 550
- **Multi-page documents:** 282
- **Documents with native files:** 52
  - MP4 video: 41
  - WAV audio: 5
  - MOV video: 6
- **Documents without natives (image + text only):** 498

**Important parser notes:**

- This is a "bare minimum" metadata production. The fields are limited to Bates range, page count, and file links. There are no date, author, custodian, email, or other substantive metadata fields. Additional metadata may be negotiated and received later — the parser and data model must handle additional columns appearing in future productions without code changes.
- Native file paths use backslash separators (`\\`). The parser must normalize these to the host OS path separator.
- The `Native Link` field is empty for documents without native files.
- Future productions may use a different Bates prefix or numbering scheme. The parser should not hardcode the prefix.

### 2.2 OPT File Format

**Confirmed from `SCHLEGEL_PROD001.opt`.** Standard Opticon format, comma-delimited, no header row. 7 fields per row:

| Index | Field       | Description                                      | Example Value                              |
|-------|------------|--------------------------------------------------|--------------------------------------------|
| 0     | Bates #    | Bates number for this page image                 | `SCHLEGEL 000002`                          |
| 1     | Volume     | Production volume name                           | `SCHLEGEL PROD001`                         |
| 2     | Image Path | Relative path to the TIFF image file             | `IMAGES\\IMG001\\SCHLEGEL 000002.tif`      |
| 3     | Doc Break  | `Y` = first page of a new document; blank = continuation page | `Y`                          |
| 4     | Box Break  | Unused in this production (always blank)         |                                            |
| 5     | Folder Break | Unused in this production (always blank)       |                                            |
| 6     | Page Count | Total pages in the document (only on doc break rows) | `5`                                   |

**Production image inventory:**

- **Total page images (TIFF files):** 14,918
- **Documents (doc breaks):** 550 (matches the .dat file)
- **Bates range:** SCHLEGEL 000001 through SCHLEGEL 014918
- **Volume:** `SCHLEGEL PROD001` (single volume)
- **Image subfolders:** 8 subfolders (IMG001 through IMG008), each containing ~1,700–2,000 TIFFs
- **Image path separator:** Backslash (`\\`). Parser must normalize to host OS separator.

**Parser logic for page grouping:** A row with `Doc Break = Y` starts a new document. All subsequent rows without `Y` in the Doc Break field are continuation pages of the same document, until the next `Y` row. This grouping determines the ordered `image_paths` array for each document record.

### 2.3 Text Files

One .txt file per document, filename matching the beginning Bates number (e.g., `SCHLEGEL 000001.txt`). Stored in subfolders under TEXT/ (e.g., `TEXT/TEXT001/`). These contain extracted/OCR text and are the primary corpus for full-text search indexing.

### 2.4 Native Files

Video and audio files stored in subfolders under NATIVES/ (e.g., `NATIVES/NATIVE001/`). Linked to documents via the `Native Link` field in the .dat file. File types in this production include MP4 (41 files), MOV (6 files), and WAV (5 files). The viewer must support streaming playback for all three formats.

### 2.5 Image Files

Single-page TIFF images stored in subfolders under IMAGES/. The .opt file maps Bates numbers to image paths and defines page groupings. These are the primary visual rendering of each document.

---

## 3. Ingest Pipeline

### 3.1 Upload & Storage

- Accept production data as a zip archive or direct folder upload to cloud storage (S3 or equivalent).
- Support incremental ingestion of rolling productions without re-processing existing data.
- Each production should be tracked as a distinct set (e.g., "Schlegel Master Prod", "Schlegel Wallace Prod") with the ability to search across or within specific productions.

### 3.2 Parsing

1. Parse the .dat file to extract all metadata fields into a structured database (Postgres recommended).
2. Parse the .opt file to build the Bates-to-image mapping and page groupings.
3. Index extracted text files for full-text search (see Section 5).
4. Link native files to document records via the metadata native file path field.
5. Validate referential integrity: flag any Bates numbers in the .dat that lack corresponding text files, images, or natives.

### 3.3 Data Model

Each **document** record should contain:

- `doc_id` (internal primary key — UUID or auto-increment, NOT Bates number)
- `production_id` (which production set it belongs to — **required** because Bates numbers may be duplicated across productions)
- **Unique constraint:** `(production_id, bates_begin)` — this is the composite natural key since Bates numbers are NOT globally unique
- All metadata fields from the .dat file, stored as key-value pairs or in a JSONB column for flexibility (field names vary across productions and additional metadata fields may arrive in future overlay files)
- `bates_begin` and `bates_end` (indexed, but not unique on their own)
- `page_count`
- `has_redactions` (boolean, from metadata if available)
- `text_content` (full extracted text, or reference to text file)
- `native_path` (path to native file if applicable)
- `image_paths` (ordered array of TIFF paths for this document)
- Review coding fields (see Section 7)

---

## 4. Document Viewer

### 4.1 Image Viewer

- Render TIFF page images in-browser. Convert TIFFs to JPEG/PNG or use a tiled viewer for performance.
- Page-by-page navigation with keyboard shortcuts (arrow keys, Page Up/Down).
- Zoom, rotate, and fit-to-width controls.
- Page count indicator (e.g., "Page 3 of 12").
- Jump to specific page number.

### 4.2 Text Panel

- Side-by-side panel showing the extracted text for the current document.
- Search term highlighting within the text panel.
- Copy-to-clipboard functionality.

### 4.3 Native/Video Viewer

- In-browser MP4 streaming with standard playback controls (play, pause, scrub, volume, fullscreen).
- Tab or toggle to switch between image view and native view for documents that have both.

### 4.4 Metadata Panel

- Collapsible panel displaying all metadata fields for the current document.
- Fields displayed as a key-value list, with field names from the .dat header row.

### 4.5 Navigation

- Previous / Next document buttons with keyboard shortcuts.
- Jump to Bates number.
- Breadcrumb showing current position in search results or document list.
- Persistent filter/search state when navigating between documents.

---

## 5. Search

### 5.1 Plain Text Search

- Full-text search across all extracted text content.
- Boolean operators: AND, OR, NOT, with parenthetical grouping.
- Phrase search using quotation marks (e.g., `"contract termination"`).
- Wildcard support: `*` for suffix matching (e.g., `negligen*`).
- Proximity search: `NEAR/N` operator (e.g., `breach NEAR/5 contract`).
- Search results should display: Bates range, document date (if available), snippet with highlighted terms, page count.
- Result count and sort options (by relevance, by Bates number, by date).

### 5.2 Advanced Search (Metadata Filters)

- Filter by any metadata field present in the .dat file.
- Date range filters (support various date field formats).
- Bates range filter.
- File type filter.
- Has Redactions filter (boolean).
- Tag/coding status filter (see Section 7).
- Production set filter.
- Combine metadata filters with full-text search.
- Save and name search queries for reuse.

### 5.3 AI-Enhanced Search

Powered by an LLM (Claude API via Anthropic). Three capabilities:

#### 5.3.1 Natural Language Search

- Accept plain English queries such as "emails discussing the settlement deadline in November" or "documents where Schlegel discusses redaction decisions."
- The system translates the natural language query into a combination of full-text search terms and metadata filters, executes the search, and returns ranked results.
- Implementation: Use embeddings (e.g., via a vector database like pgvector or Pinecone) over the extracted text corpus. On query, embed the natural language input and retrieve the top-N semantically similar document chunks, then return the parent documents ranked by relevance.

#### 5.3.2 Document Summarization

- From the document viewer, a "Summarize" button sends the document's extracted text to the Claude API and returns a concise summary (2–4 sentences).
- For long documents, chunk and summarize progressively.
- Summaries should be cached so they don't re-run on repeat views.
- Display summaries in the metadata panel or as an overlay.

#### 5.3.3 Find Similar Documents

- From any document in the viewer, a "Find Similar" button retrieves the top-N documents most semantically similar to the current document.
- Uses the same vector embedding infrastructure as natural language search.
- Display results as a ranked list with similarity scores and snippets.

---

## 6. Search Implementation Notes

- **Recommended full-text engine:** PostgreSQL with `tsvector`/`tsquery` for boolean and proximity search, or Elasticsearch/OpenSearch if production volume exceeds Postgres FTS performance limits.
- **Recommended vector store:** pgvector extension (keeps everything in one database) or a dedicated service like Pinecone.
- **Embedding model:** Use a cost-effective embedding model (e.g., Anthropic's or OpenAI's embedding endpoints, or an open-source model like `all-MiniLM-L6-v2` via sentence-transformers for self-hosted).
- **Chunking strategy:** Split extracted text into ~500-token overlapping chunks for embedding. Store chunk-to-document mappings for retrieval.

---

## 7. Document Review & Coding

### 7.1 Tagging

- Configurable tag categories. Default set:
  - **Responsiveness:** Responsive, Not Responsive, Needs Further Review
  - **Privilege:** Privileged, Not Privileged, Needs Privilege Review
  - **Issues:** Custom issue tags (user-defined, e.g., "Redaction Dispute", "Metadata Gap", "Key Document")
  - **Hot Document:** Boolean flag for key documents
- Tags are applied per-document.
- Bulk tagging: select multiple documents from a search result list and apply tags in batch.
- Tag history: record who applied what tag and when (timestamp + shared user label).

### 7.2 Notes & Comments

- Free-text notes field per document.
- Multiple notes per document, each timestamped.
- Notes are searchable via the advanced search interface.

### 7.3 Coding Keyboard Shortcuts

- Single-key shortcuts for common tags (e.g., `R` = Responsive, `P` = Privileged, `H` = Hot Document).
- After tagging, auto-advance to next document in the current result set.

### 7.4 Coding Layouts

- Configurable coding panels per review stage (e.g., First-Pass Responsiveness, Privilege Review, QC Review).
- Each layout defines which tag categories, fields, and validation rules are visible.
- Layouts are assigned to review stages — reviewers see only the coding panel relevant to their current task.
- Validation rules can enforce required fields before advancing (e.g., responsiveness decision is mandatory before moving to the next document).
- Administrators can create, edit, and reorder layouts.

### 7.5 Bulk Coding with Propagation

- Select multiple documents from search results and apply tags in batch (extends 7.1 bulk tagging).
- **Family propagation:** When tagging a parent document, optionally propagate the tag to all family members (attachments, embedded files).
- **Thread propagation:** When tagging an inclusive email, optionally propagate to all earlier messages in the thread.
- **Near-duplicate propagation:** When tagging a document, optionally propagate to all near-duplicates.
- Propagation is opt-in per action, with a confirmation dialog showing affected document count.

### 7.6 Markup & Annotations

- Highlight text passages in the text panel with color-coded highlights.
- Add margin annotations / sticky notes anchored to specific page locations on document images.
- Annotations are visible to all reviewers with attribution (who, when).
- Filter documents by "has annotations" in search.
- Annotations are preserved across sessions and included in audit trail.

### 7.7 Redactions

- Draw rectangular redaction boxes on document page images.
- Each redaction requires a reason/label (e.g., "Attorney-Client Privilege", "Work Product", "Privacy/PII").
- Redactions are stored as overlay metadata — original images are never modified.
- Toggle redaction visibility on/off in the viewer.
- Redactions are burned into images at export time (see Section 8).
- Redaction log export: list of all redactions with document, page, reason, and who applied them.

---

## 8. User Management & Access Control

### 8.1 Individual User Accounts

- Individual username/password accounts replace the shared login from Phase 1.
- Support for email-based login with password reset flow.
- Optional SSO integration via Google OAuth or SAML for firms with existing identity providers.

### 8.2 Roles & Permissions

- **Administrator:** Full access — manage users, matters, productions, coding layouts, system settings.
- **Project Manager:** Manage review queues, assign batches, view dashboards, run QC, export. Cannot manage users or system settings.
- **Reviewer:** Review assigned documents, apply tags/notes/annotations within assigned batches. Cannot access unassigned documents or admin functions.
- **Read-Only:** View documents and search, but cannot modify tags, notes, or annotations. Useful for attorneys supervising without actively coding.
- Permissions are scoped per matter (workspace) — a user can be a Reviewer on one matter and a Project Manager on another.

### 8.3 Audit Trail

- Log every significant user action with timestamp, user ID, document ID, and action details:
  - Login/logout
  - Document viewed
  - Tag applied/removed/changed
  - Note created/edited/deleted
  - Annotation created/edited/deleted
  - Redaction applied/removed
  - Search executed
  - Export performed
  - Batch assigned/completed
  - QC decision (agree/overturn)
- Audit logs are immutable (append-only) and exportable as CSV.
- Audit trail viewer in the admin UI with filtering by user, date range, action type, and document.

---

## 9. Review Workflow & Project Management

### 9.1 Matters (Workspaces)

- Each matter (case) is a separate workspace with its own productions, documents, tags, and review state.
- Users are assigned to matters with a specific role (see Section 8.2).
- Data is fully isolated between matters — no cross-matter document access.
- Dashboard showing all matters the current user has access to.

### 9.2 Review Queues & Batching

- Create named review queues scoped to a document set (e.g., "First-Pass Review", "Privilege Review of Responsive Docs").
- Queues are defined by a saved search or filter — documents matching the criteria populate the queue.
- **Batch assignment:** Divide a queue into batches of configurable size (e.g., 50 or 100 documents) and assign batches to individual reviewers.
- **Auto-assignment:** Optionally auto-assign the next available batch when a reviewer completes their current batch.
- **Checkout model:** While a batch is assigned to a reviewer, those documents are locked from other reviewers' queues to prevent duplicate work.
- Reviewers see only their assigned batch in the document viewer, with progress tracking (e.g., "Document 23 of 50").

### 9.3 Review Progress Dashboard

- **Project-level metrics:**
  - Total documents, documents reviewed, documents remaining, percent complete.
  - Breakdown by tag category (e.g., responsive vs. not responsive vs. needs further review).
  - Estimated time to completion based on current throughput.
- **Reviewer-level metrics:**
  - Documents reviewed per hour/day.
  - Current batch progress.
  - Tag distribution per reviewer (to spot potential outliers).
- **Visual charts:** Progress over time, reviewer comparison, tag distribution histograms.
- Real-time or near-real-time updates.

### 9.4 Quality Control (QC)

- **QC sampling:** Automatically or manually select a configurable percentage of reviewed documents for QC review (e.g., 10% random sample, or all documents tagged a certain way).
- **QC workflow:** QC reviewer sees the original reviewer's coding decisions and can agree or overturn with a reason.
- **Overturn tracking:** Dashboard showing overturn rate by reviewer, by tag category, and over time.
- **Escalation:** Documents flagged during QC can be escalated to a senior reviewer or project manager.
- **Reviewer agreement reports:** Compare coding decisions between two reviewers on overlapping document sets to measure inter-reviewer consistency (Cohen's kappa or simple agreement rate).

---

## 10. Document Intelligence

### 10.1 Document Family Grouping

- Parse family relationships from production metadata (if available in DAT fields like `Parent ID`, `Attachment Range`, or `Group Identifier`).
- Group parent documents with their attachments/embedded files as a family unit.
- Family navigation in the viewer: see all family members, jump between parent and attachments.
- Family-aware search: option to return all family members when any member matches.
- Family-aware tagging: option to propagate tags to all family members (see Section 7.5).

### 10.2 Email Threading

- Identify email threads using metadata fields (e.g., `Conversation Index`, `Message-ID`, `In-Reply-To`) or text-based analysis of email headers in extracted text.
- Build thread trees showing the conversation flow.
- **Inclusive email detection:** Identify the most inclusive message in each thread (the one that contains all prior content), allowing reviewers to prioritize reviewing inclusives only.
- Thread visualization in the viewer: display the thread tree with the current document highlighted.
- Thread-aware navigation: step through a thread chronologically.

### 10.3 Near-Duplicate Detection

- Compute document similarity using text fingerprinting (e.g., MinHash/LSH or simhash) during ingest or as a batch process.
- Group documents exceeding a configurable similarity threshold (e.g., 90%) as near-duplicates.
- Near-duplicate indicator in the viewer showing the duplicate group and similarity scores.
- Option to review near-duplicate groups together, coding one and propagating to the group.
- Filter search results to suppress or group near-duplicates.

### 10.4 Clustering & Conceptual Grouping

- Cluster the document corpus into topical groups using unsupervised methods (e.g., k-means or hierarchical clustering over document embeddings).
- Display clusters with auto-generated labels (top terms or LLM-generated topic names).
- Interactive cluster visualization (e.g., scatter plot or treemap) for corpus exploration.
- Assign clusters to review queues to route subject-matter experts to relevant topics.
- Re-cluster on demand as new productions are ingested.

### 10.5 Communication Analysis

- Build a communication network graph from email metadata (From, To, CC, BCC fields).
- Visualize sender-recipient relationships with edge weights representing message volume.
- Identify key custodians and communication patterns.
- Filter the graph by date range, tag status, or search terms.
- Click on a relationship edge to see the underlying documents.

---

## 11. Export

### 11.1 Tagged Set Export

- Export a filtered/tagged subset of documents as a production package:
  - Load file (.dat or .csv) containing metadata for the exported documents.
  - Corresponding image files (TIFFs or JPEGs) and/or native files.
  - Text files.
  - OPT file for the exported subset.
- Filter export by any combination of tags, search queries, or Bates ranges.
- **Redaction burn-in:** When exporting documents with redactions, burn redaction boxes into the exported images as black rectangles. Original un-redacted images are never included in redacted exports.

### 11.2 Report Export

- Export search results as CSV.
- Export tag summary report (counts by tag category).
- Export document list with metadata and applied tags as CSV or Excel.
- Export redaction log (document, page, coordinates, reason, applied by, timestamp).
- Export reviewer progress and QC reports.
- Export audit trail (filtered by date range, user, action type).

### 11.3 Privilege Log Export

- Generate a privilege log from documents tagged as Privileged.
- Columns: Bates range, date, from, to, cc, document type, subject/description, privilege basis.
- Auto-populate fields from document metadata where available.
- Allow manual editing of privilege log entries before export.
- Export as CSV or Excel.

---

## 12. Architecture

### 12.1 Deployment

- Cloud-deployed, accessible via browser from multiple machines.
- Recommended stack:
  - **Frontend:** React (Next.js or Vite) with a document viewer component.
  - **Backend:** Node.js (Express or Fastify) or Python (FastAPI).
  - **Database:** Cloud SQL for PostgreSQL (with pgvector extension enabled) or AlloyDB.
  - **File storage:** Google Cloud Storage (GCS). Use Standard storage class for frequently accessed files (images, text), Nearline for native video/audio files accessed less often.
  - **Hosting:** Google Cloud Run (containerized, auto-scaling, pay-per-use — ideal for small team with variable usage). Alternatively, a small GCE VM (e2-medium or e2-standard-2) if Cloud Run cold starts are problematic for the document viewer.
  - **Video/audio streaming:** Serve MP4/MOV/WAV files from GCS with signed URLs and range-request support (GCS supports this natively).
  - **Image serving:** Pre-convert TIFFs to JPEG/WebP on ingest, store in GCS. Optionally front with Cloud CDN for faster repeat loads.
  - **Container registry:** Artifact Registry for Docker images.
  - **Secrets:** Google Secret Manager for API keys (Claude API key, database credentials).

### 12.2 Authentication

- Phase 1: Shared login (single username/password) for initial development.
- Phase 2+: Individual user accounts with role-based access control (see Section 8).
- Protect with HTTPS.
- Optional SSO via Google OAuth, SAML, or Identity-Aware Proxy (IAP) on Cloud Run.

### 12.3 Performance Targets

- Full-text search should return results in under 2 seconds for the current corpus size.
- Document viewer should load the first page image in under 1 second.
- Ingest pipeline should process the initial 26GB production in under 2 hours.
- Support concurrent access by up to 50 reviewers without degradation.

---

## 13. Non-Functional Requirements

### 13.1 Security

- All data in transit encrypted via TLS (Cloud Run provides this by default).
- All data at rest encrypted (GCS encrypts at rest by default; Cloud SQL encrypts at rest by default).
- No public access to document storage — all file access via signed, expiring GCS URLs generated by the backend.
- Consider enabling VPC Service Controls if the firm has compliance requirements around data residency.
- This is attorney work product and potentially privileged material. Access controls are critical.
- Per-matter data isolation — users can only access matters they are assigned to.

### 13.2 Reliability

- Automated database backups (Cloud SQL provides automated daily backups with point-in-time recovery).
- File storage on GCS Standard class provides 99.999999999% annual durability.
- Application should handle ingest failures gracefully with retry logic and error reporting.

### 13.3 Cost Sensitivity

- Optimize for low-to-moderate monthly cost. Pricing should scale with usage (per-matter or per-GB).
- AI features (Claude API) should be usage-based with cost visibility.

---

## 14. Build Priorities

Build in this order. Each phase should be functional and testable before moving to the next.

### Phase 1: Ingest + View + Basic Search (done)

- DAT/OPT parser with auto-format detection
- Database schema and document model
- TIFF-to-JPEG conversion pipeline
- Document viewer (image + text + metadata panels)
- Full-text search (boolean, phrase, wildcard)
- Basic navigation (prev/next, jump to Bates)
- Shared auth

### Phase 2: Search + Core Review Workflow

- Frontend polish — production-ready UI
- Metadata field filters
- Combined text + metadata search
- Saved searches
- Tag/code documents with configurable tag categories
- Notes/comments
- Coding keyboard shortcuts and auto-advance
- Bulk tagging
- MP4/MOV/WAV media streaming

### Phase 3: User Management + Audit

- Individual user accounts (replace shared login)
- Role-based access control (Admin, Project Manager, Reviewer, Read-Only)
- Comprehensive audit trail (all user actions logged)
- Audit log viewer and export
- Password reset flow
- Optional SSO / Google OAuth integration

### Phase 4: Review Management + QC

- Multi-matter workspaces with data isolation
- Review queues and saved-search-driven document sets
- Batch creation and assignment to reviewers
- Batch checkout model (prevent duplicate work)
- Auto-assignment of next batch on completion
- Review progress dashboard (project-level and reviewer-level metrics)
- QC sampling workflows (random or targeted)
- QC agree/overturn with reasons
- Overturn rate tracking and reviewer agreement reports

### Phase 5: Coding Layouts + Annotations + Redactions

- Configurable coding layouts per review stage (first-pass, privilege, QC)
- Validation rules on coding panels (required fields before advance)
- Text highlighting and margin annotations on document images
- Annotation attribution and filtering
- Redaction tools (draw boxes on page images with reason labels)
- Redaction overlay storage (original images never modified)
- Redaction visibility toggle in viewer

### Phase 6: Document Intelligence

- Document family grouping (parse parent/attachment relationships from metadata)
- Family-aware navigation, search, and tagging propagation
- Email threading (via metadata or text-based header analysis)
- Inclusive email detection
- Thread visualization and thread-aware navigation
- Near-duplicate detection (MinHash/LSH or simhash)
- Near-duplicate grouping, indicators, and propagation coding
- Bulk coding with family/thread/near-duplicate propagation

### Phase 7: AI-Powered Review

- Vector embedding pipeline (chunk + embed all extracted text)
- Natural language search
- Document summarization (cached)
- Find Similar Documents
- Clustering and conceptual grouping with auto-generated labels
- Cluster visualization (scatter plot or treemap)
- Active learning / TAR (predictive coding for prioritized review)
- Communication analysis (network graph from email metadata)

### Phase 8: Export + Production

- Tagged set export with load file (.dat, .opt)
- Redaction burn-in on exported images
- Report exports (CSV, Excel) — search results, tag summaries, reviewer progress, QC reports
- Privilege log generation and export
- Audit trail export
- Rolling production ingest support
- Performance optimization and scalability tuning

---

## 15. Open Questions

1. ~~**DAT format:**~~ **RESOLVED.** Concordance DAT with þ/DC4 delimiters, UTF-8 with BOM. 5 fields: Begin Bates, End Bates, Page Count, Text Link, Native Link.
2. ~~**Bates numbering scheme:**~~ **PARTIALLY RESOLVED.** Format is `SCHLEGEL NNNNNN` (space-separated prefix + 6-digit zero-padded number). **CRITICAL: Bates numbers are NOT unique across productions.** The Wallace-specific production reuses the same Bates numbers as the Master production (this was flagged by Dorsey in the Schlegel document production email chain as a Relativity loading issue). Therefore, Bates number alone cannot serve as a unique document identifier. The data model must use a composite key of `(production_id, bates_begin)` or an internal UUID. The UI should always display which production set a document belongs to. This issue may or may not be resolved by Schlegel's team re-issuing with unique Bates numbers — design for the worst case.
3. **Expected total volume:** How many additional rolling productions are anticipated, and what's the estimated total corpus size? Current production is 550 documents / 26GB (bulk of storage is likely the 52 native video/audio files).
4. ~~**Domain/hosting preference:**~~ **RESOLVED.** Host on Google Cloud. Use Cloud Run + Cloud SQL + GCS.
5. **Existing infrastructure:** Any existing databases, servers, or services at the firm that could be leveraged?
6. **Metadata expansion:** This production has bare-minimum metadata (no dates, authors, custodians, email fields). If additional metadata is negotiated per the Dorsey ESI protocol discussion, the system must ingest overlay load files that add fields to existing document records. Confirm whether overlay files will use the same Concordance DAT format.
7. ~~**OPT file sample:**~~ **RESOLVED.** Standard Opticon format confirmed. 14,918 page images across 550 documents, 8 image subfolders (IMG001–IMG008). Single volume `SCHLEGEL PROD001`.
8. **WAV/MOV support:** The production includes WAV audio (5 files) and MOV video (6 files) in addition to MP4. The viewer needs playback support for all three. Confirm browser-native playback is acceptable (no need for server-side transcoding).
