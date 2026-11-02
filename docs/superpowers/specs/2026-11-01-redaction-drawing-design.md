# Redaction drawing — design

Status: draft for review · 2026-11-01 · customer batch-coding item 7

## 1. Intent

The customer is producing an email collection. Some documents must go out with
parts blacked out (privileged sentences, personal data) rather than withheld
whole. Reviewers need to draw redaction boxes on a document in the viewer and
have exactly those regions, and nothing under them, leave the building blacked
out.

Two facts shape everything below:

- **Most documents in an email production have nothing to draw on.** Every
  email and every attachment (including PDF attachments) was ingested with no
  page images; only top-level PDF uploads are imaged. The viewer shows emails as
  HTML and attachments as natives. Production rendering also needs page images,
  so today a redacted email cannot be produced at all, and an *unredacted*
  imageless email in a production set fails the render too ("No readable page
  images"). The real work is an **imaging step**; the drawing UI sits on top.
- **Leaks are the failure mode that matters.** A redaction tool that draws
  perfectly but lets the covered text out through the produced text file, the
  load file's metadata columns, a native, or a stale render is worse than none.

The backend already stores redaction boxes, burns them into page images,
gates production on a redaction QC sign-off, and has tests for all of it. There
is no redaction UI at all.

## 2. Goals, non-goals, anti-goals

**Goals (this release)**
1. Image emails, PDF attachments/natives, and image attachments on demand, so
   they can be viewed as pages, redacted, and produced as images.
2. A Redact mode in the page viewer: draw, select, move, resize, delete boxes;
   choose a reason; redact a whole page; preview the page as it will be produced.
3. Production ships, for a redacted document: burned images, **text with the
   redacted words removed**, and load-file metadata with any field blanked
   whose header line is covered.
4. Close the existing production gaps that can leak redacted content (§6).

**Non-goals (decided)**
- Word / Excel / PowerPoint attachments. They need LibreOffice in the worker
  image (~400 MB) and spreadsheets image poorly. Phase 2. The viewer says so
  plainly instead of offering a tool that can't work.
- Search-term redaction ("redact every occurrence of X"). Phase 2. Imaging
  already captures per-word positions, so it is a clean follow-up.
- Redacting natives (e.g. editing a spreadsheet). Redacted documents never ship
  a native — unchanged.
- Free-hand shapes, highlighter-style redaction, colours other than black.

**Anti-goals (would make this a failure even if it "works")**
- Any path where text under a box reaches the production: image, text file,
  DAT metadata, native, or a render made from redactions that changed after QC.
- Boxes that drift: a box drawn on one rendering of a document must cover the
  same content on the produced page. A document's page images must therefore
  never change once it has redactions.
- Silent failure: a document that can't be imaged or redacted must say why in
  the UI and block (or clearly warn) at production validation, never ship
  something different from what the reviewer saw.

## 3. Constraints and what already exists

- **Redaction rows** (`models.Redaction`): `page_num` (1-based), `x_pct`,
  `y_pct`, `w_pct`, `h_pct` as **0–100 percentages** of the page image,
  top-left origin; `reason_code` from `services/redaction.py`
  (attorney_client, work_product, pii, phi, confidential, trade_secret,
  non_responsive, other); `note`; creator; `created_at`/`updated_at`.
- **API** (`routers/redactions.py`): list/create per document, update/delete
  per id. Reviewers create; only the creator or a manager edits/deletes;
  read-only users blocked. Validation in `validate_rect`.
- **Burn-in** (`services/redaction_render.burn_page`): solid black box plus a
  white reason label, on the stored image, percentages → pixels. No rotation
  logic: stored page images are already upright.
- **Redacted previews**: `GET /documents/{id}/image/{n}?redacted=1` and
  `/pdf?redacted=1` burn boxes; `/text?redacted=1` currently returns no text if
  the document has any redaction.
- **QC** (`POST /documents/{id}/redaction-qc`, manager+): approval stands only
  while the redaction count and latest change predate the decision.
  `production_validation` raises `qc_pending` until approved.
- **Disposition**: any redaction → `redact_in_part`, unless a manual override.
  Fixed on the set item at lock.
- **Production render** (`services/production_render.render_item`): reads the
  document's *current* redactions at render time; image-only PDF or TIFF output;
  stamps Bates/designation after burning.
- **Export** (`services/production_export`): no text file and empty `TEXTPATH`
  for `redact_in_part`; `REDACTED=Y`; withheld docs blank SUBJECT and FILENAME.
- **Imaging building blocks**: `ingest_pdf.iter_pdf_pages` (PyMuPDF, 250 DPI
  JPEG per page, word boxes from the text layer, OCR fallback for scanned
  pages) and `upload_page_assets` (stores `productions/{pid}/converted/
  {stem}_{page:04d}.jpg` plus an OCR sidecar `{t,x,y,w,h,c}` per word in the
  same 0–100 page percentages). `Document.image_paths` / `ocr_paths` are
  index-aligned.
- **HTML layout without a new dependency**: a spike confirmed PyMuPDF's
  `fitz.Story` lays a long HTML email (styled text, table, non-ASCII) onto
  letter pages in ~20 ms with a real text layer, so word boxes come for free.
- **Heavy work runs on the private `vigilist-worker` service via Cloud Tasks**
  (`services/tasks._enqueue`), never on the web service (incident 2026-10-07).
- **Hosting proxy**: long endpoints must not ride the Firebase `/api` rewrite
  (60 s buffer); imaging therefore runs as a task, polled by the UI.
- **Frontend**: no tests; lints red on main; React Compiler rules (no refs or
  setState in render). `ImagePanel` renders all pages in one scroll, each in a
  `position: relative` wrapper `#page-N`; zoom 0.25–4; rotation is CSS on the
  `<img>` only. `AnnotationOverlay` already maps pointer events to page
  percentages via `getBoundingClientRect()` — the template for drawing.
- **Real-data rule**: tests and examples use ACME-style fakes only.

## 4. Imaging ("Prepare pages")

### 4.1 What gets imaged

| Document | How | Pages |
|---|---|---|
| Email parent (`file_type == "email"`) | Header block + body HTML (or plain text) → PDF with `fitz.Story` → `iter_pdf_pages` | as laid out on US Letter, 0.75 in margins |
| PDF attachment or native PDF without images | stored native → `iter_pdf_pages` | the PDF's pages |
| Image attachment (png/jpg/gif/bmp/tiff) | normalized to JPEG; one page (multi-frame TIFF: one per frame) | 1+ |
| Word / Excel / PowerPoint / other | **not supported in this release** | — |

Imaging reuses `iter_pdf_pages` and `upload_page_assets` unchanged, so imaged
pages, word sidecars and storage layout are identical to ingested PDFs. Pages
whose text layer is empty (scanned PDFs) fall back to OCR exactly as ingest
does.

**Email rendering.** A fixed, plain header block, then the body:

```
From:     Alice Smith <alice@acme.example>
To:       …
Cc:       …            (omitted when empty; Bcc likewise)
Date:     2026-03-02 03:30 UTC
Subject:  RE: Q3 planning
Attachments: budget.xlsx; notes.pdf      (names only)
──────────────────────────────
<body>
```

- Body = `email_body_html` when present, else `text_content` as preformatted
  text. The HTML is sanitized before layout: scripts, styles that hide content,
  forms, iframes and remote resources removed; `cid:` images were already
  inlined as data URIs at ingest. No network fetches during rendering.
- Date shown in UTC with the zone written out (the produced image must not
  depend on who clicked the button). *(Builder's call: the exact header
  typography.)*
- **Header field regions.** While laying out page 1, record the bounding box of
  each header line (From, To, Cc, Bcc, Date, Subject, Attachments) as 0–100
  page percentages, stored on the document (§4.3). Export uses them to blank
  metadata (§6.3).

### 4.2 When imaging runs

- **On demand from the viewer**: an imageless, imageable document shows
  "Prepare pages for redaction". The button enqueues one Cloud Task; the viewer
  shows progress and switches to the page view when done.
- **In bulk**: "Prepare pages" on a selection / all-matching search / folder
  (same target shapes as bulk tagging and folders). One task per document,
  capped like bulk actions (50,000).
- **Before lock, from the production builder** — never at render. Bates
  numbers are assigned at lock from each document's page count, so a document
  imaged after lock would get pages without numbers of their own. The existing
  `no_images` validation conflict splits in two:
  - `needs_pages` — imageless but imageable (email, PDF, image). The builder
    shows "N documents need pages prepared" with **Prepare pages** (bulk, as
    above) and polls until done. Not overridable: lock refuses while any
    member is imageable-but-unimaged or imaging is queued/running.
  - `no_images` — unimageable (Office/other), as today, with a clearer label
    ("No page images — Word/Excel/PowerPoint can't be imaged yet").
    Overridable as today.
  *(This also fixes today's dead end for unredacted emails in image
  productions: prepare pages, then lock.)*

### 4.3 Data

- `documents.image_paths`, `ocr_paths`, `page_count` are filled exactly as for
  an ingested PDF.
- New nullable columns on `documents`:
  - `imaging_status` — `null` (never imaged / ingested with images), `queued`,
    `running`, `done`, `failed`.
  - `imaging_error` — short message on failure.
  - `imaged_from` — `email` | `pdf` | `image` (provenance; distinguishes
    "imaged on demand" from ingest).
  - `header_regions` — JSONB `{field: {page, x, y, w, h}}` for emails.
- One migration; parent = current head.

### 4.4 Stability rule

**A document's page images are frozen once it has a redaction.** Re-imaging
(manual retry, render-time imaging, a future re-OCR) refuses with 409 when the
document has any redaction rows. Redaction create also requires
`imaging_status` not in (`queued`, `running`). Validation of `page_num` uses
`len(image_paths)`, not `page_count`.

### 4.5 Failure handling

Imaging never raises out of the task: failures set `imaging_status = failed`
and `imaging_error`, and the viewer shows the message with Retry. A task that
dies mid-way leaves `running`; the UI treats `running` older than the Cloud
Tasks dispatch deadline as failed-and-retryable. Partial uploads are fine: the
document's paths are written only after every page uploaded, in one
transaction (so a document is never half-imaged).

## 5. Redact mode (viewer)

### 5.1 Where it lives

`DocumentViewer` → `ImagePanel`. For an imaged document the center pane offers
**Pages** (images) alongside the existing Email/Native view; Redact mode is
only available in Pages. A toolbar toggle **Redact** (shortcut `R`) enters the
mode; `Esc` leaves it.

### 5.2 Interactions

- **Draw**: press-drag on a page draws a box; release saves it (optimistic,
  rolled back with an error toast if the API refuses). Boxes under 0.5 % of the
  page in either dimension are ignored as stray clicks.
- **Reason**: a compact reason picker in the toolbar; new boxes use the current
  reason; default is the last one used in this session (initially
  `attorney_client`). Changing the reason with a box selected changes that box.
- **Select / move / resize**: click a box to select; drag to move; corner
  handles to resize; `Delete`/`Backspace` deletes; arrow keys nudge 0.2 %.
  Moves and resizes save on release. All edits clamp to the page.
- **Whole page**: "Redact page" creates a 0,0,100,100 box on the current page.
- **Overlay rendering**: an SVG over each page wrapper, like
  `AnnotationOverlay`, boxes drawn in percentages so zoom needs no maths.
  Unselected boxes are translucent black with the reason label so the reviewer
  can still read what they're covering; **Preview as produced** toggles to the
  server-burned image (`?redacted=1`).
- **Rotation**: drawing and editing are disabled while the page is rotated
  (the viewer's rotation is cosmetic CSS); the toolbar says "Rotate back to 0°
  to redact".
- **Side list**: a "Redactions" panel lists boxes by page with reason, author
  and time; clicking scrolls to the box. Shows the document's redaction QC
  status and, for managers, Approve / Reject.
- **Permissions** follow the API: read-only users see boxes but can't draw;
  reviewers can edit their own; managers can edit any. The UI disables what the
  API would refuse rather than failing on save.
- **Imageless documents** show the right message instead of the tool:
  "Prepare pages for redaction" (imageable), a progress state, a failure with
  Retry, or "Word, Excel and PowerPoint files can't be redacted yet — withhold
  or produce in full" (unsupported).

### 5.3 Client plumbing

- Client functions + types for redactions and redaction QC (none exist).
- `fetchImageBlob` gains `redacted` and includes it in its cache key.
- Fix the blob-URL revoke in `ImagePanel` cleanup that can hand dead URLs back
  from the cache.

## 6. Production correctness (leak fixes)

### 6.1 Render only what was QC'd

At **render**, for each item, compare the document's current redactions with
the set's lock time:

- the document has redactions but the item was locked as `produce` (or
  native), or
- any redaction was created/updated/deleted after `locked_at` (deletes are
  caught by snapshotting the redaction count per item at lock — new
  `production_set_items.redaction_count`),

→ the render **refuses that set** with a clear error naming the documents
("redactions changed after lock — unlock and lock again"). Nothing is
rendered from redactions that weren't part of the locked, QC'd state.

### 6.1a Unlock (new)

There is no unlock today, so the recovery above needs one:

- `POST /production-sets/{id}/unlock`, **manager+**, audited
  (`production_set_unlocked`, with the Bates range released).
- Allowed only while the set has **not been packaged** (`package_status` is
  null or failed). A packaged volume may have been delivered and never changes;
  the fix after packaging is a new set.
- Returns the set to `draft`: clears each item's snapshot (sort order, Bates
  range, pages, disposition, native flag, designation, redaction count,
  output path), the set's lock/render fields and `conflicts_overridden_*`, and
  deletes rendered outputs from storage (best-effort; a failed delete is
  logged, the files are orphaned under the set's prefix, not reused).
- Re-locking reassigns Bates numbers from the set's start number.
- Builder UI: **Unlock** on a locked, unpackaged set, with a confirm that says
  rendered pages will be discarded.

### 6.2 No silent unredacted override

New validation conflict `redactions_overridden`: a document has redactions but
its disposition override is `produce`. Overridable like the others (audited),
but never silent.

### 6.3 Load-file metadata

For `redact_in_part` emails, a DAT metadata column (FROM, TO, CC, BCC,
DATESENT, SUBJECT, FILENAME→attachments line) is **blanked** when any redaction
overlaps that field's recorded header region on its page. Overlap = any
intersection with positive area. Documents without `header_regions` (PDF/image
attachments) keep today's behaviour for metadata. *(Builder's call: whether to
also write a `REDACTEDFIELDS` column listing what was blanked; recommended.)*
The DAT has no BCC column today; add one (blanked by the same rule).

**Attachment names (decided by the user).** When the parent's
"Attachments:" line is covered, its attachments still ship their own
FILENAME (and are produced in their own right). That raises a validation
conflict `attachment_names_redacted` per affected attachment in the set
("attachment names are redacted on the parent email, but this attachment
ships its filename"). The reviewer resolves it — redact or withhold the
attachment, or override (audited). Nothing is blanked on the children
automatically.

### 6.4 Text minus redacted words

For `redact_in_part` documents the production ships a text file built from the
page word sidecars:

- A word is redacted when the intersection of its box with any redaction box
  on that page covers ≥ 30 % of the word's area *(builder may tune; must err
  toward removing)*.
- Each maximal run of consecutive redacted words becomes one `[REDACTED]`
  token; line breaks follow the sidecar's line grouping.
- Whole-page redactions emit `[REDACTED PAGE]`.
- If **any** page of the document lacks a usable sidecar (`ocr_paths` entry
  empty or unreadable), ship **no** text for the document (today's behaviour)
  and leave `TEXTPATH` empty — never fall back to `text_content`, which has no
  positions.
- The same function backs `/text?redacted=1` so the reviewer sees exactly what
  will ship.

### 6.5 Unchanged on purpose

Redacted documents never produce natives. Withheld documents keep slip sheets.

## 7. Audit

Existing redaction audit entries gain `production_id` and before/after boxes
on update and delete (today they record neither). New entries:
`document_imaging_requested`, `document_imaged` (pages, provenance),
`document_imaging_failed`.

## 8. Rollout

1. Backend: migration, imaging service + task + endpoints, render-time
   imaging, §6 fixes, text-minus-redactions, metadata blanking. Behind no flag:
   the leak fixes should land regardless.
2. Frontend: Prepare pages, Redact mode, side list/QC, preview.
3. Verify on a copy of real data shape with ACME fakes: an email with an HTML
   body and inline image, a multi-page PDF attachment, a scanned PDF, a PNG; a
   full produce → lock → render → package of a set containing a redacted email
   with a covered subject line, checking image, text file and DAT.

## 9. Decided by the user (2026-11-01)

- Emails and PDFs first; Office files in a later phase.
- Hand-drawn boxes first; search-term redaction later.
- Produced text for redacted documents = text with the redacted words removed.
- A redacted "Attachments:" line on an email raises a validation conflict for
  its attachments; nothing is blanked on them silently.

## 10. My calls (check while reading)

- On-demand + bulk imaging before lock (never at render, so Bates stay fixed
  at lock), rather than imaging every email at ingest.
- Unlock: manager+, only until packaged, releases Bates and discards renders.
- Emails laid out on US Letter with a fixed header block in UTC.
- Header field regions recorded at imaging and used to blank DAT metadata.
- Page images frozen once a document has redactions.
- Render refuses (rather than auto-re-locks) when redactions changed after lock.
- ≥ 30 % word-area overlap to remove a word from the produced text.
- Drawing disabled while rotated rather than inverse-transforming coordinates.
- Managers may still approve QC on redactions they drew (no four-eyes rule).
