from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import UUID4, BaseModel


def get_file_type(native_path: str | None, page_count: int) -> str:
    """Derive a file type category from the native file extension."""
    if not native_path:
        return "document"  # image-only docs
    ext = native_path.rsplit(".", 1)[-1].lower() if "." in native_path else ""
    VIDEO_EXTS = {"mp4", "mov", "avi", "wmv", "mkv", "webm"}
    AUDIO_EXTS = {"wav", "mp3", "aac", "flac", "ogg", "wma"}
    PDF_EXTS = {"pdf"}
    EMAIL_EXTS = {"msg", "eml", "mbox", "pst", "ost"}
    SPREADSHEET_EXTS = {"xlsx", "xls", "csv"}
    PRESENTATION_EXTS = {"pptx", "ppt", "potx"}
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
    is_privilege: bool = False

    model_config = {"from_attributes": True}


class TagCreate(BaseModel):
    name: str
    category: str
    color: str = "gray"
    keyboard_shortcut: str | None = None


class TagPrivilegeUpdate(BaseModel):
    is_privilege: bool


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
    cluster_id: int | None = None
    cluster_label: str | None = None
    ai_decision: str | None = None
    ai_confidence: int | None = None
    ai_decided: bool = False
    source_party: str | None = None
    source_type: str | None = None

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
    redaction_count: int = 0

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


class ProposedMappingItem(BaseModel):
    """One column's proposed mapping, as returned by build_proposed_mapping."""
    source_name: str
    samples: list[str]
    target: str | None
    confidence: float
    source: str  # "alias" | "ai" | "unmapped"


class LoadFileFormat(BaseModel):
    encoding: str
    delimiter: str


class AnalyzeResponse(BaseModel):
    format: LoadFileFormat
    columns: list[ProposedMappingItem]
    sample_rows: list[dict]
    total_rows: int


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
    # Within-stage progress for the long-running summaries stage: documents
    # summarized so far out of the production's total. Always present so the
    # UI can show "Summaries · 275/550" while that stage runs.
    summarized_count: int = 0
    doc_count: int = 0


class IntakeSummaryOut(BaseModel):
    """The post-ingest receipt: what intake actually created."""
    documents: int = 0
    custodians: int = 0
    email_families: int = 0
    family_documents: int = 0
    threads: int = 0
    inclusive_emails: int = 0
    duplicate_groups: int = 0


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


class RedactionCreate(BaseModel):
    page_num: int
    x_pct: float
    y_pct: float
    w_pct: float
    h_pct: float
    reason_code: str
    note: str | None = None


class RedactionUpdate(BaseModel):
    x_pct: float | None = None
    y_pct: float | None = None
    w_pct: float | None = None
    h_pct: float | None = None
    reason_code: str | None = None
    note: str | None = None


class RedactionOut(BaseModel):
    id: int
    document_id: UUID
    page_num: int
    x_pct: float
    y_pct: float
    w_pct: float
    h_pct: float
    reason_code: str
    note: str | None = None
    created_by: str
    created_at: datetime
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class RedactionQCDecisionCreate(BaseModel):
    decision: Literal["approved", "rejected"]
    note: str | None = None


class RedactionQCDecisionOut(BaseModel):
    id: int
    document_id: UUID
    decision: str
    note: str | None
    redaction_count: int
    decided_by: str
    decided_at: datetime

    model_config = {"from_attributes": True}


class PrivilegeOverrideUpdate(BaseModel):
    disposition: str | None = None
    description: str | None = None


class RedactionQCQueueItem(BaseModel):
    document_id: UUID
    bates_begin: str
    redaction_count: int
    qc_status: str
    latest_decision: RedactionQCDecisionOut | None = None


# ── Intelligence ──

class DuplicateEntryOut(BaseModel):
    document_id: UUID
    bates_begin: str
    title: str | None
    similarity: float
    type: str
    custodian: str | None = None


class FamilyMemberOut(BaseModel):
    document_id: UUID
    bates_begin: str
    title: str | None
    is_inclusive: bool


class FamilyThreadOut(BaseModel):
    family: list[FamilyMemberOut]
    thread: list[FamilyMemberOut]


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


class ThreadStats(BaseModel):
    threads: int
    inclusive: int
    messages: int


# ── Ontology ──

class MentionSpanOut(BaseModel):
    surface_text: str
    start_offset: int | None
    end_offset: int | None


class DocEntityOut(BaseModel):
    id: UUID4
    entity_type: str
    canonical_name: str
    mention_count: int
    mentions: list[MentionSpanOut]


class DocumentEntitiesOut(BaseModel):
    entities: list[DocEntityOut]


class EntityProfileOut(BaseModel):
    id: UUID4
    production_id: int
    entity_type: str
    canonical_name: str
    aliases: list[str]
    attributes: dict
    overview: str | None
    mention_count: int
    document_count: int


class EntityDocMentionOut(BaseModel):
    surface_text: str
    context_snippet: str | None
    start_offset: int | None


class EntityDocumentMentionsOut(BaseModel):
    document_id: UUID4
    bates_begin: str
    title: str | None
    mentions: list[EntityDocMentionOut]


class EntityMentionsPageOut(BaseModel):
    documents: list[EntityDocumentMentionsOut]
    total: int


class EntityConnectionOut(BaseModel):
    entity_id: UUID4
    canonical_name: str
    entity_type: str
    relationship_type: str | None = None
    description: str | None = None
    document_id: UUID4 | None = None
    shared_doc_count: int | None = None


class SharedEventOut(BaseModel):
    event_id: int
    description: str
    event_type: str
    event_date: str | None
    document_id: UUID4


class EntityConnectionsOut(BaseModel):
    stated: list[EntityConnectionOut]
    cooccurrence: list[EntityConnectionOut]
    shared_events: list[SharedEventOut]


class EntityListItemOut(BaseModel):
    id: UUID4
    entity_type: str
    canonical_name: str
    mention_count: int
    document_count: int


class EntityListPageOut(BaseModel):
    entities: list[EntityListItemOut]
    total: int


class MergeSuggestionOut(BaseModel):
    id: int
    score: float
    rationale: str
    status: str
    entity_a: EntityListItemOut
    entity_b: EntityListItemOut


class MergeRequest(BaseModel):
    winner_id: UUID4
    loser_id: UUID4


class MergeResultOut(BaseModel):
    merge_id: int
    winner_id: UUID4
# --- P2-1: production sets --------------------------------------------------

class ProductionSetCreate(BaseModel):
    name: str
    prefix: str
    padding: int = 6
    start_number: int = 1
    sort_key: str = "control_number"
    designation: str | None = None
    image_format: str = "pdf"
    native_file_types: list[str] = []
    volume_max_mb: int | None = None


class ProductionSetOut(BaseModel):
    id: int
    production_id: int
    name: str
    status: str
    prefix: str
    padding: int
    start_number: int
    sort_key: str
    designation: str | None
    created_by: str
    created_at: datetime
    locked_by: str | None
    locked_at: datetime | None
    doc_count: int = 0
    page_count: int | None = None
    bates_begin: str | None = None
    bates_end: str | None = None
    render_status: str = "not_started"
    render_error: str | None = None
    rendered_at: datetime | None = None
    rendered_count: int = 0
    package_status: str = "not_started"
    package_error: str | None = None
    package_path: str | None = None
    packaged_at: datetime | None = None
    conflicts_overridden_by: str | None = None
    conflicts_overridden_at: datetime | None = None
    image_format: str = "pdf"
    native_file_types: list[str] = []
    volume_max_mb: int | None = None

    model_config = {"from_attributes": True}


class ProductionSetMemberOut(BaseModel):
    document_id: UUID
    control_number: str
    sort_order: int | None
    bates_begin: str | None
    bates_end: str | None
    pages: int | None
    disposition: str | None
    designation: str | None
    produce_native: bool = False


class ProductionSetAddDocuments(BaseModel):
    document_ids: list[UUID] | None = None
    tag_id: int | None = None
    include_families: bool = False
    exclude_duplicates: bool = False
    exclude_received: bool = False


class ProductionSetRemoveDocuments(BaseModel):
    document_ids: list[UUID]


class ProductionSetLockOut(BaseModel):
    doc_count: int
    page_count: int
    bates_begin: str
    bates_end: str


class ProductionSetLockRequest(BaseModel):
    override_conflicts: bool = False


# --- P3-2: defensible sampling ----------------------------------------------

class SampleCreate(BaseModel):
    name: str
    purpose: str = "richness"
    confidence: int = 95
    margin: float = 0.05
    expected_rate: float = 0.5
    size: int | None = None
    source_type: str | None = None
    scope: str | None = None          # None (all) | 'machine_negative'
    project_id: int | None = None     # required when scope='machine_negative'


class SampleOut(BaseModel):
    id: int
    production_id: int
    name: str
    purpose: str
    params: dict
    document_ids: list[str]
    created_by: str
    created_at: datetime

    model_config = {"from_attributes": True}


# --- P3-3: TAR validation ---------------------------------------------------

class TarValidationCreate(BaseModel):
    project_id: int
    control_sample_id: int
    responsive_tag_id: int
    nonresponsive_tag_id: int | None = None
    elusion_sample_id: int | None = None
    confidence: int = 95


class TarValidationOut(BaseModel):
    id: int
    production_id: int
    project_id: int
    params: dict
    results: dict
    created_by: str
    created_at: datetime

    model_config = {"from_attributes": True}


# --- P3-1: search-term hit reports ------------------------------------------

class SearchTermReportCreate(BaseModel):
    name: str
    terms: list[str]


class SearchTermReportOut(BaseModel):
    id: int
    production_id: int
    name: str
    terms: list[str]
    results: dict | None
    computed_at: datetime | None
    created_by: str
    created_at: datetime

    model_config = {"from_attributes": True}
