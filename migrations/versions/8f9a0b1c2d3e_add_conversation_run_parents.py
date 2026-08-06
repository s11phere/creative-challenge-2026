"""Add shared ConversationRun parents without renumbering QA identities.

Revision ID: 8f9a0b1c2d3e
Revises: 7e8f9a0b1c2d
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "8f9a0b1c2d3e"
down_revision = "7e8f9a0b1c2d"
branch_labels = None
depends_on = None


_PARENT_STATUS_CHECK = (
    "status IN ('created', 'queued', 'running', 'waiting_clarification', "
    "'waiting_approval', 'completed', 'refused', 'failed', 'cancel_requested', "
    "'cancelled', 'timed_out')"
)
_PARENT_KIND_CHECK = "run_kind IN ('assistant_turn', 'grounded_qa', 'skill', 'context_compaction')"
_PARENT_SELECTION_CHECK = "selection_source IN ('auto', 'command', 'none')"


def upgrade() -> None:
    op.create_table(
        "conversation_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "space_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("spaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("caller_id", sa.String(255), nullable=False),
        sa.Column(
            "user_message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("qa_messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("run_kind", sa.String(32), nullable=False),
        sa.Column("selection_source", sa.String(16), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "cancellation_requested", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("router_version", sa.String(100), nullable=False),
        sa.Column("core_prompt_version", sa.String(100), nullable=False),
        sa.Column("model_identity", sa.String(255), nullable=False),
        sa.Column("skill_name", sa.String(255), nullable=True),
        sa.Column("skill_version", sa.String(100), nullable=True),
        sa.Column("skill_content_sha256", sa.String(64), nullable=True),
        sa.Column(
            "usage",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "space_id", "caller_id", "idempotency_key", name="uq_conversation_runs_idempotency"
        ),
        sa.CheckConstraint(_PARENT_STATUS_CHECK, name="ck_conversation_runs_status"),
        sa.CheckConstraint(_PARENT_KIND_CHECK, name="ck_conversation_runs_kind"),
        sa.CheckConstraint(_PARENT_SELECTION_CHECK, name="ck_conversation_runs_selection"),
        sa.CheckConstraint(
            "(skill_name IS NULL AND skill_version IS NULL AND skill_content_sha256 IS NULL) OR "
            "(skill_name IS NOT NULL AND skill_version IS NOT NULL "
            "AND skill_content_sha256 IS NOT NULL)",
            name="ck_conversation_runs_skill_identity",
        ),
    )
    op.create_index(
        "idx_conversation_runs_conversation", "conversation_runs", ["conversation_id", "created_at"]
    )
    op.create_index("idx_conversation_runs_status", "conversation_runs", ["status", "updated_at"])

    # QA IDs become parent IDs. The backfill reads the durable QA projection and
    # deliberately stores only safe result references, not answer content.
    op.execute(
        sa.text(
            """
            INSERT INTO conversation_runs (
                id, conversation_id, space_id, caller_id, user_message_id,
                idempotency_key, run_kind, selection_source, status,
                cancellation_requested, error_code, router_version,
                core_prompt_version, model_identity, skill_name, skill_version,
                skill_content_sha256, usage, result, created_at, updated_at
            )
            SELECT
                qa.id,
                qa.conversation_id,
                qa.space_id,
                qa.caller_id,
                qa.question_message_id,
                qa.idempotency_key,
                CASE
                    WHEN qa.versions ->> 'skill_name' = 'knowledge_qa' THEN 'grounded_qa'
                    ELSE 'skill'
                END,
                'none',
                CASE WHEN qa.status = 'verifying' THEN 'running' ELSE qa.status END,
                qa.cancellation_requested,
                qa.error_code,
                'legacy-v1',
                'legacy-v1',
                qa.versions ->> 'model_identity',
                CASE
                    WHEN COALESCE(qa.versions ->> 'skill_content_sha256', '')
                        ~ '^[0-9a-f]{64}$'
                    THEN qa.versions ->> 'skill_name'
                    ELSE NULL
                END,
                CASE
                    WHEN COALESCE(qa.versions ->> 'skill_content_sha256', '')
                        ~ '^[0-9a-f]{64}$'
                    THEN qa.versions ->> 'skill_version'
                    ELSE NULL
                END,
                CASE
                    WHEN COALESCE(qa.versions ->> 'skill_content_sha256', '')
                        ~ '^[0-9a-f]{64}$'
                    THEN qa.versions ->> 'skill_content_sha256'
                    ELSE NULL
                END,
                jsonb_build_object(
                    'input_tokens', COALESCE((qa.usage ->> 'input_tokens')::integer, 0),
                    'output_tokens', COALESCE((qa.usage ->> 'output_tokens')::integer, 0),
                    'model_latency_ms',
                    COALESCE((qa.usage ->> 'model_latency_ms')::double precision, 0)
                ),
                CASE
                    WHEN qa.status IN ('completed', 'refused') THEN jsonb_build_object(
                        'kind', 'skill_result',
                        'message_id', CASE
                            WHEN qa.answer_message_id IS NULL THEN NULL
                            ELSE qa.answer_message_id::text
                        END,
                        'clarification', NULL
                    )
                    ELSE NULL
                END,
                qa.created_at,
                qa.updated_at
            FROM qa_runs AS qa
            """
        )
    )

    op.drop_constraint("qa_messages_run_id_fkey", "qa_messages", type_="foreignkey")
    op.create_foreign_key(
        "fk_qa_messages_conversation_run",
        "qa_messages",
        "conversation_runs",
        ["run_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_qa_runs_parent", "qa_runs", "conversation_runs", ["id"], ["id"], ondelete="CASCADE"
    )

    op.drop_constraint("runtime_runs_run_id_fkey", "runtime_runs", type_="foreignkey")
    op.create_foreign_key(
        "fk_runtime_runs_conversation_run",
        "runtime_runs",
        "conversation_runs",
        ["run_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint("runtime_approvals_run_id_fkey", "runtime_approvals", type_="foreignkey")
    op.create_foreign_key(
        "fk_runtime_approvals_conversation_run",
        "runtime_approvals",
        "conversation_runs",
        ["run_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint(
        "derived_knowledge_items_run_id_fkey", "derived_knowledge_items", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_derived_knowledge_conversation_run",
        "derived_knowledge_items",
        "conversation_runs",
        ["run_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.alter_column("conversation_runs", "cancellation_requested", server_default=None)
    op.alter_column("conversation_runs", "usage", server_default=None)


def downgrade() -> None:
    connection = op.get_bind()
    generic_parent = connection.execute(
        sa.text(
            """
            SELECT 1
            FROM conversation_runs AS parent
            LEFT JOIN qa_runs AS qa ON qa.id = parent.id
            WHERE qa.id IS NULL
            LIMIT 1
            """
        )
    ).scalar_one_or_none()
    if generic_parent is not None:
        raise RuntimeError(
            "Cannot downgrade ConversationRun parents while non-QA turns exist; "
            "export or remove those turns before downgrade."
        )

    op.drop_constraint("fk_qa_messages_conversation_run", "qa_messages", type_="foreignkey")
    op.drop_constraint("fk_qa_runs_parent", "qa_runs", type_="foreignkey")
    op.drop_constraint("fk_runtime_runs_conversation_run", "runtime_runs", type_="foreignkey")
    op.drop_constraint(
        "fk_runtime_approvals_conversation_run", "runtime_approvals", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_derived_knowledge_conversation_run", "derived_knowledge_items", type_="foreignkey"
    )

    op.create_foreign_key(
        "qa_messages_run_id_fkey", "qa_messages", "qa_runs", ["run_id"], ["id"], ondelete="CASCADE"
    )
    op.create_foreign_key(
        "runtime_runs_run_id_fkey",
        "runtime_runs",
        "qa_runs",
        ["run_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "runtime_approvals_run_id_fkey",
        "runtime_approvals",
        "qa_runs",
        ["run_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "derived_knowledge_items_run_id_fkey",
        "derived_knowledge_items",
        "qa_runs",
        ["run_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.drop_index("idx_conversation_runs_status", table_name="conversation_runs")
    op.drop_index("idx_conversation_runs_conversation", table_name="conversation_runs")
    op.drop_table("conversation_runs")
