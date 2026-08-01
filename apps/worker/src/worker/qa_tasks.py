"""Dramatiq actor for durable, body-free Grounded QA execution."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from uuid import UUID, uuid4

import dramatiq
from domain.grounded_qa import QAStatus
from infrastructure.config import settings
from infrastructure.database import Database
from infrastructure.qa_execution import GroundedQAExecutor
from infrastructure.qa_persistence import PostgresGroundedQARepository, PostgresQAEventStore
from infrastructure.telemetry_context import (
    bind_observability_context,
    new_trace_id,
    normalize_trace_id,
    trace_parent_context,
)
from model_gateway import GatewayConfig, ModelGateway, ModelProvider, create_model_gateway
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from sqlalchemy.pool import NullPool

from worker.broker import broker

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("worker.qa")
database = Database(settings.database_url, poolclass=NullPool)
_TERMINAL = frozenset(
    {QAStatus.COMPLETED, QAStatus.REFUSED, QAStatus.FAILED, QAStatus.CANCELLED, QAStatus.TIMED_OUT}
)


def _create_gateway() -> ModelGateway:
    return create_model_gateway(
        GatewayConfig(
            provider=ModelProvider(settings.model_provider),
            endpoint=settings.model_endpoint,
            api_key=(settings.model_api_key.get_secret_value() if settings.model_api_key else None),
            fast_chat_endpoint=settings.fast_chat_endpoint,
            fast_chat_api_key=(
                settings.fast_chat_api_key.get_secret_value()
                if settings.fast_chat_api_key
                else None
            ),
            fast_chat_model=settings.fast_chat_model,
            embedding_endpoint=settings.embedding_endpoint,
            embedding_api_key=(
                settings.embedding_api_key.get_secret_value()
                if settings.embedding_api_key
                else None
            ),
            embedding_model=settings.embedding_model,
            reranker_endpoint=settings.reranker_endpoint,
            reranker_api_key=(
                settings.reranker_api_key.get_secret_value() if settings.reranker_api_key else None
            ),
            reranker_model=settings.reranker_model,
            embedding_protocol=settings.embedding_protocol,
            fake_embedding=settings.embedding_provider == "fake",
            fake_reranker=settings.reranker_provider == "fake",
            allow_external=settings.model_allow_external,
            timeout_seconds=settings.model_timeout_seconds,
            max_retries=settings.model_max_retries,
            retry_backoff_seconds=settings.model_retry_backoff_seconds,
        )
    )


@dramatiq.actor(
    broker=broker,
    actor_name="qa_run",
    queue_name="qa",
    max_retries=settings.qa_task_max_retries,
    time_limit=settings.qa_task_timeout_ms,
    notify_shutdown=True,
)
def qa_run(*, run_id: str, trace_id: str, event_version: int) -> None:
    """Execute one persisted QA run; the message contains control metadata only."""
    uid = UUID(run_id)
    if event_version != 1:
        raise ValueError("Unsupported QA task event version")
    canonical_trace_id = normalize_trace_id(trace_id)
    if canonical_trace_id is None:
        raise ValueError("Invalid trace ID")

    with (
        tracer.start_as_current_span(
            "qa_run.process",
            context=trace_parent_context(canonical_trace_id),
            kind=SpanKind.CONSUMER,
            attributes={
                "messaging.system": "redis",
                "messaging.operation.name": "process",
                "qa.run_id": run_id,
            },
        ),
        bind_observability_context(trace_id=canonical_trace_id, task_id=run_id),
    ):
        if not _run_qa_sync(uid, canonical_trace_id):
            raise dramatiq.Retry(
                message="QA attempt lease is active",
                delay=settings.qa_task_retry_delay_ms,
            )


def _run_qa_sync(run_id: UUID, trace_id: str) -> bool:
    loop = asyncio.new_event_loop()
    gateway = _create_gateway()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_run_qa_async(run_id, trace_id, gateway))
    finally:
        try:
            loop.run_until_complete(gateway.aclose())
        finally:
            loop.close()


async def _run_qa_async(run_id: UUID, trace_id: str, gateway: ModelGateway) -> bool:
    repository = PostgresGroundedQARepository(database)
    lease_owner = str(uuid4())
    claimed = await repository.claim_run(
        run_id,
        lease_owner=lease_owner,
        lease_seconds=settings.qa_task_lease_seconds,
    )
    if claimed is None:
        return False
    if claimed.status in _TERMINAL:
        return True

    stop = asyncio.Event()
    lease_lost = asyncio.Event()
    heartbeat = asyncio.create_task(
        _heartbeat(repository, run_id, lease_owner, stop, lease_lost),
        name=f"qa-heartbeat-{run_id}",
    )
    try:
        executor = GroundedQAExecutor(
            database=database,
            gateway=gateway,
            repository=repository,
            events=PostgresQAEventStore(database),
        )
        execution = asyncio.create_task(
            executor.execute(run_id, trace_id=trace_id),
            name=f"qa-execution-{run_id}",
        )
        return await _wait_for_execution(execution, lease_lost, run_id=run_id)
    finally:
        stop.set()
        await heartbeat
        await repository.release_run_lease(run_id, lease_owner=lease_owner)


async def _wait_for_execution(
    execution: asyncio.Task[object], lease_lost: asyncio.Event, *, run_id: UUID
) -> bool:
    lease_guard = asyncio.create_task(
        lease_lost.wait(),
        name=f"qa-lease-guard-{run_id}",
    )
    done, _pending = await asyncio.wait(
        {execution, lease_guard}, return_when=asyncio.FIRST_COMPLETED
    )
    if lease_guard in done and lease_lost.is_set() and not execution.done():
        execution.cancel()
        with suppress(asyncio.CancelledError):
            await execution
        return False
    lease_guard.cancel()
    with suppress(asyncio.CancelledError):
        await lease_guard
    await execution
    return True


async def _heartbeat(
    repository: PostgresGroundedQARepository,
    run_id: UUID,
    lease_owner: str,
    stop: asyncio.Event,
    lease_lost: asyncio.Event,
) -> None:
    interval = min(settings.qa_task_heartbeat_interval_s, settings.qa_task_lease_seconds / 2)
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except TimeoutError:
            renewed = await repository.renew_run_lease(
                run_id,
                lease_owner=lease_owner,
                lease_seconds=settings.qa_task_lease_seconds,
            )
            if not renewed:
                lease_lost.set()
                return


def enqueue_qa_run(*, run_id: str, trace_id: str, event_version: int = 1) -> dramatiq.Message[None]:
    UUID(run_id)
    canonical_trace_id = normalize_trace_id(trace_id)
    if canonical_trace_id is None:
        raise ValueError("Invalid trace ID")
    if event_version != 1:
        raise ValueError("Unsupported QA task event version")
    message = qa_run.send(
        run_id=run_id,
        trace_id=canonical_trace_id,
        event_version=event_version,
    )
    logger.info(
        "qa_run_enqueued",
        extra={"message_id": message.message_id, "run_id": run_id, "trace_id": canonical_trace_id},
    )
    return message


def recover_qa_runs_sync() -> tuple[UUID, ...]:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        run_ids = loop.run_until_complete(PostgresGroundedQARepository(database).prepare_recovery())
    finally:
        loop.close()
    for run_id in run_ids:
        enqueue_qa_run(run_id=str(run_id), trace_id=new_trace_id(), event_version=1)
    if run_ids:
        logger.info("qa_runs_recovered", extra={"run_count": len(run_ids)})
    return run_ids


__all__ = ["enqueue_qa_run", "qa_run", "recover_qa_runs_sync"]
