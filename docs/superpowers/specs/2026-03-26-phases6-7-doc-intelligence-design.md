# Phases 6-7: Document Intelligence & AI-Powered Review — Design Spec

## Overview

Add near-duplicate detection, topic clustering, propagation coding, and infrastructure for document families, email threading, and communication analysis. Split into two tiers: Tier 1 works with current data (text + embeddings), Tier 2 activates when productions with richer metadata are ingested.

**Out of scope:** Scatter plot visualization, interactive network graph, redaction burn-in.

---

## Tier 1: Works with Current Data

### Near-Duplicate Detection

**Two-tier approach:**
- **Exact duplicates (95%+):** MinHash text fingerprinting on extracted text. Fast, catches forwarded emails, re-saved documents, identical content with different bates numbers.
- **Conceptual duplicates (80-95%):** Embedding cosine similarity using existing 512-dim Voyage AI vectors. Catches same-content-different-wording documents.

**Execution:** Batch job triggered per production via API endpoint (`POST /api/productions/{id}/detect-duplicates`). Manager+ role required.

**Algorithm:**
1. For each document with text, compute a MinHash signature (128 permutations) from word 3-grams
2. Compare all pairs using LSH (locality-sensitive hashing) with banding to find 95%+ candidates
3. For embedding-based similarity, use pgvector's `<=>` cosine distance operator with a threshold query: for each document, find others within 0.20 distance (0.80+ similarity). This avoids O(n^2) pairwise comparison by leveraging the HNSW index. For productions over 5,000 docs, process in batches of 500.
4. Group connected components into duplicate groups (if A≈B and B≈C, then {A,B,C} is one group)
5. Store results in `duplicate_groups` and `document_duplicates` tables. The `similarity` field stores the maximum similarity this document has to any other member of the group.

**Shared utility:** Extract the document-embedding averaging logic from `sampling.py` into a shared utility `get_document_embeddings(db, production_id) -> dict[str, list[float]]` for reuse by clustering, duplicate detection, and future features.

**Frontend:**
- In document viewer sidebar, show "Duplicates (3)" section if document has duplicates. Click to see the group members with similarity scores.
- In document list, add a filter: "Has duplicates" (boolean)
- Badge on document cards showing duplicate count

### Topic Clustering

**Backend:**
- K-means clustering on averaged document embeddings (already exists in `sampling.py`)
- Auto-detect number of clusters using silhouette score: for n < 50 try k=2..5, for n >= 50 try k=5..min(30, n/10), pick best score
- For each cluster, send the 3 most representative document excerpts to Claude (claude-haiku-4-5) and generate a 3-5 word topic label
- Store results in `document_clusters` and `document_cluster_assignments` tables
- Triggered via `POST /api/productions/{id}/cluster` endpoint. Manager+ role required.
- Re-runnable — clears old assignments before creating new ones

**Frontend:**
- "Topics" section on the main document list page (above the document table)
- Shows cluster labels as clickable cards with document count: e.g., "Student Attendance (45)", "Email Correspondence (120)", "Police Reports (8)"
- Clicking a topic filters the document list to that cluster's documents
- "Clear topic filter" button to return to all documents

### Propagation Coding

**Generic relationship-based bulk tagging:**
- When a user applies a tag to a document, check if the document has related documents (near-duplicates now, families/threads later)
- If relationships exist, show a non-blocking prompt below the tag bar: "Also tag 3 near-duplicates?" with Apply / Dismiss buttons
- Clicking Apply calls a new endpoint: `POST /api/documents/{id}/propagate-tag` with `{ tag_id, relationship_type }`
- Backend finds all related documents and applies the tag, logging each as an audit action with `{ propagated: true, source_document_id: "...", relationship_type: "duplicate" }` in details

**Relationship types (extensible):**
- `duplicate` — near-duplicate group members
- `family` — parent/child/attachment family (Tier 2)
- `thread` — email thread members (Tier 2)

---

## Tier 2: Activates with Rich Metadata

### Document Families

**Ingest-time parsing:**
- During DAT ingest, look for metadata fields matching common family field names: `Parent ID`, `ParentID`, `Parent Bates`, `Attachment Range`, `Group Identifier`, `FamilyID`, `Family ID`
- Case-insensitive, flexible matching
- If found, store `family_id` on the Document model (nullable VARCHAR)
- Documents sharing the same `family_id` within a production are a family

**Frontend:**
- In document viewer sidebar, show "Family (4)" section listing family members by bates number
- Click a family member to navigate to it
- Family indicator badge in document list

### Email Threading

**Ingest-time parsing:**
- Look for metadata fields: `Conversation Index`, `ConversationIndex`, `Message-ID`, `MessageID`, `In-Reply-To`, `InReplyTo`, `Thread-ID`
- Build thread trees: Message-ID → In-Reply-To creates parent-child relationships
- Conversation Index groups messages into threads
- Store `thread_id` on the Document model (nullable VARCHAR)

**Inclusive email detection:**
- Within a thread, the document with the longest text_content is likely the most inclusive (contains all prior messages)
- Mark with `is_inclusive` boolean flag
- Inclusive emails are review-priority — reviewers can skip non-inclusive messages in the same thread

**Frontend:**
- In document viewer sidebar, show "Thread (6)" section with thread messages in chronological order
- Inclusive email marked with a badge
- Thread members navigable by click

### Communication Analysis

**Ingest-time parsing:**
- Look for metadata fields: `From`, `To`, `CC`, `BCC`, `Date Sent`, `DateSent`, `Sent Date`
- Parse email addresses from these fields (handle "Name <email>" format and semicolon-separated lists)
- Store in `communications` table: production_id, from_email, to_email, cc (boolean), document_id, sent_date

**Frontend:**
- "Communications" tab/page accessible from the header (manager+ role)
- Table view: From | To | Messages | Date Range
- Sortable by message count (descending = most active communication pairs)
- Click a row to filter the document list to messages between those two addresses
- Search/filter by email address

---

## Data Model

### New Tables

```
duplicate_groups
  id              SERIAL PRIMARY KEY
  production_id   INTEGER FK → productions ON DELETE CASCADE
  type            VARCHAR(20) NOT NULL ('exact' | 'similar')
  created_at      TIMESTAMP DEFAULT now()

document_duplicates
  id              SERIAL PRIMARY KEY
  document_id     UUID FK → documents ON DELETE CASCADE
  group_id        INTEGER FK → duplicate_groups ON DELETE CASCADE
  similarity      FLOAT NOT NULL (0.0–1.0)
  UNIQUE(document_id, group_id)

document_clusters
  id              SERIAL PRIMARY KEY
  production_id   INTEGER FK → productions ON DELETE CASCADE
  cluster_index   INTEGER NOT NULL
  label           VARCHAR(100)
  doc_count       INTEGER DEFAULT 0
  created_at      TIMESTAMP DEFAULT now()

document_cluster_assignments
  id              SERIAL PRIMARY KEY
  document_id     UUID FK → documents ON DELETE CASCADE
  cluster_id      INTEGER FK → document_clusters ON DELETE CASCADE
  UNIQUE(document_id)

communications
  id              SERIAL PRIMARY KEY
  production_id   INTEGER FK → productions ON DELETE CASCADE
  document_id     UUID FK → documents ON DELETE CASCADE
  from_email      VARCHAR(255) NOT NULL
  to_email        VARCHAR(255) NOT NULL
  is_cc           BOOLEAN DEFAULT false
  sent_date       TIMESTAMP NULL
  created_at      TIMESTAMP DEFAULT now()
```

### Document Model Changes

Add nullable columns (populated during ingest when metadata exists):
```
family_id       VARCHAR(255) NULL
thread_id       VARCHAR(255) NULL
is_inclusive    BOOLEAN DEFAULT false
```

Indexes:
- `ix_document_duplicates_document_id` on `document_duplicates(document_id)`
- `ix_document_duplicates_group_id` on `document_duplicates(group_id)`
- `ix_document_cluster_assignments_document_id` on `document_cluster_assignments(document_id)`
- `ix_documents_family_id` on `documents(family_id)` WHERE family_id IS NOT NULL
- `ix_documents_thread_id` on `documents(thread_id)` WHERE thread_id IS NOT NULL
- `ix_communications_production_id` on `communications(production_id)`
- `ix_communications_from_email` on `communications(from_email)`

---

## API Endpoints

### Tier 1

```
POST /api/productions/{id}/detect-duplicates
  → { status, exact_groups, similar_groups, total_documents_grouped }

POST /api/productions/{id}/cluster
  Body: { num_clusters?: number }  (optional, auto-detect if omitted)
  → { status, clusters: [{ id, label, doc_count }] }

GET /api/productions/{id}/clusters
  → [{ id, label, doc_count, cluster_index }]

GET /api/documents/{id}/duplicates
  → [{ document_id, bates_begin, title, similarity, type }]

GET /api/documents/{id}/relationships
  → { duplicates: [...], family: [...], thread: [...] }

POST /api/documents/{id}/propagate-tag
  Body: { tag_id, relationship_type: 'duplicate' | 'family' | 'thread' }
  → { tagged_count }
```

### Tier 2

```
GET /api/documents/{id}/family
  → [{ document_id, bates_begin, title, role: 'parent' | 'attachment' }]

GET /api/documents/{id}/thread
  → [{ document_id, bates_begin, title, sent_date, is_inclusive }]

GET /api/productions/{id}/communications
  Query: ?sort=count&search=email
  → [{ from_email, to_email, message_count, first_date, last_date }]
```

---

## Frontend Components

### Tier 1

**TopicGroups.tsx** — clickable topic cards above the document list. Shows cluster labels with doc counts. Click filters the document list by cluster_id.

**DuplicatesSidebar section** — in document viewer left sidebar, below Pins. Shows duplicate group members with similarity percentage. Click navigates to that document.

**PropagationPrompt** — inline prompt in the tag bar area: "Also tag N near-duplicates?" with Apply/Dismiss. Appears briefly after tagging, auto-dismisses after 5 seconds if no action.

### Tier 2

**FamilySidebar section** — in document viewer left sidebar. Lists family members.

**ThreadSidebar section** — in document viewer left sidebar. Lists thread members chronologically with inclusive badge.

**CommunicationsTable.tsx** — modal or page showing sender-recipient pairs. Table with sort/filter.

---

## Implementation Order

1. Data model + migration (all tables + Document columns)
2. Near-duplicate detection backend (MinHash + embedding similarity)
3. Topic clustering backend (k-means + Claude labeling)
4. Frontend: TopicGroups + DuplicatesSidebar
5. Propagation coding (backend + frontend prompt)
6. Family/thread parsing in ingest (Tier 2 — backend only, activates with metadata)
7. Communications parsing in ingest (Tier 2 — backend only)
8. Frontend: Family/Thread/Communications UI (Tier 2)

---

## Dependencies

- `datasketch` Python package for MinHash/LSH
- Existing: `numpy`, `pgvector`, `voyageai`, `anthropic`
- No new frontend libraries needed (topic groups are simple cards, tables are existing patterns)

---

## Edge Cases

- **Documents without text:** Skip in duplicate detection and clustering. Show "No text available" in topic view.
- **Documents without embeddings:** Skip in embedding-based similarity and clustering. MinHash still works for exact dupes.
- **Small productions (<10 docs):** Clustering auto-selects k=2-3. Still useful for grouping.
- **Re-running detection:** Clears previous results before creating new ones (idempotent).
- **Cross-production:** Duplicates and clusters are always scoped to a single production. No cross-production grouping.
- **Inclusive email heuristic:** Longest-text is a first-pass approximation. Future refinement: text containment analysis (does message B contain message A's text?).

## Job Execution Model

Duplicate detection and clustering are background tasks that can take minutes for large productions. Use the existing `IngestJob` pattern: return a job ID immediately, run processing in a background task, poll for status via `GET /api/ingest/{job_id}/status`. For small productions (<500 docs), synchronous execution is acceptable.
