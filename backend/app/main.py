from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import ai, auth, documents, export, ingest, notes, productions, saved_searches, search, tags

app = FastAPI(title="Vigilist", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
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
app.include_router(saved_searches.router)
app.include_router(ai.router)
app.include_router(export.router)
app.include_router(productions.router)


@app.get("/api/health")
async def health():
    return {"status": "ok"}
