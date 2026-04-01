# Phase 5: Page Annotations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add pin-based annotations on document page images with color coding, popovers, and sidebar list.

**Architecture:** SVG overlay on each page image renders numbered, color-coded pins at percentage coordinates. Click-to-pin flow: click page → pick color → optionally type note. Annotations stored in a new `annotations` table with document_id, page_num, x/y percentages. CRUD via REST endpoints, sidebar tab for browsing.

**Tech Stack:** Python/FastAPI, SQLAlchemy async, Alembic, PostgreSQL, React 19/TypeScript, SVG

---

## File Structure

### New Backend Files
- `backend/app/routers/annotations.py` — Annotation CRUD endpoints

### New Frontend Files
- `frontend/src/components/AnnotationOverlay.tsx` — SVG overlay per page with pins + click handler
- `frontend/src/components/AnnotationPopover.tsx` — Color picker + note input/view popover
- `frontend/src/components/AnnotationSidebar.tsx` — Pins tab content for left sidebar

### Modified Files
- `backend/app/models.py` — Add `Annotation` model, add `annotations` relationship to `Document`
- `backend/app/schemas.py` — Add `AnnotationCreate`, `AnnotationUpdate`, `AnnotationOut`
- `backend/app/main.py` — Register annotations router
- `frontend/src/types/index.ts` — Add `Annotation` type
- `frontend/src/api/client.ts` — Add annotation API functions
- `frontend/src/components/ImagePanel.tsx` — Wrap pages in relative container, add overlay + click handling
- `frontend/src/components/DocumentViewer.tsx` — Fetch annotations, add Pins tab, manage annotation state

---

## Task 1: Add Annotation Model and Migration

**Files:**
- Modify: `backend/app/models.py`

- [ ] **Step 1: Add Annotation model and Float import**

In `backend/app/models.py`, add `Float` to the SQLAlchemy imports (line 5):

```python
from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
```

Add the `Annotation` model at the end of the file (after `QCDecision`):

```python
class Annotation(Base):
    __tablename__ = "annotations"
    __table_args__ = (
        Index("ix_annotations_document_id", "document_id"),
        Index("ix_annotations_created_by", "created_by"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    page_num = Column(Integer, nullable=False)
    x_pct = Column(Float, nullable=False)
    y_pct = Column(Float, nullable=False)
    color = Column(String(20), nullable=False, default="blue")
    content = Column(Text, nullable=False, default="")
    created_by = Column(String(128), nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    document = relationship("Document", back_populates="annotations")
```

- [ ] **Step 2: Add annotations relationship to Document model**

In the `Document` class (after the `notes` relationship on line 80), add:

```python
    annotations = relationship("Annotation", back_populates="document", cascade="all, delete-orphan", order_by="Annotation.page_num, Annotation.created_at")
```

- [ ] **Step 3: Generate and run migration**

```bash
cd F:/Users/WCEdm/Documents/Developer/descubre/backend
./venv/Scripts/python.exe -m alembic revision --autogenerate -m "add annotations table"
./venv/Scripts/python.exe -m alembic upgrade head
```

Review the generated migration — it should create the `annotations` table with all columns and indexes `ix_annotations_document_id` and `ix_annotations_created_by`. If indexes aren't auto-generated, add them manually:

```python
op.create_index("ix_annotations_document_id", "annotations", ["document_id"])
op.create_index("ix_annotations_created_by", "annotations", ["created_by"])
```

- [ ] **Step 4: Commit**

```bash
git add backend/app/models.py backend/alembic/versions/
git commit -m "feat: add Annotation model and migration"
```

---

## Task 2: Add Backend Schemas

**Files:**
- Modify: `backend/app/schemas.py`

- [ ] **Step 1: Add annotation schemas**

Add these imports at the top if not already present:

```python
from typing import Literal
```

Add after the `QCStats` class at the end of `schemas.py`:

```python
# ── Annotations ──

class AnnotationCreate(BaseModel):
    page_num: int
    x_pct: float
    y_pct: float
    color: Literal["red", "yellow", "green", "blue"] = "blue"
    content: str = ""


class AnnotationUpdate(BaseModel):
    content: str | None = None
    color: Literal["red", "yellow", "green", "blue"] | None = None


class AnnotationOut(BaseModel):
    id: int
    document_id: UUID
    page_num: int
    x_pct: float
    y_pct: float
    color: str
    content: str
    created_by: str
    created_by_email: str = ""
    created_by_display_name: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
```

- [ ] **Step 2: Commit**

```bash
git add backend/app/schemas.py
git commit -m "feat: add annotation schemas"
```

---

## Task 3: Create Annotations Router

**Files:**
- Create: `backend/app/routers/annotations.py`

- [ ] **Step 1: Create the annotations router**

Create `backend/app/routers/annotations.py`:

```python
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_accessible_production_ids, get_user_role_for_production, ROLE_RANK
from app.models import Annotation, Document, User
from app.routers.auth import get_current_user
from app.schemas import AnnotationCreate, AnnotationOut, AnnotationUpdate
from app.services.audit import log_action

router = APIRouter(prefix="/api", tags=["annotations"])


def _build_annotation_out(ann: Annotation, email: str, display_name: str | None) -> AnnotationOut:
    return AnnotationOut(
        id=ann.id,
        document_id=ann.document_id,
        page_num=ann.page_num,
        x_pct=ann.x_pct,
        y_pct=ann.y_pct,
        color=ann.color,
        content=ann.content,
        created_by=ann.created_by,
        created_by_email=email,
        created_by_display_name=display_name,
        created_at=ann.created_at,
        updated_at=ann.updated_at,
    )


@router.get("/documents/{doc_id}/annotations", response_model=list[AnnotationOut])
async def list_annotations(
    doc_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    doc = await db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")

    result = await db.execute(
        select(Annotation, User.email, User.display_name)
        .outerjoin(User, Annotation.created_by == User.id)
        .where(Annotation.document_id == doc_id)
        .order_by(Annotation.page_num, Annotation.created_at)
    )
    return [
        _build_annotation_out(ann, email or "", display_name)
        for ann, email, display_name in result.all()
    ]


@router.post("/documents/{doc_id}/annotations", response_model=AnnotationOut)
async def create_annotation(
    doc_id: UUID,
    body: AnnotationCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    doc = await db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")

    role = await get_user_role_for_production(db, user, doc.production_id)
    if role == "readonly":
        raise HTTPException(status_code=403, detail="Read-only access")

    if body.page_num < 1 or body.page_num > doc.page_count:
        raise HTTPException(status_code=400, detail=f"page_num must be between 1 and {doc.page_count}")
    if not (0 <= body.x_pct <= 100):
        raise HTTPException(status_code=400, detail="x_pct must be between 0 and 100")
    if not (0 <= body.y_pct <= 100):
        raise HTTPException(status_code=400, detail="y_pct must be between 0 and 100")

    ann = Annotation(
        document_id=doc_id,
        page_num=body.page_num,
        x_pct=body.x_pct,
        y_pct=body.y_pct,
        color=body.color,
        content=body.content,
        created_by=user.id,
    )
    db.add(ann)
    await db.flush()

    await log_action(db, user, "annotation_created", "annotation", str(ann.id),
                     production_id=doc.production_id,
                     details={"page_num": body.page_num, "color": body.color, "has_content": bool(body.content)})
    await db.commit()
    await db.refresh(ann)

    return _build_annotation_out(ann, user.email, user.display_name)


@router.put("/annotations/{ann_id}", response_model=AnnotationOut)
async def update_annotation(
    ann_id: int,
    body: AnnotationUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ann = await db.get(Annotation, ann_id)
    if not ann:
        raise HTTPException(status_code=404, detail="Annotation not found")

    # Access check
    doc = await db.get(Document, ann.document_id)
    accessible = await get_accessible_production_ids(db, user)
    if doc and doc.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")

    # Only creator can update
    if ann.created_by != user.id:
        raise HTTPException(status_code=403, detail="Only the creator can edit this annotation")

    changed = []
    if body.content is not None:
        ann.content = body.content
        changed.append("content")
    if body.color is not None:
        ann.color = body.color
        changed.append("color")

    ann.updated_at = datetime.utcnow()

    await log_action(db, user, "annotation_updated", "annotation", str(ann_id),
                     production_id=doc.production_id if doc else None,
                     details={"changed_fields": changed})
    await db.commit()
    await db.refresh(ann)

    creator = await db.get(User, ann.created_by)
    return _build_annotation_out(ann, creator.email if creator else "", creator.display_name if creator else None)


@router.delete("/annotations/{ann_id}")
async def delete_annotation(
    ann_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ann = await db.get(Annotation, ann_id)
    if not ann:
        raise HTTPException(status_code=404, detail="Annotation not found")

    doc = await db.get(Document, ann.document_id)
    accessible = await get_accessible_production_ids(db, user)
    if doc and doc.production_id not in accessible:
        raise HTTPException(status_code=403, detail="Access denied")

    # Creator can always delete; otherwise need manager+
    if ann.created_by != user.id:
        if doc:
            role = await get_user_role_for_production(db, user, doc.production_id)
            if ROLE_RANK.get(role, 0) < ROLE_RANK["manager"]:
                raise HTTPException(status_code=403, detail="Only the creator or a manager can delete this annotation")

    await log_action(db, user, "annotation_deleted", "annotation", str(ann_id),
                     production_id=doc.production_id if doc else None,
                     details={"page_num": ann.page_num, "color": ann.color})
    await db.delete(ann)
    await db.commit()
    return {"ok": True}
```

- [ ] **Step 2: Register in main.py**

In `backend/app/main.py`, add `annotations` to the import line:

```python
from app.routers import ai, annotations, audit, auth, batches, dashboard, documents, export, ingest, notes, productions, qc, queues, saved_searches, search, tags
```

Add after `app.include_router(notes.router)`:

```python
app.include_router(annotations.router)
```

- [ ] **Step 3: Run import check**

```bash
cd F:/Users/WCEdm/Documents/Developer/descubre/backend
./venv/Scripts/python.exe -c "from app.main import app; print('OK')"
```

- [ ] **Step 4: Commit**

```bash
git add backend/app/routers/annotations.py backend/app/main.py
git commit -m "feat: add annotations CRUD router"
```

---

## Task 3b: Add annotation_count to Document Summaries

**Files:**
- Modify: `backend/app/schemas.py`
- Modify: `backend/app/routers/documents.py`

- [ ] **Step 1: Add annotation_count to schemas**

In `backend/app/schemas.py`, add `annotation_count: int = 0` to both `DocumentSummary` and `DocumentDetail` (after `note_count`).

- [ ] **Step 2: Add annotation count query to document list endpoint**

In `backend/app/routers/documents.py`, in the `list_documents` function, add an annotation count query (same pattern as `note_count`). Import `Annotation` from models:

```python
from app.models import Annotation, Document, DocumentTag, Note, User
```

After the existing `note_counts` query, add:

```python
    ann_counts: dict = {}
    if doc_ids:
        ac_result = await db.execute(
            select(Annotation.document_id, func.count(Annotation.id))
            .where(Annotation.document_id.in_(doc_ids))
            .group_by(Annotation.document_id)
        )
        ann_counts = dict(ac_result.all())
```

Then add `annotation_count=ann_counts.get(d.id, 0)` to each `DocumentSummary(...)` construction.

Do the same for the `get_document` endpoint (single document detail) — count annotations for that document and pass `annotation_count` to `DocumentDetail(...)`.

- [ ] **Step 3: Add has_annotations filter**

In the `list_documents` function, add a query parameter:

```python
    has_annotations: bool | None = None,
```

When `has_annotations is True`, filter to documents that have at least one annotation:

```python
    if has_annotations is True:
        query = query.where(Document.id.in_(
            select(Annotation.document_id).distinct()
        ))
        count_query = count_query.where(Document.id.in_(
            select(Annotation.document_id).distinct()
        ))
```

When `has_annotations is False`, filter to documents with no annotations.

- [ ] **Step 4: Commit**

```bash
git add backend/app/schemas.py backend/app/routers/documents.py
git commit -m "feat: add annotation_count and has_annotations filter to document list"
```

---

## Task 4: Frontend Types and API Client

**Files:**
- Modify: `frontend/src/types/index.ts`
- Modify: `frontend/src/api/client.ts`

- [ ] **Step 1: Add Annotation type**

Add to the end of `frontend/src/types/index.ts`:

```typescript
// ── Annotations ──

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

- [ ] **Step 2: Add API functions**

Add `Annotation` to the import line at the top of `frontend/src/api/client.ts`:

```typescript
import type {
  Annotation, BatchDocument, DashboardStats, ...
} from '../types';
```

Add these functions (group them together, e.g., after the Notes section):

```typescript
// ── Annotations ──

export function listAnnotations(docId: string): Promise<Annotation[]> {
  return request<Annotation[]>(`/api/documents/${docId}/annotations`);
}

export function createAnnotation(docId: string, pageNum: number, xPct: number, yPct: number, color: string, content = ''): Promise<Annotation> {
  return request<Annotation>(`/api/documents/${docId}/annotations`, json({ page_num: pageNum, x_pct: xPct, y_pct: yPct, color, content }));
}

export function updateAnnotation(annId: number, data: { content?: string; color?: string }): Promise<Annotation> {
  return request<Annotation>(`/api/annotations/${annId}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
}

export function deleteAnnotation(annId: number): Promise<void> {
  return request(`/api/annotations/${annId}`, { method: 'DELETE' });
}
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/api/client.ts
git commit -m "feat: add annotation types and API client functions"
```

---

## Task 5: AnnotationOverlay Component

**Files:**
- Create: `frontend/src/components/AnnotationOverlay.tsx`

- [ ] **Step 1: Create AnnotationOverlay**

Create `frontend/src/components/AnnotationOverlay.tsx`:

```tsx
import type { Annotation } from '../types';

const PIN_COLORS: Record<string, string> = {
  red: '#e53e3e',
  yellow: '#ecc94b',
  green: '#48bb78',
  blue: '#4299e1',
};

interface Props {
  annotations: Annotation[];
  pageNum: number;
  rotation: number;
  onPinClick: (annotation: Annotation, rect: DOMRect) => void;
  onPageClick: (pageNum: number, xPct: number, yPct: number, rect: DOMRect) => void;
}

export default function AnnotationOverlay({ annotations, pageNum, rotation, onPinClick, onPageClick }: Props) {
  if (rotation !== 0) return null;

  const pageAnnotations = annotations.filter(a => a.page_num === pageNum);

  const handleSvgClick = (e: React.MouseEvent<SVGSVGElement>) => {
    const svg = e.currentTarget;
    const rect = svg.getBoundingClientRect();
    const xPct = ((e.clientX - rect.left) / rect.width) * 100;
    const yPct = ((e.clientY - rect.top) / rect.height) * 100;
    onPageClick(pageNum, xPct, yPct, rect);
  };

  const handlePinClick = (e: React.MouseEvent, ann: Annotation) => {
    e.stopPropagation();
    const target = e.currentTarget as SVGGElement;
    const rect = target.getBoundingClientRect();
    onPinClick(ann, rect);
  };

  return (
    <svg
      style={{
        position: 'absolute',
        top: 0,
        left: 0,
        width: '100%',
        height: '100%',
        pointerEvents: 'none',
        cursor: 'crosshair',
      }}
      onClick={handleSvgClick}
    >
      {/* Make background clickable for placing new pins */}
      <rect width="100%" height="100%" fill="transparent" style={{ pointerEvents: 'all', cursor: 'crosshair' }} />

      {pageAnnotations.map((ann, idx) => (
        <g
          key={ann.id}
          style={{ pointerEvents: 'all', cursor: 'pointer' }}
          onClick={(e) => handlePinClick(e, ann)}
        >
          <circle
            cx={`${ann.x_pct}%`}
            cy={`${ann.y_pct}%`}
            r={10}
            fill={PIN_COLORS[ann.color] || PIN_COLORS.blue}
            stroke="white"
            strokeWidth={2}
            opacity={0.9}
          />
          <text
            x={`${ann.x_pct}%`}
            y={`${ann.y_pct}%`}
            textAnchor="middle"
            dominantBaseline="central"
            fill="white"
            fontSize={10}
            fontWeight="bold"
            style={{ pointerEvents: 'none' }}
          >
            {idx + 1}
          </text>
        </g>
      ))}
    </svg>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/components/AnnotationOverlay.tsx
git commit -m "feat: add AnnotationOverlay SVG component"
```

---

## Task 6: AnnotationPopover Component

**Files:**
- Create: `frontend/src/components/AnnotationPopover.tsx`

- [ ] **Step 1: Create AnnotationPopover**

Create `frontend/src/components/AnnotationPopover.tsx`. This component handles three states:
1. **color-picker** — user just clicked the page, pick a color
2. **create** — color chosen, show textarea for optional note
3. **view** — viewing an existing annotation with edit/delete

```tsx
import { useEffect, useRef, useState } from 'react';
import type { Annotation } from '../types';

const PIN_COLORS: Record<string, { hex: string; label: string }> = {
  red: { hex: '#e53e3e', label: 'Issue' },
  yellow: { hex: '#ecc94b', label: 'Question' },
  green: { hex: '#48bb78', label: 'Helpful' },
  blue: { hex: '#4299e1', label: 'General' },
};

interface Props {
  mode: 'color-picker' | 'create' | 'view';
  position: { top: number; left: number };
  annotation?: Annotation;
  selectedColor?: string;
  canDelete?: boolean;
  onColorSelect: (color: string) => void;
  onSave: (content: string) => void;
  onUpdate: (data: { content?: string; color?: string }) => void;
  onDelete: () => void;
  onCancel: () => void;
}

export default function AnnotationPopover({
  mode, position, annotation, selectedColor, canDelete = false,
  onColorSelect, onSave, onUpdate, onDelete, onCancel,
}: Props) {
  const [content, setContent] = useState(annotation?.content || '');
  const [editing, setEditing] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const contentRef = useRef(content);
  contentRef.current = content;

  // Focus textarea when entering create mode or edit mode
  useEffect(() => {
    if ((mode === 'create' || editing) && textareaRef.current) {
      textareaRef.current.focus();
    }
  }, [mode, editing]);

  // Close on click outside
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        if (mode === 'create') {
          // Save with latest content (via ref to avoid stale closure)
          onSave(contentRef.current);
        } else {
          onCancel();
        }
      }
    };
    // Delay to avoid catching the click that opened the popover
    const timer = setTimeout(() => document.addEventListener('mousedown', handler), 50);
    return () => { clearTimeout(timer); document.removeEventListener('mousedown', handler); };
  }, [mode, onSave, onCancel]);

  // Close on Escape
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCancel();
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [onCancel]);

  const popoverStyle: React.CSSProperties = {
    position: 'fixed',
    top: position.top,
    left: position.left,
    zIndex: 1000,
    background: 'white',
    border: '1px solid var(--color-neutral-200)',
    borderRadius: 'var(--radius-md)',
    boxShadow: '0 4px 20px rgba(0,0,0,0.18)',
    padding: 'var(--space-2)',
    minWidth: 200,
  };

  // Color picker mode
  if (mode === 'color-picker') {
    return (
      <div ref={ref} style={{ ...popoverStyle, minWidth: 'auto', padding: 'var(--space-1-5)', display: 'flex', gap: 'var(--space-1-5)', alignItems: 'center' }}>
        {Object.entries(PIN_COLORS).map(([name, { hex, label }]) => (
          <button
            key={name}
            onClick={() => onColorSelect(name)}
            title={label}
            style={{
              width: 24, height: 24, borderRadius: '50%', background: hex,
              border: 'none', cursor: 'pointer', transition: 'transform 0.1s',
            }}
            onMouseEnter={e => (e.currentTarget.style.transform = 'scale(1.2)')}
            onMouseLeave={e => (e.currentTarget.style.transform = 'scale(1)')}
          />
        ))}
        <button
          onClick={onCancel}
          style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--color-neutral-400)', fontSize: 16, padding: '0 2px' }}
        >&times;</button>
      </div>
    );
  }

  // Create mode
  if (mode === 'create') {
    const colorInfo = PIN_COLORS[selectedColor || 'blue'];
    return (
      <div ref={ref} style={popoverStyle}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-1)', marginBottom: 'var(--space-2)' }}>
          <span style={{ width: 10, height: 10, borderRadius: '50%', background: colorInfo.hex, display: 'inline-block' }} />
          <span style={{ fontWeight: 600, fontSize: 'var(--text-xs)' }}>New annotation</span>
        </div>
        <textarea
          ref={textareaRef}
          value={content}
          onChange={e => setContent(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && e.ctrlKey) { e.preventDefault(); onSave(content); } }}
          placeholder="Add a note (optional)..."
          style={{
            width: '100%', height: 56, border: '1px solid var(--color-neutral-200)', borderRadius: 'var(--radius-sm)',
            padding: 'var(--space-1-5)', fontSize: 'var(--text-xs)', resize: 'none', fontFamily: 'inherit', boxSizing: 'border-box',
          }}
        />
        <div style={{ display: 'flex', gap: 'var(--space-1)', marginTop: 'var(--space-1-5)', justifyContent: 'flex-end' }}>
          <button className="btn btn-ghost btn-xs" onClick={onCancel}>Cancel</button>
          <button className="btn btn-primary btn-xs" onClick={() => onSave(content)}>Save</button>
        </div>
      </div>
    );
  }

  // View mode
  if (!annotation) return null;
  const colorInfo = PIN_COLORS[annotation.color] || PIN_COLORS.blue;

  if (editing) {
    return (
      <div ref={ref} style={popoverStyle}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-1)', marginBottom: 'var(--space-2)' }}>
          <span style={{ width: 10, height: 10, borderRadius: '50%', background: colorInfo.hex, display: 'inline-block' }} />
          <span style={{ fontWeight: 600, fontSize: 'var(--text-xs)' }}>Edit annotation</span>
        </div>
        <textarea
          ref={textareaRef}
          value={content}
          onChange={e => setContent(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && e.ctrlKey) { e.preventDefault(); onUpdate({ content }); } }}
          style={{
            width: '100%', height: 56, border: '1px solid var(--color-neutral-200)', borderRadius: 'var(--radius-sm)',
            padding: 'var(--space-1-5)', fontSize: 'var(--text-xs)', resize: 'none', fontFamily: 'inherit', boxSizing: 'border-box',
          }}
        />
        <div style={{ display: 'flex', gap: 'var(--space-1)', marginTop: 'var(--space-1-5)', justifyContent: 'flex-end' }}>
          <button className="btn btn-ghost btn-xs" onClick={() => { setEditing(false); setContent(annotation.content); }}>Cancel</button>
          <button className="btn btn-primary btn-xs" onClick={() => onUpdate({ content })}>Save</button>
        </div>
      </div>
    );
  }

  const timeAgo = (dateStr: string) => {
    const diff = Date.now() - new Date(dateStr).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    return `${Math.floor(hours / 24)}d ago`;
  };

  return (
    <div ref={ref} style={popoverStyle}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-1)', marginBottom: 'var(--space-1)' }}>
        <span style={{ width: 10, height: 10, borderRadius: '50%', background: colorInfo.hex, display: 'inline-block' }} />
        <span style={{ fontWeight: 600, fontSize: 'var(--text-xs)' }}>{colorInfo.label}</span>
        <button
          onClick={onCancel}
          style={{ marginLeft: 'auto', background: 'none', border: 'none', cursor: 'pointer', color: 'var(--color-neutral-400)', fontSize: 14 }}
        >&times;</button>
      </div>
      {annotation.content ? (
        <div style={{ fontSize: 'var(--text-xs)', lineHeight: 1.5, marginBottom: 'var(--space-1-5)' }}>
          {annotation.content}
        </div>
      ) : (
        <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-400)', fontStyle: 'italic', marginBottom: 'var(--space-1-5)' }}>
          No note
        </div>
      )}
      <div style={{ fontSize: 10, color: 'var(--color-neutral-400)' }}>
        {annotation.created_by_display_name || annotation.created_by_email} &middot; {timeAgo(annotation.created_at)}
      </div>
      <div style={{ display: 'flex', gap: 'var(--space-2)', marginTop: 'var(--space-1-5)' }}>
        <button
          style={{ fontSize: 10, color: 'var(--color-primary-600)', background: 'none', border: 'none', cursor: 'pointer', padding: 0 }}
          onClick={() => { setEditing(true); setContent(annotation.content); }}
        >Edit</button>
        {canDelete && (
          <button
            style={{ fontSize: 10, color: 'var(--color-danger-600)', background: 'none', border: 'none', cursor: 'pointer', padding: 0 }}
            onClick={onDelete}
          >Delete</button>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/components/AnnotationPopover.tsx
git commit -m "feat: add AnnotationPopover with color picker, create, and view modes"
```

---

## Task 7: AnnotationSidebar Component

**Files:**
- Create: `frontend/src/components/AnnotationSidebar.tsx`

- [ ] **Step 1: Create AnnotationSidebar**

Create `frontend/src/components/AnnotationSidebar.tsx`:

```tsx
import type { Annotation } from '../types';

const PIN_COLORS: Record<string, string> = {
  red: '#e53e3e',
  yellow: '#ecc94b',
  green: '#48bb78',
  blue: '#4299e1',
};

interface Props {
  annotations: Annotation[];
  rotation: number;
  pageCount: number;
  onSelect: (annotation: Annotation) => void;
}

export default function AnnotationSidebar({ annotations, rotation, pageCount, onSelect }: Props) {
  if (pageCount === 0) {
    return (
      <div style={{ padding: 'var(--space-3)', fontSize: 'var(--text-xs)', color: 'var(--color-neutral-400)', fontStyle: 'italic' }}>
        No pages available for annotation.
      </div>
    );
  }

  const timeAgo = (dateStr: string) => {
    const diff = Date.now() - new Date(dateStr).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    return `${Math.floor(hours / 24)}d ago`;
  };

  return (
    <div style={{ padding: 'var(--space-2)', overflowY: 'auto', flex: 1 }}>
      {rotation !== 0 && (
        <div style={{ padding: 'var(--space-2)', fontSize: 'var(--text-xs)', color: 'var(--color-warning-600)', background: 'var(--color-warning-50)', borderRadius: 'var(--radius-sm)', marginBottom: 'var(--space-2)' }}>
          Rotate to 0° to place or view pins on the page.
        </div>
      )}

      {annotations.length === 0 ? (
        <div style={{ fontSize: 'var(--text-xs)', color: 'var(--color-neutral-400)', fontStyle: 'italic' }}>
          No annotations yet. Click on a page image to add one.
        </div>
      ) : (
        <>
          <div style={{ fontSize: 11, color: 'var(--color-neutral-400)', marginBottom: 'var(--space-2)' }}>
            {annotations.length} annotation{annotations.length !== 1 ? 's' : ''}
          </div>
          {annotations.map(ann => (
            <div
              key={ann.id}
              onClick={() => onSelect(ann)}
              style={{
                padding: 'var(--space-2)',
                border: '1px solid var(--color-neutral-200)',
                borderLeft: `3px solid ${PIN_COLORS[ann.color] || PIN_COLORS.blue}`,
                borderRadius: 'var(--radius-sm)',
                marginBottom: 'var(--space-1-5)',
                cursor: 'pointer',
                transition: 'background 0.15s',
              }}
              onMouseEnter={e => (e.currentTarget.style.background = 'var(--color-neutral-50)')}
              onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-1)', marginBottom: 'var(--space-1)' }}>
                <span style={{ width: 8, height: 8, borderRadius: '50%', background: PIN_COLORS[ann.color] || PIN_COLORS.blue, display: 'inline-block', flexShrink: 0 }} />
                <span style={{ fontWeight: 600, fontSize: 11 }}>Page {ann.page_num}</span>
              </div>
              {ann.content ? (
                <div style={{ fontSize: 11, lineHeight: 1.4, overflow: 'hidden', textOverflow: 'ellipsis', display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical' }}>
                  {ann.content}
                </div>
              ) : (
                <div style={{ fontSize: 11, color: 'var(--color-neutral-400)', fontStyle: 'italic' }}>No note</div>
              )}
              <div style={{ fontSize: 10, color: 'var(--color-neutral-400)', marginTop: 'var(--space-1)' }}>
                {ann.created_by_display_name || ann.created_by_email} &middot; {timeAgo(ann.created_at)}
              </div>
            </div>
          ))}
        </>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/components/AnnotationSidebar.tsx
git commit -m "feat: add AnnotationSidebar pins list component"
```

---

## Task 8: Wire Annotations into ImagePanel and DocumentViewer

**Files:**
- Modify: `frontend/src/components/ImagePanel.tsx`
- Modify: `frontend/src/components/DocumentViewer.tsx`

This is the integration task — connects all the new components.

- [ ] **Step 1: Update ImagePanel to accept annotations and render overlays**

Replace the entire content of `frontend/src/components/ImagePanel.tsx` with:

```tsx
import { useCallback, useEffect, useRef, useState } from 'react';
import { imageUrl } from '../api/client';
import type { Annotation } from '../types';
import AnnotationOverlay from './AnnotationOverlay';

interface Props {
  docId: string;
  pageCount: number;
  annotations?: Annotation[];
  onPinClick?: (annotation: Annotation, rect: DOMRect) => void;
  onPageClick?: (pageNum: number, xPct: number, yPct: number, rect: DOMRect) => void;
  onRotationChange?: (rotation: number) => void;
}

export default function ImagePanel({ docId, pageCount, annotations = [], onPinClick, onPageClick, onRotationChange }: Props) {
  const [zoom, setZoom] = useState(0.5);
  const [rotation, setRotation] = useState(0);
  const [vpWidth, setVpWidth] = useState(800);
  const viewportRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setZoom(0.5);
    setRotation(0);
    viewportRef.current?.scrollTo(0, 0);
  }, [docId]);

  useEffect(() => {
    const vp = viewportRef.current;
    if (!vp) return;
    const ro = new ResizeObserver(entries => {
      for (const entry of entries) setVpWidth(entry.contentRect.width);
    });
    ro.observe(vp);
    return () => ro.disconnect();
  }, []);

  const handleWheel = useCallback((e: WheelEvent) => {
    if (e.ctrlKey) {
      e.preventDefault();
      setZoom(z => Math.max(0.25, Math.min(4, z - e.deltaY * 0.002)));
    }
  }, []);

  useEffect(() => {
    const vp = viewportRef.current;
    if (vp) vp.addEventListener('wheel', handleWheel, { passive: false });
    return () => { if (vp) vp.removeEventListener('wheel', handleWheel); };
  }, [handleWheel]);

  const imgWidth = vpWidth * zoom;

  const handlePageClick = (pageNum: number, xPct: number, yPct: number, rect: DOMRect) => {
    if (onPageClick && rotation === 0) onPageClick(pageNum, xPct, yPct, rect);
  };

  const handlePinClick = (ann: Annotation, rect: DOMRect) => {
    if (onPinClick) onPinClick(ann, rect);
  };

  return (
    <div className="viewer-main">
      <div className="image-toolbar">
        <span className="separator" />
        <button className="btn btn-secondary btn-sm" onClick={() => setZoom(z => Math.max(0.25, z - 0.25))}>−</button>
        <span className="page-info" style={{ minWidth: 40, textAlign: 'center' }}>{Math.round(zoom * 100)}%</span>
        <button className="btn btn-secondary btn-sm" onClick={() => setZoom(z => Math.min(4, z + 0.25))}>+</button>
        <button className="btn btn-secondary btn-sm" onClick={() => setZoom(0.5)}>Fit</button>
        <button className="btn btn-secondary btn-sm" onClick={() => { setRotation(r => { const next = (r + 90) % 360; onRotationChange?.(next); return next; }); }}>↻</button>
      </div>
      <div className="image-viewport" ref={viewportRef} style={{ flexDirection: 'column', alignItems: 'center', gap: 8 }}>
        {Array.from({ length: pageCount }, (_, i) => (
          <div key={i} id={`page-${i + 1}`} style={{ position: 'relative', flexShrink: 0, width: 'fit-content' }}>
            <div style={{
              position: 'absolute', top: 4, left: 4, padding: '2px 8px',
              background: 'rgba(0,0,0,0.55)', color: '#fff', fontSize: 11,
              borderRadius: 4, zIndex: 1,
            }}>
              {i + 1}
            </div>
            <img
              src={imageUrl(docId, i + 1)}
              alt={`Page ${i + 1}`}
              style={{
                width: imgWidth,
                display: 'block',
                transform: rotation ? `rotate(${rotation}deg)` : undefined,
              }}
              draggable={false}
            />
            <AnnotationOverlay
              annotations={annotations}
              pageNum={i + 1}
              rotation={rotation}
              onPinClick={handlePinClick}
              onPageClick={handlePageClick}
            />
          </div>
        ))}
      </div>
    </div>
  );
}
```

Key changes from original:
- Added `annotations`, `onPinClick`, `onPageClick`, `onRotationChange` props
- Added `id={`page-${i+1}`}` to each page div for scroll-to-page
- Renders `<AnnotationOverlay>` inside each page's relative container
- Passes `rotation` to overlay (overlay hides itself when rotation != 0)
- Calls `onRotationChange` when rotation changes so parent can track it

- [ ] **Step 2: Update DocumentViewer to manage annotation state**

In `frontend/src/components/DocumentViewer.tsx`, make these changes:

**Add imports** (at the top, alongside existing imports):

```tsx
import { createAnnotation, deleteAnnotation, listAnnotations, updateAnnotation } from '../api/client';
import type { Annotation } from '../types';
import AnnotationPopover from './AnnotationPopover';
import AnnotationSidebar from './AnnotationSidebar';
```

**Add state** (after the existing state declarations around line 40):

```tsx
  const [annotations, setAnnotations] = useState<Annotation[]>([]);
  const [imageRotation, setImageRotation] = useState(0);
  const [popover, setPopover] = useState<{
    mode: 'color-picker' | 'create' | 'view';
    position: { top: number; left: number };
    annotation?: Annotation;
    pendingPin?: { pageNum: number; xPct: number; yPct: number };
    selectedColor?: string;
  } | null>(null);
  type LeftTab = 'tags' | 'notes' | 'pins';
  const [leftTab, setLeftTab] = useState<LeftTab>('tags');
```

**Fetch annotations on document load** (add inside the existing `useEffect` that loads the document, after the `getDocumentNav` call around line 50):

```tsx
    listAnnotations(docId).then(setAnnotations).catch(() => {});
```

Also reset annotation state when document changes — add to the existing `useEffect` body:

```tsx
    setAnnotations([]);
    setPopover(null);
```

**Add annotation handlers** (after the existing `handleFindSimilar` function):

```tsx
  const handlePageClick = (pageNum: number, xPct: number, yPct: number, rect: DOMRect) => {
    setPopover({
      mode: 'color-picker',
      position: { top: rect.top + (yPct / 100) * rect.height, left: rect.left + (xPct / 100) * rect.width + 16 },
      pendingPin: { pageNum, xPct, yPct },
    });
  };

  const handleColorSelect = async (color: string) => {
    if (!popover?.pendingPin || !doc) return;
    const { pageNum, xPct, yPct } = popover.pendingPin;
    try {
      const ann = await createAnnotation(doc.id, pageNum, xPct, yPct, color);
      setAnnotations(prev => [...prev, ann]);
      setPopover({
        mode: 'create',
        position: popover.position,
        annotation: ann,
        selectedColor: color,
      });
    } catch (e) {
      setPopover(null);
    }
  };

  const handleAnnotationSave = async (content: string) => {
    if (!popover?.annotation) { setPopover(null); return; }
    if (content) {
      try {
        const updated = await updateAnnotation(popover.annotation.id, { content });
        setAnnotations(prev => prev.map(a => a.id === updated.id ? updated : a));
      } catch { /* pin stays without content */ }
    }
    setPopover(null);
  };

  const handlePinClick = (ann: Annotation, rect: DOMRect) => {
    setPopover({
      mode: 'view',
      position: { top: rect.top, left: rect.right + 8 },
      annotation: ann,
    });
  };

  const handleAnnotationUpdate = async (data: { content?: string; color?: string }) => {
    if (!popover?.annotation) return;
    try {
      const updated = await updateAnnotation(popover.annotation.id, data);
      setAnnotations(prev => prev.map(a => a.id === updated.id ? updated : a));
      setPopover(null);
    } catch { /* ignore */ }
  };

  const handleAnnotationDelete = async () => {
    if (!popover?.annotation) return;
    try {
      await deleteAnnotation(popover.annotation.id);
      setAnnotations(prev => prev.filter(a => a.id !== popover.annotation!.id));
      setPopover(null);
    } catch { /* ignore */ }
  };

  const handleSidebarSelect = (ann: Annotation) => {
    const el = document.getElementById(`page-${ann.page_num}`);
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  };
```

**Update the left sidebar** to add tabs for Tags/Notes/Pins. Replace the left sidebar `<div className="viewer-left-sidebar">` section (lines ~162-201) with:

```tsx
        <div className="viewer-left-sidebar">
          {/* Tab bar */}
          <div style={{ display: 'flex', borderBottom: '1px solid var(--color-neutral-200)' }}>
            {(['tags', 'notes', 'pins'] as const).map(tab => (
              <button
                key={tab}
                onClick={() => setLeftTab(tab)}
                style={{
                  flex: 1, padding: 'var(--space-2)', textAlign: 'center', fontSize: 'var(--text-xs)',
                  fontWeight: leftTab === tab ? 700 : 400, cursor: 'pointer',
                  borderBottom: leftTab === tab ? '2px solid var(--color-primary-800)' : '2px solid transparent',
                  color: leftTab === tab ? 'var(--color-primary-800)' : 'var(--color-neutral-500)',
                  background: 'none', border: 'none', borderBottomStyle: 'solid',
                }}
              >
                {tab === 'tags' ? 'Tags' : tab === 'notes' ? 'Notes' : `Pins${annotations.length ? ` (${annotations.length})` : ''}`}
              </button>
            ))}
          </div>

          {/* Tab content */}
          {leftTab === 'tags' && (
            <>
              <div className="sidebar-section">
                <TagBar docId={doc.id} tags={doc.tags} onTagsChanged={handleTagsChanged} onAutoAdvance={handleAutoAdvance} />
              </div>
            </>
          )}

          {leftTab === 'notes' && (
            <div className="sidebar-section sidebar-section-grow">
              <NotesPanel docId={doc.id} />
            </div>
          )}

          {leftTab === 'pins' && (
            <AnnotationSidebar
              annotations={annotations}
              rotation={imageRotation}
              pageCount={doc.page_count}
              onSelect={handleSidebarSelect}
            />
          )}

          {/* AI Actions — always visible at bottom */}
          <div className="sidebar-section">
            <div className="sidebar-section-title">AI Tools</div>
            <div style={{ padding: 'var(--space-2)', display: 'flex', flexDirection: 'column', gap: 'var(--space-1-5)' }}>
              <button className="btn btn-secondary btn-sm" style={{ width: '100%', justifyContent: 'flex-start' }} onClick={handleSummarize} disabled={summaryLoading}>
                <span className="ai-indicator" style={{ padding: '0 4px', fontSize: 9 }}>AI</span>
                {summaryLoading ? 'Generating...' : 'Summarize'}
              </button>
              {onSearch && (
                <button className="btn btn-secondary btn-sm" style={{ width: '100%', justifyContent: 'flex-start' }} onClick={handleFindSimilar} disabled={similarLoading}>
                  <span className="ai-indicator" style={{ padding: '0 4px', fontSize: 9 }}>AI</span>
                  {similarLoading ? 'Searching...' : 'Find Similar'}
                </button>
              )}
              {hasNative && (
                <a href={nativeUrl(doc.id)} className="btn btn-secondary btn-sm" style={{ width: '100%', justifyContent: 'flex-start', textDecoration: 'none' }} download>
                  Download Native
                </a>
              )}
            </div>
          </div>
        </div>
```

**Pass annotation props to ImagePanel** (in `renderCenterPanel`, where `<ImagePanel>` is rendered):

Change:
```tsx
      return <ImagePanel docId={doc.id} pageCount={doc.page_count} />;
```
To:
```tsx
      return <ImagePanel docId={doc.id} pageCount={doc.page_count} annotations={annotations} onPinClick={handlePinClick} onPageClick={handlePageClick} onRotationChange={setImageRotation} />;
```

**Render the popover** (add right before the closing `</div>` of the outermost wrapper, just before `);` at the end of the return):

```tsx
      {popover && (
        <AnnotationPopover
          mode={popover.mode}
          position={popover.position}
          annotation={popover.annotation}
          selectedColor={popover.selectedColor}
          canDelete={true}
          onColorSelect={handleColorSelect}
          onSave={handleAnnotationSave}
          onUpdate={handleAnnotationUpdate}
          onDelete={handleAnnotationDelete}
          onCancel={() => setPopover(null)}
        />
      )}
```

- [ ] **Step 3: Run TypeScript check**

```bash
cd F:/Users/WCEdm/Documents/Developer/descubre/frontend && npx tsc --noEmit
```

Fix any type errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/ImagePanel.tsx frontend/src/components/DocumentViewer.tsx
git commit -m "feat: wire annotations into ImagePanel and DocumentViewer"
```

---

## Task 9: Integration Check and Deploy

**Files:**
- No new files

- [ ] **Step 1: Backend import check**

```bash
cd F:/Users/WCEdm/Documents/Developer/descubre/backend
./venv/Scripts/python.exe -c "from app.main import app; print('OK')"
```

- [ ] **Step 2: Frontend TypeScript check**

```bash
cd F:/Users/WCEdm/Documents/Developer/descubre/frontend && npx tsc --noEmit
```

- [ ] **Step 3: Frontend build**

```bash
cd F:/Users/WCEdm/Documents/Developer/descubre/frontend && npm run build
```

- [ ] **Step 4: Manual verification checklist**

1. Open a document with page images → click a page → color picker appears at cursor
2. Pick a color → pin drops, note popover appears
3. Type a note and click Save → pin saved, appears in sidebar Pins tab
4. Click away without typing → pin saved with no text, sidebar shows "No note"
5. Click an existing pin → popover shows note text, attribution, Edit/Delete
6. Click Edit → textarea appears, modify text, Save → updated
7. Click Delete → pin removed, sidebar updates
8. Click a sidebar entry → page scrolls to that pin's page
9. Rotate image to 90° → pins disappear from page (sidebar still shows list with warning)
10. Rotate back to 0° → pins reappear

- [ ] **Step 5: Commit any fixes**

```bash
git add -A
git commit -m "fix: integration fixes for phase 5 annotations"
```
