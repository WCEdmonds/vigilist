import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import ai, annotations, audit, auth, batches, dashboard, documents, entities, export, ingest, intelligence, notes, privilege, productions, qc, queues, redactions, review, saved_searches, search, tags

# Force root logger to DEBUG regardless of uvicorn's setup
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    force=True,
)
# Keep noisy libraries at INFO/WARNING
logging.getLogger("uvicorn").setLevel(logging.INFO)
logging.getLogger("sqlalchemy").setLevel(logging.WARNING)
logging.getLogger("google").setLevel(logging.INFO)
logging.getLogger("urllib3").setLevel(logging.INFO)
logging.getLogger("httpx").setLevel(logging.INFO)
logging.getLogger("PIL").setLevel(logging.WARNING)
logging.getLogger("tifffile").setLevel(logging.WARNING)

app = FastAPI(title="Vigilist", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_origin_regex=settings.cors_origin_regex or None,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(ingest.router)
app.include_router(documents.router)
app.include_router(search.router)
app.include_router(tags.router)
app.include_router(notes.router)
app.include_router(annotations.router)
app.include_router(redactions.router)
app.include_router(saved_searches.router)
app.include_router(ai.router)
app.include_router(export.router)
app.include_router(audit.router)
app.include_router(productions.router)
app.include_router(queues.router)
app.include_router(batches.router)
app.include_router(qc.router)
app.include_router(privilege.router)
app.include_router(dashboard.router)
app.include_router(review.router)
app.include_router(intelligence.router)
app.include_router(entities.router)


@app.get("/api/health")
async def health():
    return {"status": "ok"}
