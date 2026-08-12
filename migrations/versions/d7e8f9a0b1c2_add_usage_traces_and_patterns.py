"""Add personal usage traces and distilled pattern aggregates.

Revision ID: d7e8f9a0b1c2
Revises: c8d9e0f1a2b3
Create Date: 2026-08-12 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "d7e8f9a0b1c2"
down_revision = "c8d9e0f1a2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_traces",
        sa.Column(
            "run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("conversation_runs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "conversation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("skill_name", sa.String(255), nullable=True),
        sa.Column("command", sa.String(64), nullable=True),
        sa.Column("input_summary", sa.String(1024), nullable=False),
        sa.Column("tools_used", JSONB(), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("sensitivity", sa.String(32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "(skill_name IS NOT NULL AND command IS NULL) OR "
            "(skill_name IS NULL AND command IS NOT NULL)",
            name="ck_usage_traces_identity",
        ),
        sa.CheckConstraint(
            "outcome IN ('completed', 'failed', 'refused', 'clarified')",
            name="ck_usage_traces_outcome",
        ),
        sa.CheckConstraint(
            "sensitivity IN ('public_demo', 'private_local', 'restricted')",
            name="ck_usage_traces_sensitivity",
        ),
    )
    op.create_index(
        "idx_usage_traces_conversation_created",
        "usage_traces",
        ["conversation_id", "created_at"],
    )
    op.create_table(
        "usage_patterns",
        sa.Column("key", sa.String(512), primary_key=True),
        sa.Column("skill_name", sa.String(255), nullable=True),
        sa.Column("task_category", sa.String(64), nullable=False),
        sa.Column("tool_sequence", sa.String(1024), nullable=False),
        sa.Column("input_type", sa.String(32), nullable=False),
        sa.Column("frequency", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("frequency >= 1", name="ck_usage_patterns_frequency"),
    )


def downgrade() -> None:
    op.drop_table("usage_patterns")
    op.drop_index("idx_usage_traces_conversation_created", table_name="usage_traces")
    op.drop_table("usage_traces")
