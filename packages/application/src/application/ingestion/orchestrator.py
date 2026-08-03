"""Ingestion pipeline orchestrator — stage state machine with progress tracking.

Runs the full DISCOVER → … → PUBLISH pipeline for a single
:class:`~domain.models.IngestionTask` and manages its state transitions.
Each stage is idempotent: retries resume from the stage recorded in the
task record without repeating completed work.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from domain.blob_store import BlobStore
from domain.chunking import Chunker, ChunkerConfig, ChunkingResult
from domain.embedding import EmbeddingIdentity, compute_processing_config_hash
from domain.fingerprinting import compute_content_hash, compute_storage_key, normalize_stable_key
from domain.models import (
    Document,
    DocumentStatus,
    DocumentVersion,
    IngestionTask,
    TaskStage,
    TaskStatus,
)
from domain.parsing import (
    ParsedDocument,
    ParseMetadata,
    Parser,
    ParseSuccess,
    StructNode,
    compute_blob_hash,
)
from domain.repositories import (
    ChunkRepository,
    DocumentRepository,
    DocumentVersionRepository,
    IngestionTaskRepository,
    SourceRepository,
)

from application.retrieval.dense import document_embedding_config

from .deletion import DocumentDeletionService
from .embedding import EmbeddingConfig, EmbeddingService, TextEmbedder

logger = logging.getLogger(__name__)

# Ordered stages for the ingestion pipeline.  Stages before this index
# are considered "already completed" on retry.
_STAGE_ORDER: tuple[TaskStage, ...] = (
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
)

_STAGE_INDEX = {s: i for i, s in enumerate(_STAGE_ORDER)}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CancelledError(Exception):
    """Raised when an ingestion task has been requested to cancel."""


def _strip_nul(node: StructNode) -> StructNode:
    """Remove PostgreSQL-incompatible NULs while preserving node locations."""

    return StructNode(
        node_type=node.node_type,
        text=node.text.replace("\x00", ""),
        level=node.level,
        start_line=node.start_line,
        end_line=node.end_line,
        start_page=node.start_page,
        end_page=node.end_page,
        language=node.language,
        children=tuple(_strip_nul(child) for child in node.children),
    )


def _normalize_parsed_document(document: ParsedDocument) -> ParsedDocument:
    """Normalize parser output before hashing, chunking, and persistence."""

    return ParsedDocument(
        metadata=document.metadata,
        text=document.text.replace("\x00", ""),
        structure=tuple(_strip_nul(node) for node in document.structure),
        total_lines=document.total_lines,
    )


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IngestionResult:
    """Outcome of a completed ingestion pipeline run."""

    task_id: UUID
    document_id: UUID
    version_id: UUID
    chunk_count: int
    status: TaskStatus
    last_stage: TaskStage | None = None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IngestionConfig:
    """Runtime configuration for the ingestion pipeline."""

    chunk_size: int = 512
    chunk_overlap: int = 64
    min_chunk_size: int = 100
    max_segment_size: int = 4096
    embedding_batch_size: int = 32
    embedding_identity: EmbeddingIdentity = field(default_factory=EmbeddingIdentity)

    def processing_config(self) -> dict[str, str]:
        """Return the complete parser/chunker/embedding processing identity."""
        return {
            "chunk_overlap": str(self.chunk_overlap),
            "chunk_size": str(self.chunk_size),
            "min_chunk_size": str(self.min_chunk_size),
            "max_segment_size": str(self.max_segment_size),
            **self.embedding_identity.processing_config(),
        }


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class IngestionOrchestrator:
    """Application service that runs the full ingestion pipeline.

    Dependencies (constructor-injected)
    ------------------------------------
    All repository and service dependencies listed below.
    """

    def __init__(
        self,
        source_repo: SourceRepository,
        document_repo: DocumentRepository,
        version_repo: DocumentVersionRepository,
        chunk_repo: ChunkRepository,
        task_repo: IngestionTaskRepository,
        parser: Parser,
        chunker: Chunker,
        text_embedder: TextEmbedder,
        blob_store: BlobStore,
    ) -> None:
        self._source_repo = source_repo
        self._document_repo = document_repo
        self._version_repo = version_repo
        self._chunk_repo = chunk_repo
        self._task_repo = task_repo
        self._parser = parser
        self._chunker = chunker
        self._blob_store = blob_store

        self._embedding_service = EmbeddingService(
            text_embedder=text_embedder,
            chunk_repo=chunk_repo,
            version_repo=version_repo,
            document_repo=document_repo,
        )
        self._deletion_service = DocumentDeletionService(
            document_repo=document_repo,
            version_repo=version_repo,
            task_repo=task_repo,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_pipeline(
        self,
        task: IngestionTask,
        *,
        config: IngestionConfig | None = None,
    ) -> IngestionResult:
        """Execute the full ingestion pipeline for *task*.

        Parameters
        ----------
        task:
            The ingestion task to process.  The ``task.stage`` field
            determines where to resume on retry — stages before it are
            skipped.
        config:
            Pipeline configuration (chunk sizes, embedding batch, etc.).
            Defaults are used when ``None``.

        Returns
        -------
        IngestionResult
            Outcome of the pipeline run.

        Raises
        ------
        CancelledError
            If cancellation was requested before or during the pipeline.
        RuntimeError
            If a required entity (source, document, version, blob) cannot
            be found.
        ValueError
            If parsing fails or validation fails.
        """
        cfg = config or IngestionConfig()

        # ------------------------------------------------------------------
        # Bootstrap: load source, document, version, blob
        # ------------------------------------------------------------------
        source = await self._source_repo.get(task.source_id)
        if source is None:
            raise RuntimeError(f"Source {task.source_id} not found for task {task.id}")

        # Resolve version first (via target_version_id or fallback).
        version = await self._determine_version(task)
        if version is None:
            raise RuntimeError(
                f"Could not determine target version for task {task.id} "
                f"(target_version_id={task.target_version_id!r})"
            )

        # Get the owning document from the version record.
        document = await self._document_repo.get(version.document_id)
        if document is None:
            raise RuntimeError(
                f"Document {version.document_id} not found for version {version.id} "
                f"(task {task.id})"
            )

        storage_key = compute_storage_key(source.id, version.blob_hash)
        raw_bytes = await self._blob_store.retrieve(storage_key)
        if raw_bytes is None:
            raise RuntimeError(f"Blob not found at key {storage_key!r}")

        # ------------------------------------------------------------------
        # Pipeline stages — each stage runs only if not yet completed
        # ------------------------------------------------------------------

        # --- DISCOVER (already done by registration) ---
        if self._stage_needed(task, TaskStage.DISCOVER):
            await self._update_task_stage(task, TaskStage.DISCOVER, 0.0)

        # --- FINGERPRINT (already done by registration) ---
        if self._stage_needed(task, TaskStage.FINGERPRINT):
            await self._update_task_stage(task, TaskStage.FINGERPRINT, 0.05)

        # --- PARSE ---
        # Always parse (idempotent); only update task stage if this stage
        # hasn't been completed yet.
        await self._check_cancelled(task)
        parse_meta = ParseMetadata(
            file_name=document.stable_key,
            file_size=len(raw_bytes),
            mime_type="",
            encoding="utf-8",
        )
        parse_result = await self._parser.parse(raw=raw_bytes, metadata=parse_meta)
        if not isinstance(parse_result, ParseSuccess):
            raise ValueError(f"Parse failed: {parse_result.message} (code={parse_result.code})")
        parsed_doc = _normalize_parsed_document(parse_result.document)
        if self._stage_needed(task, TaskStage.PARSE):
            task = await self._update_task_stage(task, TaskStage.PARSE, 0.15)
            await self._task_repo.checkpoint()

        # --- NORMALIZE ---
        await self._check_cancelled(task)
        normalized_text = parsed_doc.text
        content_hash = compute_content_hash(normalized_text)
        if self._stage_needed(task, TaskStage.NORMALIZE):
            version = await self._version_repo.update(
                DocumentVersion(
                    id=version.id,
                    document_id=version.document_id,
                    blob_hash=version.blob_hash,
                    content_hash=content_hash,
                    parser_version=version.parser_version,
                    normalizer_version=version.normalizer_version,
                    chunker_version=version.chunker_version,
                    embedding_version=version.embedding_version,
                    processing_config_hash=version.processing_config_hash,
                    processing_config=version.processing_config,
                    status=version.status,
                    file_path=version.file_path,
                    created_at=version.created_at,
                )
            )
            task = await self._update_task_stage(task, TaskStage.NORMALIZE, 0.25)
            await self._task_repo.checkpoint()

        # --- ENRICH (placeholder, no-op) ---
        await self._check_cancelled(task)
        if self._stage_needed(task, TaskStage.ENRICH):
            task = await self._update_task_stage(task, TaskStage.ENRICH, 0.35)
            await self._task_repo.checkpoint()

        # --- CHUNK ---
        await self._check_cancelled(task)
        chunking_result: ChunkingResult
        advance_to_chunk = self._stage_needed(task, TaskStage.CHUNK)
        retry_from_chunk = task.stage == TaskStage.CHUNK
        if advance_to_chunk or retry_from_chunk:
            chunker_config = ChunkerConfig(
                chunk_size=cfg.chunk_size,
                chunk_overlap=cfg.chunk_overlap,
                min_chunk_size=cfg.min_chunk_size,
                max_segment_size=cfg.max_segment_size,
            )
            chunking_result = await self._chunker.chunk(parsed_doc, config=chunker_config)
            processing_config = cfg.processing_config()
            # Write chunker identity back to the version record so the
            # version carries the actual processing config used.
            version = await self._version_repo.update(
                DocumentVersion(
                    id=version.id,
                    document_id=version.document_id,
                    blob_hash=version.blob_hash,
                    content_hash=version.content_hash,
                    parser_version=version.parser_version,
                    normalizer_version=version.normalizer_version,
                    chunker_version=chunking_result.chunker_version,
                    embedding_version=version.embedding_version,
                    processing_config_hash=compute_processing_config_hash(processing_config),
                    processing_config=processing_config,
                    status=version.status,
                    file_path=version.file_path,
                    created_at=version.created_at,
                )
            )
            if advance_to_chunk:
                task = await self._update_task_stage(task, TaskStage.CHUNK, 0.50)
                await self._task_repo.checkpoint()
        else:
            # EmbeddingService publishes before the task advances past CHUNK.
            # Later checkpoints therefore only need validation below.
            existing_chunks = await self._chunk_repo.get_by_version(version.id)
            chunking_result = ChunkingResult(
                chunks=(),
                chunker_version="",
                config_hash="",
                total_ordinals=len(existing_chunks),
            )

        # --- EMBED / INDEX / VALIDATE / PUBLISH ---
        await self._check_cancelled(task)
        if _STAGE_INDEX.get(task.stage, 0) <= _STAGE_INDEX[TaskStage.CHUNK]:
            embed_config = EmbeddingConfig(
                batch_size=cfg.embedding_batch_size,
                embedding_identity=cfg.embedding_identity,
                document_prefix=document_embedding_config(cfg.embedding_identity),
            )
            embed_result = await self._embedding_service.embed_and_publish(
                document=document,
                version=version,
                chunk_outputs=chunking_result.chunks,
                config=embed_config,
            )
            chunk_count = embed_result.chunk_count
            version = embed_result.version

            if self._stage_needed(task, TaskStage.EMBED):
                task = await self._update_task_stage(task, TaskStage.EMBED, 0.65)
                await self._task_repo.checkpoint()
            if self._stage_needed(task, TaskStage.INDEX):
                task = await self._update_task_stage(task, TaskStage.INDEX, 0.75)
                await self._task_repo.checkpoint()
            if self._stage_needed(task, TaskStage.VALIDATE):
                task = await self._update_task_stage(task, TaskStage.VALIDATE, 0.85)
                await self._task_repo.checkpoint()
            if self._stage_needed(task, TaskStage.PUBLISH):
                task = await self._update_task_stage(task, TaskStage.PUBLISH, 1.0)
                await self._task_repo.checkpoint()
        else:
            existing_chunks = await self._chunk_repo.get_by_version(version.id)
            if (
                version.status != DocumentStatus.PUBLISHED
                or document.current_version_id != version.id
            ):
                raise RuntimeError(
                    f"Task {task.id} is past CHUNK but version {version.id} "
                    "is not atomically published"
                )
            chunk_count = len(existing_chunks)

        # ------------------------------------------------------------------
        # Mark task SUCCEEDED
        # ------------------------------------------------------------------
        final_task = await self._task_repo.update(
            IngestionTask(
                id=task.id,
                source_id=task.source_id,
                operation=task.operation,
                status=TaskStatus.SUCCEEDED,
                stage=TaskStage.PUBLISH,
                target_version_id=version.id,
                idempotency_key=task.idempotency_key,
                progress=1.0,
                retry_count=task.retry_count,
                max_retries=task.max_retries,
                cancel_requested_at=task.cancel_requested_at,
                enqueued_at=task.enqueued_at,
                heartbeat_at=datetime.now(UTC),
                lease_expires_at=task.lease_expires_at,
                error_code=None,
                error=None,
                created_at=task.created_at,
            )
        )

        logger.info(
            "Ingestion task %s SUCCEEDED (document %s, version %s, %d chunks)",
            final_task.id,
            document.id,
            version.id,
            chunk_count,
        )

        return IngestionResult(
            task_id=final_task.id,
            document_id=document.id,
            version_id=version.id,
            chunk_count=chunk_count,
            status=TaskStatus.SUCCEEDED,
            last_stage=TaskStage.PUBLISH,
        )

    async def cancel_task(self, task: IngestionTask) -> IngestionTask:
        """Request cancellation of an in-flight ingestion task.

        Sets ``cancel_requested_at``; the running worker checks this flag
        at the next stage boundary and stops cleanly.
        """
        updated = await self._task_repo.update(
            IngestionTask(
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
        )
        logger.info("Cancellation requested for task %s", updated.id)
        return updated

    async def handle_pipeline_error(
        self,
        task: IngestionTask,
        exc: Exception,
    ) -> IngestionResult:
        """Record a pipeline failure on *task* and return the error result.

        If the retry budget is exhausted, transitions to FAILED; otherwise
        leaves the status as RUNNING so Dramatiq can retry.
        """
        error_code = _classify_error(exc)
        error_msg = str(exc)[:2000]

        new_retry = task.retry_count + 1
        cancellation_won = task.cancel_requested_at is not None
        new_status = (
            TaskStatus.CANCELLED
            if cancellation_won
            else TaskStatus.FAILED
            if new_retry > task.max_retries
            else TaskStatus.RUNNING
        )

        updated = await self._task_repo.update(
            IngestionTask(
                id=task.id,
                source_id=task.source_id,
                operation=task.operation,
                status=new_status,
                stage=task.stage,
                target_version_id=task.target_version_id,
                idempotency_key=task.idempotency_key,
                progress=task.progress,
                retry_count=new_retry,
                max_retries=task.max_retries,
                cancel_requested_at=task.cancel_requested_at,
                enqueued_at=task.enqueued_at,
                heartbeat_at=datetime.now(UTC),
                lease_expires_at=task.lease_expires_at,
                error_code=None if cancellation_won else error_code,
                error=None if cancellation_won else error_msg,
                created_at=task.created_at,
            )
        )

        logger.error(
            "Ingestion task %s error (retry %d/%d): %s",
            updated.id,
            new_retry,
            task.max_retries,
            "cancellation requested" if cancellation_won else error_msg,
        )

        return IngestionResult(
            task_id=updated.id,
            document_id=UUID(int=0),
            version_id=UUID(int=0),
            chunk_count=0,
            status=new_status,
            last_stage=task.stage,
        )

    async def handle_cancellation(self, task: IngestionTask) -> IngestionResult:
        """Mark *task* as CANCELLED and return the final result."""
        updated = await self._task_repo.update(
            IngestionTask(
                id=task.id,
                source_id=task.source_id,
                operation=task.operation,
                status=TaskStatus.CANCELLED,
                stage=task.stage,
                target_version_id=task.target_version_id,
                idempotency_key=task.idempotency_key,
                progress=task.progress,
                retry_count=task.retry_count,
                max_retries=task.max_retries,
                cancel_requested_at=task.cancel_requested_at,
                enqueued_at=task.enqueued_at,
                heartbeat_at=datetime.now(UTC),
                lease_expires_at=task.lease_expires_at,
                error_code=None,
                error=None,
                created_at=task.created_at,
            )
        )

        logger.info("Ingestion task %s CANCELLED at stage %s", updated.id, task.stage)

        return IngestionResult(
            task_id=updated.id,
            document_id=UUID(int=0),
            version_id=UUID(int=0),
            chunk_count=0,
            status=TaskStatus.CANCELLED,
            last_stage=task.stage,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _determine_version(
        self,
        task: IngestionTask,
        document: Document | None = None,
    ) -> DocumentVersion:
        """Return the target version for *task*.

        Resolution order
        -----------------
        1. ``task.target_version_id`` (pinned by the caller — the primary
           path; every newly-created task sets this).
        2. ``document.current_version_id`` (set by a previous PUBLISH).
        3. ``get_latest`` — the most-recent version that is still in a
           processable state (PENDING / PARSING / PARSED / EMBEDDED).

        *document* is only needed for fallback paths 2 and 3.  When task
        creation always provides ``target_version_id`` (the post-P0-fix
        contract), *document* can be ``None``.

        Raises
        ------
        RuntimeError
            If no usable version can be found.
        """
        # 1) Task-pinned version
        if task.target_version_id is not None:
            version = await self._version_repo.get(task.target_version_id)
            if version is not None:
                if not version.blob_hash:
                    raise RuntimeError(
                        f"Target version {version.id} for task {task.id} has an empty blob_hash"
                    )
                return version

        # If no target_version_id and no document, we cannot fall back.
        if document is None:
            raise RuntimeError(
                f"Task {task.id} has no target_version_id and no document "
                "was provided for fallback — cannot determine version."
            )

        # 2) Document-pinned version (set by a previous PUBLISH).
        if document.current_version_id is not None:
            version = await self._version_repo.get(document.current_version_id)
            if version is not None and version.blob_hash:
                return version

        # 3) Latest processable version
        latest = await self._version_repo.get_latest(document.id)
        if (
            latest is not None
            and latest.blob_hash
            and latest.status
            in (
                DocumentStatus.PENDING,
                DocumentStatus.PARSING,
                DocumentStatus.PARSED,
                DocumentStatus.EMBEDDED,
            )
        ):
            return latest

        raise RuntimeError(
            f"No usable version found for document {document.id} "
            f"(source {document.source_id}, "
            f"current_version_id={document.current_version_id!r}). "
            "The document may not have been registered with a blob_hash. "
            "Upload the file again to trigger re-registration."
        )

    @staticmethod
    def _stage_needed(task: IngestionTask, stage: TaskStage) -> bool:
        """Return ``True`` if *stage* has not yet been completed for *task*."""
        current_idx = _STAGE_INDEX.get(task.stage, 0)
        needed_idx = _STAGE_INDEX.get(stage, 0)
        return needed_idx > current_idx

    async def _update_task_stage(
        self,
        task: IngestionTask,
        stage: TaskStage,
        progress: float,
    ) -> IngestionTask:
        """Persist a stage/progress update and return the refreshed task.

        Checks the database for ``cancel_requested_at`` so that an
        externally‑requested cancellation is not accidentally overwritten
        by the stage update.
        """
        # Reload cancellation state from DB to avoid overwriting a
        # cancellation that was requested between stage updates.
        fresh = await self._task_repo.get(task.id)
        cancel_ts = fresh.cancel_requested_at if fresh is not None else task.cancel_requested_at

        await self._task_repo.update(
            IngestionTask(
                id=task.id,
                source_id=task.source_id,
                operation=task.operation,
                status=TaskStatus.RUNNING,
                stage=stage,
                target_version_id=task.target_version_id,
                idempotency_key=task.idempotency_key,
                progress=progress,
                retry_count=task.retry_count,
                max_retries=task.max_retries,
                cancel_requested_at=cancel_ts,
                enqueued_at=task.enqueued_at,
                heartbeat_at=datetime.now(UTC),
                lease_expires_at=task.lease_expires_at,
                error_code=task.error_code,
                error=task.error,
                created_at=task.created_at,
            )
        )
        object.__setattr__(task, "stage", stage)
        object.__setattr__(task, "progress", progress)
        object.__setattr__(task, "heartbeat_at", datetime.now(UTC))
        return task

    async def _check_cancelled(self, task: IngestionTask) -> None:
        """Reload the task from DB and raise ``CancelledError`` if cancelled."""
        fresh = await self._task_repo.get(task.id)
        if fresh is not None and fresh.cancel_requested_at is not None:
            raise CancelledError(f"Task {task.id} was cancelled at {fresh.cancel_requested_at}")

    # ------------------------------------------------------------------
    # Step 7: Incremental maintenance & delete
    # ------------------------------------------------------------------

    async def is_content_unchanged(
        self,
        document: Document,
        raw_bytes: bytes,
    ) -> bool:
        """Return ``True`` if *raw_bytes* produce the same content hash as the
        most-recently published version of *document*.

        When unchanged, the caller can skip the ingestion pipeline entirely.
        """
        if document.current_version_id is None:
            return False  # no published version to compare against

        current_version = await self._version_repo.get(document.current_version_id)
        if current_version is None:
            return False

        # Compare blob hash first (fast)
        new_blob_hash = compute_blob_hash(raw_bytes)
        if new_blob_hash != current_version.blob_hash:
            return False

        # Compare content hash
        normalized_text = raw_bytes.decode("utf-8", errors="replace")
        new_content_hash = compute_content_hash(normalized_text)
        current_content_hash: str = current_version.content_hash
        return bool(new_content_hash == current_content_hash)

    async def delete_document(
        self,
        document: Document,
        *,
        idempotency_key: str | None = None,
    ) -> IngestionTask | None:
        """Atomically unpublish *document* and create a cleanup task."""
        return await self._deletion_service.delete_document(
            document,
            idempotency_key=idempotency_key,
        )

    async def update_document_path(
        self,
        document: Document,
        new_stable_key: str,
        source_id: UUID,
    ) -> Document:
        """Update the stable key of *document* (path change / rename).

        Only updates when no other document in the same source already uses
        the target key.  Returns the updated document.

        Raises
        ------
        ValueError
            If *new_stable_key* is already taken by another document in the
            same source.
        """
        normalized = normalize_stable_key(new_stable_key)

        # Check for conflicts within the same source
        existing = await self._document_repo.get_by_stable_key(source_id, normalized)
        if existing is not None and existing.id != document.id:
            raise ValueError(
                f"Cannot rename document {document.id}: stable_key "
                f"{normalized!r} is already used by document {existing.id}"
            )

        updated = Document(
            id=document.id,
            source_id=document.source_id,
            stable_key=normalized,
            current_version_id=document.current_version_id,
            deleted_at=document.deleted_at,
            created_at=document.created_at,
        )
        return await self._document_repo.update(updated)

    async def run_cleanup(self, task: IngestionTask) -> IngestionResult:
        """Execute the cleanup pipeline for a DELETE task.

        Removes all chunks and blobs associated with the document versions.
        """
        source = await self._source_repo.get(task.source_id)
        if source is None:
            raise RuntimeError(f"Source {task.source_id} not found for cleanup task {task.id}")

        if task.target_version_id is None:
            # A document without any versions has no derived artifacts.
            return await self._mark_cleanup_succeeded(task)

        target_version = await self._version_repo.get(task.target_version_id)
        if target_version is None:
            # The version and its cascade-owned chunks are already gone.
            return await self._mark_cleanup_succeeded(task)

        document = await self._document_repo.get(target_version.document_id)
        if document is None:
            return await self._mark_cleanup_succeeded(task)
        if document.source_id != source.id:
            raise RuntimeError(
                f"Target version {target_version.id} does not belong to source {source.id}"
            )
        if document.deleted_at is None:
            raise RuntimeError(
                f"Refusing cleanup for active document {document.id} in task {task.id}"
            )

        # Get all versions for this document and remove their chunks first.
        versions = await self._version_repo.get_by_document(document.id)
        for version in versions:
            await self._chunk_repo.delete_by_version(version.id)

        # Blob keys are shared by same-byte documents within a source. Keep a
        # blob while any other active document still references it.
        for blob_hash in {version.blob_hash for version in versions if version.blob_hash}:
            if not await self._active_document_references_blob(
                source.id,
                excluding_document_id=document.id,
                blob_hash=blob_hash,
            ):
                await self._blob_store.delete(compute_storage_key(source.id, blob_hash))

        return await self._mark_cleanup_succeeded(task)

    async def _active_document_references_blob(
        self,
        source_id: UUID,
        *,
        excluding_document_id: UUID,
        blob_hash: str,
    ) -> bool:
        """Return whether an active document still references a source blob."""
        documents = await self._document_repo.get_by_source(source_id)
        for document in documents:
            if document.id == excluding_document_id or document.deleted_at is not None:
                continue
            versions = await self._version_repo.get_by_document(document.id)
            if any(version.blob_hash == blob_hash for version in versions):
                return True
        return False

    async def _mark_cleanup_succeeded(self, task: IngestionTask) -> IngestionResult:
        """Mark a cleanup task as SUCCEEDED."""
        updated = await self._task_repo.update(
            IngestionTask(
                id=task.id,
                source_id=task.source_id,
                operation=task.operation,
                status=TaskStatus.SUCCEEDED,
                stage=TaskStage.CLEANUP,
                target_version_id=task.target_version_id,
                idempotency_key=task.idempotency_key,
                progress=1.0,
                retry_count=task.retry_count,
                max_retries=task.max_retries,
                cancel_requested_at=task.cancel_requested_at,
                enqueued_at=task.enqueued_at,
                heartbeat_at=datetime.now(UTC),
                lease_expires_at=task.lease_expires_at,
                error_code=None,
                error=None,
                created_at=task.created_at,
            )
        )
        logger.info("Cleanup task %s SUCCEEDED", updated.id)

        return IngestionResult(
            task_id=updated.id,
            document_id=UUID(int=0),
            version_id=UUID(int=0),
            chunk_count=0,
            status=TaskStatus.SUCCEEDED,
            last_stage=TaskStage.CLEANUP,
        )


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def _classify_error(exc: Exception) -> str:
    """Map an exception to a stable error code."""
    if isinstance(exc, CancelledError):
        return "CANCELLED"
    if isinstance(exc, ValueError):
        return "VALIDATION_ERROR"
    if isinstance(exc, RuntimeError):
        return "RUNTIME_ERROR"
    if isinstance(exc, TimeoutError):
        return "TIMEOUT"
    return "UNKNOWN_ERROR"
