"""Persist conversation effort preferences and mapped Run profiles.

Revision ID: 0a1b2c3d4e5f
Revises: b1c2d3e4f5a6
Create Date: 2026-08-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0a1b2c3d4e5f"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None

_EFFORT_CHECK = (
    "reasoning_effort IN ('auto', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max')"
)
_LEGACY_PROFILE = (
    '{"schema_version":"reasoning-profile-v1","requested_effort":"auto",'
    '"effective_effort":"none","provider":"legacy","model":"legacy",'
    '"mapping_version":"reasoning-mapping-v1","mode":"disabled",'
    '"downgrade_reason":"capability_unavailable"}'
)


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("reasoning_effort", sa.String(length=16), nullable=False, server_default="auto"),
    )
    op.create_check_constraint("ck_conversations_reasoning_effort", "conversations", _EFFORT_CHECK)
    op.add_column(
        "conversation_runs",
        sa.Column(
            "reasoning_profile",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text(f"'{_LEGACY_PROFILE}'::jsonb"),
        ),
    )
    op.alter_column("conversations", "reasoning_effort", server_default=None)
    op.alter_column("conversation_runs", "reasoning_profile", server_default=None)


def downgrade() -> None:
    connection = op.get_bind()
    preference_exists = connection.execute(
        sa.text("SELECT 1 FROM conversations WHERE reasoning_effort <> 'auto' LIMIT 1")
    ).scalar_one_or_none()
    profile_exists = connection.execute(
        sa.text(
            "SELECT 1 FROM conversation_runs "
            "WHERE reasoning_profile <> CAST(:profile AS jsonb) LIMIT 1"
        ),
        {"profile": _LEGACY_PROFILE},
    ).scalar_one_or_none()
    if preference_exists is not None or profile_exists is not None:
        raise RuntimeError(
            "Cannot downgrade reasoning profiles after a conversation preference or mapped "
            "Run exists."
        )
    op.drop_column("conversation_runs", "reasoning_profile")
    op.drop_constraint("ck_conversations_reasoning_effort", "conversations", type_="check")
    op.drop_column("conversations", "reasoning_effort")
