"""Tests for the IngestionOrchestrator (application/ingestion/orchestrator.py)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from application.ingestion.orchestrator import (
    CancelledError,
    IngestionOrchestrator,
)
from domain.blob_store import BlobStore
from domain.chunking import (
    Chunker,
    ChunkerConfig,
    ChunkingResult,
    ChunkOutput,
)
from domain.fingerprinting import compute_content_hash, compute_storage_key
from domain.models import (
    Chunk,
    Document,
    DocumentVersion,
    IngestionTask,
    Source,
    SourceType,
    Space,
    TaskOperation,
    TaskStage,
    TaskStatus,
)
from domain.parsing import ParsedDocument, ParseError, ParseMetadata, Parser, ParseSuccess
from domain.repositories import (
    ChunkRepository,
    DocumentVersionRepository,
    IngestionTaskRepository,
)

from .test_source_registration import _FakeDocumentRepo, _FakeSourceRepo

# ---------------------------------------------------------------------------
# In-memory fake repositories
# ---------------------------------------------------------------------------


@dataclass
class _FakeChunkRepo(ChunkRepository):
    _chunks: dict[UUID, Chunk] = field(default_factory=dict)

    async def create_batch(self, chunks: list[Chunk]) -> list[Chunk]:
        for c in chunks:
            self._chunks[c.id] = c
        return chunks

    async def get_by_version(self, version_id: UUID) -> list[Chunk]:
        return [c for c in self._chunks.values() if c.version_id == version_id]

    async def delete_by_version(self, version_id: UUID) -> int:
        keys = [cid for cid, c in self._chunks.items() if c.version_id == version_id]
        for k in keys:
            del self._chunks[k]
        return len(keys)


@dataclass
class _FakeVersionRepo(DocumentVersionRepository):
    """In-memory fake with create/get/get_by_document/get_latest/update."""

    _versions: dict[UUID, DocumentVersion] = field(default_factory=dict)
    _by_document: dict[UUID, list[UUID]] = field(default_factory=dict)

    async def create(self, version: DocumentVersion) -> DocumentVersion:
        self._versions[version.id] = version
        self._by_document.setdefault(version.document_id, []).append(version.id)
        return version

    async def get(self, version_id: UUID) -> DocumentVersion | None:
        return self._versions.get(version_id)

    async def get_by_document(self, document_id: UUID) -> list[DocumentVersion]:
        vids = self._by_document.get(document_id, [])
        return [self._versions[vid] for vid in vids]

    async def get_latest(self, document_id: UUID) -> DocumentVersion | None:
        vids = self._by_document.get(document_id, [])
        if not vids:
            return None
        return self._versions[vids[-1]]

    async def update(self, version: DocumentVersion) -> DocumentVersion:
        self._versions[version.id] = version
        return version


@dataclass
class _FakeTaskRepo(IngestionTaskRepository):
    _tasks: dict[UUID, IngestionTask] = field(default_factory=dict)

    async def create(self, task: IngestionTask) -> IngestionTask:
        self._tasks[task.id] = task
        return task

    async def get(self, task_id: UUID) -> IngestionTask | None:
        return self._tasks.get(task_id)

    async def get_by_source(self, source_id: UUID) -> list[IngestionTask]:
        return [t for t in self._tasks.values() if t.source_id == source_id]

    async def update(self, task: IngestionTask) -> IngestionTask:
        self._tasks[task.id] = task
        return task


# ---------------------------------------------------------------------------
# Fake parser
# ---------------------------------------------------------------------------


class _FakeParser(Parser):
    """A parser that returns a configurable result."""

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.call_count = 0

    async def parse(self, raw: bytes, metadata: ParseMetadata) -> ParseSuccess | ParseError:
        del raw
        self.call_count += 1
        if self._fail:
            return ParseError(message="Simulated parse failure", code="unsupported_format")
        return ParseSuccess(
            document=ParsedDocument(
                text="Line A content here. " * 20 + "\nLine B more text. " * 20,
                metadata=metadata,
                total_lines=2,
            )
        )


# ---------------------------------------------------------------------------
# Fake chunker
# ---------------------------------------------------------------------------


class _FakeChunker(Chunker):
    """A chunker that returns configurable chunks."""

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.call_count = 0

    async def chunk(
        self, document: ParsedDocument, *, config: ChunkerConfig | None = None
    ) -> ChunkingResult:
        del config
        self.call_count += 1
        if self._fail:
            raise ValueError("Simulated chunker failure")
        chunks = (
            ChunkOutput(ordinal=0, text=document.text[:50], chunk_hash="h1"),
            ChunkOutput(ordinal=1, text=document.text[50:], chunk_hash="h2"),
        )
        return ChunkingResult(chunks=chunks, config_hash="cfg_v1")


# ---------------------------------------------------------------------------
# Fake text embedder
# ---------------------------------------------------------------------------


class _FakeTextEmbedder:
    """Returns deterministic 768-dim vectors (matching ADR-005 schema)."""

    DIMENSIONS = 768

    def __init__(self) -> None:
        self.call_count = 0

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        self.call_count += 1
        return tuple(tuple(0.5 for _ in range(self.DIMENSIONS)) for _ in texts)


# ---------------------------------------------------------------------------
# Fake blob store
# ---------------------------------------------------------------------------


class _FakeBlobStore(BlobStore):
    """In-memory blob store."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    async def store(self, key: str, data: bytes) -> None:
        self._store[key] = data

    async def retrieve(self, key: str) -> bytes | None:
        return self._store.get(key)

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    async def exists(self, key: str) -> bool:
        return key in self._store

    async def store_and_verify(self, key: str, data: bytes, expected_hash: str) -> None:
        import hashlib

        actual = hashlib.sha256(data).hexdigest()
        if actual != expected_hash:
            raise ValueError("Hash mismatch")
        await self.store(key, data)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_orchestrator(
    *,
    parser: Parser | None = None,
    chunker: Chunker | None = None,
    embedder: _FakeTextEmbedder | None = None,
    blob_store: BlobStore | None = None,
) -> tuple[IngestionOrchestrator, dict]:
    """Create an orchestrator with in-memory fakes."""
    source_repo = _FakeSourceRepo()
    doc_repo = _FakeDocumentRepo()
    version_repo = _FakeVersionRepo()
    chunk_repo = _FakeChunkRepo()
    task_repo = _FakeTaskRepo()
    p = parser or _FakeParser()
    c = chunker or _FakeChunker()
    e = embedder or _FakeTextEmbedder()
    bs = blob_store or _FakeBlobStore()

    orch = IngestionOrchestrator(
        source_repo=source_repo,
        document_repo=doc_repo,
        version_repo=version_repo,
        chunk_repo=chunk_repo,
        task_repo=task_repo,
        parser=p,
        chunker=c,
        text_embedder=e,
        blob_store=bs,
    )

    fakes = {
        "source_repo": source_repo,
        "doc_repo": doc_repo,
        "version_repo": version_repo,
        "chunk_repo": chunk_repo,
        "task_repo": task_repo,
        "parser": p,
        "chunker": c,
        "embedder": e,
        "blob_store": bs,
    }
    return orch, fakes


def _make_task(
    source_id: UUID,
    *,
    stage: TaskStage = TaskStage.DISCOVER,
    status: TaskStatus = TaskStatus.QUEUED,
    target_version_id: UUID | None = None,
) -> IngestionTask:
    return IngestionTask(
        id=uuid4(),
        source_id=source_id,
        operation=TaskOperation.INGEST,
        status=status,
        stage=stage,
        target_version_id=target_version_id,
    )


def _seed_source_doc_version(
    fakes: dict,
    *,
    stable_key: str = "test/file.md",
    blob_data: bytes = b"test content",
) -> tuple[Source, Document, DocumentVersion]:
    """Create a source, document, and version with a blob in the store."""
    space = Space(name="test-space", owner_id=uuid4())
    source = Source(space_id=space.id, source_type=SourceType.UPLOAD, uri=stable_key)
    source = fakes["source_repo"]._sources.setdefault(source.id, source)

    doc = Document(source_id=source.id, stable_key=stable_key)
    doc = fakes["doc_repo"]._documents.setdefault(doc.id, doc)
    fakes["doc_repo"]._by_source_key[(doc.source_id, doc.stable_key)] = doc

    version = DocumentVersion(
        document_id=doc.id,
        blob_hash=hashlib.sha256(blob_data).hexdigest(),
        content_hash="",
    )
    version = fakes["version_repo"]._versions.setdefault(version.id, version)
    fakes["version_repo"]._by_document.setdefault(version.document_id, [])
    if version.id not in fakes["version_repo"]._by_document[version.document_id]:
        fakes["version_repo"]._by_document[version.document_id].append(version.id)

    # Ensure version blob_hash is set
    fakes["version_repo"]._versions[version.id] = DocumentVersion(
        id=version.id,
        document_id=version.document_id,
        blob_hash=version.blob_hash,
        content_hash=version.content_hash,
    )

    return source, doc, version


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestStageNeeded:
    """Unit tests for the static _stage_needed helper."""

    def test_stage_before_current_is_needed(self) -> None:
        task = _make_task(source_id=uuid4(), stage=TaskStage.DISCOVER)
        assert IngestionOrchestrator._stage_needed(task, TaskStage.PARSE)

    def test_stage_already_past_is_not_needed(self) -> None:
        task = _make_task(source_id=uuid4(), stage=TaskStage.CHUNK)
        assert not IngestionOrchestrator._stage_needed(task, TaskStage.PARSE)

    def test_same_stage_is_not_needed(self) -> None:
        task = _make_task(source_id=uuid4(), stage=TaskStage.PARSE)
        assert not IngestionOrchestrator._stage_needed(task, TaskStage.PARSE)

    def test_future_stage_is_needed(self) -> None:
        task = _make_task(source_id=uuid4(), stage=TaskStage.NORMALIZE)
        assert IngestionOrchestrator._stage_needed(task, TaskStage.CHUNK)

    def test_discover_never_needed(self) -> None:
        task = _make_task(source_id=uuid4(), stage=TaskStage.DISCOVER)
        assert not IngestionOrchestrator._stage_needed(task, TaskStage.DISCOVER)


class TestIngestionPipeline:
    """Tests for the full ingestion pipeline."""

    async def test_successful_pipeline(self) -> None:
        """A complete run through all stages succeeds and updates task state."""
        orch, fakes = _make_orchestrator()
        source, doc, version = _seed_source_doc_version(fakes)

        # Manually create a task with the version hash
        task = _make_task(source.id, stage=TaskStage.DISCOVER, target_version_id=version.id)

        # Seed the blob
        blob_data = b"test content"
        storage_key = compute_storage_key(source.id, version.blob_hash)
        await fakes["blob_store"].store(storage_key, blob_data)

        result = await orch.run_pipeline(task)

        assert result.status == TaskStatus.SUCCEEDED
        assert result.last_stage == TaskStage.PUBLISH
        assert result.chunk_count == 2

        # Verify task state was updated
        updated = await fakes["task_repo"].get(task.id)
        assert updated is not None
        assert updated.status == TaskStatus.SUCCEEDED
        assert updated.stage == TaskStage.PUBLISH
        assert updated.progress == 1.0

    async def test_retry_resumes_from_stage(self) -> None:
        """A task that failed at CHUNK skips chunking and re-uses existing chunks."""
        orch, fakes = _make_orchestrator()
        source, doc, version = _seed_source_doc_version(fakes)

        # Task already completed up to CHUNK
        task = _make_task(source.id, stage=TaskStage.CHUNK, target_version_id=version.id)

        blob_data = b"test content"
        storage_key = compute_storage_key(source.id, version.blob_hash)
        await fakes["blob_store"].store(storage_key, blob_data)

        # Seed existing chunks (as if CHUNK was already done)
        existing_chunks = [
            Chunk(id=uuid4(), version_id=version.id, ordinal=0, chunk_hash="h1", text="chunk1"),
            Chunk(id=uuid4(), version_id=version.id, ordinal=1, chunk_hash="h2", text="chunk2"),
        ]
        await fakes["chunk_repo"].create_batch(existing_chunks)

        result = await orch.run_pipeline(task)

        # Pipeline succeeds but without new chunking the embed step produces 0 chunks
        # (the orchestrator skips chunker and embedder since chunk_outputs is empty)
        assert result.status == TaskStatus.SUCCEEDED

        # Parser and chunker should not have been called again
        assert fakes["parser"].call_count == 1  # always parses (idempotent)
        assert fakes["chunker"].call_count == 0  # skipped because stage is past CHUNK
        assert fakes["embedder"].call_count == 0  # skipped because no new chunks

    async def test_cancellation_raises(self) -> None:
        """A task with cancel_requested_at raises CancelledError."""
        orch, fakes = _make_orchestrator()
        source, doc, version = _seed_source_doc_version(fakes)
        task = _make_task(source.id, stage=TaskStage.DISCOVER, target_version_id=version.id)

        # Seed blob so DISCOVER/FINGERPRINT can proceed
        blob_data = b"test content"
        storage_key = compute_storage_key(source.id, version.blob_hash)
        await fakes["blob_store"].store(storage_key, blob_data)

        # Mark as cancelled
        fakes["task_repo"]._tasks[task.id] = IngestionTask(
            id=task.id,
            source_id=task.source_id,
            operation=task.operation,
            status=TaskStatus.CANCEL_REQUESTED,
            stage=TaskStage.FINGERPRINT,
            target_version_id=task.target_version_id,
            idempotency_key=task.idempotency_key,
            progress=task.progress,
            retry_count=task.retry_count,
            max_retries=task.max_retries,
            cancel_requested_at=datetime.now(UTC),
            enqueued_at=task.enqueued_at,
            heartbeat_at=task.heartbeat_at,
            lease_expires_at=task.lease_expires_at,
            error_code=task.error_code,
            error=task.error,
            created_at=task.created_at,
        )

        with pytest.raises(CancelledError):
            await orch.run_pipeline(task)

    async def test_parse_failure(self) -> None:
        """A parse failure raises ValueError."""
        orch, fakes = _make_orchestrator(parser=_FakeParser(fail=True))
        source, doc, version = _seed_source_doc_version(fakes)
        task = _make_task(source.id, stage=TaskStage.DISCOVER, target_version_id=version.id)

        blob_data = b"bad content"
        storage_key = compute_storage_key(source.id, version.blob_hash)
        await fakes["blob_store"].store(storage_key, blob_data)

        with pytest.raises(ValueError, match="Parse failed"):
            await orch.run_pipeline(task)

    async def test_chunker_failure(self) -> None:
        """A chunker failure raises ValueError."""
        orch, fakes = _make_orchestrator(chunker=_FakeChunker(fail=True))
        source, doc, version = _seed_source_doc_version(fakes)
        task = _make_task(source.id, stage=TaskStage.DISCOVER, target_version_id=version.id)

        blob_data = b"test content"
        storage_key = compute_storage_key(source.id, version.blob_hash)
        await fakes["blob_store"].store(storage_key, blob_data)

        with pytest.raises(ValueError, match="Simulated chunker failure"):
            await orch.run_pipeline(task)

    async def test_missing_source(self) -> None:
        """A task with a non-existent source raises RuntimeError."""
        orch, fakes = _make_orchestrator()
        task = _make_task(source_id=uuid4(), stage=TaskStage.DISCOVER)

        with pytest.raises(RuntimeError, match="not found"):
            await orch.run_pipeline(task)

    async def test_missing_blob(self) -> None:
        """A task whose blob was deleted raises RuntimeError."""
        orch, fakes = _make_orchestrator()
        source, doc, version = _seed_source_doc_version(fakes)
        task = _make_task(source.id, stage=TaskStage.DISCOVER, target_version_id=version.id)

        # Don't store the blob
        with pytest.raises(RuntimeError, match="Blob not found at key"):
            await orch.run_pipeline(task)


class TestCancelTask:
    """Tests for the cancel_task method."""

    async def test_cancel_sets_flag(self) -> None:
        orch, fakes = _make_orchestrator()
        source = Source(space_id=uuid4(), source_type=SourceType.UPLOAD)
        source = fakes["source_repo"]._sources.setdefault(source.id, source)
        task = _make_task(source.id)

        result = await orch.cancel_task(task)

        assert result.cancel_requested_at is not None
        updated = await fakes["task_repo"].get(task.id)
        assert updated is not None
        assert updated.status == TaskStatus.CANCEL_REQUESTED


class TestHandlePipelineError:
    """Tests for the handle_pipeline_error method."""

    async def test_error_increments_retry(self) -> None:
        orch, fakes = _make_orchestrator()
        source = Source(space_id=uuid4(), source_type=SourceType.UPLOAD)
        source = fakes["source_repo"]._sources.setdefault(source.id, source)
        task = _make_task(source.id, stage=TaskStage.PARSE)

        exc = ValueError("Something went wrong")
        result = await orch.handle_pipeline_error(task, exc)

        assert result.status == TaskStatus.RUNNING  # still has retries
        updated = await fakes["task_repo"].get(task.id)
        assert updated is not None
        assert updated.retry_count == 1
        assert updated.error_code == "VALIDATION_ERROR"

    async def test_error_exhausts_retries(self) -> None:
        orch, fakes = _make_orchestrator()
        source = Source(space_id=uuid4(), source_type=SourceType.UPLOAD)
        source = fakes["source_repo"]._sources.setdefault(source.id, source)
        task = _make_task(source.id, stage=TaskStage.PARSE)
        # Already at max retries
        task = IngestionTask(
            id=task.id,
            source_id=task.source_id,
            operation=task.operation,
            status=TaskStatus.RUNNING,
            stage=task.stage,
            target_version_id=task.target_version_id,
            idempotency_key=task.idempotency_key,
            progress=task.progress,
            retry_count=3,
            max_retries=3,
            cancel_requested_at=task.cancel_requested_at,
            enqueued_at=task.enqueued_at,
            heartbeat_at=task.heartbeat_at,
            lease_expires_at=task.lease_expires_at,
            error_code=task.error_code,
            error=task.error,
            created_at=task.created_at,
        )

        exc = RuntimeError("Fatal error")
        result = await orch.handle_pipeline_error(task, exc)

        assert result.status == TaskStatus.FAILED
        updated = await fakes["task_repo"].get(task.id)
        assert updated is not None
        assert updated.retry_count == 4
        assert updated.status == TaskStatus.FAILED
        assert updated.error_code == "RUNTIME_ERROR"


class TestHandleCancellation:
    """Tests for the handle_cancellation method."""

    async def test_cancellation_records_status(self) -> None:
        orch, fakes = _make_orchestrator()
        source = Source(space_id=uuid4(), source_type=SourceType.UPLOAD)
        source = fakes["source_repo"]._sources.setdefault(source.id, source)
        task = _make_task(source.id, stage=TaskStage.FINGERPRINT)
        task = IngestionTask(
            id=task.id,
            source_id=task.source_id,
            operation=task.operation,
            status=TaskStatus.CANCEL_REQUESTED,
            stage=task.stage,
            target_version_id=task.target_version_id,
            idempotency_key=task.idempotency_key,
            progress=task.progress,
            retry_count=task.retry_count,
            max_retries=task.max_retries,
            cancel_requested_at=datetime.now(UTC),
            enqueued_at=task.enqueued_at,
            heartbeat_at=task.heartbeat_at,
            lease_expires_at=task.lease_expires_at,
            error_code=task.error_code,
            error=task.error,
            created_at=task.created_at,
        )

        result = await orch.handle_cancellation(task)

        assert result.status == TaskStatus.CANCELLED
        updated = await fakes["task_repo"].get(task.id)
        assert updated is not None
        assert updated.status == TaskStatus.CANCELLED


class TestContentUnchanged:
    """Tests for the is_content_unchanged method."""

    async def test_unchanged_when_blob_hash_matches(self) -> None:
        orch, fakes = _make_orchestrator()
        source, doc, version = _seed_source_doc_version(fakes)

        # Set content_hash on version to match
        blob_data = b"test content"
        expected_content_hash = compute_content_hash(blob_data.decode("utf-8", errors="replace"))
        fakes["version_repo"]._versions[version.id] = DocumentVersion(
            id=version.id,
            document_id=version.document_id,
            blob_hash=version.blob_hash,
            content_hash=expected_content_hash,
        )

        # Update document with current_version_id and reload
        fakes["doc_repo"]._documents[doc.id] = Document(
            id=doc.id,
            source_id=doc.source_id,
            stable_key=doc.stable_key,
            current_version_id=version.id,
            deleted_at=None,
            created_at=doc.created_at,
            updated_at=doc.updated_at,
        )
        updated_doc = await fakes["doc_repo"].get(doc.id)
        assert updated_doc is not None

        assert await orch.is_content_unchanged(updated_doc, blob_data)

    async def test_changed_when_no_published_version(self) -> None:
        orch, fakes = _make_orchestrator()
        source, doc, version = _seed_source_doc_version(fakes)
        # No current_version_id → no version to compare against
        assert not await orch.is_content_unchanged(doc, b"test content")

    async def test_changed_when_different_bytes(self) -> None:
        orch, fakes = _make_orchestrator()
        source, doc, version = _seed_source_doc_version(fakes)

        # Set content_hash on version to match the seed
        blob_data = b"test content"
        expected_content_hash = compute_content_hash(blob_data.decode("utf-8", errors="replace"))
        fakes["version_repo"]._versions[version.id] = DocumentVersion(
            id=version.id,
            document_id=version.document_id,
            blob_hash=version.blob_hash,
            content_hash=expected_content_hash,
        )

        fakes["doc_repo"]._documents[doc.id] = Document(
            id=doc.id,
            source_id=doc.source_id,
            stable_key=doc.stable_key,
            current_version_id=version.id,
            deleted_at=None,
            created_at=doc.created_at,
            updated_at=doc.updated_at,
        )
        updated_doc = await fakes["doc_repo"].get(doc.id)
        assert updated_doc is not None

        # Different bytes → different blob_hash
        assert not await orch.is_content_unchanged(updated_doc, b"different content")


class TestDeleteDocument:
    """Tests for the delete_document method."""

    async def test_delete_creates_tombstone_and_task(self) -> None:
        orch, fakes = _make_orchestrator()
        source = Source(space_id=uuid4(), source_type=SourceType.UPLOAD)
        source = fakes["source_repo"]._sources.setdefault(source.id, source)

        doc = Document(source_id=source.id, stable_key="file.md")
        doc = fakes["doc_repo"]._documents.setdefault(doc.id, doc)

        result = await orch.delete_document(doc)

        assert result is not None
        assert result.operation == TaskOperation.DELETE
        assert result.status == TaskStatus.QUEUED

        # Verify document is tombstoned
        updated_doc = await fakes["doc_repo"].get(doc.id)
        assert updated_doc is not None
        assert updated_doc.deleted_at is not None
        assert updated_doc.current_version_id is None

    async def test_delete_skips_already_deleted(self) -> None:
        orch, fakes = _make_orchestrator()
        source = Source(space_id=uuid4(), source_type=SourceType.UPLOAD)
        source = fakes["source_repo"]._sources.setdefault(source.id, source)

        doc = Document(source_id=source.id, stable_key="file.md", deleted_at=datetime.now(UTC))
        doc = fakes["doc_repo"]._documents.setdefault(doc.id, doc)

        result = await orch.delete_document(doc)
        assert result is None


class TestUpdateDocumentPath:
    """Tests for the update_document_path method."""

    async def test_update_stable_key(self) -> None:
        orch, fakes = _make_orchestrator()
        source = Source(space_id=uuid4(), source_type=SourceType.UPLOAD)
        source = fakes["source_repo"]._sources.setdefault(source.id, source)

        doc = Document(source_id=source.id, stable_key="old/path.md")
        doc = fakes["doc_repo"]._documents.setdefault(doc.id, doc)
        fakes["doc_repo"]._by_source_key[(doc.source_id, doc.stable_key)] = doc

        updated = await orch.update_document_path(doc, "new/path.md", source.id)

        assert updated.stable_key == "new/path.md"

    async def test_conflict_raises_error(self) -> None:
        orch, fakes = _make_orchestrator()
        source = Source(space_id=uuid4(), source_type=SourceType.UPLOAD)
        source = fakes["source_repo"]._sources.setdefault(source.id, source)

        doc1 = Document(source_id=source.id, stable_key="doc1.md")
        doc1 = fakes["doc_repo"]._documents.setdefault(doc1.id, doc1)
        fakes["doc_repo"]._by_source_key[(doc1.source_id, doc1.stable_key)] = doc1

        doc2 = Document(source_id=source.id, stable_key="doc2.md")
        doc2 = fakes["doc_repo"]._documents.setdefault(doc2.id, doc2)
        fakes["doc_repo"]._by_source_key[(doc2.source_id, doc2.stable_key)] = doc2

        # Try to rename doc2 to doc1's key
        with pytest.raises(ValueError, match="already used"):
            await orch.update_document_path(doc2, "doc1.md", source.id)


class TestRunCleanup:
    """Tests for the run_cleanup method (DELETE task execution)."""

    async def test_cleanup_removes_chunks_and_blobs(self) -> None:
        orch, fakes = _make_orchestrator()
        source, doc, version = _seed_source_doc_version(fakes)

        # Store blob and chunks
        blob_data = b"test content"
        storage_key = compute_storage_key(source.id, version.blob_hash)
        await fakes["blob_store"].store(storage_key, blob_data)

        chunks = [
            Chunk(id=uuid4(), version_id=version.id, ordinal=0, chunk_hash="h1", text="c1"),
            Chunk(id=uuid4(), version_id=version.id, ordinal=1, chunk_hash="h2", text="c2"),
        ]
        await fakes["chunk_repo"].create_batch(chunks)

        # Mark as deleted
        fakes["doc_repo"]._documents[doc.id] = Document(
            id=doc.id,
            source_id=doc.source_id,
            stable_key=doc.stable_key,
            current_version_id=None,
            deleted_at=datetime.now(UTC),
            created_at=doc.created_at,
            updated_at=doc.updated_at,
        )

        task = IngestionTask(
            source_id=source.id,
            operation=TaskOperation.DELETE,
            target_version_id=version.id,
        )

        result = await orch.run_cleanup(task)

        assert result.status == TaskStatus.SUCCEEDED
        assert result.last_stage == TaskStage.CLEANUP

        # Verify chunks are gone
        remaining = await fakes["chunk_repo"].get_by_version(version.id)
        assert len(remaining) == 0

        # Verify blob is gone
        blob = await fakes["blob_store"].retrieve(storage_key)
        assert blob is None

    async def test_cleanup_without_deleted_document(self) -> None:
        """Cleanup succeeds even when document is already gone from the repo."""
        orch, fakes = _make_orchestrator()
        source = Source(space_id=uuid4(), source_type=SourceType.UPLOAD)
        source = fakes["source_repo"]._sources.setdefault(source.id, source)

        # No document → nothing to clean
        task = IngestionTask(
            source_id=source.id,
            operation=TaskOperation.DELETE,
        )

        result = await orch.run_cleanup(task)
        assert result.status == TaskStatus.SUCCEEDED
