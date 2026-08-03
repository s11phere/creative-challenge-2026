"""Complete durable Grounded QA persistence identities and constraints.

Revision ID: 07b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-07-31 16:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "07b8c9d0e1f2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None

_QA_STATUSES = (
    "created",
    "queued",
    "running",
    "verifying",
    "completed",
    "refused",
    "failed",
    "cancel_requested",
    "cancelled",
    "timed_out",
)
_STATUS_CHECK = "status IN (" + ", ".join(f"'{item}'" for item in _QA_STATUSES) + ")"


def upgrade() -> None:
    op.add_column("conversations", sa.Column("archived_at", sa.DateTime(timezone=True)))
    op.add_column(
        "qa_runs",
        sa.Column("question_message_id", postgresql.UUID(as_uuid=True), nullable=False),
    )
    op.add_column("qa_runs", sa.Column("answer_message_id", postgresql.UUID(as_uuid=True)))
    op.create_check_constraint("ck_qa_runs_status", "qa_runs", _STATUS_CHECK)
    op.create_foreign_key(
        "fk_qa_runs_question_message",
        "qa_runs",
        "qa_messages",
        ["question_message_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_qa_runs_answer_message",
        "qa_runs",
        "qa_messages",
        ["answer_message_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_table(
        "qa_run_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("qa_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column(
            "previous_attempt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("qa_run_attempts.id", ondelete="RESTRICT"),
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("cancellation_requested", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(100)),
        sa.Column("usage", postgresql.JSONB(), nullable=False),
        sa.Column("result", postgresql.JSONB()),
        sa.Column(
            "answer_message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("qa_messages.id", ondelete="RESTRICT"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "number", name="uq_qa_run_attempts_number"),
        sa.CheckConstraint("number >= 1", name="ck_qa_run_attempts_number_positive"),
        sa.CheckConstraint(_STATUS_CHECK, name="ck_qa_run_attempts_status"),
    )
    op.create_index("idx_qa_run_attempts_run_number", "qa_run_attempts", ["run_id", "number"])
    op.create_foreign_key(
        "fk_qa_evidence_attempt",
        "qa_evidence",
        "qa_run_attempts",
        ["attempt_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_qa_citations_attempt",
        "qa_citations",
        "qa_run_attempts",
        ["attempt_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "uq_qa_messages_conversation_idempotency",
        "qa_messages",
        ["conversation_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_table(
        "qa_feedback",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("qa_messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("qa_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "attempt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("qa_run_attempts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "space_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("spaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("caller_id", sa.String(255), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("review_status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "run_id", "caller_id", "idempotency_key", name="uq_qa_feedback_idempotency"
        ),
    )
    op.create_index("idx_qa_feedback_review_status", "qa_feedback", ["review_status", "created_at"])


def downgrade() -> None:
    op.drop_index("idx_qa_feedback_review_status", table_name="qa_feedback")
    op.drop_table("qa_feedback")
    op.drop_index("uq_qa_messages_conversation_idempotency", table_name="qa_messages")
    op.drop_constraint("fk_qa_citations_attempt", "qa_citations", type_="foreignkey")
    op.drop_constraint("fk_qa_evidence_attempt", "qa_evidence", type_="foreignkey")
    op.drop_index("idx_qa_run_attempts_run_number", table_name="qa_run_attempts")
    op.drop_table("qa_run_attempts")
    op.drop_constraint("fk_qa_runs_answer_message", "qa_runs", type_="foreignkey")
    op.drop_constraint("fk_qa_runs_question_message", "qa_runs", type_="foreignkey")
    op.drop_constraint("ck_qa_runs_status", "qa_runs", type_="check")
    op.drop_column("qa_runs", "answer_message_id")
    op.drop_column("qa_runs", "question_message_id")
    op.drop_column("conversations", "archived_at")
