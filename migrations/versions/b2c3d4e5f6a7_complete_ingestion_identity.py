"""complete_ingestion_identity

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-18 15:30:00.000000
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EMPTY_CONFIG_HASH = hashlib.sha256(b"{}").hexdigest()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fail_on_duplicate(query: str, identity: str) -> None:
    duplicate = op.get_bind().execute(sa.text(query)).first()
    if duplicate is not None:
        raise RuntimeError(
            f"Cannot add {identity} uniqueness: existing rows contain duplicate identities"
        )


def upgrade() -> None:
    op.add_column("documents", sa.Column("deleted_at", sa.DateTime(timezone=True)))

    op.add_column("document_versions", sa.Column("blob_hash", sa.String(64), nullable=True))
    op.add_column(
        "document_versions",
        sa.Column("normalizer_version", sa.String(50), server_default="1.0", nullable=False),
    )
    op.add_column(
        "document_versions",
        sa.Column("chunker_version", sa.String(50), server_default="1.0", nullable=False),
    )
    op.add_column(
        "document_versions",
        sa.Column("embedding_version", sa.String(100), server_default="1.0", nullable=False),
    )
    op.add_column(
        "document_versions",
        sa.Column("processing_config_hash", sa.String(64), nullable=True),
    )
    op.add_column(
        "document_versions",
        sa.Column(
            "processing_config",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )

    bind = op.get_bind()
    versions = bind.execute(sa.text("SELECT id, content_hash FROM document_versions")).mappings()
    for version in versions:
        content_hash = str(version["content_hash"])
        blob_hash = (
            content_hash.lower()
            if len(content_hash) == 64
            and all(character in "0123456789abcdefABCDEF" for character in content_hash)
            else _sha256(f"legacy:{version['id']}:{content_hash}")
        )
        bind.execute(
            sa.text(
                "UPDATE document_versions "
                "SET blob_hash = :blob_hash, processing_config_hash = :config_hash "
                "WHERE id = :version_id"
            ),
            {
                "blob_hash": blob_hash,
                "config_hash": _EMPTY_CONFIG_HASH,
                "version_id": version["id"],
            },
        )
    op.alter_column("document_versions", "blob_hash", nullable=False)
    op.alter_column("document_versions", "processing_config_hash", nullable=False)

    op.add_column("chunks", sa.Column("chunk_hash", sa.String(64), nullable=True))
    chunks = bind.execute(sa.text("SELECT id, text FROM chunks")).mappings()
    for chunk in chunks:
        bind.execute(
            sa.text("UPDATE chunks SET chunk_hash = :chunk_hash WHERE id = :chunk_id"),
            {"chunk_hash": _sha256(str(chunk["text"])), "chunk_id": chunk["id"]},
        )
    op.alter_column("chunks", "chunk_hash", nullable=False)

    op.add_column(
        "ingestion_tasks",
        sa.Column("operation", sa.String(20), server_default="ingest", nullable=False),
    )
    op.add_column(
        "ingestion_tasks",
        sa.Column("status", sa.String(20), server_default="queued", nullable=False),
    )
    op.add_column(
        "ingestion_tasks",
        sa.Column("target_version_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("ingestion_tasks", sa.Column("idempotency_key", sa.String(255), nullable=True))
    op.add_column(
        "ingestion_tasks",
        sa.Column("max_retries", sa.Integer(), server_default="3", nullable=False),
    )
    op.add_column("ingestion_tasks", sa.Column("cancel_requested_at", sa.DateTime(timezone=True)))
    op.add_column("ingestion_tasks", sa.Column("enqueued_at", sa.DateTime(timezone=True)))
    op.add_column("ingestion_tasks", sa.Column("heartbeat_at", sa.DateTime(timezone=True)))
    op.add_column("ingestion_tasks", sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
    op.add_column("ingestion_tasks", sa.Column("error_code", sa.String(100)))

    bind.execute(
        sa.text(
            "UPDATE ingestion_tasks SET "
            "idempotency_key = 'legacy:' || id::text, "
            "status = CASE "
            "  WHEN stage = 'complete' THEN 'succeeded' "
            "  WHEN stage = 'failed' THEN 'failed' "
            "  ELSE 'queued' "
            "END, "
            "stage = CASE "
            "  WHEN stage = 'complete' THEN 'publish' "
            "  WHEN stage = 'failed' THEN 'discover' "
            "  ELSE stage "
            "END"
        )
    )
    op.alter_column("ingestion_tasks", "idempotency_key", nullable=False)

    _fail_on_duplicate(
        "SELECT source_id, stable_key FROM documents "
        "GROUP BY source_id, stable_key HAVING count(*) > 1 LIMIT 1",
        "Document stable key",
    )
    _fail_on_duplicate(
        "SELECT document_id, content_hash, parser_version, normalizer_version, "
        "chunker_version, embedding_version, processing_config_hash "
        "FROM document_versions "
        "GROUP BY document_id, content_hash, parser_version, normalizer_version, "
        "chunker_version, embedding_version, processing_config_hash "
        "HAVING count(*) > 1 LIMIT 1",
        "DocumentVersion processing",
    )
    _fail_on_duplicate(
        "SELECT version_id, ordinal FROM chunks "
        "GROUP BY version_id, ordinal HAVING count(*) > 1 LIMIT 1",
        "Chunk ordinal",
    )

    op.drop_index("idx_documents_stable_key", table_name="documents")
    op.create_unique_constraint(
        "uq_documents_source_stable_key", "documents", ["source_id", "stable_key"]
    )
    op.create_foreign_key(
        "fk_documents_current_version_id",
        "documents",
        "document_versions",
        ["current_version_id"],
        ["id"],
        ondelete="SET NULL",
        use_alter=True,
    )

    op.create_index("idx_document_versions_blob_hash", "document_versions", ["blob_hash"])
    op.create_unique_constraint(
        "uq_document_versions_processing_identity",
        "document_versions",
        [
            "document_id",
            "content_hash",
            "parser_version",
            "normalizer_version",
            "chunker_version",
            "embedding_version",
            "processing_config_hash",
        ],
    )

    op.create_index("idx_chunks_chunk_hash", "chunks", ["chunk_hash"])
    op.create_unique_constraint("uq_chunks_version_ordinal", "chunks", ["version_id", "ordinal"])

    op.create_foreign_key(
        "fk_ingestion_tasks_target_version_id",
        "ingestion_tasks",
        "document_versions",
        ["target_version_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_ingestion_tasks_source_idempotency",
        "ingestion_tasks",
        ["source_id", "idempotency_key"],
    )
    op.create_check_constraint(
        "ck_ingestion_tasks_progress",
        "ingestion_tasks",
        "progress >= 0.0 AND progress <= 1.0",
    )
    op.create_check_constraint(
        "ck_ingestion_tasks_retry_count", "ingestion_tasks", "retry_count >= 0"
    )
    op.create_check_constraint(
        "ck_ingestion_tasks_max_retries", "ingestion_tasks", "max_retries >= 0"
    )
    op.create_index(
        "idx_ingestion_tasks_status_lease",
        "ingestion_tasks",
        ["status", "lease_expires_at"],
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE ingestion_tasks SET stage = CASE "
            "  WHEN status = 'succeeded' THEN 'complete' "
            "  WHEN status IN ('partial_failed', 'failed', 'dead_letter') THEN 'failed' "
            "  ELSE stage "
            "END"
        )
    )
    op.drop_index("idx_ingestion_tasks_status_lease", table_name="ingestion_tasks")
    op.drop_constraint("ck_ingestion_tasks_max_retries", "ingestion_tasks", type_="check")
    op.drop_constraint("ck_ingestion_tasks_retry_count", "ingestion_tasks", type_="check")
    op.drop_constraint("ck_ingestion_tasks_progress", "ingestion_tasks", type_="check")
    op.drop_constraint("uq_ingestion_tasks_source_idempotency", "ingestion_tasks", type_="unique")
    op.drop_constraint(
        "fk_ingestion_tasks_target_version_id", "ingestion_tasks", type_="foreignkey"
    )
    for column in (
        "error_code",
        "lease_expires_at",
        "heartbeat_at",
        "enqueued_at",
        "cancel_requested_at",
        "max_retries",
        "idempotency_key",
        "target_version_id",
        "status",
        "operation",
    ):
        op.drop_column("ingestion_tasks", column)

    op.drop_constraint("uq_chunks_version_ordinal", "chunks", type_="unique")
    op.drop_index("idx_chunks_chunk_hash", table_name="chunks")
    op.drop_column("chunks", "chunk_hash")

    op.drop_constraint(
        "uq_document_versions_processing_identity", "document_versions", type_="unique"
    )
    op.drop_index("idx_document_versions_blob_hash", table_name="document_versions")
    for column in (
        "processing_config",
        "processing_config_hash",
        "embedding_version",
        "chunker_version",
        "normalizer_version",
        "blob_hash",
    ):
        op.drop_column("document_versions", column)

    op.drop_constraint("fk_documents_current_version_id", "documents", type_="foreignkey")
    op.drop_constraint("uq_documents_source_stable_key", "documents", type_="unique")
    op.create_index("idx_documents_stable_key", "documents", ["stable_key"])
    op.drop_column("documents", "deleted_at")
