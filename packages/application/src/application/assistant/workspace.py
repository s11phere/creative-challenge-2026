"""Conversation-scoped Agent workspace selection."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from domain.conversation_run import ConversationRun, ConversationRunStatus
from domain.qa_persistence import ConversationRecord


class ConversationWorkspaceError(ValueError):
    """Safe application errors for workspace selection."""


class WorkspacePathResolver(Protocol):
    def select(self, requested_path: str) -> WorkspacePathSelection: ...


class WorkspacePathSelection(Protocol):
    @property
    def path(self) -> str: ...


class ConversationWorkspaceRepository(Protocol):
    async def get_conversation(self, conversation_id: UUID) -> ConversationRecord | None: ...

    async def set_workspace_path(
        self, conversation_id: UUID, workspace_path: str | None
    ) -> ConversationRecord: ...


type RunLister = Callable[[UUID], Awaitable[tuple[ConversationRun, ...]]]

_ACTIVE_STATUSES = frozenset(
    {
        ConversationRunStatus.CREATED,
        ConversationRunStatus.QUEUED,
        ConversationRunStatus.RUNNING,
        ConversationRunStatus.WAITING_CLARIFICATION,
        ConversationRunStatus.WAITING_APPROVAL,
        ConversationRunStatus.CANCEL_REQUESTED,
    }
)


@dataclass(frozen=True)
class ConversationWorkspace:
    """Safe, client-visible logical workspace metadata."""

    path: str | None


class ConversationWorkspaceService:
    """Set a durable workspace only between executable conversation turns."""

    def __init__(
        self,
        *,
        conversations: ConversationWorkspaceRepository,
        resolve_path: WorkspacePathResolver,
        list_runs: RunLister,
    ) -> None:
        self._conversations = conversations
        self._resolve_path = resolve_path
        self._list_runs = list_runs

    async def get(self, conversation_id: UUID) -> ConversationWorkspace:
        conversation = await self._conversations.get_conversation(conversation_id)
        if conversation is None or conversation.archived_at is not None:
            raise ConversationWorkspaceError("CONVERSATION_NOT_FOUND")
        return ConversationWorkspace(path=conversation.workspace_path)

    async def select(self, conversation_id: UUID, requested_path: str) -> ConversationWorkspace:
        conversation = await self._conversations.get_conversation(conversation_id)
        if conversation is None or conversation.archived_at is not None:
            raise ConversationWorkspaceError("CONVERSATION_NOT_FOUND")
        if any(run.status in _ACTIVE_STATUSES for run in await self._list_runs(conversation_id)):
            raise ConversationWorkspaceError("WORKSPACE_RUN_ACTIVE")
        try:
            selection = self._resolve_path.select(requested_path)
            path = selection.path
        except ValueError as exc:
            raise ConversationWorkspaceError("WORKSPACE_PATH_DENIED") from exc
        if not isinstance(path, str) or len(path) > 1024:
            raise ConversationWorkspaceError("WORKSPACE_PATH_DENIED")
        updated = await self._conversations.set_workspace_path(conversation_id, path)
        return ConversationWorkspace(path=updated.workspace_path)


__all__ = [
    "ConversationWorkspace",
    "ConversationWorkspaceError",
    "ConversationWorkspaceService",
    "WorkspacePathResolver",
]
