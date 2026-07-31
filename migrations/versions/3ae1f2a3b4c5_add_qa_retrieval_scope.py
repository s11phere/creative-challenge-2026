"""Persist fixed retrieval scope for Grounded QA runs.

Revision ID: 3ae1f2a3b4c5
Revises: 29d0e1f2a3b4
Create Date: 2026-07-31 21:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "3ae1f2a3b4c5"
down_revision = "29d0e1f2a3b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "qa_runs",
        sa.Column(
            "retrieval_scope",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("qa_runs", "retrieval_scope")
