# Phase 5: Page Annotations — Design Spec

## Overview

Pin-based annotations on document page images. Reviewers click anywhere on a page to drop a color-coded pin, optionally attach a text note, and browse all annotations in a sidebar list. Annotations are visible to all reviewers with full attribution.

**Out of scope:** Text highlights, redactions, coding layouts.

## User Interaction

### Placing an annotation

1. Click anywhere on a page image in the document viewer
2. A compact color picker bubble appears at the cursor position — four colored circles (red, yellow, green, blue) plus a cancel button (x)
3. Pick a color — the pin drops immediately and is saved to the server with empty content
4. A note popover appears next to the pin with a textarea (autofocused). Typing is optional.
5. If the user types a note and clicks Save (or presses Ctrl+Enter), the annotation is updated with the content
6. If the user clicks away or presses Escape, the pin remains with no text — this is valid

### Viewing annotations

- Click any existing pin on a page — popover appears showing: color dot, note text (if any), creator name, timestamp, Edit and Delete actions
- Pins with no text show just the color dot, attribution, and actions in the popover
- The left sidebar has a "Pins" tab (alongside Tags and Notes) listing all annotations for the current document
- Each sidebar entry shows: color indicator, page number, note text preview (or "No note" in muted text), and attribution (creator + relative time)
- Clicking a sidebar entry scrolls the page viewport to that page and briefly pulses the pin

### Editing and deleting

- Click a pin to open its popover, then click Edit — textarea appears with current content, Save/Cancel buttons
- Click Delete — confirmation not needed for your own pins (small team), pin is removed immediately
- Only the pin creator or a manager+ role can delete a pin

## Rendering Approach

### SVG Overlay

Each page image in `ImagePanel` gets an SVG element layered on top, matching the image dimensions. The SVG uses `position: absolute; top: 0; left: 0; width: 100%; height: 100%`.

Pins are SVG `<circle>` elements with a numbered `<text>` child, positioned using percentage-based `cx`/`cy` coordinates. The SVG inherits zoom from the parent container's CSS transform — no manual zoom recalculation needed.

The SVG has `pointer-events: none` on the root element, with `pointer-events: all` on individual pin `<g>` groups, so clicks pass through to the image for placing new pins but existing pins are clickable.

**Rotation handling:** Annotations are disabled when rotation != 0. When the image is rotated (90/180/270), the click-to-pin interaction is hidden and pins are not rendered. The Pins sidebar tab still shows the list (for reference) but with a note: "Rotate to 0° to place or view pins on the page." This avoids complex coordinate transforms between rotated visual space and original image coordinates.

### Pin appearance

- Circle with white stroke, colored fill, numbered label (1, 2, 3... per page)
- Radius: 10px at 100% zoom (scales naturally with CSS transform)
- Colors: red (#e53e3e), yellow (#ecc94b), green (#48bb78), blue (#4299e1)
- Numbering is dynamic — always computed from the current set of annotations ordered by `created_at`, resets per page. Deletion causes renumbering.
- Not rendered when image rotation != 0°

### Color picker bubble

- Appears at the click location (positioned absolutely relative to the page image container)
- Horizontal row of 4 color circles (22px diameter) plus a cancel x button
- Positioned to the right of the click point; if near the right edge, flips to the left
- Dismissed on cancel click, Escape key, or clicking outside

### Note popover

- Appears adjacent to the pin after color selection
- Contains: color dot + "New annotation" header, textarea (optional input), Cancel and Save buttons
- For existing pins: shows note text, attribution, Edit/Delete actions
- Dismissed by clicking outside, pressing Escape, or clicking the x button
- Positioned to avoid overflowing the viewport (flip left/right/up/down as needed)

## Data Model

### Annotation table

```
annotations
  id           SERIAL PRIMARY KEY
  document_id  UUID NOT NULL FK → documents(id) ON DELETE CASCADE
  page_num     INTEGER NOT NULL (1-indexed)
  x_pct        FLOAT NOT NULL (0.0–100.0, percentage from left edge)
  y_pct        FLOAT NOT NULL (0.0–100.0, percentage from top edge)
  color        VARCHAR(20) NOT NULL DEFAULT 'blue'
  content      TEXT NOT NULL DEFAULT '' (empty string = no note)
  created_by   VARCHAR(128) NOT NULL (no FK constraint, matches Note pattern)
  created_at   TIMESTAMP NOT NULL DEFAULT now()
  updated_at   TIMESTAMP NOT NULL DEFAULT now()
```

**Indexes:**
- `ix_annotations_document_id` on `document_id` (primary lookup)
- `ix_annotations_created_by` on `created_by`

**Coordinates:** Stored as percentages of the original image dimensions (0.0 to 100.0). This makes annotations zoom-independent. Annotations are only interactive at 0° rotation (see Rendering Approach).

**Relationships:** Add `creator = relationship("User", foreign_keys=[created_by])` and `document = relationship("Document", back_populates="annotations")` to the model. Add `annotations = relationship("Annotation", back_populates="document", cascade="all, delete-orphan")` to the `Document` model.

**FK pattern:** `created_by` uses `VARCHAR(128)` with no FK constraint, matching the existing `Note` model pattern (plain string, no FK).

## API Endpoints

### List annotations for a document

```
GET /api/documents/{doc_id}/annotations
```

Returns: `Annotation[]` ordered by `page_num ASC, created_at ASC`

Response includes `created_by_email` and `created_by_display_name` resolved via a join to the `users` table in the query. The route handler joins `Annotation` with `User` on `Annotation.created_by == User.id` and constructs the response manually (same pattern as `ReviewBatchOut` in the batches router).

### Create annotation

```
POST /api/documents/{doc_id}/annotations
Body: { page_num, x_pct, y_pct, color, content? }
```

Returns: the created `Annotation` object.

Validates: `page_num` is between 1 and `document.page_count`, `x_pct` and `y_pct` are between 0 and 100, `color` is one of "red", "yellow", "green", "blue".

Audit log: `annotation_created` action.

### Update annotation

```
PUT /api/annotations/{id}
Body: { content?, color? }
```

Only the creator can update. Updates `updated_at`.

Audit log: `annotation_updated` action.

### Delete annotation

```
DELETE /api/annotations/{id}
```

Creator or manager+ role. The endpoint loads the annotation, joins to the document to get `production_id`, then checks `ProductionAccess` for the requesting user's role via `get_user_role_for_production`. Deletes the row.

Audit log: `annotation_deleted` action.

## Frontend Components

### AnnotationOverlay (new)

- Renders an SVG element over a single page image
- Props: `docId`, `pageNum`, `annotations` (filtered for this page), `onPinClick`, `onPageClick`
- Renders numbered circle pins at percentage positions
- Handles click events on the SVG background (for placing new pins) vs. on pin groups (for viewing existing)

### AnnotationPopover (new)

- Renders at a given position relative to the page container
- Two modes: "create" (color picker → textarea → save) and "view" (show existing annotation with edit/delete)
- Props: `position`, `mode`, `annotation?`, `onSave`, `onDelete`, `onCancel`

### ColorPicker (new, small)

- Inline horizontal row of 4 colored circles + cancel
- Props: `onSelect(color)`, `onCancel`

### AnnotationSidebar (new)

- Tab content for the "Pins" tab in DocumentViewer's left sidebar
- Lists all annotations for the document, sorted by page then creation time
- Each entry: color dot, page number, note preview (truncated), attribution
- Entries with no text show "No note" in muted style
- Click entry → scrolls viewport to that page, pulses the pin
- Props: `annotations`, `onSelect(annotationId)`

### Changes to existing components

**ImagePanel.tsx:**
- Wrap each page image + SVG overlay in a `position: relative` container
- Pass click events up to parent for new annotation placement
- Accept `annotations` prop (full list, component filters by page)

**DocumentViewer.tsx:**
- Fetch annotations on document load (`GET /api/documents/{doc_id}/annotations`)
- Manage annotation CRUD state (create, update, delete → refetch)
- Add "Pins" tab to left sidebar tabs
- Pass annotations and handlers to ImagePanel and AnnotationSidebar

## Frontend Types

```typescript
export interface Annotation {
  id: number;
  document_id: string;
  page_num: number;
  x_pct: number;
  y_pct: number;
  color: string;
  content: string;
  created_by: string;
  created_by_email: string;
  created_by_display_name: string | null;
  created_at: string;
  updated_at: string;
}
```

## Backend Schemas

```python
class AnnotationCreate(BaseModel):
    page_num: int
    x_pct: float
    y_pct: float
    color: str = "blue"
    content: str = ""

class AnnotationUpdate(BaseModel):
    content: str | None = None
    color: str | None = None

class AnnotationOut(BaseModel):
    id: int
    document_id: UUID
    page_num: int
    x_pct: float
    y_pct: float
    color: str
    content: str
    created_by: str
    created_by_email: str
    created_by_display_name: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
```

## Document Summary Integration

- Add `annotation_count` field to `DocumentSummary` schema (like `note_count`)
- Query: count of annotations per document, joined in the document list query
- Add `has_annotations` boolean filter to document list endpoint (like `tag_id` filter)

## Audit Trail

All annotation actions are logged via the existing `log_action` service:
- `annotation_created` — details: `{ page_num, color, has_content }`
- `annotation_updated` — details: `{ changed_fields }`
- `annotation_deleted` — details: `{ page_num, color }`

## Pin Color Semantics

Colors have suggested meanings but are not enforced:
- Red (#e53e3e) — Issue / Problem
- Yellow (#ecc94b) — Question / Follow-up
- Green (#48bb78) — Helpful / Favorable
- Blue (#4299e1) — General note

The labels are shown as tooltips in the color picker. Users can use any color for any purpose.

## Edge Cases and Notes

- **Documents with 0 pages (native-only):** The Pins tab shows "No pages available for annotation." Click-to-pin is not available since there are no page images.
- **Concurrent edits:** No real-time sync. Annotations refresh on document load. Acceptable for a 2-5 user team.
- **Color validation:** Use `Literal["red", "yellow", "green", "blue"]` in `AnnotationCreate` and `AnnotationUpdate` schemas for automatic Pydantic validation.
- **Migration:** Standard Alembic autogenerate. `Float` must be imported in `models.py` (add to existing SQLAlchemy imports).
