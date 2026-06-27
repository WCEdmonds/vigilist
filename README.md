# Vigilist

Vigilist is a lightweight, self-hosted e-discovery review platform for small-to-mid-size legal teams — a leaner alternative to heavyweight tools like Relativity. It ingests standard Relativity-format productions (DAT/OPT load files, native files, and images), makes them searchable with both full-text and semantic search, and provides tagging, review queue, and AI-assisted review workflows on top.

## Features

- **Production ingest** — Load Relativity-style productions: `.dat` (Concordance) and `.opt` (Opticon) load files, native files, TIFF/JPEG images, and PDFs. Includes OCR, PDF text extraction, and image conversion.
- **Search** — Full-text search plus semantic/vector search over document embeddings, with saved searches.
- **Review workflows** — Tagging, notes, annotations, review queues, batching, and QC.
- **AI-assisted review** — Document classification and review suggestions, near-duplicate detection, clustering, corpus analysis, claims extraction, and statistical sampling.
- **Case intelligence** — Cross-corpus analysis and intelligence views to surface key documents.
- **Exports** — Produce review output back out of the platform.
- **Audit trail** — Action-level audit logging across the review lifecycle.
- **Auth** — Firebase Authentication (with OIDC support).

## Architecture

Vigilist is a two-part app — a React SPA frontend and a FastAPI backend — backed by Postgres with `pgvector` for embeddings.

```
frontend (React + Vite)  ──>  Firebase Hosting
                                   │  /api/** rewrite
                                   ▼
backend (FastAPI)        ──>  Cloud Run (vigilist-api)
                                   │
                          Postgres + pgvector (Neon)
                          Google Cloud Storage (files/images)
                          Cloud Tasks (async ingest fan-out)
                          Anthropic / OpenAI / Voyage AI (LLM + embeddings)
```

### Tech stack

| Layer | Technology |
|-------|-----------|
| Frontend | React 19, TypeScript, Vite, Firebase JS SDK |
| Backend | FastAPI, Python 3.13, SQLAlchemy (async) + asyncpg, Alembic |
| Database | PostgreSQL 16 + `pgvector` |
| Storage | Google Cloud Storage / Firebase Storage |
| AI / ML | Anthropic, OpenAI, Voyage AI (embeddings), pgvector |
| Documents | PyMuPDF (PDF), Pillow, pytesseract + Google Cloud Vision (OCR), datasketch (MinHash dedup) |
| Async | Google Cloud Tasks (fans out long ingest jobs across Cloud Run invocations) |
| Email | Resend |
| Hosting | Firebase Hosting (frontend) + Cloud Run (backend) |

## Repository layout

```
backend/        FastAPI app
  app/
    routers/    HTTP endpoints (auth, ingest, search, review, export, ...)
    services/   business logic (ingest, embeddings, OCR, AI review, dedup, ...)
    models*.py  SQLAlchemy models
    schemas*.py Pydantic schemas
  alembic/      database migrations
  ingest_cli.py CLI for ingesting productions without the web server
frontend/       React + Vite SPA
docker-compose.yml  local Postgres
firebase.json   Hosting config + /api/** -> Cloud Run rewrite
```

## Getting started (local development)

### Prerequisites

- Python 3.13
- Node.js 20+
- Docker (for local Postgres)

### 1. Start the database

```bash
docker compose up -d        # Postgres 16 on localhost:5432 (db/user/pass: vigilist)
```

### 2. Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
alembic upgrade head                                # run migrations
uvicorn app.main:app --reload --port 8000
```

Backend configuration is read from environment variables (prefixed `VIGILIST_`) or a `backend/.env` file. Common settings:

| Variable | Purpose |
|----------|---------|
| `VIGILIST_DATABASE_URL` | Postgres connection string (async) |
| `VIGILIST_FIREBASE_PROJECT_ID` | Firebase project for auth |
| `VIGILIST_FIREBASE_STORAGE_BUCKET` | GCS bucket for files/images |
| `VIGILIST_ANTHROPIC_API_KEY` | Anthropic API key for AI features |
| `VIGILIST_RESEND_API_KEY` | Resend API key for email |
| `VIGILIST_GCP_PROJECT_ID` / `VIGILIST_CLOUD_TASKS_QUEUE` | Cloud Tasks for async ingest (optional locally; falls back to in-process background tasks) |

See `backend/app/config.py` for the full list and defaults.

### 3. Frontend

```bash
cd frontend
cp .env.example .env        # fill in your Firebase web config
npm install
npm run dev                 # Vite dev server on http://localhost:5173
```

## Ingesting a production

You can ingest from the UI, or directly from the CLI without running the web server:

```bash
cd backend
python ingest_cli.py "<production_name>" "<path/to/production_root>" "optional description"
```

## Deployment

Deployment is automated via GitHub Actions on push to `main`:

- **Backend** → built from `backend/` and deployed to **Cloud Run** (`vigilist-api`) in project `ediscover`. The workflow also runs Alembic migrations against the live database and refuses to deploy if migrations can't be run (so code never ships ahead of its schema).
- **Frontend** → built with `npm run build` and deployed to **Firebase Hosting**. Hosting rewrites `/api/**` to the Cloud Run backend, so the SPA and API share one origin.

To build the frontend manually:

```bash
cd frontend
npm run build               # outputs to frontend/dist
```
