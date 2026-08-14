"""Add durable enabled state to installed Skill activations.

Revision ID: b3c4d5e6f7a8
Revises: a3b4c5d6e7f8
Create Date: 2026-08-14 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "b3c4d5e6f7a8"
down_revision = "a3b4c5d6e7f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "skill_activations",
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.alter_column("skill_activations", "active", server_default=None)


def downgrade() -> None:
    op.drop_column("skill_activations", "active")
