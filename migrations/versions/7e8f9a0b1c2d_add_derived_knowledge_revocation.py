"""Add lifecycle state to derived knowledge items."""

import sqlalchemy as sa
from alembic import op

revision = "7e8f9a0b1c2d"
down_revision = "6d7e8f9a0b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runtime_approvals",
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "runtime_approvals",
        sa.Column("revoked_by", sa.String(255), nullable=True),
    )
    op.drop_constraint("ck_runtime_approval_status", "runtime_approvals", type_="check")
    op.create_check_constraint(
        "ck_runtime_approval_status",
        "runtime_approvals",
        "status in ('pending', 'approved', 'rejected', 'revoked')",
    )
    op.add_column(
        "derived_knowledge_items",
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
    )
    op.add_column(
        "derived_knowledge_items",
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "derived_knowledge_items",
        sa.Column("revoked_by", sa.String(255), nullable=True),
    )
    op.create_check_constraint(
        "ck_derived_knowledge_status",
        "derived_knowledge_items",
        "status in ('active', 'revoked')",
    )
    op.alter_column("derived_knowledge_items", "status", server_default=None)


def downgrade() -> None:
    op.drop_constraint("ck_derived_knowledge_status", "derived_knowledge_items", type_="check")
    op.drop_column("derived_knowledge_items", "revoked_by")
    op.drop_column("derived_knowledge_items", "revoked_at")
    op.drop_column("derived_knowledge_items", "status")
    op.drop_constraint("ck_runtime_approval_status", "runtime_approvals", type_="check")
    op.create_check_constraint(
        "ck_runtime_approval_status",
        "runtime_approvals",
        "status in ('pending', 'approved', 'rejected')",
    )
    op.drop_column("runtime_approvals", "revoked_by")
    op.drop_column("runtime_approvals", "revoked_at")
