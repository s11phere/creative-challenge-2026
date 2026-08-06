"""Add append-only rolling conversation summaries.

Revision ID: b1c2d3e4f5a6
Revises: 9a0b1c2d3e4f
Create Date: 2026-08-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b1c2d3e4f5a6"
down_revision = "9a0b1c2d3e4f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("qa_runs", sa.Column("standalone_request", sa.Text(), nullable=True))
    op.add_column(
        "qa_runs",
        sa.Column(
            "context_sensitivity",
            sa.String(length=32),
            nullable=False,
            server_default="private_local",
        ),
    )
    op.create_table(
        "conversation_summaries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "space_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("spaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversation_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "covered_start_message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("qa_messages.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "covered_end_message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("qa_messages.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("covered_message_count", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=100), nullable=False),
        sa.Column("model_identity", sa.String(length=255), nullable=False),
        sa.Column("sensitivity", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", name="uq_conversation_summaries_run"),
        sa.UniqueConstraint(
            "conversation_id",
            "covered_end_message_id",
            "prompt_version",
            name="uq_conversation_summaries_coverage",
        ),
        sa.CheckConstraint("covered_message_count >= 1", name="ck_conversation_summaries_count"),
        sa.CheckConstraint(
            "sensitivity IN ('public_demo', 'private_local', 'restricted')",
            name="ck_conversation_summaries_sensitivity",
        ),
    )
    op.create_index(
        "idx_conversation_summaries_conversation",
        "conversation_summaries",
        ["conversation_id", "created_at"],
    )
    op.alter_column("qa_runs", "context_sensitivity", server_default=None)


def downgrade() -> None:
    op.drop_index("idx_conversation_summaries_conversation", table_name="conversation_summaries")
    op.drop_table("conversation_summaries")
    op.drop_column("qa_runs", "context_sensitivity")
    op.drop_column("qa_runs", "standalone_request")
