"""Worker-side usage-trace capture and pattern distillation hooks."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, TypedDict
from uuid import UUID

import dramatiq
from application.usage_traces import (
    UsagePatternService,
    UsageTraceRecorder,
)
from domain.usage_traces import UsagePatternSnapshot
from infrastructure.agent_events import PostgresAgentRunEventStore
from infrastructure.config import settings
from infrastructure.conversation_runs import PostgresConversationRunRepository
from infrastructure.database import Database
from infrastructure.qa_persistence import PostgresGroundedQARepository
from infrastructure.telemetry_context import (
    bind_observability_context,
    new_trace_id,
    normalize_trace_id,
    trace_parent_context,
)
from infrastructure.usage_traces import PostgresUsagePatternRepository, PostgresUsageTraceRepository
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from sqlalchemy.pool import NullPool

from worker.broker import broker

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("worker.usage_traces")
database = Database(settings.database_url, poolclass=NullPool)


async def record_usage_trace(run_id: UUID) -> None:
    """Best-effort trace capture; failures are logged, never break the Run path."""
    try:
        recorder = UsageTraceRecorder(
            runs=PostgresConversationRunRepository(database),
            data=PostgresGroundedQARepository(database),
            qa=PostgresGroundedQARepository(database),
            agent_events=PostgresAgentRunEventStore(database),
            traces=PostgresUsageTraceRepository(database),
        )
        await recorder.record_run(run_id)
    except Exception:
        logger.exception("usage_trace_record_failed", extra={"run_id": str(run_id)})


class DistillResult(TypedDict):
    event_version: int
    pattern_count: int


def distill_usage_patterns_sync() -> tuple[UsagePatternSnapshot, ...]:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_distill_async())
    finally:
        loop.close()


async def _distill_async() -> tuple[UsagePatternSnapshot, ...]:
    service = UsagePatternService(
        traces=PostgresUsageTraceRepository(database),
        patterns=PostgresUsagePatternRepository(database),
    )
    return await service.distill_all()


@dramatiq.actor(
    broker=broker,
    actor_name="usage_patterns_distill",
    queue_name="usage_traces",
    max_retries=settings.usage_pattern_distill_max_retries,
    time_limit=settings.usage_pattern_distill_timeout_ms,
    notify_shutdown=True,
)
def usage_patterns_distill(*, trace_id: str, event_version: int) -> DistillResult:
    """Recompute the persisted usage-pattern snapshot from raw traces."""
    canonical_trace_id = normalize_trace_id(trace_id)
    if canonical_trace_id is None:
        raise ValueError("Invalid trace ID")
    if event_version != 1:
        raise ValueError("Unsupported usage pattern distill event version")
    with (
        tracer.start_as_current_span(
            "usage_patterns_distill.process",
            context=trace_parent_context(canonical_trace_id),
            kind=SpanKind.CONSUMER,
            attributes={"messaging.system": "redis", "messaging.operation.name": "process"},
        ),
        bind_observability_context(trace_id=canonical_trace_id),
    ):
        patterns = distill_usage_patterns_sync()
        logger.info(
            "usage_patterns_distill_completed",
            extra={"event_version": event_version, "pattern_count": len(patterns)},
        )
        return DistillResult(event_version=event_version, pattern_count=len(patterns))


def enqueue_usage_pattern_distill(
    *, trace_id: str | None = None, event_version: int = 1
) -> dramatiq.Message[Any]:
    canonical_trace_id = normalize_trace_id(trace_id) if trace_id is not None else new_trace_id()
    if canonical_trace_id is None:
        raise ValueError("Invalid trace ID")
    if event_version != 1:
        raise ValueError("Unsupported usage pattern distill event version")
    with (
        tracer.start_as_current_span(
            "usage_patterns_distill.enqueue",
            context=trace_parent_context(canonical_trace_id),
            kind=SpanKind.PRODUCER,
            attributes={"messaging.system": "redis", "messaging.operation.name": "send"},
        ),
        bind_observability_context(trace_id=canonical_trace_id),
    ):
        message = usage_patterns_distill.send(
            trace_id=canonical_trace_id,
            event_version=event_version,
        )
        logger.info(
            "usage_patterns_distill_enqueued",
            extra={"message_id": message.message_id, "event_version": event_version},
        )
        return message


__all__ = [
    "DistillResult",
    "distill_usage_patterns_sync",
    "enqueue_usage_pattern_distill",
    "record_usage_trace",
    "usage_patterns_distill",
]
