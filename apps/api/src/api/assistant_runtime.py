"""API-side dispatcher for durable product-level Assistant Worker execution."""

from __future__ import annotations

import logging
from collections.abc import Callable
from uuid import UUID

from domain.conversation_run import ConversationRunRepository
from infrastructure.telemetry_context import new_trace_id

logger = logging.getLogger(__name__)

AssistantEnqueuer = Callable[..., object]


class AssistantWorkerDispatcher:
    """Dispatch Assistant control metadata to the existing QA queue only."""

    def __init__(
        self,
        *,
        repository: ConversationRunRepository,
        enqueuer: AssistantEnqueuer | None = None,
    ) -> None:
        self._repository = repository
        self._enqueuer = enqueuer

    def start(self, run_id: UUID) -> bool:
        try:
            self._enqueue(run_id)
        except Exception:
            logger.exception("assistant_run_enqueue_failed", extra={"run_id": str(run_id)})
            return False
        return True

    async def recover(self) -> tuple[UUID, ...]:
        run_ids = (
            *await self._repository.prepare_assistant_recovery(),
            *await self._repository.prepare_context_compaction_recovery(),
        )
        for run_id in run_ids:
            self.start(run_id)
        return run_ids

    async def recover_one(self, run_id: UUID) -> bool:
        run_ids = (
            *await self._repository.prepare_assistant_recovery(),
            *await self._repository.prepare_context_compaction_recovery(),
        )
        if run_id not in run_ids:
            return False
        return self.start(run_id)

    def _enqueue(self, run_id: UUID) -> object:
        enqueuer = self._enqueuer
        if enqueuer is None:
            from worker.assistant_tasks import enqueue_assistant_run

            enqueuer = enqueue_assistant_run
        return enqueuer(run_id=str(run_id), trace_id=new_trace_id(), event_version=2)


__all__ = ["AssistantWorkerDispatcher"]
