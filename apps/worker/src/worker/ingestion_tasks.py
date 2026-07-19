"""Ingestion pipeline Dramatiq actors.

Runs the full DISCOVER → … → PUBLISH pipeline for each ingestion task
with explicit state-machine tracking, bounded retry, and cancellation
support.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import dramatiq
from application.ingestion.orchestrator import (
    CancelledError,
    IngestionConfig,
    IngestionOrchestrator,
)
from domain.blob_store import BlobStore
from domain.models import IngestionTask, TaskStatus
from domain.parsing import ParseMetadata, ParseResult
from infrastructure.blob_store import LocalFileBlobStore
from infrastructure.chunkers import StructureChunker
from infrastructure.config import settings
from infrastructure.database import Database
from infrastructure.parsers import ParserFactory
from infrastructure.queue import create_redis_broker
from infrastructure.repositories import (
    ChunkRepository,
    DocumentRepository,
    DocumentVersionRepository,
    IngestionTaskRepository,
    SourceRepository,
)
from infrastructure.telemetry_context import (
    bind_observability_context,
    new_trace_id,
    normalize_trace_id,
    trace_parent_context,
)
from model_gateway import (
    CapabilityAlias,
    EmbeddingRequest,
    GatewayConfig,
    ModelGateway,
    ModelProvider,
    create_model_gateway,
)
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def _create_gateway() -> ModelGateway:
    """Create a ModelGateway from application settings."""
    api_key = settings.model_api_key.get_secret_value() if settings.model_api_key else None
    return create_model_gateway(
        GatewayConfig(
            provider=ModelProvider(settings.model_provider),
            endpoint=settings.model_endpoint,
            api_key=api_key,
            fast_chat_model=settings.fast_chat_model,
            embedding_model=settings.embedding_model,
            allow_external=settings.model_allow_external,
            timeout_seconds=settings.model_timeout_seconds,
            max_retries=settings.model_max_retries,
            retry_backoff_seconds=settings.model_retry_backoff_seconds,
        )
    )


# ---------------------------------------------------------------------------
# Shared infrastructure
# ---------------------------------------------------------------------------

broker = create_redis_broker(settings.redis_url)
tracer = trace.get_tracer("worker.ingestion")
database = Database(settings.database_url)
gateway = _create_gateway()
blob_store: BlobStore = LocalFileBlobStore()


# ---------------------------------------------------------------------------
# Adapters: bridge infrastructure implementations to application protocols
# ---------------------------------------------------------------------------


class _ParserAdapter:
    """Adapt ``ParserFactory`` to the domain ``Parser`` protocol."""

    def __init__(self) -> None:
        self._factory = ParserFactory()

    async def parse(self, raw: bytes, metadata: ParseMetadata) -> ParseResult:
        return await self._factory.parse(raw, metadata.file_name, metadata.mime_type)


class _GatewayTextEmbedder:
    """Adapt ``ModelGateway`` to the ``TextEmbedder`` protocol."""

    def __init__(self, gw: ModelGateway) -> None:
        self._gateway = gw

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        response = await self._gateway.embed(
            EmbeddingRequest(texts=texts),
            capability=CapabilityAlias.EMBEDDING_ZH,
        )
        return response.vectors


# ---------------------------------------------------------------------------
# Orchestrator factory (one per actor invocation)
# ---------------------------------------------------------------------------


def _build_orchestrator(
    session: AsyncSession, gw: ModelGateway, bs: BlobStore
) -> IngestionOrchestrator:
    """Construct an ``IngestionOrchestrator`` with infrastructure adapters."""
    return IngestionOrchestrator(
        source_repo=SourceRepository(session),
        document_repo=DocumentRepository(session),
        version_repo=DocumentVersionRepository(session),
        chunk_repo=ChunkRepository(session),
        task_repo=IngestionTaskRepository(session),
        parser=_ParserAdapter(),
        chunker=StructureChunker(),
        text_embedder=_GatewayTextEmbedder(gw),
        blob_store=bs,
    )


# ---------------------------------------------------------------------------
# Dead-letter handler
# ---------------------------------------------------------------------------


@dramatiq.actor(
    broker=broker,
    actor_name="ingestion_task_permanently_failed",
    queue_name="ingestion",
    max_retries=0,
)
def ingestion_task_permanently_failed(
    message_data: dict[str, Any], retry_data: dict[str, Any]
) -> None:
    """Handle ingestion tasks whose retry budget is exhausted.

    Transitions the task to ``DEAD_LETTER`` status in the database.
    The original task metadata is never logged.
    """
    kwargs = message_data.get("kwargs", {})
    task_id = str(kwargs.get("task_id", ""))
    trace_id = normalize_trace_id(str(kwargs.get("trace_id", ""))) or new_trace_id()

    with (
        tracer.start_as_current_span(
            "ingestion_task.permanently_failed",
            context=trace_parent_context(trace_id),
            kind=SpanKind.CONSUMER,
        ),
        bind_observability_context(trace_id=trace_id, task_id=task_id),
    ):
        logger.error(
            "ingestion_task_permanently_failed",
            extra={
                "task_id": task_id,
                "trace_id": trace_id,
                "retries": retry_data.get("retries"),
                "max_retries": retry_data.get("max_retries"),
            },
        )

        # Record dead-letter status in the DB
        import asyncio  # noqa: PLC0415

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()  # noqa: RUF006

        loop.run_until_complete(_record_dead_letter(task_id))


async def _record_dead_letter(task_id: str) -> None:
    """Transition *task_id* to ``DEAD_LETTER`` in a fresh session."""
    try:
        uid = UUID(task_id)
    except ValueError:
        return

    async with database.session() as session:
        repo = IngestionTaskRepository(session)
        task = await repo.get(uid)
        if task is None:
            return

        await repo.update(
            IngestionTask(
                id=task.id,
                source_id=task.source_id,
                operation=task.operation,
                status=TaskStatus.DEAD_LETTER,
                stage=task.stage,
                target_version_id=task.target_version_id,
                idempotency_key=task.idempotency_key,
                progress=task.progress,
                retry_count=task.retry_count,
                max_retries=task.max_retries,
                cancel_requested_at=task.cancel_requested_at,
                enqueued_at=task.enqueued_at,
                heartbeat_at=task.heartbeat_at,
                lease_expires_at=task.lease_expires_at,
                error_code="RETRY_EXHAUSTED",
                error="Task exceeded maximum retry count",
                created_at=task.created_at,
            )
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Main ingestion actor
# ---------------------------------------------------------------------------


@dramatiq.actor(
    broker=broker,
    actor_name="ingestion_task",
    queue_name="ingestion",
    max_retries=settings.ingestion_task_max_retries,
    min_backoff=settings.ingestion_task_min_backoff_ms,
    time_limit=settings.ingestion_task_timeout_ms,
    notify_shutdown=True,
    on_retry_exhausted="ingestion_task_permanently_failed",
)
def ingestion_task(*, task_id: str, trace_id: str) -> None:
    """Process a single ingestion task.

    The actor receives only control-plane metadata (IDs); all business data
    is loaded from the database or blob store inside the actor body.

    Parameters
    ----------
    task_id:
        UUID of the ``IngestionTask`` record.
    trace_id:
        32-character W3C trace ID for observability correlation.
    """
    canonical_trace_id = normalize_trace_id(trace_id) or new_trace_id()

    with (
        tracer.start_as_current_span(
            "ingestion_task.process",
            context=trace_parent_context(canonical_trace_id),
            kind=SpanKind.CONSUMER,
            attributes={
                "messaging.system": "redis",
                "messaging.operation.name": "process",
                "ingestion.task_id": task_id,
            },
        ),
        bind_observability_context(trace_id=canonical_trace_id, task_id=task_id),
    ):
        try:
            _run_ingestion_sync(task_id, canonical_trace_id)
        except CancelledError:
            logger.info("Ingestion task %s cancelled, recorded in DB", task_id)
        except Exception:
            logger.exception("Ingestion task %s failed (will retry)", task_id)
            raise


def _run_ingestion_sync(task_id: str, canonical_trace_id: str) -> None:
    """Synchronous wrapper that manages the async event loop for Dramatiq."""
    import asyncio  # noqa: PLC0415

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()  # noqa: RUF006
        asyncio.set_event_loop(loop)

    loop.run_until_complete(_run_ingestion_async(task_id, canonical_trace_id))


async def _run_ingestion_async(task_id: str, _canonical_trace_id: str) -> None:
    """Core async ingestion logic with session management."""
    tid = UUID(task_id)
    cfg = IngestionConfig()

    # ------------------------------------------------------------------
    # Phase 1: Run the pipeline in its own session
    # ------------------------------------------------------------------
    try:
        async with database.session() as session:
            orch = _build_orchestrator(session, gateway, blob_store)
            task = await orch._task_repo.get(tid)
            if task is None:
                logger.warning("Ingestion task %s not found, skipping", tid)
                return

            # Check cancellation before starting
            if task.cancel_requested_at is not None:
                await orch.handle_cancellation(task)
                await session.commit()
                return

            result = await orch.run_pipeline(task, config=cfg)
            await session.commit()

        logger.info(
            "Ingestion task %s completed: %d chunks, status=%s",
            task_id,
            result.chunk_count,
            result.status,
        )

    except CancelledError:
        # Record cancellation in a fresh session (pipeline session is rolled back)
        await _record_cancellation(tid)
        return  # Don't re-raise — cancellation is intentional

    except Exception as exc:
        # Record error in a fresh session (pipeline session is rolled back)
        await _record_error(tid, exc)
        raise  # Re-raise so Dramatiq can retry


async def _record_cancellation(task_id: UUID) -> None:
    """Record a clean cancellation in the database."""
    async with database.session() as session:
        repo = IngestionTaskRepository(session)
        task = await repo.get(task_id)
        if task is None:
            return
        await repo.update(
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
                heartbeat_at=task.heartbeat_at,
                lease_expires_at=task.lease_expires_at,
                error_code=None,
                error=None,
                created_at=task.created_at,
            )
        )
        await session.commit()


async def _record_error(task_id: UUID, exc: Exception) -> None:
    """Record a pipeline error and increment retry count."""
    error_code = _classify_error(exc)
    error_msg = str(exc)[:2000]

    async with database.session() as session:
        repo = IngestionTaskRepository(session)
        task = await repo.get(task_id)
        if task is None:
            return

        new_retry = task.retry_count + 1
        new_status = TaskStatus.FAILED if new_retry > task.max_retries else TaskStatus.RUNNING

        logger.error(
            "Ingestion task %s error (retry %d/%d): code=%s msg=%s",
            task_id,
            new_retry,
            task.max_retries,
            error_code,
            error_msg[:200],
        )

        await repo.update(
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
                heartbeat_at=task.heartbeat_at,
                lease_expires_at=task.lease_expires_at,
                error_code=error_code,
                error=error_msg,
                created_at=task.created_at,
            )
        )
        await session.commit()


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


def enqueue_ingestion_task(*, task_id: str, trace_id: str) -> dramatiq.Message[None]:
    """Enqueue an ingestion task and record a producer-side event."""
    canonical_trace_id = normalize_trace_id(trace_id)
    if canonical_trace_id is None:
        raise ValueError("Invalid trace ID")

    message = ingestion_task.send(task_id=task_id, trace_id=canonical_trace_id)
    logger.info(
        "ingestion_task_enqueued",
        extra={
            "message_id": message.message_id,
            "task_id": task_id,
            "trace_id": canonical_trace_id,
        },
    )
    return message
