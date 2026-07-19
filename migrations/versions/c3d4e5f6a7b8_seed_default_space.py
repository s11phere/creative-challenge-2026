"""Seed the default Space for the Web UI MVP.

The frontend (``apps/web/src/sources.ts``) hardcodes
``00000000-0000-0000-0000-000000000000`` as the default space UUID.
Without this row, source creation fails with a foreign-key violation on an
empty database.

This is a data migration — it is safe to run multiple times (idempotent:
uses ``INSERT … ON CONFLICT DO NOTHING``).

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-19 20:00:00.000000
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | None = None
depends_on: str | None = None


DEFAULT_SPACE_ID: str = "00000000-0000-0000-0000-000000000000"


def upgrade() -> None:
    op.execute(
        sa.text(
            "INSERT INTO spaces (id, name, owner_id, retrieval_profile) "
            "VALUES (:id, :name, :owner_id, CAST(:retrieval_profile AS jsonb)) "
            "ON CONFLICT (id) DO NOTHING"
        ).bindparams(
            id=uuid.UUID(DEFAULT_SPACE_ID),
            name="Default Space",
            owner_id="system",
            retrieval_profile="{}",
        )
    )


def downgrade() -> None:
    # Keep user data intact: the default Space may own Sources by the time a
    # downgrade is requested, and those relationships use ON DELETE CASCADE.
    statement = sa.text(
        "DELETE FROM spaces "
        "WHERE id = :id "
        "AND owner_id = 'system' "
        "AND name = 'Default Space' "
        "AND NOT EXISTS (SELECT 1 FROM sources WHERE sources.space_id = spaces.id)"
    ).bindparams(
        sa.bindparam(
            "id",
            value=uuid.UUID(DEFAULT_SPACE_ID),
            type_=postgresql.UUID(as_uuid=True),
        )
    )
    op.execute(statement)
