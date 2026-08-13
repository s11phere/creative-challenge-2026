"""Worker-side auto-extraction of repeated work patterns into personal-Skill drafts.

Phase 6 (Path B): mine strong usage patterns, draft candidate Skills via the
Phase 4 creator mechanism, and keep only the candidates that pass the dual gate
(Phase 1 structural eval + historical exemplar anchoring). Nothing is activated
automatically — the pipeline only ever creates user-facing drafts.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, TypedDict

import dramatiq
from application.skills import (
    DraftSkillEvalRunner,
    PatternExtractionService,
    PersonalSkillStore,
    SkillDraftStore,
)
from infrastructure.config import settings
from infrastructure.database import Database
from infrastructure.qa_execution import assistant_skill_registry
from infrastructure.skill_lifecycle import PostgresSkillActivationStore
from infrastructure.telemetry_context import (
    bind_observability_context,
    new_trace_id,
    normalize_trace_id,
    trace_parent_context,
)
from infrastructure.usage_traces import PostgresUsageTraceRepository
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from sqlalchemy.pool import NullPool

from worker.broker import broker

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("worker.skill_extraction")
database = Database(settings.database_url, poolclass=NullPool)

_THROTTLE_KEY = "personalization:skill_extraction:due"


class SkillExtractionResult(TypedDict):
    event_version: int
    candidates: int
    created_drafts: list[str]
    rejected: list[dict[str, str | None]]
    skipped: list[str]


def extract_skill_patterns_sync() -> SkillExtractionResult:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_extract_async())
    finally:
        loop.close()


async def _extract_async() -> SkillExtractionResult:
    registry = assistant_skill_registry()
    personal_store = PersonalSkillStore(
        registry=registry, store=PostgresSkillActivationStore(database)
    )
    drafts = SkillDraftStore(
        registry=registry,
        personal_store=personal_store,
        eval_runner=DraftSkillEvalRunner(registry=registry),
    )
    service = PatternExtractionService(
        traces=PostgresUsageTraceRepository(database),
        drafts=drafts,
        existing_names=lambda: frozenset(registry.names()).union(frozenset(registry.draft_names())),
    )
    result = await service.extract()
    return SkillExtractionResult(
        event_version=1,
        candidates=result.candidates,
        created_drafts=list(result.created_drafts),
        rejected=[
            {"name": item.name, "reason": item.reason, "detail": item.detail}
            for item in result.rejected
        ],
        skipped=list(result.skipped),
    )


@dramatiq.actor(
    broker=broker,
    actor_name="skill_pattern_extract",
    queue_name="usage_traces",
    max_retries=settings.skill_extraction_max_retries,
    time_limit=settings.skill_extraction_timeout_ms,
    notify_shutdown=True,
)
def skill_pattern_extract(*, trace_id: str, event_version: int) -> SkillExtractionResult:
    """Mine usage patterns and keep dual-gated candidate Skill drafts."""
    canonical_trace_id = normalize_trace_id(trace_id)
    if canonical_trace_id is None:
        raise ValueError("Invalid trace ID")
    if event_version != 1:
        raise ValueError("Unsupported Skill extraction event version")
    with (
        tracer.start_as_current_span(
            "skill_pattern_extract.process",
            context=trace_parent_context(canonical_trace_id),
            kind=SpanKind.CONSUMER,
            attributes={"messaging.system": "redis", "messaging.operation.name": "process"},
        ),
        bind_observability_context(trace_id=canonical_trace_id),
    ):
        result = extract_skill_patterns_sync()
        logger.info(
            "skill_pattern_extract_completed",
            extra={
                "event_version": event_version,
                "candidates": result["candidates"],
                "created_drafts": result["created_drafts"],
                "rejected": len(result["rejected"]),
            },
        )
        return result


def maybe_enqueue_skill_pattern_extract(*, throttle_seconds: int | None = None) -> bool:
    """Best-effort, throttled scheduling after a finished turn (Phase 6).

    Returns ``True`` when a ``skill_pattern_extract`` message was enqueued. A Redis
    SETNX-with-TTL key bounds runs (default 30 minutes) so a burst of turns triggers
    at most one extraction window. Redis or enqueue failures are logged and treated
    as "not enqueued" — never breaking the turn path.
    """
    throttle = throttle_seconds or settings.skill_extraction_throttle_seconds
    try:
        acquired = broker.client.set(_THROTTLE_KEY, "1", nx=True, ex=throttle)
    except Exception:
        logger.exception("skill_extraction_throttle_unavailable")
        return False
    if not acquired:
        return False
    try:
        enqueue_skill_pattern_extract()
        return True
    except Exception:
        logger.exception("skill_extraction_enqueue_failed")
        return False


def enqueue_skill_pattern_extract(
    *, trace_id: str | None = None, event_version: int = 1
) -> dramatiq.Message[Any]:
    canonical_trace_id = normalize_trace_id(trace_id) if trace_id is not None else new_trace_id()
    if canonical_trace_id is None:
        raise ValueError("Invalid trace ID")
    if event_version != 1:
        raise ValueError("Unsupported Skill extraction event version")
    with (
        tracer.start_as_current_span(
            "skill_pattern_extract.enqueue",
            context=trace_parent_context(canonical_trace_id),
            kind=SpanKind.PRODUCER,
            attributes={"messaging.system": "redis", "messaging.operation.name": "send"},
        ),
        bind_observability_context(trace_id=canonical_trace_id),
    ):
        message = skill_pattern_extract.send(
            trace_id=canonical_trace_id,
            event_version=event_version,
        )
        logger.info(
            "skill_pattern_extract_enqueued",
            extra={"message_id": message.message_id, "event_version": event_version},
        )
        return message


__all__ = [
    "SkillExtractionResult",
    "enqueue_skill_pattern_extract",
    "extract_skill_patterns_sync",
    "maybe_enqueue_skill_pattern_extract",
    "skill_pattern_extract",
]
