# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Vigilist** is a lightweight e-discovery document review platform for a small legal team (2-5 users). It replaces a full Relativity license for reviewing litigation document productions in active Maryland litigation. The full requirements specification is in `EDISCOVERY_PLATFORM_REQUIREMENTS.md`.

## Architecture

- **Frontend:** React 18 + Vite + TypeScript (`frontend/`)
- **Backend:** Python 3.14 + FastAPI + SQLAlchemy async (`backend/`)
- **Database:** PostgreSQL 16 via Docker Compose, with tsvector full-text search
- **File Storage:** Local filesystem for dev (`backend/storage/`), GCS for prod
- **Auth:** Session-based shared login (itsdangerous signed cookies)

## Development Commands

```bash
# Start Postgres
docker compose up -d

# Backend
cd backend
.\venv\Scripts\Activate.ps1      # PowerShell
# source venv/Scripts/activate   # Git Bash alternative
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# Frontend (proxies /api to backend via vite.config.ts)
cd frontend
npm install
npm run dev                      # http://localhost:5173

# Database migrations
cd backend
.\venv\Scripts\Activate.ps1
alembic upgrade head             # apply migrations
alembic revision --autogenerate -m "description"  # generate new migration

# Ingest a production (CLI)
cd backend
.\venv\Scripts\Activate.ps1
python ingest_cli.py "SCHLEGEL_PROD001" "C:\path\to\production\root"

# Type-check frontend
cd frontend && npx tsc --noEmit
```

Default dev credentials: `admin` / `vigilist2026` (set via `VIGILIST_AUTH_USERNAME` / `VIGILIST_AUTH_PASSWORD` env vars).

## Production Data Format

Productions follow Relativity export format with four directories: DATA/, TEXT/, NATIVES/, IMAGES/.

Key parsing details:
- **DAT files:** Concordance format — UTF-8 with BOM, `þ` (U+00FE) field wrapper, `DC4` (0x14) field separator, CRLF row terminator
- **OPT files:** Standard Opticon format, comma-delimited, no header. Doc Break field (`Y`) determines page grouping
- **Bates format:** `PREFIX NNNNNN` (space-separated, 6-digit zero-padded). Do NOT hardcode prefix
- **Bates numbers are NOT globally unique** — must use composite key `(production_id, bates_begin)`
- Native file paths use backslashes — normalize to forward slashes
- DAT field count varies across productions — parser must handle additional columns dynamically (stored in JSONB `metadata` column)

## Key Design Constraints

- Bates numbers can collide across productions — always scope queries by `production_id`
- DAT metadata fields are minimal now (5 columns) but overlay files may add fields later — JSONB column handles this
- TIFFs are pre-converted to JPEG on ingest (Pillow) and stored in `storage/converted/`
- The `TSVector` custom type in `models.py` must also be defined in migration files (Alembic doesn't auto-resolve it)
- All backend config uses `VIGILIST_` env prefix via pydantic-settings

## Build Phases

1. **Phase 1 (done):** Ingest pipeline + document viewer + full-text search + shared auth
2. **Phase 2 (done):** Search + core review workflow (tagging, notes, shortcuts, bulk tagging, media streaming). Remaining: metadata field filters in search
3. **Phase 3 (partial):** User management + audit. Done: Firebase Auth (email/password + Google OAuth). Remaining: RBAC, audit trail, audit viewer/export
4. **Phase 4:** Review management + QC (multi-matter, queues, batching, dashboards, QC sampling)
5. **Phase 5:** Coding layouts + annotations + redactions
6. **Phase 6:** Document intelligence (families, email threading, near-duplicates, propagation coding)
7. **Phase 7:** AI-powered review (embeddings, NL search, summarization, clustering, TAR, communication analysis)
8. **Phase 8:** Export + production (load files, redaction burn-in, privilege log, reports)
