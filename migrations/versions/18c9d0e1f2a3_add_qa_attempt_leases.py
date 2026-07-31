"""Add durable Worker leases to Grounded QA attempts.

Revision ID: 18c9d0e1f2a3
Revises: 07b8c9d0e1f2
Create Date: 2026-07-31 17:30:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "18c9d0e1f2a3"
down_revision = "07b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("qa_run_attempts", sa.Column("lease_owner", sa.String(255)))
    op.add_column("qa_run_attempts", sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
    op.add_column("qa_run_attempts", sa.Column("heartbeat_at", sa.DateTime(timezone=True)))
    op.create_index(
        "idx_qa_run_attempts_recovery",
        "qa_run_attempts",
        ["status", "lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_qa_run_attempts_recovery", table_name="qa_run_attempts")
    op.drop_column("qa_run_attempts", "heartbeat_at")
    op.drop_column("qa_run_attempts", "lease_expires_at")
    op.drop_column("qa_run_attempts", "lease_owner")
