"""Add a persisted PostgreSQL FTS document for published chunk retrieval.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-21 20:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A stored generated column computes existing rows during ALTER TABLE and
    # remains synchronized with future chunk text writes without application code.
    op.add_column(
        "chunks",
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('simple'::regconfig, coalesce(text, ''::text))", persisted=True
            ),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_chunks_search_vector",
        "chunks",
        ["search_vector"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("idx_chunks_search_vector", table_name="chunks")
    op.drop_column("chunks", "search_vector")
