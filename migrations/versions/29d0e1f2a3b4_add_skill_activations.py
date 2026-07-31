"""Add durable Skill active pointers.

Revision ID: 29d0e1f2a3b4
Revises: 18c9d0e1f2a3
Create Date: 2026-07-31 20:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "29d0e1f2a3b4"
down_revision = "18c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skill_activations",
        sa.Column("skill_name", sa.String(255), primary_key=True),
        sa.Column("active_version", sa.String(100), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("revision >= 1", name="ck_skill_activations_revision_positive"),
        sa.CheckConstraint(
            "char_length(content_sha256) = 64",
            name="ck_skill_activations_content_sha256_length",
        ),
    )


def downgrade() -> None:
    op.drop_table("skill_activations")
