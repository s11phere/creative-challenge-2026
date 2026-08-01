"""Add durable Runtime approvals and citation-backed derived knowledge."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "5c6d7e8f9a0b"
down_revision = "4bf6c7d8e9f0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_approvals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("caller_id", sa.String(255), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("tool_name", sa.String(255), nullable=False),
        sa.Column("tool_version", sa.String(100), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(255), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "metadata", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.ForeignKeyConstraint(["run_id"], ["qa_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["space_id"], ["spaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id", "action", "idempotency_key", name="uq_runtime_approvals_idempotency"
        ),
        sa.CheckConstraint(
            "status in ('pending', 'approved', 'rejected')", name="ck_runtime_approval_status"
        ),
    )
    op.create_index("idx_runtime_approvals_run_status", "runtime_approvals", ["run_id", "status"])
    op.create_table(
        "derived_knowledge_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("content", postgresql.JSONB, nullable=False),
        sa.Column(
            "citation_ids", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["run_id"], ["qa_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["space_id"], ["spaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "idempotency_key", name="uq_derived_knowledge_idempotency"),
    )
    op.create_index(
        "idx_derived_knowledge_space_created", "derived_knowledge_items", ["space_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("idx_derived_knowledge_space_created", table_name="derived_knowledge_items")
    op.drop_table("derived_knowledge_items")
    op.drop_index("idx_runtime_approvals_run_status", table_name="runtime_approvals")
    op.drop_table("runtime_approvals")
