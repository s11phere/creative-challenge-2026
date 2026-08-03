"""Persist human feedback review decisions and export controls."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "6d7e8f9a0b1"
down_revision = "5c6d7e8f9a0b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("qa_feedback", sa.Column("reviewer_id", sa.String(255), nullable=True))
    op.add_column(
        "qa_feedback", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "qa_feedback",
        sa.Column(
            "authorization_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "qa_feedback",
        sa.Column("redaction_complete", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("qa_feedback", sa.Column("expected_behavior", sa.String(16), nullable=True))
    op.add_column(
        "qa_feedback",
        sa.Column(
            "approved_evidence_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column("qa_feedback", sa.Column("gold_answer_sha256", sa.String(64), nullable=True))
    op.add_column("qa_feedback", sa.Column("rejection_reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_qa_feedback_review_status",
        "qa_feedback",
        "review_status in ('pending_review', 'accepted', 'rejected')",
    )
    op.create_check_constraint(
        "ck_qa_feedback_expected_behavior",
        "qa_feedback",
        "expected_behavior is null or expected_behavior in ('answer', 'refuse')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_qa_feedback_expected_behavior", "qa_feedback", type_="check")
    op.drop_constraint("ck_qa_feedback_review_status", "qa_feedback", type_="check")
    op.drop_column("qa_feedback", "rejection_reason")
    op.drop_column("qa_feedback", "gold_answer_sha256")
    op.drop_column("qa_feedback", "approved_evidence_ids")
    op.drop_column("qa_feedback", "expected_behavior")
    op.drop_column("qa_feedback", "redaction_complete")
    op.drop_column("qa_feedback", "authorization_confirmed")
    op.drop_column("qa_feedback", "reviewed_at")
    op.drop_column("qa_feedback", "reviewer_id")
