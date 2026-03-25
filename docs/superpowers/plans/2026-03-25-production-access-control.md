# Production Access Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-production ownership and access control so documents are only visible to the production owner and users they invite.

**Architecture:** Add `owner_id` to productions, a `production_access` join table, and a `pending_invites` table. Create a reusable `get_accessible_production_ids()` helper that returns the set of production IDs a user can access. All document/search/export queries get filtered by this. A new `productions` router handles listing, access management, and invitations via Firebase Dynamic Links.

**Tech Stack:** SQLAlchemy, Alembic, FastAPI, Firebase Admin SDK (for email invites)

**Spec:** `docs/superpowers/specs/2026-03-25-firebase-auth-access-control-design.md` (Sections 1, 2, 4)

---

## File Structure

### Backend — new/modified files
| File | Responsibility |
|------|---------------|
| `backend/app/models.py` | Add ProductionAccess, PendingInvite models; add owner_id to Production |
| `backend/app/schemas.py` | Add ProductionAccessOut, PendingInviteOut, InviteRequest schemas |
| `backend/app/routers/auth.py` | Add pending invite resolution to sync endpoint |
| `backend/app/routers/productions.py` | New — list productions, manage access, invite users |
| `backend/app/routers/documents.py` | Scope all queries by production access |
| `backend/app/routers/search.py` | Filter search by accessible productions |
| `backend/app/routers/tags.py` | Scope document tag operations by production access |
| `backend/app/routers/notes.py` | Scope note operations by production access |
| `backend/app/routers/export.py` | Scope exports by production access |
| `backend/app/routers/ai.py` | Scope AI operations by production access |
| `backend/app/routers/ingest.py` | Set owner_id on new productions |
| `backend/app/routers/saved_searches.py` | Scope to user's own saved searches |
| `backend/app/main.py` | Register productions router |
| `backend/app/services/search.py` | Accept accessible_production_ids filter |
| Alembic migration | Add production_access, pending_invites tables + owner_id column |

### Frontend — new/modified files
| File | Responsibility |
|------|---------------|
| `frontend/src/api/client.ts` | Add productions, access management API calls |
| `frontend/src/components/ManageAccess.tsx` | New — invite panel for production owner |
| `frontend/src/App.tsx` | Add production context, show manage access button |

---

## Task 1: Add models and migration

**Files:**
- Modify: `backend/app/models.py`
- Create: Alembic migration (autogenerate or manual)

- [ ] **Step 1: Add ProductionAccess and PendingInvite models, add owner_id to Production**

In `backend/app/models.py`, add `owner_id` to the `Production` class:

```python
class Production(Base):
    __tablename__ = "productions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False, unique=True)
    description = Column(Text, nullable=True)
    owner_id = Column(String(128), ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    owner = relationship("User", foreign_keys=[owner_id])
    documents = relationship("Document", back_populates="production")
    access_list = relationship("ProductionAccess", back_populates="production", cascade="all, delete-orphan")
```

Add after `SavedSearch`:

```python
class ProductionAccess(Base):
    __tablename__ = "production_access"
    __table_args__ = (
        UniqueConstraint("production_id", "user_id", name="uq_prod_user"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    production_id = Column(Integer, ForeignKey("productions.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(String(128), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    granted_by = Column(String(128), ForeignKey("users.id"), nullable=False)
    granted_at = Column(DateTime, server_default=func.now(), nullable=False)

    production = relationship("Production", back_populates="access_list")
    user = relationship("User", foreign_keys=[user_id])
    granter = relationship("User", foreign_keys=[granted_by])


class PendingInvite(Base):
    __tablename__ = "pending_invites"
    __table_args__ = (
        UniqueConstraint("production_id", "email", name="uq_prod_email_invite"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    production_id = Column(Integer, ForeignKey("productions.id", ondelete="CASCADE"), nullable=False)
    email = Column(String(255), nullable=False)
    invited_by = Column(String(128), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    production = relationship("Production")
    inviter = relationship("User", foreign_keys=[invited_by])
```

- [ ] **Step 2: Create Alembic migration**

Run: `cd backend && alembic revision --autogenerate -m "add production access control"`

If autogenerate fails (no DB), write migration manually with:
- Add `owner_id` column to `productions` (nullable, FK to users.id)
- Create `production_access` table
- Create `pending_invites` table

- [ ] **Step 3: Verify imports**

Run: `cd backend && source venv/Scripts/activate && python -c "from app.models import ProductionAccess, PendingInvite; print('OK')"`

- [ ] **Step 4: Commit**

```bash
git add backend/app/models.py backend/alembic/versions/
git commit -m "feat: add ProductionAccess, PendingInvite models and owner_id"
```

---

## Task 2: Add schemas for access control

**Files:**
- Modify: `backend/app/schemas.py`

- [ ] **Step 1: Add new schemas**

Add to `backend/app/schemas.py`:

```python
# ── Production Access ──

class ProductionWithAccess(BaseModel):
    id: int
    name: str
    description: str | None
    owner_id: str | None
    is_owner: bool = False
    created_at: datetime

    model_config = {"from_attributes": True}


class ProductionAccessOut(BaseModel):
    id: int
    user_id: str
    user_email: str
    user_display_name: str | None
    granted_by: str
    granted_at: datetime

    model_config = {"from_attributes": True}


class InviteRequest(BaseModel):
    email: str


class PendingInviteOut(BaseModel):
    id: int
    email: str
    invited_by: str
    created_at: datetime

    model_config = {"from_attributes": True}
```

- [ ] **Step 2: Commit**

```bash
git add backend/app/schemas.py
git commit -m "feat: add production access control schemas"
```

---

## Task 3: Create productions router

**Files:**
- Create: `backend/app/routers/productions.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Create the productions router**

Create `backend/app/routers/productions.py`:

```python
"""Production listing and access management."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import PendingInvite, Production, ProductionAccess, User
from app.routers.auth import get_current_user
from app.schemas import (
    InviteRequest,
    PendingInviteOut,
    ProductionAccessOut,
    ProductionWithAccess,
)

router = APIRouter(prefix="/api/productions", tags=["productions"])


async def get_accessible_production_ids(db: AsyncSession, user: User) -> list[int]:
    """Return list of production IDs the user can access (owner or granted)."""
    owned = select(Production.id).where(Production.owner_id == user.id)
    granted = select(ProductionAccess.production_id).where(ProductionAccess.user_id == user.id)
    result = await db.execute(owned.union(granted))
    return [row[0] for row in result.all()]


@router.get("", response_model=list[ProductionWithAccess])
async def list_productions(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List productions the user owns or has access to."""
    prod_ids = await get_accessible_production_ids(db, user)
    if not prod_ids:
        return []
    result = await db.execute(
        select(Production)
        .where(Production.id.in_(prod_ids))
        .order_by(Production.created_at.desc())
    )
    prods = result.scalars().all()
    return [
        ProductionWithAccess(
            id=p.id,
            name=p.name,
            description=p.description,
            owner_id=p.owner_id,
            is_owner=(p.owner_id == user.id),
            created_at=p.created_at,
        )
        for p in prods
    ]


@router.get("/{production_id}/access", response_model=list[ProductionAccessOut])
async def list_access(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List users with access to a production. Owner only."""
    prod = await db.get(Production, production_id)
    if not prod:
        raise HTTPException(status_code=404, detail="Production not found")
    if prod.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Only the owner can manage access")

    result = await db.execute(
        select(ProductionAccess, User)
        .join(User, ProductionAccess.user_id == User.id)
        .where(ProductionAccess.production_id == production_id)
        .order_by(ProductionAccess.granted_at)
    )
    rows = result.all()
    return [
        ProductionAccessOut(
            id=pa.id,
            user_id=pa.user_id,
            user_email=u.email,
            user_display_name=u.display_name,
            granted_by=pa.granted_by,
            granted_at=pa.granted_at,
        )
        for pa, u in rows
    ]


@router.get("/{production_id}/invites", response_model=list[PendingInviteOut])
async def list_pending_invites(
    production_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List pending invites for a production. Owner only."""
    prod = await db.get(Production, production_id)
    if not prod:
        raise HTTPException(status_code=404, detail="Production not found")
    if prod.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Only the owner can manage access")

    result = await db.execute(
        select(PendingInvite)
        .where(PendingInvite.production_id == production_id)
        .order_by(PendingInvite.created_at)
    )
    return result.scalars().all()


@router.post("/{production_id}/access")
async def invite_user(
    production_id: int,
    body: InviteRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Invite a user by email. Creates access if user exists, pending invite if not."""
    prod = await db.get(Production, production_id)
    if not prod:
        raise HTTPException(status_code=404, detail="Production not found")
    if prod.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Only the owner can invite users")

    email = body.email.strip().lower()

    # Check if user already exists
    result = await db.execute(select(User).where(User.email == email))
    target_user = result.scalar_one_or_none()

    if target_user:
        # Check if already has access
        existing = await db.execute(
            select(ProductionAccess).where(
                ProductionAccess.production_id == production_id,
                ProductionAccess.user_id == target_user.id,
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="User already has access")

        # Grant access directly
        pa = ProductionAccess(
            production_id=production_id,
            user_id=target_user.id,
            granted_by=user.id,
        )
        db.add(pa)
        await db.commit()
        return {"status": "granted", "email": email}
    else:
        # Create pending invite
        existing = await db.execute(
            select(PendingInvite).where(
                PendingInvite.production_id == production_id,
                PendingInvite.email == email,
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Invite already pending")

        invite = PendingInvite(
            production_id=production_id,
            email=email,
            invited_by=user.id,
        )
        db.add(invite)
        await db.commit()

        # TODO: Send Firebase Dynamic Link email in a later plan
        return {"status": "invited", "email": email}


@router.delete("/{production_id}/access/{user_id}")
async def revoke_access(
    production_id: int,
    user_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Revoke a user's access. Owner only."""
    prod = await db.get(Production, production_id)
    if not prod:
        raise HTTPException(status_code=404, detail="Production not found")
    if prod.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Only the owner can revoke access")

    result = await db.execute(
        select(ProductionAccess).where(
            ProductionAccess.production_id == production_id,
            ProductionAccess.user_id == user_id,
        )
    )
    pa = result.scalar_one_or_none()
    if not pa:
        raise HTTPException(status_code=404, detail="Access entry not found")

    await db.delete(pa)
    await db.commit()
    return {"ok": True}
```

- [ ] **Step 2: Register the router in main.py**

Read `backend/app/main.py`. Add import and registration:

```python
from app.routers import ai, auth, documents, export, ingest, notes, productions, saved_searches, search, tags

# ... existing includes ...
app.include_router(productions.router)
```

- [ ] **Step 3: Verify imports**

Run: `cd backend && source venv/Scripts/activate && python -c "from app.main import app; print('OK')"`

- [ ] **Step 4: Commit**

```bash
git add backend/app/routers/productions.py backend/app/main.py
git commit -m "feat: add productions router with access management"
```

---

## Task 4: Resolve pending invites on auth sync

**Files:**
- Modify: `backend/app/routers/auth.py`

- [ ] **Step 1: Update sync endpoint to resolve pending invites**

Read `backend/app/routers/auth.py`. In the `sync_user` endpoint, after the user is upserted (via the `get_current_user` dependency), add logic to resolve pending invites:

```python
from app.models import PendingInvite, ProductionAccess

@router.post("/sync", response_model=UserOut)
async def sync_user(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Called after Firebase login to ensure user exists in backend DB.
    Also resolves any pending invites for this user's email.
    """
    # Resolve pending invites
    result = await db.execute(
        select(PendingInvite).where(PendingInvite.email == user.email.lower())
    )
    pending = result.scalars().all()
    for invite in pending:
        # Check if access already exists
        existing = await db.execute(
            select(ProductionAccess).where(
                ProductionAccess.production_id == invite.production_id,
                ProductionAccess.user_id == user.id,
            )
        )
        if not existing.scalar_one_or_none():
            db.add(ProductionAccess(
                production_id=invite.production_id,
                user_id=user.id,
                granted_by=invite.invited_by,
            ))
        await db.delete(invite)

    await db.commit()
    return user
```

- [ ] **Step 2: Verify imports**

Run: `cd backend && source venv/Scripts/activate && python -c "from app.routers.auth import router; print('OK')"`

- [ ] **Step 3: Commit**

```bash
git add backend/app/routers/auth.py
git commit -m "feat: resolve pending invites on auth sync"
```

---

## Task 5: Set owner_id on ingest

**Files:**
- Modify: `backend/app/routers/ingest.py`
- Modify: `backend/app/services/ingest.py`

- [ ] **Step 1: Pass user to ingest service**

Read both files. Update `backend/app/routers/ingest.py` to pass the user to the ingest service:

Change `_user` to `user` (so it's not ignored) and pass `user.id` to the service:

```python
@router.post("/ingest", response_model=IngestResponse)
async def ingest(
    body: IngestRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await ingest_production(
        db,
        production_name=body.production_name,
        production_root=body.production_root,
        description=body.description,
        owner_id=user.id,
    )
    return result
```

- [ ] **Step 2: Update ingest service to accept and set owner_id**

In `backend/app/services/ingest.py`, add `owner_id` parameter to `ingest_production`:

```python
async def ingest_production(
    db: AsyncSession,
    production_name: str,
    production_root: str,
    description: str = "",
    owner_id: str | None = None,
) -> dict:
```

When creating the Production object, set owner_id:

```python
production = Production(name=production_name, description=description, owner_id=owner_id)
```

- [ ] **Step 3: Commit**

```bash
git add backend/app/routers/ingest.py backend/app/services/ingest.py
git commit -m "feat: set owner_id when ingesting productions"
```

---

## Task 6: Scope all document/search/export queries by production access

**Files:**
- Modify: `backend/app/routers/documents.py`
- Modify: `backend/app/routers/search.py`
- Modify: `backend/app/services/search.py`
- Modify: `backend/app/routers/export.py`
- Modify: `backend/app/routers/ai.py`
- Modify: `backend/app/routers/tags.py`
- Modify: `backend/app/routers/notes.py`
- Modify: `backend/app/routers/saved_searches.py`

This is the largest task. The pattern is the same everywhere:

1. Change `_user: User` to `user: User` (so the user object is available)
2. Call `get_accessible_production_ids(db, user)` to get allowed production IDs
3. Add a filter: `Document.production_id.in_(accessible_ids)` or equivalent
4. For endpoints that take a `doc_id`, verify the document's production is accessible

- [ ] **Step 1: Update documents.py**

Read the file. Import `get_accessible_production_ids` from the productions router:

```python
from app.routers.productions import get_accessible_production_ids
```

In `list_documents`: change `_user` to `user`, get accessible IDs, add filter:

```python
async def list_documents(
    ...
    user: User = Depends(get_current_user),
):
    accessible = await get_accessible_production_ids(db, user)
    query = select(Document).options(...).where(Document.production_id.in_(accessible))
    count_query = select(func.count(Document.id)).where(Document.production_id.in_(accessible))
    # ... rest stays the same but with existing production_id/tag_id filters added on top
```

For single-document endpoints (`get_document`, `get_by_bates`, `get_image`, `get_native`, `stream_native`, `get_text`, `get_nav`): after fetching the document, verify access:

```python
accessible = await get_accessible_production_ids(db, user)
# ... after fetching doc ...
if doc.production_id not in accessible:
    raise HTTPException(status_code=403, detail="Access denied")
```

- [ ] **Step 2: Update search.py and services/search.py**

In `search.py`: get accessible IDs, pass to search service.
In `services/search.py`: add `accessible_production_ids` parameter, add filter:

```python
async def search_documents(
    db, query, production_id=None, page=1, per_page=50, sort="relevance",
    accessible_production_ids: list[int] | None = None,
):
    # ... existing code ...
    if accessible_production_ids is not None:
        where.append(Document.production_id.in_(accessible_production_ids))
```

- [ ] **Step 3: Update export.py**

Get accessible IDs, filter document queries.

- [ ] **Step 4: Update ai.py**

For summarize, nl-search, find-similar: verify document access.

- [ ] **Step 5: Update tags.py**

For document tag endpoints: verify document's production is accessible.

- [ ] **Step 6: Update notes.py**

Same pattern: verify document's production is accessible.

- [ ] **Step 7: Update saved_searches.py**

Scope to user's own saved searches:

```python
query = select(SavedSearch).where(SavedSearch.created_by == user.id).order_by(...)
```

- [ ] **Step 8: Verify backend starts**

Run: `cd backend && source venv/Scripts/activate && python -c "from app.main import app; print('OK')"`

- [ ] **Step 9: Commit**

```bash
git add backend/app/routers/ backend/app/services/search.py
git commit -m "feat: scope all queries by production access control"
```

---

## Task 7: Add frontend API calls and ManageAccess component

**Files:**
- Modify: `frontend/src/api/client.ts`
- Create: `frontend/src/components/ManageAccess.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/types/index.ts`

- [ ] **Step 1: Add types**

Read `frontend/src/types/index.ts`. Add:

```typescript
export interface ProductionInfo {
  id: number;
  name: string;
  description: string | null;
  owner_id: string | null;
  is_owner: boolean;
  created_at: string;
}

export interface ProductionAccessEntry {
  id: number;
  user_id: string;
  user_email: string;
  user_display_name: string | null;
  granted_by: string;
  granted_at: string;
}

export interface PendingInviteEntry {
  id: number;
  email: string;
  invited_by: string;
  created_at: string;
}
```

- [ ] **Step 2: Add API functions**

Add to `frontend/src/api/client.ts`:

```typescript
// ── Productions ──

export const listProductions = () =>
  request<ProductionInfo[]>('/api/productions');

export const getProductionAccess = (productionId: number) =>
  request<ProductionAccessEntry[]>(`/api/productions/${productionId}/access`);

export const getProductionInvites = (productionId: number) =>
  request<PendingInviteEntry[]>(`/api/productions/${productionId}/invites`);

export const inviteUser = (productionId: number, email: string) =>
  request<{ status: string; email: string }>(`/api/productions/${productionId}/access`, json({ email }));

export const revokeAccess = (productionId: number, userId: string) =>
  request(`/api/productions/${productionId}/access/${userId}`, { method: 'DELETE' });
```

Add the type imports at the top.

- [ ] **Step 3: Create ManageAccess.tsx**

Create a panel component that shows:
- List of users with access (email, name, revoke button)
- List of pending invites
- Email input + Invite button
- Only shown to owner

- [ ] **Step 4: Add production context to App.tsx**

Read App.tsx. Add state for the active production and load productions on mount. Show a production picker if multiple, or auto-select if one. Add a "Manage Access" button in the header for the owner.

- [ ] **Step 5: Verify TypeScript compiles**

Run: `cd frontend && npx tsc --noEmit`

- [ ] **Step 6: Commit**

```bash
git add frontend/src/
git commit -m "feat: add production access UI with invite flow"
```
