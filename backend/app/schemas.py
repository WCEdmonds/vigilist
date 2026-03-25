from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class ProductionOut(BaseModel):
    id: int
    name: str
    description: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Tags ──

class TagOut(BaseModel):
    id: int
    name: str
    category: str
    color: str
    keyboard_shortcut: str | None

    model_config = {"from_attributes": True}


class TagCreate(BaseModel):
    name: str
    category: str
    color: str = "gray"
    keyboard_shortcut: str | None = None


class DocumentTagOut(BaseModel):
    id: int
    tag: TagOut
    applied_by: str
    applied_at: datetime

    model_config = {"from_attributes": True}


class ApplyTagsRequest(BaseModel):
    tag_ids: list[int]


class BulkTagRequest(BaseModel):
    doc_ids: list[UUID]
    tag_ids: list[int]


# ── Notes ──

class NoteOut(BaseModel):
    id: int
    document_id: UUID
    content: str
    created_by: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class NoteCreate(BaseModel):
    content: str


class NoteUpdate(BaseModel):
    content: str


# ── Saved Searches ──

class SavedSearchOut(BaseModel):
    id: int
    name: str
    query: str
    filters: dict
    created_by: str
    created_at: datetime

    model_config = {"from_attributes": True}


class SavedSearchCreate(BaseModel):
    name: str
    query: str = ""
    filters: dict = {}


# ── Documents ──

class DocumentSummary(BaseModel):
    id: UUID
    production_id: int
    bates_begin: str
    bates_end: str
    page_count: int
    has_native: bool
    title: str | None = None
    tags: list[TagOut] = []
    note_count: int = 0

    model_config = {"from_attributes": True}


class DocumentDetail(BaseModel):
    id: UUID
    production_id: int
    bates_begin: str
    bates_end: str
    page_count: int
    title: str | None = None
    summary: str | None = None
    metadata: dict
    text_content: str | None
    native_path: str | None
    image_paths: list[str]
    tags: list[DocumentTagOut] = []
    note_count: int = 0

    model_config = {"from_attributes": True}


class SearchResult(BaseModel):
    id: UUID
    production_id: int
    bates_begin: str
    bates_end: str
    page_count: int
    title: str | None = None
    snippet: str
    rank: float
    tags: list[TagOut] = []

    model_config = {"from_attributes": True}


class SearchResponse(BaseModel):
    results: list[SearchResult]
    total: int
    page: int
    per_page: int


class PaginatedDocuments(BaseModel):
    documents: list[DocumentSummary]
    total: int
    page: int
    per_page: int


class IngestRequest(BaseModel):
    production_name: str
    production_root: str
    description: str = ""


class IngestResponse(BaseModel):
    production_id: int
    production_name: str
    documents_ingested: int
    errors: list[str]
    error_count: int


class LoginRequest(BaseModel):
    username: str
    password: str
