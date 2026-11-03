import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import ai, annotations, audit, auth, batches, conversations, dashboard, defensibility, documents, entities, export, folders, imaging, ingest, intelligence, notes, privilege, production_sets, productions, qc, queues, redactions, review, sampling, saved_searches, search, search_terms, tags, tar

# Root logger at INFO. DEBUG is never enabled in prod: client SDKs (voyage,
# anthropic, httpx/httpcore) log full request bodies at DEBUG, which put
# privileged document text into Cloud Logging.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    force=True,
)
# Libraries that log request/response payloads, or are just noisy.
for _name in ("voyage", "voyageai", "anthropic", "httpx", "httpcore", "openai",
              "urllib3", "google", "sqlalchemy", "PIL", "tifffile"):
    logging.getLogger(_name).setLevel(logging.WARNING)
logging.getLogger("uvicorn").setLevel(logging.INFO)

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
# Before documents: its /api/documents/{doc_id} routes would shadow imaging-worker.
app.include_router(imaging.router)
app.include_router(documents.router)
app.include_router(search.router)
app.include_router(search_terms.router)
app.include_router(sampling.router)
app.include_router(tar.router)
app.include_router(defensibility.router)
app.include_router(tags.router)
app.include_router(notes.router)
app.include_router(annotations.router)
app.include_router(redactions.router)
app.include_router(saved_searches.router)
app.include_router(folders.router)
app.include_router(ai.router)
app.include_router(conversations.router)
app.include_router(export.router)
app.include_router(audit.router)
app.include_router(productions.router)
app.include_router(production_sets.router)
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
