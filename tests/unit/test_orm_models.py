"""Tests for ORM model construction and domain mapper functions."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

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
    TaskStage,
)
from infrastructure.orm import (
    ChunkModel,
    DocumentModel,
    DocumentVersionModel,
    IngestionTaskModel,
    SourceModel,
    SpaceModel,
)
from infrastructure.repositories import (
    _chunk_from_domain,
    _chunk_to_domain,
    _document_from_domain,
    _document_to_domain,
    _source_from_domain,
    _source_to_domain,
    _space_from_domain,
    _space_to_domain,
    _task_from_domain,
    _task_to_domain,
    _version_from_domain,
    _version_to_domain,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class TestSpaceMapper:
    def test_domain_to_orm_and_back(self) -> None:
        space = Space(
            name="Test Space",
            owner_id="user-1",
            retrieval_profile=RetrievalProfile(chunk_size=256),
        )
        model = _space_from_domain(space)
        assert model.name == "Test Space"
        assert model.owner_id == "user-1"
        assert model.retrieval_profile["chunk_size"] == 256

        restored = _space_to_domain(model)
        assert restored.id == space.id
        assert restored.name == space.name
        assert restored.retrieval_profile.chunk_size == 256

    def test_default_space_roundtrip(self) -> None:
        space = Space()
        model = _space_from_domain(space)
        restored = _space_to_domain(model)
        assert restored.id == space.id
        assert restored.name == ""
        assert restored.retrieval_profile.chunk_size == 512


class TestSourceMapper:
    def test_upload_source_roundtrip(self) -> None:
        source = Source(
            source_type=SourceType.UPLOAD,
            uri="/tmp/test.txt",
        )
        model = _source_from_domain(source)
        assert model.source_type == "upload"

        restored = _source_to_domain(model)
        assert restored.source_type == SourceType.UPLOAD
        assert restored.uri == "/tmp/test.txt"

    def test_folder_source_roundtrip(self) -> None:
        source = Source(
            source_type=SourceType.FOLDER,
            uri="/data/docs",
            sync_cursor="cursor-xyz",
        )
        model = _source_from_domain(source)
        assert model.sync_cursor == "cursor-xyz"

        restored = _source_to_domain(model)
        assert restored.sync_cursor == "cursor-xyz"


class TestDocumentMapper:
    def test_roundtrip(self) -> None:
        version_id = UUID("00000000-0000-4000-8000-000000000001")
        doc = Document(stable_key="abc123", current_version_id=version_id)
        model = _document_from_domain(doc)
        assert model.stable_key == "abc123"

        restored = _document_to_domain(model)
        assert restored.stable_key == "abc123"
        assert restored.current_version_id == version_id


class TestVersionMapper:
    def test_roundtrip(self) -> None:
        version = DocumentVersion(
            content_hash="e3b0c44298fc1c149afbf4c8996fb924",
            status=DocumentStatus.PARSED,
            file_path="/tmp/test.md",
        )
        model = _version_from_domain(version)
        assert model.status == "parsed"
        assert model.file_path == "/tmp/test.md"

        restored = _version_to_domain(model)
        assert restored.status == DocumentStatus.PARSED
        assert restored.content_hash == version.content_hash
        assert restored.file_path == "/tmp/test.md"


class TestChunkMapper:
    def test_roundtrip(self) -> None:
        chunk = Chunk(
            ordinal=1,
            text="Hello world",
            meta={"page": "1"},
            embedding=[0.1, 0.2, 0.3],
        )
        model = _chunk_from_domain(chunk)
        assert model.ordinal == 1
        assert model.text == "Hello world"

        restored = _chunk_to_domain(model)
        assert restored.ordinal == 1
        assert restored.text == "Hello world"
        assert restored.embedding == [0.1, 0.2, 0.3]

    def test_without_embedding(self) -> None:
        chunk = Chunk(ordinal=0, text="no vector")
        model = _chunk_from_domain(chunk)
        assert model.embedding is None

        restored = _chunk_to_domain(model)
        assert restored.embedding is None

    def test_empty_meta_default(self) -> None:
        chunk = Chunk(ordinal=0, text="test")
        model = _chunk_from_domain(chunk)
        assert model.meta == {}
        assert isinstance(model.meta, dict)

        restored = _chunk_to_domain(model)
        assert restored.meta == {}


class TestTaskMapper:
    def test_roundtrip(self) -> None:
        task = IngestionTask(
            stage=TaskStage.PARSE,
            progress=0.5,
            retry_count=1,
            error="timeout",
        )
        model = _task_from_domain(task)
        assert model.stage == "parse"
        assert model.progress == 0.5

        restored = _task_to_domain(model)
        assert restored.stage == TaskStage.PARSE
        assert restored.error == "timeout"

    def test_default_roundtrip(self) -> None:
        task = IngestionTask()
        model = _task_from_domain(task)
        restored = _task_to_domain(model)
        assert restored.stage == TaskStage.DISCOVER
        assert restored.progress == 0.0
        assert restored.error is None


class TestOrmModelConstruction:
    """Verify ORM models can be constructed with required fields."""

    def test_space_model(self) -> None:
        model = SpaceModel(name="Test", owner_id="u1", retrieval_profile={})
        assert model.name == "Test"
        assert model.owner_id == "u1"
        assert model.retrieval_profile == {}

    def test_source_model(self) -> None:
        space_id = UUID("00000000-0000-4000-8000-000000000001")
        model = SourceModel(space_id=space_id, source_type="upload")
        assert model.space_id == space_id
        assert model.source_type == "upload"

    def test_document_model(self) -> None:
        source_id = UUID("00000000-0000-4000-8000-000000000002")
        model = DocumentModel(source_id=source_id, stable_key="key-1")
        assert model.source_id == source_id
        assert model.stable_key == "key-1"

    def test_document_version_model(self) -> None:
        doc_id = UUID("00000000-0000-4000-8000-000000000003")
        model = DocumentVersionModel(document_id=doc_id, status="parsed")
        assert model.document_id == doc_id
        assert model.status == "parsed"

    def test_chunk_model(self) -> None:
        version_id = UUID("00000000-0000-4000-8000-000000000004")
        model = ChunkModel(version_id=version_id, ordinal=0, text="test", meta={})
        assert model.version_id == version_id
        assert model.ordinal == 0
        assert model.text == "test"
        assert model.meta == {}
        assert model.embedding is None

    def test_ingestion_task_model(self) -> None:
        source_id = UUID("00000000-0000-4000-8000-000000000005")
        model = IngestionTaskModel(source_id=source_id, stage="discover", progress=0.0)
        assert model.source_id == source_id
        assert model.stage == "discover"
        assert model.progress == 0.0

    def test_chunk_embedding_vector_type(self) -> None:
        """The embedding column should accept a list of floats."""
        version_id = UUID("00000000-0000-4000-8000-000000000006")
        embedding = [0.1, 0.2, 0.3]
        model = ChunkModel(version_id=version_id, embedding=embedding)
        assert model.embedding == embedding
