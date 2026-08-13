"""Worker-side cross-session memory distillation hook (personalization Phase 5)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, TypedDict

import dramatiq
from application.memory import MemoryDistiller
from infrastructure.config import settings
from infrastructure.database import Database
from infrastructure.memory_entries import (
    GatewayTextEmbedder,
    PostgresMemoryDistillationSource,
    PostgresMemoryEntryRepository,
)
from infrastructure.telemetry_context import (
    bind_observability_context,
    new_trace_id,
    normalize_trace_id,
    trace_parent_context,
)
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from sqlalchemy.pool import NullPool

from worker.broker import broker

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("worker.memory")
database = Database(settings.database_url, poolclass=NullPool)


class MemoryDistillResult(TypedDict):
    event_version: int
    candidates: int
    inserted: int
    updated: int
    merged: int
    persisted: int


def distill_memories_sync() -> MemoryDistillResult:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_distill_async())
    finally:
        loop.close()


async def _distill_async() -> MemoryDistillResult:
    from worker.qa_tasks import _create_gateway

    gateway = _create_gateway()
    try:
        result = await MemoryDistiller(
            source=PostgresMemoryDistillationSource(database),
            entries=PostgresMemoryEntryRepository(database),
            gateway=gateway,
            embedder=GatewayTextEmbedder(gateway),
        ).distill()
    finally:
        await gateway.aclose()
    return MemoryDistillResult(
        event_version=1,
        candidates=result.candidates,
        inserted=result.inserted,
        updated=result.updated,
        merged=result.merged,
        persisted=result.persisted,
    )


@dramatiq.actor(
    broker=broker,
    actor_name="memory_distill",
    queue_name="usage_traces",
    max_retries=settings.memory_distill_max_retries,
    time_limit=settings.memory_distill_timeout_ms,
    notify_shutdown=True,
)
def memory_distill(*, trace_id: str, event_version: int) -> MemoryDistillResult:
    """Distill durable memory entries from summaries and usage patterns."""
    canonical_trace_id = normalize_trace_id(trace_id)
    if canonical_trace_id is None:
        raise ValueError("Invalid trace ID")
    if event_version != 1:
        raise ValueError("Unsupported memory distill event version")
    with (
        tracer.start_as_current_span(
            "memory_distill.process",
            context=trace_parent_context(canonical_trace_id),
            kind=SpanKind.CONSUMER,
            attributes={"messaging.system": "redis", "messaging.operation.name": "process"},
        ),
        bind_observability_context(trace_id=canonical_trace_id),
    ):
        result = distill_memories_sync()
        logger.info(
            "memory_distill_completed",
            extra={
                "event_version": event_version,
                "candidates": result["candidates"],
                "persisted": result["persisted"],
            },
        )
        return result


def enqueue_memory_distill(
    *, trace_id: str | None = None, event_version: int = 1
) -> dramatiq.Message[Any]:
    canonical_trace_id = normalize_trace_id(trace_id) if trace_id is not None else new_trace_id()
    if canonical_trace_id is None:
        raise ValueError("Invalid trace ID")
    if event_version != 1:
        raise ValueError("Unsupported memory distill event version")
    with (
        tracer.start_as_current_span(
            "memory_distill.enqueue",
            context=trace_parent_context(canonical_trace_id),
            kind=SpanKind.PRODUCER,
            attributes={"messaging.system": "redis", "messaging.operation.name": "send"},
        ),
        bind_observability_context(trace_id=canonical_trace_id),
    ):
        message = memory_distill.send(
            trace_id=canonical_trace_id,
            event_version=event_version,
        )
        logger.info(
            "memory_distill_enqueued",
            extra={"message_id": message.message_id, "event_version": event_version},
        )
        return message


__all__ = [
    "MemoryDistillResult",
    "distill_memories_sync",
    "enqueue_memory_distill",
    "memory_distill",
]
