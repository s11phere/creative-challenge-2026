"""Application service for durable, non-model Assistant turn creation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from domain.conversation_run import (
    ConversationRun,
    ConversationRunKind,
    ConversationRunRepository,
    ConversationRunSelectionSource,
)
from domain.qa_persistence import ConversationRecord, MessageRecord, MessageRole
from domain.reasoning import ReasoningEffort, ReasoningProfile

from .reasoning import ReasoningProfileResolver


class ConversationReader(Protocol):
    async def get_conversation(self, conversation_id: UUID) -> ConversationRecord | None: ...


class ConversationRunApplicationError(ValueError):
    """Safe application-level error for a missing or archived conversation."""


@dataclass(frozen=True)
class AssistantTurnSubmission:
    conversation_id: UUID
    content: str
    idempotency_key: str
    selection_source: ConversationRunSelectionSource = ConversationRunSelectionSource.NONE

    def __post_init__(self) -> None:
        if not self.content.strip() or not self.idempotency_key.strip():
            raise ValueError("Assistant turns require content and an idempotency key")


class AssistantTurnApplicationPort(Protocol):
    async def submit(self, submission: AssistantTurnSubmission) -> ConversationRun: ...

    async def get(self, run_id: UUID) -> ConversationRun | None: ...

    async def cancel(self, run_id: UUID) -> ConversationRun: ...


class ConversationRunService:
    """Create a user message and shared parent Run without selecting a model or Skill."""

    def __init__(
        self,
        *,
        conversations: ConversationReader,
        runs: ConversationRunRepository,
        reasoning: ReasoningProfileResolver | None = None,
    ) -> None:
        self._conversations = conversations
        self._runs = runs
        self._reasoning = reasoning

    async def submit(self, submission: AssistantTurnSubmission) -> ConversationRun:
        conversation = await self._conversations.get_conversation(submission.conversation_id)
        if conversation is None or conversation.archived_at is not None:
            raise ConversationRunApplicationError("Conversation not found")
        now = max(datetime.now(UTC), conversation.updated_at + timedelta(microseconds=1))
        message = MessageRecord(
            message_id=uuid4(),
            conversation_id=conversation.conversation_id,
            space_id=conversation.space_id,
            role=MessageRole.USER,
            content=submission.content,
            idempotency_key=submission.idempotency_key,
            created_at=now,
        )
        run = ConversationRun(
            run_id=uuid4(),
            conversation_id=conversation.conversation_id,
            space_id=conversation.space_id,
            caller_id=conversation.owner_id,
            user_message_id=message.message_id,
            idempotency_key=submission.idempotency_key,
            selection_source=submission.selection_source,
            run_kind=ConversationRunKind.ASSISTANT_TURN,
            router_version="assistant-native-tool-use-v2",
            core_prompt_version="assistant-base-prompt-v8",
            reasoning_profile=self._resolve_reasoning(conversation),
            created_at=now,
            updated_at=now,
        )
        return await self._runs.create_turn(run, message)

    def _resolve_reasoning(self, conversation: ConversationRecord) -> ReasoningProfile:
        if self._reasoning is None:
            if conversation.reasoning_effort not in {ReasoningEffort.AUTO, ReasoningEffort.NONE}:
                raise ConversationRunApplicationError("Reasoning capability mapping is unavailable")
            return ReasoningProfile.unresolved(conversation.reasoning_effort)
        return self._reasoning.resolve(conversation.reasoning_effort)

    async def get(self, run_id: UUID) -> ConversationRun | None:
        return await self._runs.get_conversation_run(run_id)

    async def cancel(self, run_id: UUID) -> ConversationRun:
        return await self._runs.request_conversation_cancel(run_id)


__all__ = [
    "AssistantTurnApplicationPort",
    "AssistantTurnSubmission",
    "ConversationReader",
    "ConversationRunApplicationError",
    "ConversationRunService",
]
