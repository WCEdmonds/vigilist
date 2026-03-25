import uuid
from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
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


class Production(Base):
    __tablename__ = "productions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False, unique=True)
    description = Column(Text, nullable=True)
    owner_id = Column(String(128), ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    documents = relationship("Document", back_populates="production")
    owner = relationship("User", foreign_keys=[owner_id])
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

    production = relationship("Production", back_populates="documents")
    tags = relationship("DocumentTag", back_populates="document", cascade="all, delete-orphan")
    notes = relationship("Note", back_populates="document", cascade="all, delete-orphan", order_by="Note.created_at.desc()")


class Tag(Base):
    __tablename__ = "tags"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    category = Column(String(50), nullable=False)
    color = Column(String(20), nullable=False, default="gray")
    keyboard_shortcut = Column(String(5), nullable=True)
    production_id = Column(Integer, ForeignKey("productions.id"), nullable=True)

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
    total_files = Column(Integer, nullable=False, default=0)
    processed_files = Column(Integer, nullable=False, default=0)
    errors = Column(JSONB, nullable=False, default=list)
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
