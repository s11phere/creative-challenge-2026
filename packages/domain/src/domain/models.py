"""Core domain entities for knowledge ingestion.

Every entity is an immutable dataclass with no external dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4


class SourceType(StrEnum):
    UPLOAD = "upload"
    FOLDER = "folder"


class DocumentStatus(StrEnum):
    PENDING = "pending"
    PARSING = "parsing"
    PARSED = "parsed"
    EMBEDDED = "embedded"
    PUBLISHED = "published"
    FAILED = "failed"


class TaskOperation(StrEnum):
    INGEST = "ingest"
    REBUILD = "rebuild"
    DELETE = "delete"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL_FAILED = "partial_failed"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    DEAD_LETTER = "dead_letter"


class TaskStage(StrEnum):
    DISCOVER = "discover"
    FINGERPRINT = "fingerprint"
    PARSE = "parse"
    NORMALIZE = "normalize"
    ENRICH = "enrich"
    CHUNK = "chunk"
    EMBED = "embed"
    INDEX = "index"
    VALIDATE = "validate"
    PUBLISH = "publish"
    CLEANUP = "cleanup"


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetrievalProfile:
    """Search and ranking configuration attached to a Space."""

    chunk_size: int = 512
    chunk_overlap: int = 64
    top_k: int = 10
    rerank_k: int = 5
    fusion_alpha: float = 0.5
    extra: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Space:
    """Knowledge isolation boundary with its own retrieval configuration."""

    id: UUID = field(default_factory=uuid4)
    name: str = ""
    owner_id: str = ""
    retrieval_profile: RetrievalProfile = field(default_factory=RetrievalProfile)
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True)
class Source:
    """A data source — upload session, watched folder, etc."""

    id: UUID = field(default_factory=uuid4)
    space_id: UUID = field(default_factory=uuid4)
    source_type: SourceType = SourceType.UPLOAD
    uri: str = ""
    sync_cursor: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True)
class Document:
    """Logical document that retains its identity across content changes."""

    id: UUID = field(default_factory=uuid4)
    source_id: UUID = field(default_factory=uuid4)
    stable_key: str = ""
    current_version_id: UUID | None = None
    deleted_at: datetime | None = None
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True)
class DocumentVersion:
    """An immutable snapshot of a document at a point in time."""

    id: UUID = field(default_factory=uuid4)
    document_id: UUID = field(default_factory=uuid4)
    blob_hash: str = ""
    content_hash: str = ""
    parser_version: str = "1.0"
    normalizer_version: str = "1.0"
    chunker_version: str = "1.0"
    embedding_version: str = "1.0"
    processing_config_hash: str = ""
    processing_config: dict[str, str] = field(default_factory=dict)
    status: DocumentStatus = DocumentStatus.PENDING
    file_path: str | None = None
    created_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True)
class Chunk:
    """A single text chunk with optional embedding vector."""

    id: UUID = field(default_factory=uuid4)
    version_id: UUID = field(default_factory=uuid4)
    ordinal: int = 0
    chunk_hash: str = ""
    text: str = ""
    meta: dict[str, str] = field(default_factory=dict)
    embedding: list[float] | None = None
    created_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True)
class IngestionTask:
    """Tracks the progress of an ingestion pipeline run."""

    id: UUID = field(default_factory=uuid4)
    source_id: UUID = field(default_factory=uuid4)
    operation: TaskOperation = TaskOperation.INGEST
    status: TaskStatus = TaskStatus.QUEUED
    stage: TaskStage = TaskStage.DISCOVER
    target_version_id: UUID | None = None
    idempotency_key: str = field(default_factory=lambda: uuid4().hex)
    progress: float = 0.0
    retry_count: int = 0
    max_retries: int = 3
    cancel_requested_at: datetime | None = None
    enqueued_at: datetime | None = None
    heartbeat_at: datetime | None = None
    lease_expires_at: datetime | None = None
    error_code: str | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
