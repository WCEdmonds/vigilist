# Firebase Auth, Access Control, Cloud Storage, Ingest, Welcome Screen & Polish

**Date:** 2026-03-25
**Status:** Approved

## Problem

Descubre currently uses a single shared credential (env var username/password) with itsdangerous session cookies. This is inadequate for multi-user access control. Productions contain attorney work product and must be strictly locked to the user who imported them and those they invite.

Additionally, file storage is local-only and ingest requires CLI access. Both need to move to the cloud (Firebase Storage) with browser-based upload.

## Decisions

- **Auth provider:** Firebase Auth (frontend SDK + backend token verification)
- **Auth methods:** Email/password registration AND Google Sign-In
- **Registration:** Open (anyone can create an account)
- **Access model:** Per-production, flat (no roles). Owner + invited users get full access. Owner is whoever ingested the production.
- **Invitations:** Email invitations via Firebase Dynamic Links / built-in email
- **Auth approach:** Firebase ID tokens sent as Bearer tokens; backend verifies with firebase-admin SDK
- **File storage:** Firebase Storage (GCS under the hood)
- **Ingest:** Browser-based folder upload to Firebase Storage, async backend processing

## 1. Database Changes

### New table: `users`

| Column | Type | Notes |
|--------|------|-------|
| id | String(128), PK | Firebase UID |
| email | String(255), unique | From Firebase token |
| display_name | String(255), nullable | From Firebase token or registration |
| created_at | DateTime | server_default=now() |

Created via upsert on first successful API call (from Firebase token claims).

### New table: `production_access`

| Column | Type | Notes |
|--------|------|-------|
| id | Integer, PK | Auto-increment |
| production_id | Integer, FK → productions | |
| user_id | String(128), FK → users | |
| granted_by | String(128), FK → users | |
| granted_at | DateTime | server_default=now() |

Unique constraint on `(production_id, user_id)`.

### New table: `pending_invites`

| Column | Type | Notes |
|--------|------|-------|
| id | Integer, PK | Auto-increment |
| production_id | Integer, FK → productions | |
| email | String(255) | Invited email |
| invited_by | String(128), FK → users | |
| created_at | DateTime | server_default=now() |

Unique constraint on `(production_id, email)`. On user registration, pending invites matching the user's email are converted to `production_access` rows and deleted.

### New table: `ingest_jobs`

| Column | Type | Notes |
|--------|------|-------|
| id | UUID, PK | |
| production_id | Integer, FK → productions | |
| user_id | String(128), FK → users | |
| status | String(20) | pending, processing, complete, failed |
| total_files | Integer | Total files to process |
| processed_files | Integer | Files processed so far |
| errors | JSONB | List of error messages |
| created_at | DateTime | server_default=now() |
| completed_at | DateTime, nullable | |

### Modify `productions`

- Add `owner_id` (String(128), FK → users, nullable initially for migration)

### Modify `document_tags` and `notes`

- `applied_by` / `created_by` columns remain String but now store Firebase UID instead of shared username

### Access rule

A user can access a production if:
- They are the owner (`productions.owner_id == user.id`), OR
- They have a row in `production_access`

All document queries (list, search, view, export) are filtered by this rule.

## 2. Backend Auth

### Replace `routers/auth.py`

Remove itsdangerous, shared credentials, session cookies entirely.

**`get_current_user` dependency:**
- Extracts `Authorization: Bearer <firebase_id_token>` from request header
- Verifies token using `firebase_admin.auth.verify_id_token()`
- Upserts user in Postgres (by Firebase UID)
- Returns user object (id, email, display_name)

**`require_production_access(production_id)` dependency:**
- Wraps `get_current_user`
- Checks user is owner or has `production_access` row
- Raises 403 if not authorized

### Config changes

Remove from `Settings`:
- `auth_username`
- `auth_password`
- `storage_root`

Add to `Settings`:
- `firebase_project_id` (string, from `DESCUBRE_FIREBASE_PROJECT_ID`)
- `firebase_storage_bucket` (string, from `DESCUBRE_FIREBASE_STORAGE_BUCKET`)

Firebase Admin SDK initializes from `GOOGLE_APPLICATION_CREDENTIALS` env var (service account JSON) or project ID alone with emulator.

### New endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/auth/sync` | POST | Called after Firebase login; upserts user, resolves pending invites, returns profile |
| `/api/auth/me` | GET | Returns current user profile |
| `/api/productions` | GET | List productions the user owns or has access to |
| `/api/productions/{id}/access` | GET | List users with access (owner only) |
| `/api/productions/{id}/access` | POST | Invite user by email — creates `production_access` or `pending_invite`, sends Firebase email |
| `/api/productions/{id}/access/{user_id}` | DELETE | Revoke access (owner only) |
| `/api/ingest` | POST | Start ingest job (creates production, returns job ID) |
| `/api/ingest/{job_id}/status` | GET | Poll ingest job progress |

### Updated endpoints

All existing endpoints that use `get_current_user`:
- Document list, detail, image, stream, native, text, nav — scoped by production access; read files from Firebase Storage
- Search, export — filtered by accessible productions
- Tags, notes — scoped by production access; store Firebase UID
- Ingest — sets `owner_id` to current user

## 3. Frontend Auth

### New dependencies

- `firebase` npm package

### New file: `src/firebase.ts`

Initialize Firebase app with env vars:
- `VITE_FIREBASE_API_KEY`
- `VITE_FIREBASE_AUTH_DOMAIN`
- `VITE_FIREBASE_PROJECT_ID`
- `VITE_FIREBASE_STORAGE_BUCKET`

### Replace `useAuth` hook

- Uses `onAuthStateChanged` from Firebase
- On login/register, gets Firebase ID token and calls `POST /api/auth/sync`
- Exposes: `user`, `loading`, `login(email, password)`, `register(email, password, displayName)`, `loginWithGoogle()`, `logout()`, `getIdToken()`

### Update `api/client.ts`

- Remove `credentials: 'include'` (no cookies)
- `request()` helper gets current Firebase ID token and sends `Authorization: Bearer <token>` header
- Remove 401 → redirect logic (Firebase handles auth state)

### Replace `Login.tsx` → `AuthPage.tsx`

- Toggle between sign-in and registration forms
- Email/password fields for both modes
- Display name field for registration
- "Sign in with Google" button
- Firebase auth error handling (wrong password, email in use, etc.)
- Styled consistently with current login page aesthetic

## 4. Welcome Screen & Production Access UI

### Welcome page (`WelcomePage.tsx`)

Shown when authenticated but user has no productions (neither owned nor invited to).

- Descubre branding and brief platform explanation
- Feature highlights (search, tag, AI tools)
- Two paths: "Ingest a Production" button and "Waiting for an invite?" text
- Visually distinct from document review UI — more of an onboarding feel

### Production selector

- If user has 0 productions: show welcome page
- If user has 1 production: go straight to it
- If user has multiple: show production list/cards to pick which one to work in
- Header shows active production with dropdown switcher

### Invite flow

Production owner sees "Manage Access" in production header. Opens a panel with:
- List of users with access (email + display name)
- Email input + "Invite" button
- Remove button per user (owner only)

### Email invitation

- Backend creates `production_access` row (if user exists) or `pending_invite` row (if not)
- Generates a Firebase Dynamic Link: `https://yourapp.com/invite?production_id=X&email=Y`
- Firebase sends the email
- Invitee clicks link: if registered, lands in app and sees the production. If not, routed to registration. After sign-up, `POST /api/auth/sync` resolves pending invites.

## 5. Cloud Storage (Firebase Storage)

### Storage structure

```
productions/
  {production_id}/
    raw/                    # Original uploaded files
      DATA/
      TEXT/
      IMAGES/
      NATIVES/
    converted/              # JPEG versions of TIFFs (generated by backend)
      {bates_number}.jpg
```

### Security rules

Firebase Storage security rules restrict access per production — only users with `production_access` (or owner) can read files. Write access to `raw/` during upload is granted to the uploading user.

### Backend file serving changes

- Image endpoint (`/image/{page_num}`) reads from Firebase Storage instead of local disk
- Stream endpoint (`/stream`) generates signed URLs or proxies from Firebase Storage
- Native download endpoint does the same
- Config removes `storage_root` local path, adds Firebase Storage bucket name

### Migration

The existing `backend/storage/` directory pattern is replaced entirely. No local file storage in production.

## 6. Browser-Based Ingest

### Frontend upload flow

1. User clicks "Ingest a Production" (from welcome page or production list)
2. Opens an ingest dialog/page: production name input, description, and a folder picker button
3. User selects the production root folder via `<input webkitdirectory>`
4. Frontend validates the folder structure (checks for DATA/, TEXT/, IMAGES/, NATIVES/ subdirectories, at least one .dat and .opt file)
5. Upload progress UI: shows overall progress bar, file count, estimated time. Files upload in parallel batches (e.g., 10 concurrent uploads) to Firebase Storage under `productions/{production_id}/raw/`
6. When upload completes, frontend calls `POST /api/ingest` to trigger backend processing

### Backend processing

1. `POST /api/ingest` receives `{production_name, production_id, description}`
2. Creates an `ingest_jobs` record, returns job ID immediately
3. Background task:
   - Downloads DAT and OPT files from Firebase Storage to temp dir
   - Parses DAT/OPT as today
   - For each document: downloads TIFF from Firebase Storage, converts to JPEG, uploads JPEG back to `converted/`
   - Reads text files from Firebase Storage, indexes into Postgres
   - Creates all database records (production, documents) with `owner_id`
   - Updates `ingest_jobs` progress as it goes
4. Processing runs via FastAPI `BackgroundTasks`

### Frontend progress polling

- Frontend polls `GET /api/ingest/{job_id}/status` for progress
- Status includes: total files, processed count, errors, state (uploading/processing/complete/failed)
- Progress bar and status text update in real time

### CLI deprecation

`ingest_cli.py` becomes a dev-only convenience. Primary ingest path is browser-based.

## 7. Polish

### Auth page
- Polished Descubre branding, subtle background treatment
- Smooth transitions between sign-in and register forms

### Header
- User avatar (from Google) or initials badge
- Production name with dropdown switcher
- Cleaner sign-out flow

### Welcome page
- Visually distinct onboarding feel
- Brief feature highlights

### UI consistency
- Loading states during Firebase auth transitions (no flash of login page)
- Toast/notification for invite sent, access granted
- Smooth transitions between welcome → production list → document view

## Files Touched

### Backend
1. `app/config.py` — remove shared auth + local storage, add Firebase config
2. `app/models.py` — new User, ProductionAccess, PendingInvite, IngestJob models; modify Production, DocumentTag, Note
3. `app/schemas.py` — new schemas for user, production access, invites, ingest jobs
4. `app/routers/auth.py` — complete rewrite (Firebase token verification)
5. `app/routers/documents.py` — production access checks, Firebase Storage file serving
6. `app/routers/search.py` — filter by accessible productions
7. `app/routers/tags.py` — production access checks, store UID
8. `app/routers/notes.py` — production access checks, store UID
9. `app/routers/saved_searches.py` — scope to user
10. `app/routers/export.py` — production access checks
11. `app/routers/ingest.py` — rewrite for async cloud-based ingest
12. `app/routers/ai.py` — production access checks
13. New `app/routers/productions.py` — production list, access management, invites
14. New `app/services/storage.py` — Firebase Storage read/write helpers
15. New Alembic migration
16. `requirements.txt` — add `firebase-admin`

### Frontend
1. New `src/firebase.ts` — Firebase initialization
2. `src/hooks/useAuth.tsx` — complete rewrite
3. `src/api/client.ts` — Bearer token auth
4. `src/components/Login.tsx` → `src/components/AuthPage.tsx` — sign-in + register + Google
5. New `src/components/WelcomePage.tsx`
6. New `src/components/ProductionPicker.tsx`
7. New `src/components/ManageAccess.tsx`
8. New `src/components/IngestWizard.tsx` — folder picker, upload progress, processing status
9. `src/App.tsx` — routing for welcome/production picker/ingest/review
10. `src/styles/layout.css` — welcome page, production picker, ingest wizard styles
11. `src/styles/components.css` — toast notifications, avatar badge, progress bar
12. `package.json` — add `firebase` dependency
