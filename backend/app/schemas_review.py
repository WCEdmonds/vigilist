"""Pydantic schemas for AI Review Workflow."""

from datetime import datetime
from uuid import UUID
from pydantic import BaseModel


class ReviewProjectCreate(BaseModel):
    name: str
    prompt_text: str
    sample_size: int = 50
    agreement_threshold: float = 0.80
    categories: list[dict] | None = None  # [{name, color, description}]


class ReviewProjectUpdate(BaseModel):
    name: str | None = None
    prompt_text: str | None = None
    sample_size: int | None = None
    agreement_threshold: float | None = None


class ReviewProjectOut(BaseModel):
    id: int
    production_id: int
    name: str
    prompt_text: str
    prompt_versions: list[dict]
    categories: list[dict]
    sample_size: int
    agreement_threshold: float
    status: str
    total_documents: int
    processed_documents: int
    total_cost_tokens: int
    created_by: str
    created_at: datetime
    updated_at: datetime
    sample_agreement_rate: float | None = None
    decision_breakdown: dict | None = None

    model_config = {"from_attributes": True}


class AIReviewResultOut(BaseModel):
    id: int
    project_id: int
    document_id: UUID
    bates_begin: str | None = None
    title: str | None = None
    is_sample: int
    ai_decision: str
    confidence_score: int
    reasoning: str
    key_excerpts: list[dict]
    considerations: str | None = None
    attorney_decision: str | None = None
    attorney_note: str | None = None
    prompt_version: int
    api_model: str
    api_cost_tokens: int
    created_at: datetime

    model_config = {"from_attributes": True}


class AttorneyDecision(BaseModel):
    decision: str
    note: str | None = None


class PaginatedResults(BaseModel):
    results: list[AIReviewResultOut]
    total: int
    page: int
    per_page: int
    agreement_rate: float | None = None
