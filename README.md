# Vigilist

A lightweight, self-hosted e-discovery review platform for small-to-mid-size legal teams — a leaner alternative to heavyweight tools like Relativity.

Vigilist ingests standard Relativity-format productions (DAT/OPT load files, natives, and images), makes them searchable with full-text and semantic search, and provides tagging, review, and AI-assisted review workflows.

## Features

- **Production ingest** — DAT/OPT load files, native files, images, and PDFs, with OCR and text extraction.
- **Search** — Full-text and semantic (vector) search, plus saved searches.
- **Review workflows** — Tagging, notes, annotations, review queues, batching, and QC.
- **AI-assisted review** — Classification suggestions, near-duplicate detection, clustering, and corpus analysis.
- **Exports & audit** — Produce review output and track activity with a full audit trail.

## Tech stack

- **Frontend:** React + TypeScript + Vite
- **Backend:** FastAPI (Python)
- **Database:** PostgreSQL + pgvector
- **Auth:** Firebase Authentication

## Development

```bash
# Database
docker compose up -d

# Backend
cd backend
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload

# Frontend
cd frontend
cp .env.example .env
npm install
npm run dev
```

## Custom domain (vigilist.co)

The marketing site (`marketing/`) serves `vigilist.co` / `www.vigilist.co` via
Cloudflare Pages (see `marketing/README.md`). To serve the **app** on a
`*.vigilist.co` subdomain (e.g. `app.vigilist.co`):

1. **Firebase Hosting** — Console → Hosting → *Add custom domain* →
   `app.vigilist.co`, then create the TXT/A records it shows in Cloudflare DNS
   (set the records to **DNS only** / grey cloud so Firebase can provision TLS).
2. **Firebase Authentication** — Console → Authentication → Settings →
   *Authorized domains* → add `app.vigilist.co` (and any other subdomain that
   will serve the app). Authorized domains do **not** support wildcards — each
   subdomain must be added individually. Sign-in (Google popup and
   email/password) fails with `auth/unauthorized-domain` until this is done.
3. **Backend CORS** — already accepts `https://vigilist.co`,
   `https://www.vigilist.co`, and any `https://*.vigilist.co` subdomain
   (see `cors_origins` / `cors_origin_regex` in `backend/app/config.py`).
   Extra origins can be added via the `VIGILIST_CORS_ORIGINS` env var.
4. **Invite emails** — set `VIGILIST_APP_URL=https://app.vigilist.co` on the
   Cloud Run service so emailed invite links point at the new domain.

### Organizations (firm-wide access)

An **organization** (the `organizations` table) lets everyone at a firm share
productions by email domain instead of per-user invites:

- Users whose email domain is in the org's `member_domains` automatically get
  `member_role` access to **every** production the org owns.
- A production is filed under an org at creation time when its creator's email
  domain is in `member_domains`, or the creator's exact email is in
  `creator_emails`.

The initial migration seeds **Thiru Law** (`slug='thirulaw'`,
`member_domains={thirulaw.com}`, `member_role=manager`,
`creator_emails={wcedmonds28@gmail.com}`) and back-fills every existing
production to it. To add another firm, insert an `organizations` row (no code
change needed) and point its subdomain at Firebase Hosting per the steps above.

Notes:
- API access (documents, search, review) is enforced server-side on every
  request, so org access is effective immediately.
- Direct Firebase **Storage** reads are gated by custom claims, which refresh
  on the member's next login / token refresh — so a brand-new production added
  to an org becomes directly file-readable to existing members after their
  token refreshes (API-served files are immediate). Custom claims are also
  capped at 1000 bytes (~200 production IDs); a firm past that needs a
  claims-free storage-auth model (see `app/services/claims.py`).
