"""Persist conversation-scoped always-allowed workspace Tool types.

Revision ID: c8d9e0f1a2b3
Revises: f7a8b9c0d1e2
Create Date: 2026-08-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c8d9e0f1a2b3"
down_revision = "f7a8b9c0d1e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column(
            "always_allowed_tool_names",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("conversations", "always_allowed_tool_names")
