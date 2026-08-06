"""Add durable execution leases and SSE events for Assistant turns.

Revision ID: 9a0b1c2d3e4f
Revises: 8f9a0b1c2d3e
Create Date: 2026-08-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "9a0b1c2d3e4f"
down_revision = "8f9a0b1c2d3e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversation_runs",
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "conversation_runs",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "conversation_runs",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_conversation_runs_recovery",
        "conversation_runs",
        ["run_kind", "status", "lease_expires_at"],
    )
    op.create_table(
        "assistant_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["conversation_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_assistant_events_run_sequence"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    durable_assistant_data = connection.execute(
        sa.text(
            """
            SELECT 1
            FROM conversation_runs
            WHERE run_kind = 'assistant_turn'
            LIMIT 1
            """
        )
    ).scalar_one_or_none()
    if durable_assistant_data is not None:
        raise RuntimeError(
            "Cannot downgrade Assistant execution while assistant turns exist; "
            "export or remove those turns before downgrade."
        )

    op.drop_table("assistant_events")
    op.drop_index("idx_conversation_runs_recovery", table_name="conversation_runs")
    op.drop_column("conversation_runs", "heartbeat_at")
    op.drop_column("conversation_runs", "lease_expires_at")
    op.drop_column("conversation_runs", "lease_owner")
