# Cloud Storage + Browser Ingest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move file storage from local disk to Firebase Storage, replace CLI ingest with browser-based folder upload, and add async backend processing with progress tracking.

**Architecture:** Frontend uses Firebase Storage SDK to upload files directly from the browser (parallel batches). After upload, frontend calls the backend to trigger processing. Backend downloads files from Firebase Storage to a temp directory, runs the existing parse/convert pipeline, uploads converted JPEGs back to Firebase Storage, and updates the database. An `ingest_jobs` table tracks progress. File serving endpoints (images, natives, streaming) switch from local `FileResponse` to proxying from Firebase Storage.

**Tech Stack:** `firebase-admin` (Python, Storage), `firebase/storage` (JS SDK), Firebase Storage, FastAPI BackgroundTasks

**Spec:** `docs/superpowers/specs/2026-03-25-firebase-auth-access-control-design.md` (Sections 5-6)

---

## File Structure

### Backend — new/modified files
| File | Responsibility |
|------|---------------|
| `backend/app/config.py` | Add `firebase_storage_bucket` setting |
| `backend/app/models.py` | Add `IngestJob` model |
| `backend/app/schemas.py` | Add `IngestJobOut`, update `IngestResponse` |
| `backend/app/services/storage.py` | New — Firebase Storage read/write/download helpers |
| `backend/app/services/ingest.py` | Rewrite to read from Firebase Storage, upload converted files back |
| `backend/app/routers/ingest.py` | Rewrite for async processing with job tracking |
| `backend/app/routers/documents.py` | Switch file serving from local to Firebase Storage |
| Alembic migration | Add `ingest_jobs` table |

### Frontend — new/modified files
| File | Responsibility |
|------|---------------|
| `frontend/src/firebase.ts` | Export Firebase Storage instance |
| `frontend/src/components/IngestWizard.tsx` | New — folder picker, upload progress, processing status |
| `frontend/src/api/client.ts` | Add ingest job API calls |
| `frontend/src/App.tsx` | Add ingest wizard trigger |
| `frontend/src/types/index.ts` | Add IngestJob type |

---

## Task 1: Add Firebase Storage config and helpers

**Files:**
- Modify: `backend/app/config.py`
- Create: `backend/app/services/storage.py`
- Modify: `backend/requirements.txt` (add `google-cloud-storage`)

- [ ] **Step 1: Update config**

Add to `Settings` in `backend/app/config.py`:
```python
firebase_storage_bucket: str = ""
```

- [ ] **Step 2: Add google-cloud-storage to requirements**

Add `google-cloud-storage>=2.18` to `backend/requirements.txt`. The `firebase-admin` SDK uses this under the hood but we need it explicitly for bucket operations.

- [ ] **Step 3: Create storage service**

Create `backend/app/services/storage.py`:

```python
"""Firebase Storage helper functions."""

import os
import tempfile
from pathlib import Path

import firebase_admin
from firebase_admin import storage

from app.config import settings


def get_bucket():
    """Get the Firebase Storage bucket."""
    return storage.bucket(settings.firebase_storage_bucket)


def download_file(remote_path: str, local_path: str) -> str:
    """Download a file from Firebase Storage to a local path."""
    bucket = get_bucket()
    blob = bucket.blob(remote_path)
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    blob.download_to_filename(local_path)
    return local_path


def download_to_temp(remote_path: str, suffix: str = "") -> str:
    """Download a file from Firebase Storage to a temp file. Returns temp path."""
    bucket = get_bucket()
    blob = bucket.blob(remote_path)
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    blob.download_to_filename(tmp_path)
    return tmp_path


def upload_file(local_path: str, remote_path: str, content_type: str | None = None) -> str:
    """Upload a local file to Firebase Storage. Returns the remote path."""
    bucket = get_bucket()
    blob = bucket.blob(remote_path)
    blob.upload_from_filename(local_path, content_type=content_type)
    return remote_path


def list_files(prefix: str) -> list[str]:
    """List all file paths under a prefix in Firebase Storage."""
    bucket = get_bucket()
    blobs = bucket.list_blobs(prefix=prefix)
    return [blob.name for blob in blobs]


def get_download_bytes(remote_path: str) -> bytes:
    """Download a file from Firebase Storage as bytes."""
    bucket = get_bucket()
    blob = bucket.blob(remote_path)
    return blob.download_as_bytes()


def get_signed_url(remote_path: str, expiration_minutes: int = 60) -> str:
    """Generate a signed URL for a file in Firebase Storage."""
    import datetime
    bucket = get_bucket()
    blob = bucket.blob(remote_path)
    url = blob.generate_signed_url(
        expiration=datetime.timedelta(minutes=expiration_minutes),
        method="GET",
    )
    return url


def file_exists(remote_path: str) -> bool:
    """Check if a file exists in Firebase Storage."""
    bucket = get_bucket()
    blob = bucket.blob(remote_path)
    return blob.exists()
```

- [ ] **Step 4: Install dependency and verify**

```bash
cd backend && source venv/Scripts/activate && pip install google-cloud-storage>=2.18
python -c "from app.services.storage import get_bucket; print('OK')"
```

- [ ] **Step 5: Commit**

---

## Task 2: Add IngestJob model and migration

**Files:**
- Modify: `backend/app/models.py`
- Modify: `backend/app/schemas.py`
- Create: Alembic migration

- [ ] **Step 1: Add IngestJob model**

Add to `backend/app/models.py` after `PendingInvite`:

```python
class IngestJob(Base):
    __tablename__ = "ingest_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    production_id = Column(Integer, ForeignKey("productions.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(String(128), ForeignKey("users.id"), nullable=False)
    status = Column(String(20), nullable=False, default="pending")  # pending, processing, complete, failed
    total_files = Column(Integer, nullable=False, default=0)
    processed_files = Column(Integer, nullable=False, default=0)
    errors = Column(JSONB, nullable=False, default=list)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    completed_at = Column(DateTime, nullable=True)

    production = relationship("Production")
    user = relationship("User")
```

- [ ] **Step 2: Add schemas**

Add to `backend/app/schemas.py`:

```python
class IngestJobOut(BaseModel):
    id: UUID
    production_id: int
    production_name: str = ""
    status: str
    total_files: int
    processed_files: int
    errors: list[str]
    created_at: datetime
    completed_at: datetime | None

    model_config = {"from_attributes": True}
```

- [ ] **Step 3: Create migration**

Generate or manually write Alembic migration for `ingest_jobs` table.

- [ ] **Step 4: Commit**

---

## Task 3: Rewrite ingest router for async cloud processing

**Files:**
- Modify: `backend/app/routers/ingest.py`
- Modify: `backend/app/schemas.py` (update IngestRequest)

- [ ] **Step 1: Rewrite ingest router**

Replace `backend/app/routers/ingest.py` entirely:

```python
"""Ingest endpoints: start processing, check status."""

import uuid as uuid_mod
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import IngestJob, Production, User
from app.routers.auth import get_current_user
from app.schemas import IngestJobOut

router = APIRouter(prefix="/api", tags=["ingest"])


@router.post("/ingest", response_model=IngestJobOut)
async def start_ingest(
    body: dict,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Create a production and start async ingest processing.

    Frontend has already uploaded files to Firebase Storage under
    productions/{production_id}/raw/. This endpoint creates the
    production record and kicks off background processing.
    """
    production_name = body.get("production_name", "").strip()
    description = body.get("description", "").strip()
    total_files = body.get("total_files", 0)

    if not production_name:
        raise HTTPException(status_code=400, detail="production_name is required")

    # Check for duplicate production name
    existing = await db.execute(
        select(Production).where(Production.name == production_name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Production with this name already exists")

    # Create production
    production = Production(name=production_name, description=description, owner_id=user.id)
    db.add(production)
    await db.flush()

    # Create ingest job
    job = IngestJob(
        production_id=production.id,
        user_id=user.id,
        status="processing",
        total_files=total_files,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    await db.refresh(production)

    # Start background processing
    background_tasks.add_task(
        run_ingest_job,
        job_id=str(job.id),
        production_id=production.id,
        production_name=production_name,
    )

    return IngestJobOut(
        id=job.id,
        production_id=production.id,
        production_name=production_name,
        status=job.status,
        total_files=job.total_files,
        processed_files=0,
        errors=[],
        created_at=job.created_at,
        completed_at=None,
    )


@router.get("/ingest/{job_id}/status", response_model=IngestJobOut)
async def get_ingest_status(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Poll ingest job progress."""
    job = await db.get(IngestJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.user_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    prod = await db.get(Production, job.production_id)

    return IngestJobOut(
        id=job.id,
        production_id=job.production_id,
        production_name=prod.name if prod else "",
        status=job.status,
        total_files=job.total_files,
        processed_files=job.processed_files,
        errors=job.errors or [],
        created_at=job.created_at,
        completed_at=job.completed_at,
    )


async def run_ingest_job(job_id: str, production_id: int, production_name: str):
    """Background task that processes uploaded files from Firebase Storage."""
    from app.database import async_session_factory
    from app.services.ingest import ingest_from_storage

    async with async_session_factory() as db:
        try:
            await ingest_from_storage(db, job_id, production_id, production_name)
        except Exception as e:
            import logging
            logging.getLogger(__name__).exception("Ingest job failed")
            job = await db.get(IngestJob, job_id)
            if job:
                job.status = "failed"
                job.errors = (job.errors or []) + [str(e)]
                await db.commit()
```

- [ ] **Step 2: Add async_session_factory to database.py**

Read `backend/app/database.py`. Add a session factory that can be used outside of FastAPI dependency injection (for background tasks):

```python
from sqlalchemy.ext.asyncio import async_sessionmaker

async_session_factory = async_sessionmaker(engine, expire_on_commit=False)
```

- [ ] **Step 3: Commit**

---

## Task 4: Rewrite ingest service for Firebase Storage

**Files:**
- Modify: `backend/app/services/ingest.py`

- [ ] **Step 1: Add `ingest_from_storage` function**

Keep the existing `ingest_production` function (for CLI backwards compatibility). Add a new function `ingest_from_storage` that:

1. Downloads DAT and OPT files from Firebase Storage (`productions/{production_id}/raw/DATA/`)
2. Parses them using existing `parse_dat`/`parse_opt`
3. For each document:
   - Downloads text file from Firebase Storage, reads content
   - Downloads TIFF images, converts to JPEG locally, uploads JPEGs to `productions/{production_id}/converted/`
   - Stores Firebase Storage paths (not local paths) in `image_paths` and `native_path`
4. Updates `IngestJob` progress as it goes
5. Runs tsvector update and AI title generation
6. Marks job as complete

```python
async def ingest_from_storage(
    db: AsyncSession,
    job_id: str,
    production_id: int,
    production_name: str,
) -> None:
    """Process uploaded production files from Firebase Storage."""
    import tempfile
    import shutil
    from datetime import datetime
    from app.models import IngestJob
    from app.services.storage import download_file, upload_file, list_files, download_to_temp

    job = await db.get(IngestJob, job_id)
    if not job:
        return

    prefix = f"productions/{production_id}/raw/"
    errors: list[str] = []

    # Create temp directory for processing
    tmp_dir = tempfile.mkdtemp(prefix=f"ingest_{production_id}_")

    try:
        # Download DAT and OPT files
        data_files = list_files(f"{prefix}DATA/")
        dat_remote = next((f for f in data_files if f.lower().endswith(".dat")), None)
        opt_remote = next((f for f in data_files if f.lower().endswith(".opt")), None)

        if not dat_remote:
            raise FileNotFoundError("No .dat file found in uploaded DATA/ folder")
        if not opt_remote:
            raise FileNotFoundError("No .opt file found in uploaded DATA/ folder")

        dat_local = os.path.join(tmp_dir, "data.dat")
        opt_local = os.path.join(tmp_dir, "data.opt")
        download_file(dat_remote, dat_local)
        download_file(opt_remote, opt_local)

        # Parse
        dat_records = parse_dat(dat_local)
        opt_pages = parse_opt(opt_local)

        job.total_files = len(dat_records)
        await db.commit()

        converted_tmp = os.path.join(tmp_dir, "converted")
        os.makedirs(converted_tmp, exist_ok=True)

        documents = []
        for i, record in enumerate(dat_records):
            bates_begin = record.get("Begin Bates", "").strip()
            bates_end = record.get("End Bates", "").strip()
            page_count_str = record.get("Page Count", "1").strip()
            text_link = record.get("Text Link", "").strip()
            native_link = record.get("Native Link", "").strip()

            if not bates_begin:
                errors.append(f"Row {i+1}: missing Begin Bates")
                continue

            page_count = int(page_count_str) if page_count_str.isdigit() else 1

            # Read text from Firebase Storage
            text_content = None
            if text_link:
                text_remote = f"{prefix}{text_link.replace(chr(92), '/')}"
                try:
                    from app.services.storage import get_download_bytes
                    text_bytes = get_download_bytes(text_remote)
                    text_content = text_bytes.decode("utf-8-sig", errors="replace")
                    text_content = text_content.replace("\x00", "")
                except Exception:
                    errors.append(f"{bates_begin}: text file not found: {text_link}")

            # Convert images
            raw_image_paths = opt_pages.get(bates_begin, [])
            jpeg_storage_paths = []
            for rel_path in raw_image_paths:
                remote_tiff = f"{prefix}{rel_path.replace(chr(92), '/')}"
                try:
                    tiff_local = download_to_temp(remote_tiff, suffix=".tif")
                    stem = Path(rel_path).stem
                    jpeg_local = os.path.join(converted_tmp, f"{stem}.jpg")
                    from PIL import Image
                    with Image.open(tiff_local) as img:
                        if img.mode not in ("RGB", "L"):
                            img = img.convert("RGB")
                        img.save(jpeg_local, "JPEG", quality=85)
                    os.unlink(tiff_local)

                    # Upload JPEG to Firebase Storage
                    jpeg_remote = f"productions/{production_id}/converted/{stem}.jpg"
                    upload_file(jpeg_local, jpeg_remote, content_type="image/jpeg")
                    jpeg_storage_paths.append(jpeg_remote)
                except Exception as e:
                    errors.append(f"{bates_begin}: image conversion failed: {rel_path}: {e}")
                    jpeg_storage_paths.append("")

            # Native path — store the Firebase Storage path
            native_storage_path = None
            if native_link:
                native_storage_path = f"{prefix}{native_link.replace(chr(92), '/')}"

            metadata = {}
            for key, value in record.items():
                if key not in FIELD_MAP and value:
                    metadata[key] = value

            doc = Document(
                production_id=production_id,
                bates_begin=bates_begin,
                bates_end=bates_end,
                page_count=page_count,
                metadata_=metadata,
                text_content=text_content,
                native_path=native_storage_path,
                image_paths=jpeg_storage_paths,
            )
            documents.append(doc)

            # Update progress
            job.processed_files = i + 1
            job.errors = errors.copy()
            if (i + 1) % 50 == 0:
                await db.commit()

        db.add_all(documents)
        await db.flush()

        # Update tsvector
        await db.execute(
            text(
                "UPDATE documents SET text_search_vector = to_tsvector('english', COALESCE(text_content, '')) "
                "WHERE production_id = :pid"
            ),
            {"pid": production_id},
        )

        # Generate AI titles
        if settings.anthropic_api_key:
            texts = [(str(doc.id), doc.text_content) for doc in documents]
            titles = await generate_titles_batch(texts)
            for doc in documents:
                title = titles.get(str(doc.id))
                if title:
                    doc.title = title
            await db.flush()

        job.status = "complete"
        job.processed_files = len(documents)
        job.errors = errors
        job.completed_at = datetime.utcnow()
        await db.commit()

    except Exception as e:
        job.status = "failed"
        job.errors = errors + [str(e)]
        await db.commit()
        raise
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
```

- [ ] **Step 2: Commit**

---

## Task 5: Update file serving endpoints for Firebase Storage

**Files:**
- Modify: `backend/app/routers/documents.py`

- [ ] **Step 1: Update get_image to serve from Firebase Storage**

The `image_paths` now store Firebase Storage paths (like `productions/1/converted/SCHLEGEL 000001.jpg`) instead of local paths. Update `get_image`:

```python
@router.get("/{doc_id}/image/{page_num}")
async def get_image(...):
    # ... access checks same as before ...
    raw_path = doc.image_paths[page_num - 1]
    if not raw_path:
        raise HTTPException(status_code=404, detail="Image file not found")

    # Check if it's a Firebase Storage path or local path
    if raw_path.startswith("productions/"):
        # Firebase Storage path — proxy the file
        from app.services.storage import get_download_bytes
        try:
            data = get_download_bytes(raw_path)
        except Exception:
            raise HTTPException(status_code=404, detail="Image file not found in storage")
        from fastapi.responses import Response
        return Response(content=data, media_type="image/jpeg")
    else:
        # Legacy local path
        path = Path(raw_path.replace("\\", "/")).resolve()
        if not path.exists():
            raise HTTPException(status_code=404, detail="Image file not found")
        return FileResponse(str(path), media_type="image/jpeg")
```

- [ ] **Step 2: Update get_native for Firebase Storage**

Same pattern — check if `native_path` starts with `productions/`, if so proxy from storage. Keep legacy local path support.

- [ ] **Step 3: Update stream_native for Firebase Storage**

For streaming, generate a signed URL and redirect, or proxy with range request support. The simplest approach for Firebase Storage is to generate a signed URL and redirect:

```python
if doc.native_path.startswith("productions/"):
    from app.services.storage import get_signed_url
    url = get_signed_url(doc.native_path, expiration_minutes=60)
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=url)
```

This lets the browser handle range requests natively against GCS.

- [ ] **Step 4: Verify**

```bash
cd backend && source venv/Scripts/activate && python -c "from app.main import app; print('OK')"
```

- [ ] **Step 5: Commit**

---

## Task 6: Frontend — Firebase Storage export and IngestWizard

**Files:**
- Modify: `frontend/src/firebase.ts`
- Modify: `frontend/src/types/index.ts`
- Modify: `frontend/src/api/client.ts`
- Create: `frontend/src/components/IngestWizard.tsx`
- Modify: `frontend/src/App.tsx`

- [ ] **Step 1: Export Firebase Storage from firebase.ts**

Add to `frontend/src/firebase.ts`:
```typescript
import { getStorage } from 'firebase/storage';
export const firebaseStorage = getStorage(app);
```

- [ ] **Step 2: Add types**

Add to `frontend/src/types/index.ts`:
```typescript
export interface IngestJob {
  id: string;
  production_id: number;
  production_name: string;
  status: 'pending' | 'processing' | 'complete' | 'failed';
  total_files: number;
  processed_files: number;
  errors: string[];
  created_at: string;
  completed_at: string | null;
}
```

- [ ] **Step 3: Add API functions**

Add to `frontend/src/api/client.ts`:
```typescript
export const startIngest = (productionName: string, description: string, totalFiles: number) =>
  request<IngestJob>('/api/ingest', json({ production_name: productionName, description, total_files: totalFiles }));

export const getIngestStatus = (jobId: string) =>
  request<IngestJob>(`/api/ingest/${jobId}/status`);
```

- [ ] **Step 4: Create IngestWizard component**

Create `frontend/src/components/IngestWizard.tsx`. This component:

1. Shows a production name input and folder picker button
2. User selects folder via `<input webkitdirectory>`
3. Validates folder structure (DATA/, at least one .dat file)
4. Uploads all files to Firebase Storage under `productions/{temp_id}/raw/` using `uploadBytesResumable` in parallel batches
5. Shows upload progress (file count, progress bar)
6. After upload, calls `POST /api/ingest` to start backend processing
7. Polls `GET /api/ingest/{job_id}/status` for processing progress
8. Shows processing progress and completion/error state

Key implementation details:
- Use `ref(firebaseStorage, path)` and `uploadBytesResumable` from `firebase/storage`
- Upload in batches of 10 concurrent files
- Track total bytes uploaded / total bytes for progress
- After all files uploaded, call `startIngest` API
- Poll status every 2 seconds until complete or failed

- [ ] **Step 5: Integrate into App.tsx**

Add an "Ingest Production" button that opens the IngestWizard. Show it in the header or in the empty state when no productions exist. After ingest completes, reload productions.

- [ ] **Step 6: Verify TypeScript**

```bash
cd frontend && npx tsc --noEmit
```

- [ ] **Step 7: Commit**
