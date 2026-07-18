"""Tests for domain entities and value objects."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from domain.models import (
    Chunk,
    Document,
    DocumentStatus,
    DocumentVersion,
    IngestionTask,
    RetrievalProfile,
    Source,
    SourceType,
    Space,
    TaskOperation,
    TaskStage,
    TaskStatus,
)


class TestRetrievalProfile:
    def test_default_values(self) -> None:
        profile = RetrievalProfile()
        assert profile.chunk_size == 512
        assert profile.chunk_overlap == 64
        assert profile.top_k == 10
        assert profile.rerank_k == 5
        assert profile.fusion_alpha == 0.5
        assert profile.extra == {}

    def test_custom_values(self) -> None:
        profile = RetrievalProfile(
            chunk_size=256,
            top_k=20,
            extra={"model": "test"},
        )
        assert profile.chunk_size == 256
        assert profile.top_k == 20
        assert profile.extra == {"model": "test"}

    def test_is_frozen(self) -> None:
        profile = RetrievalProfile()
        with pytest.raises(AttributeError):
            profile.chunk_size = 999  # type: ignore[misc]


class TestSpace:
    def test_default_creation(self) -> None:
        space = Space()
        assert isinstance(space.id, UUID)
        assert space.name == ""
        assert space.owner_id == ""
        assert isinstance(space.retrieval_profile, RetrievalProfile)
        assert isinstance(space.created_at, datetime)
        assert isinstance(space.updated_at, datetime)

    def test_custom_creation(self) -> None:
        space = Space(
            name="My Space",
            owner_id="user-1",
            retrieval_profile=RetrievalProfile(top_k=25),
        )
        assert space.name == "My Space"
        assert space.owner_id == "user-1"
        assert space.retrieval_profile.top_k == 25

    def test_timestamps_are_utc(self) -> None:
        space = Space()
        assert space.created_at.tzinfo is not None
        assert space.created_at.tzinfo.utcoffset(space.created_at) is not None


class TestSource:
    def test_default_creation(self) -> None:
        source = Source()
        assert isinstance(source.id, UUID)
        assert isinstance(source.space_id, UUID)
        assert source.source_type == SourceType.UPLOAD
        assert source.uri == ""
        assert source.sync_cursor is None

    def test_folder_source(self) -> None:
        source = Source(
            source_type=SourceType.FOLDER,
            uri="/data/docs",
            sync_cursor="2026-01-01T00:00:00Z",
        )
        assert source.source_type == SourceType.FOLDER
        assert source.uri == "/data/docs"
        assert source.sync_cursor == "2026-01-01T00:00:00Z"


class TestDocument:
    def test_default_creation(self) -> None:
        doc = Document()
        assert isinstance(doc.id, UUID)
        assert isinstance(doc.source_id, UUID)
        assert doc.stable_key == ""
        assert doc.current_version_id is None
        assert doc.deleted_at is None

    def test_with_version(self) -> None:
        version_id = UUID("00000000-0000-4000-8000-000000000001")
        doc = Document(stable_key="abc123", current_version_id=version_id)
        assert doc.stable_key == "abc123"
        assert doc.current_version_id == version_id


class TestDocumentVersion:
    def test_default_creation(self) -> None:
        version = DocumentVersion()
        assert isinstance(version.id, UUID)
        assert isinstance(version.document_id, UUID)
        assert version.blob_hash == ""
        assert version.content_hash == ""
        assert version.parser_version == "1.0"
        assert version.normalizer_version == "1.0"
        assert version.chunker_version == "1.0"
        assert version.embedding_version == "1.0"
        assert version.processing_config_hash == ""
        assert version.processing_config == {}
        assert version.status == DocumentStatus.PENDING
        assert version.file_path is None

    def test_parsed_status(self) -> None:
        version = DocumentVersion(
            content_hash="e3b0c44298fc1c149afbf4c8996fb924",
            status=DocumentStatus.PARSED,
            file_path="/tmp/test.md",
        )
        assert version.status == DocumentStatus.PARSED
        assert version.file_path == "/tmp/test.md"


class TestChunk:
    def test_default_creation(self) -> None:
        chunk = Chunk()
        assert isinstance(chunk.id, UUID)
        assert isinstance(chunk.version_id, UUID)
        assert chunk.ordinal == 0
        assert chunk.chunk_hash == ""
        assert chunk.text == ""
        assert chunk.meta == {}
        assert chunk.embedding is None

    def test_with_embedding(self) -> None:
        chunk = Chunk(
            ordinal=1,
            chunk_hash="a" * 64,
            text="Hello world",
            meta={"page": "1"},
            embedding=[0.1, 0.2, 0.3],
        )
        assert chunk.ordinal == 1
        assert chunk.chunk_hash == "a" * 64
        assert chunk.text == "Hello world"
        assert chunk.meta == {"page": "1"}
        assert chunk.embedding == [0.1, 0.2, 0.3]

    def test_meta_default_factory(self) -> None:
        """meta default factory creates a fresh dict per instance."""
        c1 = Chunk(meta={"a": "1"})
        c2 = Chunk(meta={"b": "2"})
        assert c1.meta == {"a": "1"}
        assert c2.meta == {"b": "2"}
        # Verify the dataclass is frozen (attribute reassignment fails)
        with pytest.raises(AttributeError):
            c1.meta = {"c": "3"}  # type: ignore[misc]


class TestIngestionTask:
    def test_default_creation(self) -> None:
        task = IngestionTask()
        assert isinstance(task.id, UUID)
        assert isinstance(task.source_id, UUID)
        assert task.operation == TaskOperation.INGEST
        assert task.status == TaskStatus.QUEUED
        assert task.stage == TaskStage.DISCOVER
        assert task.target_version_id is None
        assert task.idempotency_key
        assert task.progress == 0.0
        assert task.retry_count == 0
        assert task.max_retries == 3
        assert task.cancel_requested_at is None
        assert task.enqueued_at is None
        assert task.heartbeat_at is None
        assert task.lease_expires_at is None
        assert task.error_code is None
        assert task.error is None

    def test_progress_update_requires_new_instance(self) -> None:
        task = IngestionTask(
            status=TaskStatus.RUNNING,
            stage=TaskStage.PARSE,
            progress=0.5,
        )
        assert task.stage == TaskStage.PARSE
        assert task.progress == 0.5
        # Moving to next stage requires a new dataclass
        task2 = IngestionTask(
            id=task.id,
            source_id=task.source_id,
            operation=task.operation,
            status=task.status,
            stage=TaskStage.CHUNK,
            idempotency_key=task.idempotency_key,
            progress=0.8,
            retry_count=task.retry_count,
        )
        assert task2.stage == TaskStage.CHUNK

    def test_failed_task(self) -> None:
        task = IngestionTask(
            status=TaskStatus.FAILED,
            stage=TaskStage.PARSE,
            error_code="parser_unsupported",
            error="ParserError: unsupported format",
        )
        assert task.status == TaskStatus.FAILED
        assert task.stage == TaskStage.PARSE
        assert task.error_code == "parser_unsupported"
        assert task.error == "ParserError: unsupported format"

    def test_recovery_fields(self) -> None:
        now = datetime.now(UTC)
        version_id = UUID("00000000-0000-4000-8000-000000000002")
        task = IngestionTask(
            operation=TaskOperation.REBUILD,
            status=TaskStatus.RUNNING,
            target_version_id=version_id,
            idempotency_key="rebuild:v2",
            enqueued_at=now,
            heartbeat_at=now,
            lease_expires_at=now,
        )
        assert task.operation == TaskOperation.REBUILD
        assert task.target_version_id == version_id
        assert task.lease_expires_at == now


class TestEnums:
    def test_source_type_values(self) -> None:
        assert SourceType.UPLOAD.value == "upload"
        assert SourceType.FOLDER.value == "folder"

    def test_document_status_values(self) -> None:
        assert DocumentStatus.PENDING.value == "pending"
        assert DocumentStatus.PARSING.value == "parsing"
        assert DocumentStatus.PARSED.value == "parsed"
        assert DocumentStatus.FAILED.value == "failed"

    def test_task_stage_order(self) -> None:
        stages = list(TaskStage)
        assert stages == [
            TaskStage.DISCOVER,
            TaskStage.FINGERPRINT,
            TaskStage.PARSE,
            TaskStage.NORMALIZE,
            TaskStage.ENRICH,
            TaskStage.CHUNK,
            TaskStage.EMBED,
            TaskStage.INDEX,
            TaskStage.VALIDATE,
            TaskStage.PUBLISH,
            TaskStage.CLEANUP,
        ]

    def test_task_operation_and_status_values(self) -> None:
        assert TaskOperation.INGEST.value == "ingest"
        assert TaskOperation.REBUILD.value == "rebuild"
        assert TaskOperation.DELETE.value == "delete"
        assert TaskStatus.QUEUED.value == "queued"
        assert TaskStatus.DEAD_LETTER.value == "dead_letter"
