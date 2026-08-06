"""Projection of an Assistant Skill selection into the existing QA Run/Worker path."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol
from uuid import UUID

from application.assistant import SkillProjectionPort
from domain.conversation_run import ConversationRun, ConversationRunRepository, FixedSkillIdentity
from domain.grounded_qa import QAAttempt, QAEvent, QAStatus
from domain.qa_persistence import (
    GroundedQARepository,
    QARetrievalScope,
    QARunRecord,
    QARunVersions,
)


class QARunStarter(Protocol):
    def __call__(self, run_id: UUID) -> bool: ...


class AssistantQASkillProjection(SkillProjectionPort):
    def __init__(
        self,
        *,
        repository: GroundedQARepository,
        parent_runs: ConversationRunRepository,
        versions: Callable[[str], Awaitable[QARunVersions]],
        start: QARunStarter,
    ) -> None:
        self._repository = repository
        self._parent_runs = parent_runs
        self._versions = versions
        self._start = start

    async def create(
        self,
        run: ConversationRun,
        *,
        skill: FixedSkillIdentity,
        arguments: Mapping[str, object],
        resource_scope: object | None,
    ) -> ConversationRun:
        _ = arguments
        versions = await self._versions(skill.name)
        scope = (
            resource_scope
            if isinstance(resource_scope, QARetrievalScope)
            else QARetrievalScope()
        )
        qa_run = QARunRecord(
            run_id=run.run_id,
            attempt=QAAttempt(run_id=run.run_id),
            conversation_id=run.conversation_id,
            question_message_id=run.user_message_id,
            space_id=run.space_id,
            caller_id=run.caller_id,
            idempotency_key=run.idempotency_key,
            versions=versions,
            retrieval_scope=scope,
        )
        created = await self._repository.create_run(qa_run)
        if created.status is QAStatus.CREATED:
            created = await self._repository.transition_run(created.run_id, QAEvent.QUEUE)
        self._start(created.run_id)
        parent = await self._parent_runs.get_conversation_run(created.run_id)
        if parent is None:
            raise ValueError("RUN_AGENT_DECISION_INVALID")
        return parent


__all__ = ["AssistantQASkillProjection"]
