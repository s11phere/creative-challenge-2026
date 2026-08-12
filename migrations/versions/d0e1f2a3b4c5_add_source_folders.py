"""add_source_folders

Add a user-facing ``name`` to sources so they can act as named folders, and
migrate the legacy shared browser source (``web-upload://browser``) into a
``默认文件夹`` folder.

Revision ID: d0e1f2a3b4c5
Revises: c8d9e0f1a2b3
Create Date: 2026-08-12 12:20:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d0e1f2a3b4c5"
down_revision: str | None = "c8d9e0f1a2b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("name", sa.String(255), nullable=True))

    bind = op.get_bind()
    # Turn the legacy shared browser source into the default folder. Idempotent:
    # re-running only touches rows that still carry the browser URI.
    bind.execute(
        sa.text(
            "UPDATE sources SET source_type = 'folder', name = '默认文件夹' "
            "WHERE uri = 'web-upload://browser'"
        )
    )
    # A space could hold more than one legacy browser source (an old race).
    # Keep the earliest as 默认文件夹 and blank the rest so the partial unique
    # index below does not fail on duplicates.
    bind.execute(
        sa.text(
            "WITH ranked AS ("
            "  SELECT id, row_number() OVER (PARTITION BY space_id ORDER BY created_at, id) AS rn "
            "  FROM sources WHERE uri = 'web-upload://browser'"
            ") "
            "UPDATE sources SET name = '' WHERE id IN (SELECT id FROM ranked WHERE rn > 1)"
        )
    )
    bind.execute(sa.text("UPDATE sources SET name = '' WHERE name IS NULL"))

    op.alter_column("sources", "name", nullable=False, server_default="")

    # Enforce unique folder names per space while exempting legacy upload
    # sources that never got a user-facing name.
    op.create_index(
        "uq_sources_space_name",
        "sources",
        ["space_id", "name"],
        unique=True,
        postgresql_where=sa.text("name <> ''"),
    )


def downgrade() -> None:
    op.drop_index("uq_sources_space_name", table_name="sources")
    op.drop_column("sources", "name")
