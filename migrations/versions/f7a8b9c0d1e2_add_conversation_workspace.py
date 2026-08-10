"""Persist the selected logical workspace for each conversation.

Revision ID: f7a8b9c0d1e2
Revises: 1b2c3d4e5f6a
Create Date: 2026-08-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f7a8b9c0d1e2"
down_revision = "1b2c3d4e5f6a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations", sa.Column("workspace_path", sa.String(length=1024), nullable=True)
    )


def downgrade() -> None:
    connection = op.get_bind()
    selected = connection.execute(
        sa.text("SELECT 1 FROM conversations WHERE workspace_path IS NOT NULL LIMIT 1")
    ).scalar_one_or_none()
    if selected is not None:
        raise RuntimeError("Cannot remove conversation workspaces while selections exist.")
    op.drop_column("conversations", "workspace_path")
