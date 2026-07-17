from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


def get_file_type(native_path: str | None, page_count: int) -> str:
    """Derive a file type category from the native file extension."""
    if not native_path:
        return "document"  # image-only docs
    ext = native_path.rsplit(".", 1)[-1].lower() if "." in native_path else ""
    VIDEO_EXTS = {"mp4", "mov", "avi", "wmv", "mkv", "webm"}
    AUDIO_EXTS = {"wav", "mp3", "aac", "flac", "ogg", "wma"}
    PDF_EXTS = {"pdf"}
    EMAIL_EXTS = {"msg", "eml"}
    SPREADSHEET_EXTS = {"xlsx", "xls", "csv"}
    PRESENTATION_EXTS = {"pptx", "ppt"}
    IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "bmp", "tiff"}
    if ext in VIDEO_EXTS: return "video"
    if ext in AUDIO_EXTS: return "audio"
    if ext in PDF_EXTS: return "pdf"
    if ext in EMAIL_EXTS: return "email"
    if ext in SPREADSHEET_EXTS: return "spreadsheet"
    if ext in PRESENTATION_EXTS: return "presentation"
    if ext in IMAGE_EXTS: return "image"
    if ext: return "other"
    return "document"


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
    timestamp: float | None = None
    created_by: str
    created_by_email: str = ""
    created_by_display_name: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class NoteCreate(BaseModel):
    content: str
    timestamp: float | None = None


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
    file_type: str = "document"
    title: str | None = None
    processing_status: str = "complete"
    tags: list[TagOut] = []
    note_count: int = 0
    annotation_count: int = 0

    model_config = {"from_attributes": True}


class DocumentDetail(BaseModel):
    id: UUID
    production_id: int
    bates_begin: str
    bates_end: str
    page_count: int
    title: str | None = None
    summary: str | None = None
    processing_status: str = "complete"
    metadata: dict
    text_content: str | None
    native_path: str | None
    image_paths: list[str]
    tags: list[DocumentTagOut] = []
    note_count: int = 0
    annotation_count: int = 0

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
    document_count: int = 0
    case_context: str | None = None
    has_brief: bool = False

    model_config = {"from_attributes": True}


class ProductionUpdate(BaseModel):
    description: str | None = None
    case_context: str | None = None


class PipelineStatusOut(BaseModel):
    status: dict | None = None
    brief: dict | None = None
    case_context: str | None = None


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
    skipped_files: int = 0
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


# ── Annotations ──

class AnnotationCreate(BaseModel):
    page_num: int
    x_pct: float
    y_pct: float
    color: Literal["red", "yellow", "green", "blue"] = "blue"
    content: str = ""


class AnnotationUpdate(BaseModel):
    content: str | None = None
    color: Literal["red", "yellow", "green", "blue"] | None = None


class AnnotationOut(BaseModel):
    id: int
    document_id: UUID
    page_num: int
    x_pct: float
    y_pct: float
    color: str
    content: str
    created_by: str
    created_by_email: str = ""
    created_by_display_name: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ── Intelligence ──

class DuplicateEntryOut(BaseModel):
    document_id: UUID
    bates_begin: str
    title: str | None
    similarity: float
    type: str


class ClusterOut(BaseModel):
    id: int
    cluster_index: int
    label: str | None
    doc_count: int
    page_count: int = 0

    model_config = {"from_attributes": True}


class PropagateTagRequest(BaseModel):
    tag_id: int
    relationship_type: Literal["duplicate", "family", "thread"]


class ClusterDocumentOut(BaseModel):
    document_id: str
    bates_begin: str
    title: str | None = None
