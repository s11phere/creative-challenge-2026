"""Add durable, redacted Agent Loop v3 event history.

Revision ID: 1b2c3d4e5f6a
Revises: 0a1b2c3d4e5f
Create Date: 2026-08-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "1b2c3d4e5f6a"
down_revision = "0a1b2c3d4e5f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_run_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_key", sa.String(length=200), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("sequence >= 1", name="ck_agent_run_events_sequence_positive"),
        sa.ForeignKeyConstraint(["run_id"], ["conversation_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_agent_run_events_run_sequence"),
        sa.UniqueConstraint("run_id", "event_key", name="uq_agent_run_events_run_key"),
    )
    op.create_index(
        "idx_agent_run_events_run_sequence",
        "agent_run_events",
        ["run_id", "sequence"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    history_exists = connection.execute(
        sa.text("SELECT 1 FROM agent_run_events LIMIT 1")
    ).scalar_one_or_none()
    if history_exists is not None:
        raise RuntimeError(
            "Cannot downgrade Agent Loop v3 history while durable events exist; "
            "export or remove them before downgrade."
        )
    op.drop_index("idx_agent_run_events_run_sequence", table_name="agent_run_events")
    op.drop_table("agent_run_events")
