"""Add cross-session long-term memory entries (personalization Phase 5).

Revision ID: a3b4c5d6e7f8
Revises: e8f9a0b1c2d3
Create Date: 2026-08-13 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import UUID

revision = "a3b4c5d6e7f8"
down_revision = "e8f9a0b1c2d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_entries",
        sa.Column("memory_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("entry_type", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column(
            "source_conversation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "source_summary_id",
            UUID(as_uuid=True),
            sa.ForeignKey("conversation_summaries.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source_kind", sa.String(16), nullable=False),
        sa.Column("embedding", Vector(768), nullable=True),
        sa.Column("sensitivity", sa.String(32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("frequency", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "entry_type IN ('fact', 'preference', 'pattern')",
            name="ck_memory_entries_type",
        ),
        sa.CheckConstraint(
            "sensitivity IN ('public_demo', 'private_local', 'restricted')",
            name="ck_memory_entries_sensitivity",
        ),
        sa.CheckConstraint(
            "source_kind IN ('summary', 'pattern')",
            name="ck_memory_entries_source_kind",
        ),
        sa.CheckConstraint("frequency >= 1", name="ck_memory_entries_frequency"),
        sa.UniqueConstraint("content_sha256", name="uq_memory_entries_content"),
    )
    op.create_index(
        "idx_memory_entries_embedding",
        "memory_entries",
        ["embedding"],
        postgresql_using="ivfflat",
        postgresql_with={"lists": 100},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index(
        "idx_memory_entries_source_updated",
        "memory_entries",
        ["source_conversation_id", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_memory_entries_source_updated", table_name="memory_entries")
    op.drop_index("idx_memory_entries_embedding", table_name="memory_entries")
    op.drop_table("memory_entries")
