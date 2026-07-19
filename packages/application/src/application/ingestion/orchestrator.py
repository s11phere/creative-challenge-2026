"""Ingestion pipeline orchestrator — stage state machine with progress tracking.

Runs the full DISCOVER → … → PUBLISH pipeline for a single
:class:`~domain.models.IngestionTask` and manages its state transitions.
Each stage is idempotent: retries resume from the stage recorded in the
task record without repeating completed work.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from domain.blob_store import BlobStore
from domain.chunking import Chunker, ChunkerConfig, ChunkingResult
from domain.fingerprinting import compute_content_hash, compute_storage_key
from domain.models import (
    Document,
    DocumentStatus,
    DocumentVersion,
    IngestionTask,
    TaskStage,
    TaskStatus,
)
from domain.parsing import ParseMetadata, Parser, ParseSuccess
from domain.repositories import (
    ChunkRepository,
    DocumentRepository,
    DocumentVersionRepository,
    IngestionTaskRepository,
    SourceRepository,
)

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
    embedding_batch_size: int = 32
    embedding_version: str = "1.0"


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

        docs = await self._document_repo.get_by_source(source.id)
        if not docs:
            raise RuntimeError(f"No document found for source {source.id}")
        document = docs[0]

        version = await self._determine_version(task, document)

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
        source_type_str = source.source_type.value if source.source_type else "upload"
        parse_meta = ParseMetadata(
            file_name=document.stable_key,
            file_size=len(raw_bytes),
            mime_type=_guess_mime(source_type_str),
            encoding="utf-8",
        )
        parse_result = await self._parser.parse(raw=raw_bytes, metadata=parse_meta)
        if not isinstance(parse_result, ParseSuccess):
            raise ValueError(f"Parse failed: {parse_result.message} (code={parse_result.code})")
        parsed_doc = parse_result.document
        if self._stage_needed(task, TaskStage.PARSE):
            task = await self._update_task_stage(task, TaskStage.PARSE, 0.15)

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

        # --- ENRICH (placeholder, no-op) ---
        await self._check_cancelled(task)
        if self._stage_needed(task, TaskStage.ENRICH):
            task = await self._update_task_stage(task, TaskStage.ENRICH, 0.35)

        # --- CHUNK ---
        await self._check_cancelled(task)
        chunking_result: ChunkingResult
        if self._stage_needed(task, TaskStage.CHUNK):
            chunker_config = ChunkerConfig(
                chunk_size=cfg.chunk_size,
                chunk_overlap=cfg.chunk_overlap,
                min_chunk_size=cfg.min_chunk_size,
            )
            chunking_result = await self._chunker.chunk(parsed_doc, config=chunker_config)
            task = await self._update_task_stage(task, TaskStage.CHUNK, 0.50)
        else:
            # On retry past CHUNK, load existing chunks from the DB
            existing_chunks = await self._chunk_repo.get_by_version(version.id)
            chunking_result = ChunkingResult(
                chunks=(),
                chunker_version="",
                config_hash="",
                total_ordinals=len(existing_chunks),
            )

        # --- EMBED / INDEX / VALIDATE / PUBLISH ---
        await self._check_cancelled(task)
        if chunking_result.chunks:
            embed_config = EmbeddingConfig(
                batch_size=cfg.embedding_batch_size,
                embedding_version=cfg.embedding_version,
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
            if self._stage_needed(task, TaskStage.INDEX):
                task = await self._update_task_stage(task, TaskStage.INDEX, 0.75)
            if self._stage_needed(task, TaskStage.VALIDATE):
                task = await self._update_task_stage(task, TaskStage.VALIDATE, 0.85)
            if self._stage_needed(task, TaskStage.PUBLISH):
                task = await self._update_task_stage(task, TaskStage.PUBLISH, 1.0)
        else:
            chunk_count = 0

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
        new_status = TaskStatus.FAILED if new_retry > task.max_retries else TaskStatus.RUNNING

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
                error_code=error_code,
                error=error_msg,
                created_at=task.created_at,
            )
        )

        logger.error(
            "Ingestion task %s error (retry %d/%d): %s",
            updated.id,
            new_retry,
            task.max_retries,
            error_msg,
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
        document: Document,
    ) -> DocumentVersion:
        """Return the target version for *task*, creating one if needed."""
        if task.target_version_id is not None:
            version = await self._version_repo.get(task.target_version_id)
            if version is not None:
                return version

        latest = await self._version_repo.get_latest(document.id)
        if latest is not None and latest.status in (
            DocumentStatus.PENDING,
            DocumentStatus.PARSING,
            DocumentStatus.PARSED,
            DocumentStatus.EMBEDDED,
        ):
            return latest

        version = DocumentVersion(document_id=document.id, blob_hash="", content_hash="")
        return await self._version_repo.create(version)

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


def _guess_mime(source_type: str) -> str:
    """Map a source type string to a MIME type hint for the parser."""
    mime_map = {
        "upload": "application/octet-stream",
        "markdown": "text/markdown",
        "text": "text/plain",
        "pdf": "application/pdf",
    }
    return mime_map.get(source_type, "application/octet-stream")
