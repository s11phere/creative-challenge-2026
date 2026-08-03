"""API-side dispatch adapter for durable Grounded QA Worker execution."""

from __future__ import annotations

import logging
from collections.abc import Callable
from uuid import UUID

from agent_runtime import FileSystemSkillRegistry
from application.skills import SkillLifecycleService
from domain.qa_persistence import GroundedQARepository, QARunVersions
from infrastructure.qa_execution import qa_execution_versions
from infrastructure.telemetry_context import new_trace_id

logger = logging.getLogger(__name__)


QAEnqueuer = Callable[..., object]


class QAWorkerDispatcher:
    """Dispatch IDs only; PostgreSQL remains the execution and recovery authority."""

    def __init__(
        self,
        *,
        repository: GroundedQARepository,
        skill_registry: FileSystemSkillRegistry | None = None,
        skill_lifecycle: SkillLifecycleService | None = None,
        enqueuer: QAEnqueuer | None = None,
    ) -> None:
        self.versions = qa_execution_versions(skill_registry)
        self._skill_registry = skill_registry
        self._skill_lifecycle = skill_lifecycle
        self._repository = repository
        self._enqueuer = enqueuer

    async def current_versions(self, skill_name: str = "knowledge_qa") -> QARunVersions:
        if self._skill_lifecycle is not None:
            await self._skill_lifecycle.current(skill_name)
        return qa_execution_versions(self._skill_registry, skill_name=skill_name)

    def start(self, run_id: UUID) -> bool:
        try:
            self._enqueue(run_id)
        except Exception:
            logger.exception("qa_run_enqueue_failed", extra={"run_id": str(run_id)})
            return False
        return True

    async def recover(self) -> tuple[UUID, ...]:
        prepare = getattr(self._repository, "prepare_recovery", None)
        if prepare is None:
            return ()
        run_ids: tuple[UUID, ...] = await prepare()
        for run_id in run_ids:
            self.start(run_id)
        return run_ids

    async def recover_one(self, run_id: UUID) -> bool:
        """Requeue a single recoverable Run without creating another identity."""
        prepare = getattr(self._repository, "prepare_recovery", None)
        if prepare is None:
            return False
        run_ids: tuple[UUID, ...] = await prepare()
        if run_id not in run_ids:
            return False
        return self.start(run_id)

    def _enqueue(self, run_id: UUID) -> object:
        enqueuer = self._enqueuer
        if enqueuer is None:
            from worker.qa_tasks import enqueue_qa_run

            enqueuer = enqueue_qa_run
        return enqueuer(run_id=str(run_id), trace_id=new_trace_id(), event_version=1)


__all__ = ["QAWorkerDispatcher"]
