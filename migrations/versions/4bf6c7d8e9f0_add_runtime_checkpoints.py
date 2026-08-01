"""Add durable Runtime snapshots and append-only checkpoints.

Revision ID: 4bf6c7d8e9f0
Revises: 3ae1f2a3b4c5
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "4bf6c7d8e9f0"
down_revision = "3ae1f2a3b4c5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_runs",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("caller_id", sa.String(255), nullable=False),
        sa.Column("trace_id", sa.String(255), nullable=False),
        sa.Column("skill_name", sa.String(255), nullable=False),
        sa.Column("skill_version", sa.String(100), nullable=False),
        sa.Column("skill_content_sha256", sa.String(64), nullable=False),
        sa.Column("granted_permissions", postgresql.JSONB, nullable=False),
        sa.Column("budget", postgresql.JSONB, nullable=False),
        sa.Column("usage", postgresql.JSONB, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("current_step", sa.String(32), nullable=True),
        sa.Column("checkpoint_sequence", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", postgresql.JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["run_id"], ["qa_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["space_id"], ["spaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id"),
        sa.CheckConstraint("checkpoint_sequence >= 0", name="ck_runtime_runs_checkpoint_sequence"),
    )
    op.create_index("idx_runtime_runs_status", "runtime_runs", ["status", "updated_at"])
    op.create_table(
        "runtime_checkpoints",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer, nullable=False),
        sa.Column("schema_version", sa.Integer, nullable=False),
        sa.Column("skill_name", sa.String(255), nullable=False),
        sa.Column("skill_version", sa.String(100), nullable=False),
        sa.Column("skill_content_sha256", sa.String(64), nullable=False),
        sa.Column("state", postgresql.JSONB, nullable=False),
        sa.Column("state_sha256", sa.String(64), nullable=False),
        sa.Column("usage", postgresql.JSONB, nullable=False),
        sa.Column("next_step", sa.String(32), nullable=False),
        sa.Column("next_node", sa.String(255), nullable=False),
        sa.Column("verified", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["run_id"], ["runtime_runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id", "sequence"),
        sa.CheckConstraint("sequence >= 1", name="ck_runtime_checkpoints_sequence_positive"),
        sa.CheckConstraint("schema_version >= 1", name="ck_runtime_checkpoints_schema_positive"),
        sa.CheckConstraint("char_length(state_sha256) = 64", name="ck_runtime_checkpoints_state_sha256"),
    )


def downgrade() -> None:
    op.drop_table("runtime_checkpoints")
    op.drop_index("idx_runtime_runs_status", table_name="runtime_runs")
    op.drop_table("runtime_runs")
