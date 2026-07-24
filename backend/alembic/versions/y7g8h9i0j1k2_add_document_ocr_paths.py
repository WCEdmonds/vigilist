"""add ocr_paths to documents

Per-page OCR word-box sidecar paths (GCS), index-aligned with image_paths.
"" marks a page whose OCR or sidecar upload failed.

Revision ID: y7g8h9i0j1k2
Revises: x6f7g8h9i0j1
Create Date: 2026-07-24
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "y7g8h9i0j1k2"
down_revision = "x6f7g8h9i0j1"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "documents",
        sa.Column(
            "ocr_paths",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade():
    op.drop_column("documents", "ocr_paths")
