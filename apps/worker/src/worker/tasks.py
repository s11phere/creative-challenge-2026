"""Body-free diagnostic tasks used to verify Worker delivery semantics."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, TypedDict
from uuid import UUID

import dramatiq
from infrastructure.config import settings
from infrastructure.queue import create_redis_broker
from infrastructure.telemetry import configure_observability
from infrastructure.telemetry_context import (
    bind_observability_context,
    new_trace_id,
    normalize_trace_id,
    trace_parent_context,
)
from opentelemetry import trace
from opentelemetry.trace import SpanKind

logger = logging.getLogger(__name__)

broker = create_redis_broker(settings.redis_url)
dramatiq.set_broker(broker)
tracer = trace.get_tracer("worker.tasks")


def setup_worker() -> None:
    """Configure process-local observability when Dramatiq imports the broker."""
    configure_observability(settings, service_name="worker")


class DiagnosticResult(TypedDict):
    task_id: str
    trace_id: str
    event_version: int
    count: int
    requested_at: str


def process_diagnostic_task(
    *,
    task_id: str,
    trace_id: str,
    event_version: int,
    count: int,
    requested_at: str,
) -> DiagnosticResult:
    """Validate metadata and return a deterministic, side-effect-free result."""
    UUID(task_id)
    UUID(trace_id)
    if event_version != 1:
        raise ValueError("Unsupported diagnostic event version")
    if count < 0:
        raise ValueError("Diagnostic count must be non-negative")
    parsed_requested_at = datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
    if parsed_requested_at.utcoffset() is None:
        raise ValueError("Diagnostic requested_at must include a timezone")
    return DiagnosticResult(
        task_id=task_id,
        trace_id=trace_id,
        event_version=event_version,
        count=count,
        requested_at=requested_at,
    )


@dramatiq.actor(
    broker=broker,
    actor_name="diagnostic_task_permanently_failed",
    queue_name="diagnostics",
    max_retries=0,
)
def diagnostic_task_permanently_failed(
    message_data: dict[str, Any], retry_data: dict[str, Any]
) -> None:
    """Record bounded-retry exhaustion without logging payload bodies."""
    kwargs = message_data.get("kwargs", {})
    task_id = str(kwargs.get("task_id", ""))
    trace_id = normalize_trace_id(str(kwargs.get("trace_id", ""))) or new_trace_id()
    with (
        tracer.start_as_current_span(
            "diagnostic_task.permanently_failed",
            context=trace_parent_context(trace_id),
            kind=SpanKind.CONSUMER,
        ),
        bind_observability_context(trace_id=trace_id, task_id=task_id),
    ):
        logger.error(
            "diagnostic_task_permanently_failed",
            extra={
                "task_id": task_id,
                "trace_id": trace_id,
                "event_version": kwargs.get("event_version"),
                "retries": retry_data.get("retries"),
                "max_retries": retry_data.get("max_retries"),
            },
        )


@dramatiq.actor(
    broker=broker,
    actor_name="diagnostic_task",
    queue_name="diagnostics",
    max_retries=settings.diagnostic_task_max_retries,
    min_backoff=settings.diagnostic_task_min_backoff_ms,
    time_limit=settings.diagnostic_task_timeout_ms,
    notify_shutdown=True,
    on_retry_exhausted="diagnostic_task_permanently_failed",
)
def diagnostic_task(
    *,
    task_id: str,
    trace_id: str,
    event_version: int,
    count: int,
    requested_at: str,
) -> DiagnosticResult:
    """Process diagnostic metadata; no document or prompt content is accepted."""
    canonical_trace_id = normalize_trace_id(trace_id) or new_trace_id()
    with (
        tracer.start_as_current_span(
            "diagnostic_task.process",
            context=trace_parent_context(canonical_trace_id),
            kind=SpanKind.CONSUMER,
            attributes={"messaging.system": "redis", "messaging.operation.name": "process"},
        ),
        bind_observability_context(trace_id=canonical_trace_id, task_id=task_id),
    ):
        logger.info(
            "diagnostic_task_started",
            extra={"event_version": event_version},
        )
        result = process_diagnostic_task(
            task_id=task_id,
            trace_id=trace_id,
            event_version=event_version,
            count=count,
            requested_at=requested_at,
        )
        logger.info(
            "diagnostic_task_completed",
            extra={
                "event_version": event_version,
                "count": count,
                "requested_at": requested_at,
            },
        )
        return result


def enqueue_diagnostic_task(
    *,
    task_id: str,
    trace_id: str,
    event_version: int,
    count: int,
    requested_at: str,
) -> dramatiq.Message[DiagnosticResult]:
    """Enqueue diagnostic metadata and record a producer-side correlation event."""
    canonical_trace_id = normalize_trace_id(trace_id)
    if canonical_trace_id is None:
        raise ValueError("Invalid trace ID")
    with (
        tracer.start_as_current_span(
            "diagnostic_task.enqueue",
            context=trace_parent_context(canonical_trace_id),
            kind=SpanKind.PRODUCER,
            attributes={"messaging.system": "redis", "messaging.operation.name": "send"},
        ),
        bind_observability_context(trace_id=canonical_trace_id, task_id=task_id),
    ):
        message = diagnostic_task.send(
            task_id=task_id,
            trace_id=canonical_trace_id,
            event_version=event_version,
            count=count,
            requested_at=requested_at,
        )
        logger.info(
            "diagnostic_task_enqueued",
            extra={"message_id": message.message_id, "event_version": event_version},
        )
        return message
