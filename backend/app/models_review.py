"""AI Review Workflow models."""

import uuid

from sqlalchemy import (
    Column, DateTime, Float, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from app.models import Base


class ReviewProject(Base):
    __tablename__ = "review_projects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    production_id = Column(Integer, ForeignKey("productions.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(255), nullable=False)
    prompt_text = Column(Text, nullable=False)
    categories = Column(JSONB, nullable=False, default=list)  # [{name, color, description}]
    prompt_versions = Column(JSONB, nullable=False, default=list)
    sample_size = Column(Integer, nullable=False, default=50)
    agreement_threshold = Column(Float, nullable=False, default=0.80)
    status = Column(String(20), nullable=False, default="draft")
    total_documents = Column(Integer, nullable=False, default=0)
    processed_documents = Column(Integer, nullable=False, default=0)
    total_cost_tokens = Column(Integer, nullable=False, default=0)
    created_by = Column(String(128), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    production = relationship("Production")
    creator = relationship("User", foreign_keys=[created_by])
    results = relationship("AIReviewResult", back_populates="project", cascade="all, delete-orphan")


class AIReviewResult(Base):
    __tablename__ = "ai_review_results"
    __table_args__ = (
        UniqueConstraint("project_id", "document_id", name="uq_project_doc"),
        Index("ix_review_results_project", "project_id"),
        Index("ix_review_results_confidence", "project_id", "confidence_score"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("review_projects.id", ondelete="CASCADE"), nullable=False)
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    is_sample = Column(Integer, nullable=False, default=0)
    ai_decision = Column(String(20), nullable=False)
    confidence_score = Column(Integer, nullable=False)
    reasoning = Column(Text, nullable=False)
    key_excerpts = Column(JSONB, nullable=False, default=list)
    considerations = Column(Text, nullable=True)
    attorney_decision = Column(String(30), nullable=True)
    attorney_note = Column(Text, nullable=True)
    prompt_version = Column(Integer, nullable=False, default=1)
    api_model = Column(String(50), nullable=False)
    api_cost_tokens = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    project = relationship("ReviewProject", back_populates="results")
    document = relationship("Document")
