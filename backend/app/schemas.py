from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class UserOut(BaseModel):
    id: str
    email: str
    display_name: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


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
    role: str
    granted_by: str
    granted_at: datetime

    model_config = {"from_attributes": True}


class InviteRequest(BaseModel):
    email: str
    role: str = "reviewer"


class PendingInviteOut(BaseModel):
    id: int
    email: str
    invited_by: str
    created_at: datetime

    model_config = {"from_attributes": True}


class AuditLogOut(BaseModel):
    id: int
    user_id: str
    user_email: str
    action: str
    resource_type: str
    resource_id: str | None
    production_id: int | None
    details: dict
    created_at: datetime

    model_config = {"from_attributes": True}


class PaginatedAuditLogs(BaseModel):
    logs: list[AuditLogOut]
    total: int
    page: int
    per_page: int


class IngestJobOut(BaseModel):
    id: UUID
    production_id: int
    production_name: str = ""
    status: str
    total_files: int
    processed_files: int
    errors: list[str]
    created_at: datetime
    completed_at: datetime | None

    model_config = {"from_attributes": True}


# ── Review Queues & Batches ──

class ReviewQueueCreate(BaseModel):
    name: str
    description: str = ""
    query: str = ""
    filters: dict = {}


class ReviewQueueOut(BaseModel):
    id: int
    production_id: int
    name: str
    description: str | None
    query: str
    filters: dict
    status: str
    created_by: str
    created_at: datetime
    batch_count: int = 0
    total_documents: int = 0
    reviewed_documents: int = 0

    model_config = {"from_attributes": True}


class ReviewBatchOut(BaseModel):
    id: int
    queue_id: int
    queue_name: str = ""
    reviewer_id: str | None
    reviewer_email: str | None = None
    status: str
    size: int
    reviewed_count: int
    assigned_at: datetime | None
    completed_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class BatchDocumentOut(BaseModel):
    id: int
    batch_id: int
    document_id: UUID
    position: int
    reviewed: str
    reviewed_at: datetime | None
    bates_begin: str = ""
    title: str | None = None

    model_config = {"from_attributes": True}


class BatchCreateRequest(BaseModel):
    batch_size: int = 50
    reviewer_id: str | None = None


class BatchAssignRequest(BaseModel):
    reviewer_id: str


class BatchDocumentUpdate(BaseModel):
    reviewed: str


# ── QC ──

class QCSampleRequest(BaseModel):
    queue_id: int
    sample_percent: float = 10.0
    reviewer_id: str | None = None


class QCDecisionCreate(BaseModel):
    decision: str
    reason: str | None = None
    new_tag_ids: list[int] | None = None


class QCDecisionOut(BaseModel):
    id: int
    batch_document_id: int
    original_reviewer_id: str
    original_reviewer_email: str = ""
    qc_reviewer_id: str
    qc_reviewer_email: str = ""
    decision: str
    reason: str | None
    original_tags: list
    new_tags: list | None
    created_at: datetime
    bates_begin: str = ""

    model_config = {"from_attributes": True}


# ── Dashboard ──

class DashboardStats(BaseModel):
    total_documents: int
    reviewed_documents: int
    pending_documents: int
    percent_complete: float
    tag_breakdown: dict
    reviewer_stats: list[dict]
    queue_stats: list[dict]


class QCStats(BaseModel):
    total_decisions: int
    agree_count: int
    overturn_count: int
    overturn_rate: float
    by_reviewer: list[dict]
