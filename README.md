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
