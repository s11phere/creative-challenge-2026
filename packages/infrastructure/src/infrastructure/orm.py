"""SQLAlchemy ORM models for core knowledge entities.

Uses SQLAlchemy 2.0 ``Mapped`` style with ``DeclarativeBase``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# ---------------------------------------------------------------------------
# Null UUID sentinel for unsafe ``default=`` in legacy patterns.
# Not used in this module — kept for reference.
_NULL_UUID = uuid.UUID(int=0)
EMBEDDING_DIMENSIONS = 768


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _default_reasoning_profile() -> dict[str, str]:
    return {
        "schema_version": "reasoning-profile-v1",
        "requested_effort": "auto",
        "effective_effort": "none",
        "provider": "unresolved",
        "model": "unresolved",
        "mapping_version": "reasoning-mapping-v1",
        "mode": "disabled",
        "downgrade_reason": "capability_unavailable",
    }


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


# ---------------------------------------------------------------------------
# Skill lifecycle (ADR-006)
# ---------------------------------------------------------------------------


class SkillActivationModel(Base):
    __tablename__ = "skill_activations"

    skill_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    active_version: Mapped[str] = mapped_column(String(100), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_skill_activations_revision_positive"),
        CheckConstraint(
            "char_length(content_sha256) = 64",
            name="ck_skill_activations_content_sha256_length",
        ),
    )


# ---------------------------------------------------------------------------
# Space
# ---------------------------------------------------------------------------


class SpaceModel(Base):
    __tablename__ = "spaces"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), default="")
    owner_id: Mapped[str] = mapped_column(String(255), default="")
    retrieval_profile: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    sources: Mapped[list[SourceModel]] = relationship(
        "SourceModel", back_populates="space", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------


class SourceModel(Base):
    __tablename__ = "sources"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False
    )
    source_type: Mapped[str] = mapped_column(String(50), default="upload")
    uri: Mapped[str] = mapped_column(String(1024), default="")
    sync_cursor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    space: Mapped[SpaceModel] = relationship("SpaceModel", back_populates="sources")
    documents: Mapped[list[DocumentModel]] = relationship(
        "DocumentModel", back_populates="source", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("idx_sources_space_id", "space_id"),)


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------


class DocumentModel(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    stable_key: Mapped[str] = mapped_column(String(255), default="")
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "document_versions.id",
            name="fk_documents_current_version_id",
            ondelete="SET NULL",
            use_alter=True,
        ),
        nullable=True,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    source: Mapped[SourceModel] = relationship("SourceModel", back_populates="documents")
    versions: Mapped[list[DocumentVersionModel]] = relationship(
        "DocumentVersionModel",
        back_populates="document",
        cascade="all, delete-orphan",
        foreign_keys="DocumentVersionModel.document_id",
    )

    __table_args__ = (
        Index("idx_documents_source_id", "source_id"),
        UniqueConstraint("source_id", "stable_key", name="uq_documents_source_stable_key"),
    )


# ---------------------------------------------------------------------------
# DocumentVersion
# ---------------------------------------------------------------------------


class DocumentVersionModel(Base):
    __tablename__ = "document_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    blob_hash: Mapped[str] = mapped_column(String(64), default="")
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    parser_version: Mapped[str] = mapped_column(String(50), default="1.0")
    normalizer_version: Mapped[str] = mapped_column(String(50), default="1.0")
    chunker_version: Mapped[str] = mapped_column(String(50), default="1.0")
    embedding_version: Mapped[str] = mapped_column(String(100), default="1.0")
    processing_config_hash: Mapped[str] = mapped_column(String(64), default="")
    processing_config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    document: Mapped[DocumentModel] = relationship(
        "DocumentModel", back_populates="versions", foreign_keys=[document_id]
    )
    chunks: Mapped[list[ChunkModel]] = relationship(
        "ChunkModel", back_populates="version", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_document_versions_document_id", "document_id"),
        Index("idx_document_versions_content_hash", "content_hash"),
        Index("idx_document_versions_blob_hash", "blob_hash"),
        UniqueConstraint(
            "document_id",
            "content_hash",
            "parser_version",
            "normalizer_version",
            "chunker_version",
            "embedding_version",
            "processing_config_hash",
            name="uq_document_versions_processing_identity",
        ),
    )


# ---------------------------------------------------------------------------
# Chunk (with pgvector embedding)
# ---------------------------------------------------------------------------


class ChunkModel(Base):
    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    chunk_hash: Mapped[str] = mapped_column(String(64), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    meta: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict)
    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('simple'::regconfig, coalesce(text, ''::text))", persisted=True),
        nullable=False,
    )
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIMENSIONS), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    version: Mapped[DocumentVersionModel] = relationship(
        "DocumentVersionModel", back_populates="chunks"
    )

    __table_args__ = (
        Index("idx_chunks_version_id", "version_id"),
        Index("idx_chunks_chunk_hash", "chunk_hash"),
        Index("idx_chunks_search_vector", search_vector, postgresql_using="gin"),
        UniqueConstraint("version_id", "ordinal", name="uq_chunks_version_ordinal"),
        Index(
            "idx_chunks_embedding",
            embedding,
            postgresql_using="ivfflat",
            postgresql_with={"lists": 100},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


# ---------------------------------------------------------------------------
# IngestionTask
# ---------------------------------------------------------------------------


class IngestionTaskModel(Base):
    __tablename__ = "ingestion_tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    operation: Mapped[str] = mapped_column(String(20), default="ingest")
    status: Mapped[str] = mapped_column(String(20), default="queued")
    stage: Mapped[str] = mapped_column(String(20), default="discover")
    target_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), default=lambda: uuid.uuid4().hex)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        Index("idx_ingestion_tasks_source_id", "source_id"),
        Index("idx_ingestion_tasks_status_lease", "status", "lease_expires_at"),
        UniqueConstraint(
            "source_id", "idempotency_key", name="uq_ingestion_tasks_source_idempotency"
        ),
        CheckConstraint("progress >= 0.0 AND progress <= 1.0", name="ck_ingestion_tasks_progress"),
        CheckConstraint("retry_count >= 0", name="ck_ingestion_tasks_retry_count"),
        CheckConstraint("max_retries >= 0", name="ck_ingestion_tasks_max_retries"),
    )


# ---------------------------------------------------------------------------
# Grounded QA (ADR-007)
# ---------------------------------------------------------------------------


class ConversationModel(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False
    )
    owner_id: Mapped[str] = mapped_column(String(255), nullable=False)
    reasoning_effort: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    workspace_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "reasoning_effort IN "
            "('auto', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max')",
            name="ck_conversations_reasoning_effort",
        ),
        Index("idx_conversations_space_owner", "space_id", "owner_id"),
    )


_QA_STATUS_CHECK = (
    "status IN ('created', 'queued', 'running', 'verifying', 'completed', 'refused', "
    "'failed', 'cancel_requested', 'cancelled', 'timed_out')"
)

_CONVERSATION_RUN_STATUS_CHECK = (
    "status IN ('created', 'queued', 'running', 'waiting_clarification', 'waiting_approval', "
    "'completed', 'refused', 'failed', 'cancel_requested', 'cancelled', 'timed_out')"
)
_CONVERSATION_RUN_KIND_CHECK = (
    "run_kind IN ('assistant_turn', 'grounded_qa', 'skill', 'context_compaction')"
)
_CONVERSATION_RUN_SELECTION_CHECK = "selection_source IN ('auto', 'command', 'none')"


class ConversationRunModel(Base):
    """Shared durable parent identity for Assistant, QA, and Runtime work."""

    __tablename__ = "conversation_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False
    )
    caller_id: Mapped[str] = mapped_column(String(255), nullable=False)
    user_message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("qa_messages.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    run_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    selection_source: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="created")
    cancellation_requested: Mapped[bool] = mapped_column(default=False)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    router_version: Mapped[str] = mapped_column(String(100), nullable=False)
    core_prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    model_identity: Mapped[str] = mapped_column(String(255), nullable=False)
    reasoning_profile: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=_default_reasoning_profile
    )
    skill_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    skill_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    skill_content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "space_id", "caller_id", "idempotency_key", name="uq_conversation_runs_idempotency"
        ),
        CheckConstraint(_CONVERSATION_RUN_STATUS_CHECK, name="ck_conversation_runs_status"),
        CheckConstraint(_CONVERSATION_RUN_KIND_CHECK, name="ck_conversation_runs_kind"),
        CheckConstraint(_CONVERSATION_RUN_SELECTION_CHECK, name="ck_conversation_runs_selection"),
        CheckConstraint(
            "(skill_name IS NULL AND skill_version IS NULL AND skill_content_sha256 IS NULL) OR "
            "(skill_name IS NOT NULL AND skill_version IS NOT NULL "
            "AND skill_content_sha256 IS NOT NULL)",
            name="ck_conversation_runs_skill_identity",
        ),
        Index("idx_conversation_runs_conversation", "conversation_id", "created_at"),
        Index("idx_conversation_runs_status", "status", "updated_at"),
        Index("idx_conversation_runs_recovery", "run_kind", "status", "lease_expires_at"),
    )


class QARunModel(Base):
    __tablename__ = "qa_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversation_runs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False
    )
    caller_id: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    question_message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("qa_messages.id", ondelete="RESTRICT"), nullable=False
    )
    answer_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("qa_messages.id", ondelete="RESTRICT"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="created")
    cancellation_requested: Mapped[bool] = mapped_column(default=False)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    versions: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    retrieval_scope: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    standalone_request: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_sensitivity: Mapped[str] = mapped_column(
        String(32), nullable=False, default="private_local"
    )
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("space_id", "caller_id", "idempotency_key", name="uq_qa_runs_idempotency"),
        CheckConstraint(_QA_STATUS_CHECK, name="ck_qa_runs_status"),
        Index("idx_qa_runs_conversation", "conversation_id", "created_at"),
        Index("idx_qa_runs_status", "status"),
    )


class QAMessageModel(Base):
    __tablename__ = "qa_messages"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversation_runs.id", ondelete="CASCADE"), nullable=True
    )
    space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    __table_args__ = (
        Index(
            "uq_qa_messages_conversation_idempotency",
            "conversation_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )


class ConversationSummaryModel(Base):
    """Append-only rolling summaries; source messages remain unchanged."""

    __tablename__ = "conversation_summaries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversation_runs.id", ondelete="CASCADE"), nullable=False
    )
    covered_start_message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("qa_messages.id", ondelete="RESTRICT"), nullable=False
    )
    covered_end_message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("qa_messages.id", ondelete="RESTRICT"), nullable=False
    )
    covered_message_count: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    model_identity: Mapped[str] = mapped_column(String(255), nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        UniqueConstraint("run_id", name="uq_conversation_summaries_run"),
        UniqueConstraint(
            "conversation_id",
            "covered_end_message_id",
            "prompt_version",
            name="uq_conversation_summaries_coverage",
        ),
        CheckConstraint("covered_message_count >= 1", name="ck_conversation_summaries_count"),
        CheckConstraint(
            "sensitivity IN ('public_demo', 'private_local', 'restricted')",
            name="ck_conversation_summaries_sensitivity",
        ),
        Index("idx_conversation_summaries_conversation", "conversation_id", "created_at"),
    )


class QARunAttemptModel(Base):
    __tablename__ = "qa_run_attempts"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("qa_runs.id", ondelete="CASCADE"), nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("qa_run_attempts.id", ondelete="RESTRICT"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    cancellation_requested: Mapped[bool] = mapped_column(nullable=False, default=False)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    answer_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("qa_messages.id", ondelete="RESTRICT"), nullable=True
    )
    lease_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    __table_args__ = (
        UniqueConstraint("run_id", "number", name="uq_qa_run_attempts_number"),
        CheckConstraint("number >= 1", name="ck_qa_run_attempts_number_positive"),
        CheckConstraint(_QA_STATUS_CHECK, name="ck_qa_run_attempts_status"),
        Index("idx_qa_run_attempts_run_number", "run_id", "number"),
        Index("idx_qa_run_attempts_recovery", "status", "lease_expires_at"),
    )


class QAEvidenceModel(Base):
    __tablename__ = "qa_evidence"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("qa_runs.id", ondelete="CASCADE"), nullable=False
    )
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("qa_run_attempts.id", ondelete="CASCADE"), nullable=False
    )
    space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    resolution_status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    __table_args__ = (UniqueConstraint("attempt_id", "id", name="uq_qa_evidence_attempt_id"),)


class QACitationModel(Base):
    __tablename__ = "qa_citations"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("qa_runs.id", ondelete="CASCADE"), nullable=False
    )
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("qa_run_attempts.id", ondelete="CASCADE"), nullable=False
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("qa_messages.id", ondelete="CASCADE"), nullable=False
    )
    evidence_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    __table_args__ = (
        UniqueConstraint("attempt_id", "evidence_id", name="uq_qa_citations_attempt_evidence"),
    )


class QAEventModel(Base):
    __tablename__ = "qa_events"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("qa_runs.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    __table_args__ = (UniqueConstraint("run_id", "sequence", name="uq_qa_events_run_sequence"),)


class AssistantEventModel(Base):
    """Privacy-safe product-level Assistant events, distinct from legacy QA SSE."""

    __tablename__ = "assistant_events"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversation_runs.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_assistant_events_run_sequence"),
    )


class AgentRunEventModel(Base):
    """Append-only generic Agent Loop v3 event history."""

    __tablename__ = "agent_run_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversation_runs.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_key: Mapped[str] = mapped_column(String(200), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    __table_args__ = (
        CheckConstraint("sequence >= 1", name="ck_agent_run_events_sequence_positive"),
        UniqueConstraint("run_id", "sequence", name="uq_agent_run_events_run_sequence"),
        UniqueConstraint("run_id", "event_key", name="uq_agent_run_events_run_key"),
        Index("idx_agent_run_events_run_sequence", "run_id", "sequence"),
    )


class QAFeedbackModel(Base):
    __tablename__ = "qa_feedback"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("qa_messages.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("qa_runs.id", ondelete="CASCADE"), nullable=False
    )
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("qa_run_attempts.id", ondelete="CASCADE"), nullable=False
    )
    space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False
    )
    caller_id: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_status: Mapped[str] = mapped_column(String(32), nullable=False)
    reviewer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    authorization_confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    redaction_complete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    expected_behavior: Mapped[str | None] = mapped_column(String(16), nullable=True)
    approved_evidence_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    gold_answer_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    __table_args__ = (
        UniqueConstraint(
            "run_id", "caller_id", "idempotency_key", name="uq_qa_feedback_idempotency"
        ),
        Index("idx_qa_feedback_review_status", "review_status", "created_at"),
    )


class RuntimeRunModel(Base):
    """Durable Runtime snapshot sharing the QA run identity."""

    __tablename__ = "runtime_runs"

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversation_runs.id", ondelete="CASCADE"), primary_key=True
    )
    space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False
    )
    caller_id: Mapped[str] = mapped_column(String(255), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(255), nullable=False)
    skill_name: Mapped[str] = mapped_column(String(255), nullable=False)
    skill_version: Mapped[str] = mapped_column(String(100), nullable=False)
    skill_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    granted_permissions: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    budget: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    current_step: Mapped[str | None] = mapped_column(String(32), nullable=True)
    checkpoint_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    __table_args__ = (
        CheckConstraint("checkpoint_sequence >= 0", name="ck_runtime_runs_checkpoint_sequence"),
        Index("idx_runtime_runs_status", "status", "updated_at"),
    )


class RuntimeCheckpointModel(Base):
    """Append-only verified recovery points for a shared QA/Runtime run."""

    __tablename__ = "runtime_checkpoints"

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runtime_runs.run_id", ondelete="CASCADE"), primary_key=True
    )
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    skill_name: Mapped[str] = mapped_column(String(255), nullable=False)
    skill_version: Mapped[str] = mapped_column(String(100), nullable=False)
    skill_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    state_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    next_step: Mapped[str] = mapped_column(String(32), nullable=False)
    next_node: Mapped[str] = mapped_column(String(255), nullable=False)
    verified: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    __table_args__ = (
        CheckConstraint("sequence >= 1", name="ck_runtime_checkpoints_sequence_positive"),
        CheckConstraint("schema_version >= 1", name="ck_runtime_checkpoints_schema_positive"),
        CheckConstraint(
            "char_length(state_sha256) = 64", name="ck_runtime_checkpoints_state_sha256"
        ),
    )


class RuntimeApprovalModel(Base):
    """Durable approval for a bounded Runtime write operation."""

    __tablename__ = "runtime_approvals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversation_runs.id", ondelete="CASCADE"), nullable=False
    )
    space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False
    )
    caller_id: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(100), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    __table_args__ = (
        UniqueConstraint(
            "run_id", "action", "idempotency_key", name="uq_runtime_approvals_idempotency"
        ),
        CheckConstraint(
            "status in ('pending', 'approved', 'rejected', 'revoked')",
            name="ck_runtime_approval_status",
        ),
        Index("idx_runtime_approvals_run_status", "run_id", "status"),
    )


class DerivedKnowledgeItemModel(Base):
    """Idempotent, citation-backed derived knowledge produced by a Skill."""

    __tablename__ = "derived_knowledge_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversation_runs.id", ondelete="CASCADE"), nullable=False
    )
    space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    citation_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    __table_args__ = (
        UniqueConstraint("run_id", "idempotency_key", name="uq_derived_knowledge_idempotency"),
        CheckConstraint("status in ('active', 'revoked')", name="ck_derived_knowledge_status"),
        Index("idx_derived_knowledge_space_created", "space_id", "created_at"),
    )
