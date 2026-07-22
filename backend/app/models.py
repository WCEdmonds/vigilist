import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.types import UserDefinedType


class TSVector(UserDefinedType):
    cache_ok = True

    def get_col_spec(self):
        return "TSVECTOR"


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(String(128), primary_key=True)  # Firebase UID
    email = Column(String(255), nullable=False, unique=True)
    display_name = Column(String(255), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class Organization(Base):
    """A tenant (e.g. a law firm) served on a *.vigilist.co subdomain.

    Access model:
    - Any user whose email domain is in `member_domains` is a member of the
      org and gets `member_role` access to every production owned by the org.
    - A production is auto-assigned to the org at creation time when its
      creator's email domain is in `member_domains`, OR the creator's exact
      email is in `creator_emails` (for individuals outside the member
      domains — e.g. an external admin whose personal email should still
      file productions under this org).
    """

    __tablename__ = "organizations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Subdomain label — e.g. "thirulaw" for thirulaw.vigilist.co.
    slug = Column(String(63), nullable=False, unique=True)
    name = Column(String(255), nullable=False)
    # Role granted to member-domain users on the org's productions.
    member_role = Column(String(20), nullable=False, server_default="reviewer")
    # Email domains whose users are members (lowercase, no "@"): ["thirulaw.com"]
    member_domains = Column(ARRAY(String), nullable=False, server_default="{}")
    # Extra individual emails (lowercase) whose new productions file under this
    # org even though their domain isn't a member domain.
    creator_emails = Column(ARRAY(String), nullable=False, server_default="{}")
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    productions = relationship("Production", back_populates="organization")


class Production(Base):
    __tablename__ = "productions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False, unique=True)
    description = Column(Text, nullable=True)
    owner_id = Column(String(128), ForeignKey("users.id"), nullable=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True)
    case_context = Column(Text, nullable=True)
    brief = Column(JSONB, nullable=True)
    ai_pipeline_status = Column(JSONB, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    documents = relationship("Document", back_populates="production")
    owner = relationship("User", foreign_keys=[owner_id])
    organization = relationship("Organization", back_populates="productions")
    access_list = relationship("ProductionAccess", back_populates="production", cascade="all, delete-orphan")


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("production_id", "bates_begin", name="uq_prod_bates"),
        Index("ix_documents_bates_begin", "bates_begin"),
        Index("ix_documents_bates_end", "bates_end"),
        Index("ix_documents_text_search", "text_search_vector", postgresql_using="gin"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    production_id = Column(Integer, ForeignKey("productions.id"), nullable=False)
    bates_begin = Column(String(50), nullable=False)
    bates_end = Column(String(50), nullable=False)
    page_count = Column(Integer, nullable=False, default=1)
    metadata_ = Column("metadata", JSONB, nullable=False, default=dict)
    title = Column(String(200), nullable=True)
    summary = Column(Text, nullable=True)
    text_content = Column(Text, nullable=True)
    text_search_vector = Column(TSVector, nullable=True)
    native_path = Column(String(500), nullable=True)
    image_paths = Column(JSONB, nullable=False, default=list)
    raw_image_paths = Column(JSONB, nullable=False, default=list)
    processing_status = Column(String(20), nullable=False, default="pending")
    family_id = Column(String(255), nullable=True)
    thread_id = Column(String(255), nullable=True)
    is_inclusive = Column(Boolean, nullable=False, default=False)
    # Phase 0 SP4b-2 — email threading headers (parent email Documents only)
    message_id = Column(String(500), nullable=True, index=True)
    in_reply_to = Column(String(500), nullable=True)
    email_references = Column(Text, nullable=True)

    # Phase 0 SP1 — typed metadata (promoted from load-file columns)
    custodian = Column(String(255), nullable=True, index=True)
    date_sent = Column(DateTime(timezone=True), nullable=True, index=True)
    date_received = Column(DateTime(timezone=True), nullable=True)
    date_created = Column(DateTime(timezone=True), nullable=True)
    date_modified = Column(DateTime(timezone=True), nullable=True)
    file_hash_md5 = Column(String(32), nullable=True)
    file_hash_sha256 = Column(String(64), nullable=True, index=True)
    file_type = Column(String(50), nullable=True, index=True)
    file_name = Column(String(500), nullable=True)
    source_path = Column(String(1000), nullable=True)
    extraction_status = Column(String(20), nullable=False, server_default="ok")
    extraction_error = Column(Text, nullable=True)
    email_from = Column(String(500), nullable=True)
    email_to = Column(Text, nullable=True)
    email_cc = Column(Text, nullable=True)
    email_bcc = Column(Text, nullable=True)
    email_subject = Column(String(1000), nullable=True)

    # P1-4/5 — privilege overrides (NULL = derived / templated)
    privilege_disposition = Column(String(20), nullable=True)
    privilege_description = Column(Text, nullable=True)

    production = relationship("Production", back_populates="documents")
    tags = relationship("DocumentTag", back_populates="document", cascade="all, delete-orphan")
    notes = relationship("Note", back_populates="document", cascade="all, delete-orphan", order_by="Note.created_at.desc()")
    annotations = relationship("Annotation", back_populates="document", cascade="all, delete-orphan", order_by="Annotation.page_num, Annotation.created_at")
    chunks = relationship("DocumentChunk", back_populates="document", cascade="all, delete-orphan")


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_chunk_doc_idx"),
        Index("ix_chunks_document_id", "document_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    chunk_index = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    embedding = Column(Vector(1024), nullable=False)

    document = relationship("Document", back_populates="chunks")


class Tag(Base):
    __tablename__ = "tags"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    category = Column(String(50), nullable=False)
    color = Column(String(20), nullable=False, default="gray")
    keyboard_shortcut = Column(String(5), nullable=True)
    production_id = Column(Integer, ForeignKey("productions.id"), nullable=True)
    is_privilege = Column(Boolean, nullable=False, default=False)

    document_tags = relationship("DocumentTag", back_populates="tag")


class DocumentTag(Base):
    __tablename__ = "document_tags"
    __table_args__ = (
        UniqueConstraint("document_id", "tag_id", name="uq_doc_tag"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    tag_id = Column(Integer, ForeignKey("tags.id", ondelete="CASCADE"), nullable=False)
    applied_by = Column(String(100), nullable=False)
    applied_at = Column(DateTime, server_default=func.now(), nullable=False)

    document = relationship("Document", back_populates="tags")
    tag = relationship("Tag", back_populates="document_tags")


class Note(Base):
    __tablename__ = "notes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    content = Column(Text, nullable=False)
    timestamp = Column(Float, nullable=True)  # seconds into media file, null = no timestamp
    created_by = Column(String(100), nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    document = relationship("Document", back_populates="notes")


class SavedSearch(Base):
    __tablename__ = "saved_searches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    query = Column(String(1000), nullable=False, default="")
    filters = Column(JSONB, nullable=False, default=dict)
    created_by = Column(String(100), nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


# Role values: "admin", "manager", "reviewer", "readonly"
# The production owner (Production.owner_id) implicitly has full admin access.
class ProductionAccess(Base):
    __tablename__ = "production_access"
    __table_args__ = (
        UniqueConstraint("production_id", "user_id", name="uq_prod_user"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    production_id = Column(Integer, ForeignKey("productions.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(String(128), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role = Column(String(20), nullable=False, server_default="reviewer")
    granted_by = Column(String(128), ForeignKey("users.id"), nullable=False)
    granted_at = Column(DateTime, server_default=func.now(), nullable=False)

    production = relationship("Production", back_populates="access_list")
    user = relationship("User", foreign_keys=[user_id])
    granter = relationship("User", foreign_keys=[granted_by])


class PendingInvite(Base):
    __tablename__ = "pending_invites"
    __table_args__ = (
        UniqueConstraint("production_id", "email", name="uq_prod_email_invite"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    production_id = Column(Integer, ForeignKey("productions.id", ondelete="CASCADE"), nullable=False)
    email = Column(String(255), nullable=False)
    invited_by = Column(String(128), ForeignKey("users.id"), nullable=False)
    role = Column(String(20), nullable=False, server_default="reviewer")
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    production = relationship("Production")
    inviter = relationship("User", foreign_keys=[invited_by])


class IngestJob(Base):
    __tablename__ = "ingest_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    production_id = Column(Integer, ForeignKey("productions.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(String(128), ForeignKey("users.id"), nullable=False)
    status = Column(String(20), nullable=False, default="pending")
    source_format = Column(String(20), nullable=False, server_default="relativity")
    total_files = Column(Integer, nullable=False, default=0)
    processed_files = Column(Integer, nullable=False, default=0)
    skipped_files = Column(Integer, nullable=False, default=0)
    errors = Column(JSONB, nullable=False, default=list)
    field_mapping = Column(JSONB, nullable=False, default=dict)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    completed_at = Column(DateTime, nullable=True)

    production = relationship("Production")
    user = relationship("User")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(128), ForeignKey("users.id"), nullable=False)
    user_email = Column(String(255), nullable=False)
    action = Column(String(50), nullable=False)
    resource_type = Column(String(50), nullable=False)
    resource_id = Column(String(255), nullable=True)
    production_id = Column(Integer, ForeignKey("productions.id", ondelete="CASCADE"), nullable=True)
    details = Column(JSONB, nullable=False, default=dict)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    user = relationship("User", foreign_keys=[user_id])


class ReviewQueue(Base):
    __tablename__ = "review_queues"

    id = Column(Integer, primary_key=True, autoincrement=True)
    production_id = Column(Integer, ForeignKey("productions.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    query = Column(String(1000), nullable=False, default="")
    filters = Column(JSONB, nullable=False, default=dict)
    status = Column(String(20), nullable=False, default="active")
    created_by = Column(String(128), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    production = relationship("Production")
    creator = relationship("User", foreign_keys=[created_by])
    batches = relationship("ReviewBatch", back_populates="queue", cascade="all, delete-orphan")


class ReviewBatch(Base):
    __tablename__ = "review_batches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    queue_id = Column(Integer, ForeignKey("review_queues.id", ondelete="CASCADE"), nullable=False)
    reviewer_id = Column(String(128), ForeignKey("users.id"), nullable=True)
    status = Column(String(20), nullable=False, default="pending")
    size = Column(Integer, nullable=False, default=0)
    reviewed_count = Column(Integer, nullable=False, default=0)
    assigned_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    queue = relationship("ReviewQueue", back_populates="batches")
    reviewer = relationship("User", foreign_keys=[reviewer_id])
    documents = relationship("BatchDocument", back_populates="batch", cascade="all, delete-orphan")


class BatchDocument(Base):
    __tablename__ = "batch_documents"
    __table_args__ = (
        UniqueConstraint("batch_id", "document_id", name="uq_batch_doc"),
        Index("ix_batch_documents_document_id", "document_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    batch_id = Column(Integer, ForeignKey("review_batches.id", ondelete="CASCADE"), nullable=False)
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    position = Column(Integer, nullable=False)
    reviewed = Column(String(20), nullable=False, default="pending")
    reviewed_at = Column(DateTime, nullable=True)

    batch = relationship("ReviewBatch", back_populates="documents")
    document = relationship("Document")


class QCDecision(Base):
    __tablename__ = "qc_decisions"
    __table_args__ = (
        UniqueConstraint("batch_document_id", "qc_reviewer_id", name="uq_qc_decision"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    batch_document_id = Column(Integer, ForeignKey("batch_documents.id", ondelete="CASCADE"), nullable=False)
    original_reviewer_id = Column(String(128), ForeignKey("users.id"), nullable=False)
    qc_reviewer_id = Column(String(128), ForeignKey("users.id"), nullable=False)
    decision = Column(String(20), nullable=False)
    reason = Column(Text, nullable=True)
    original_tags = Column(JSONB, nullable=False, default=list)
    new_tags = Column(JSONB, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    batch_document = relationship("BatchDocument")
    original_reviewer = relationship("User", foreign_keys=[original_reviewer_id])
    qc_reviewer = relationship("User", foreign_keys=[qc_reviewer_id])


class Annotation(Base):
    __tablename__ = "annotations"
    __table_args__ = (
        Index("ix_annotations_document_id", "document_id"),
        Index("ix_annotations_created_by", "created_by"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    page_num = Column(Integer, nullable=False)
    x_pct = Column(Float, nullable=False)
    y_pct = Column(Float, nullable=False)
    color = Column(String(20), nullable=False, default="blue")
    content = Column(Text, nullable=False, default="")
    created_by = Column(String(128), nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    document = relationship("Document", back_populates="annotations")


class Redaction(Base):
    __tablename__ = "redactions"
    __table_args__ = (
        Index("ix_redactions_document_id", "document_id"),
        Index("ix_redactions_doc_page", "document_id", "page_num"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    page_num = Column(Integer, nullable=False)
    x_pct = Column(Float, nullable=False)
    y_pct = Column(Float, nullable=False)
    w_pct = Column(Float, nullable=False)
    h_pct = Column(Float, nullable=False)
    reason_code = Column(String(40), nullable=False)
    note = Column(Text, nullable=True)
    created_by = Column(String(128), nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, onupdate=func.now(), nullable=True)


class RedactionQCDecision(Base):
    __tablename__ = "redaction_qc_decisions"
    __table_args__ = (
        Index("ix_rqc_document_id", "document_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    decision = Column(String(20), nullable=False)  # 'approved' | 'rejected'
    note = Column(Text, nullable=True)
    redaction_count = Column(Integer, nullable=False)  # snapshot at decision time
    decided_by = Column(String(128), nullable=False)
    decided_at = Column(DateTime, server_default=func.now(), nullable=False)


class DuplicateGroup(Base):
    __tablename__ = "duplicate_groups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    production_id = Column(Integer, ForeignKey("productions.id", ondelete="CASCADE"), nullable=False)
    type = Column(String(20), nullable=False)  # 'hash' | 'exact' | 'similar'
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    members = relationship("DocumentDuplicate", back_populates="group", cascade="all, delete-orphan")


class DocumentDuplicate(Base):
    __tablename__ = "document_duplicates"
    __table_args__ = (
        UniqueConstraint("document_id", "group_id", name="uq_doc_dup_group"),
        Index("ix_document_duplicates_document_id", "document_id"),
        Index("ix_document_duplicates_group_id", "group_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    group_id = Column(Integer, ForeignKey("duplicate_groups.id", ondelete="CASCADE"), nullable=False)
    similarity = Column(Float, nullable=False)

    group = relationship("DuplicateGroup", back_populates="members")
    document = relationship("Document")


class DocumentCluster(Base):
    __tablename__ = "document_clusters"

    id = Column(Integer, primary_key=True, autoincrement=True)
    production_id = Column(Integer, ForeignKey("productions.id", ondelete="CASCADE"), nullable=False)
    cluster_index = Column(Integer, nullable=False)
    label = Column(String(100), nullable=True)
    doc_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    assignments = relationship("DocumentClusterAssignment", back_populates="cluster", cascade="all, delete-orphan")


class DocumentClusterAssignment(Base):
    __tablename__ = "document_cluster_assignments"
    __table_args__ = (
        UniqueConstraint("document_id", name="uq_doc_cluster"),
        Index("ix_doc_cluster_assignments_document_id", "document_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    cluster_id = Column(Integer, ForeignKey("document_clusters.id", ondelete="CASCADE"), nullable=False)

    cluster = relationship("DocumentCluster", back_populates="assignments")
    document = relationship("Document")
