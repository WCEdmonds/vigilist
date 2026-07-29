"""add chat_conversations and chat_messages

Saved AI chat history, owned by a production and shared by its team: anyone
with access to the matter can read its conversations. `created_by` records the
author (for attribution and to gate deletion) and `chat_messages.user_id`
records who asked each question — in a shared thread that is not recoverable
from the conversation's creator alone.

Auto-saved on every turn; deletion is real, hence ON DELETE CASCADE from
messages rather than a soft-delete flag. Conversations also cascade from
productions: deleting a matter must not leave its chat logs behind.

Revision ID: z8h9i0j1k2l3
Revises: y7g8h9i0j1k2
Create Date: 2026-07-28
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "z8h9i0j1k2l3"
down_revision = "y7g8h9i0j1k2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "chat_conversations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "production_id",
            sa.Integer(),
            sa.ForeignKey("productions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by",
            sa.String(128),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("title", sa.String(200), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
    )
    # Serves the only listing query: conversations in this matter, newest first.
    op.create_index(
        "ix_chat_conversations_production_updated",
        "chat_conversations",
        ["production_id", "updated_at"],
    )

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "conversation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("chat_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        # Null on assistant turns; set to the asker on user turns.
        sa.Column(
            "user_id",
            sa.String(128),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "attachments",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_chat_messages_conversation",
        "chat_messages",
        ["conversation_id", "created_at"],
    )


def downgrade():
    op.drop_index("ix_chat_messages_conversation", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index(
        "ix_chat_conversations_production_updated", table_name="chat_conversations"
    )
    op.drop_table("chat_conversations")
