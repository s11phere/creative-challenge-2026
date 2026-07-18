"""SQLAlchemy ORM models for core knowledge entities.

Uses SQLAlchemy 2.0 ``Mapped`` style with ``DeclarativeBase``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# ---------------------------------------------------------------------------
# Null UUID sentinel for unsafe ``default=`` in legacy patterns.
# Not used in this module — kept for reference.
_NULL_UUID = uuid.UUID(int=0)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


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
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    source: Mapped[SourceModel] = relationship("SourceModel", back_populates="documents")
    versions: Mapped[list[DocumentVersionModel]] = relationship(
        "DocumentVersionModel",
        back_populates="document",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("idx_documents_source_id", "source_id"),
        Index("idx_documents_stable_key", "stable_key"),
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
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    parser_version: Mapped[str] = mapped_column(String(50), default="1.0")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    document: Mapped[DocumentModel] = relationship("DocumentModel", back_populates="versions")
    chunks: Mapped[list[ChunkModel]] = relationship(
        "ChunkModel", back_populates="version", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_document_versions_document_id", "document_id"),
        Index("idx_document_versions_content_hash", "content_hash"),
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
    text: Mapped[str] = mapped_column(Text, default="")
    meta: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(768), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    version: Mapped[DocumentVersionModel] = relationship(
        "DocumentVersionModel", back_populates="chunks"
    )

    __table_args__ = (
        Index("idx_chunks_version_id", "version_id"),
        Index(
            "idx_chunks_embedding",
            embedding,
            postgresql_using="ivfflat",
            postgresql_with={"lists": 100},
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
    stage: Mapped[str] = mapped_column(String(20), default="discover")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (Index("idx_ingestion_tasks_source_id", "source_id"),)
