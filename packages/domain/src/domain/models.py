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
    FAILED = "failed"


class TaskStage(StrEnum):
    DISCOVER = "discover"
    PARSE = "parse"
    CHUNK = "chunk"
    EMBED = "embed"
    INDEX = "index"
    COMPLETE = "complete"
    FAILED = "failed"


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
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True)
class DocumentVersion:
    """An immutable snapshot of a document at a point in time."""

    id: UUID = field(default_factory=uuid4)
    document_id: UUID = field(default_factory=uuid4)
    content_hash: str = ""
    parser_version: str = "1.0"
    status: DocumentStatus = DocumentStatus.PENDING
    file_path: str | None = None
    created_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True)
class Chunk:
    """A single text chunk with optional embedding vector."""

    id: UUID = field(default_factory=uuid4)
    version_id: UUID = field(default_factory=uuid4)
    ordinal: int = 0
    text: str = ""
    meta: dict[str, str] = field(default_factory=dict)
    embedding: list[float] | None = None
    created_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True)
class IngestionTask:
    """Tracks the progress of an ingestion pipeline run."""

    id: UUID = field(default_factory=uuid4)
    source_id: UUID = field(default_factory=uuid4)
    stage: TaskStage = TaskStage.DISCOVER
    progress: float = 0.0
    retry_count: int = 0
    error: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
