from __future__ import annotations

from pathlib import Path
from typing import cast
from uuid import UUID

import infrastructure.workspaces as workspaces
import pytest
from application.assistant import (
    ConversationRunService,
    ConversationWorkspaceError,
    ConversationWorkspaceService,
)
from application.assistant.runs import AssistantTurnSubmission
from application.qa import InMemoryGroundedQARepository
from domain.conversation_run import ConversationRun, ConversationRunStatus
from domain.qa_persistence import ConversationRecord
from infrastructure.workspaces import WorkspacePathError, WorkspaceRoot


def test_workspace_root_stores_a_logical_path_and_rejects_unsafe_selections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspaces"
    project = root / "project"
    nested = project / "src"
    nested.mkdir(parents=True)
    selector = WorkspaceRoot(root)

    assert selector.select("project").path == "project"
    assert selector.select(str(nested)).path == "project/src"
    assert selector.select(".").path == "."
    assert selector.resolve("project/src").root == nested.resolve()

    with pytest.raises(WorkspacePathError):
        selector.select("project/../project")
    with pytest.raises(WorkspacePathError):
        selector.select(str(tmp_path))
    with pytest.raises(WorkspacePathError):
        selector.resolve("../project")

    linked = root / "linked"
    (linked / "child").mkdir(parents=True)
    monkeypatch.setattr(workspaces, "_is_link_or_junction", lambda path: path.name == "linked")
    with pytest.raises(WorkspacePathError):
        selector.select("linked/child")


@pytest.mark.asyncio
async def test_workspace_service_persists_selection_and_rejects_active_runs(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    (root / "project").mkdir(parents=True)
    repository = InMemoryGroundedQARepository()
    conversation = ConversationRecord(
        conversation_id=UUID(int=701), space_id=UUID(int=702), owner_id="workspace-user"
    )
    await repository.create_conversation(conversation)
    service = ConversationWorkspaceService(
        conversations=repository,
        resolve_path=WorkspaceRoot(root),
        list_runs=repository.list_conversation_runs,
    )

    selected = await service.select(conversation.conversation_id, "project")
    assert selected.path == "project"
    assert (await service.get(conversation.conversation_id)).path == "project"
    stored = await repository.get_conversation(conversation.conversation_id)
    assert stored is not None and stored.workspace_path == "project"

    await ConversationRunService(conversations=repository, runs=repository).submit(
        AssistantTurnSubmission(
            conversation_id=conversation.conversation_id,
            content="Inspect the project.",
            idempotency_key="workspace-active-run",
        )
    )
    with pytest.raises(ConversationWorkspaceError, match="WORKSPACE_RUN_ACTIVE"):
        await service.select(conversation.conversation_id, ".")


@pytest.mark.asyncio
async def test_workspace_service_treats_clarification_as_an_active_run(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    (root / "project").mkdir(parents=True)
    repository = InMemoryGroundedQARepository()
    conversation = ConversationRecord(
        conversation_id=UUID(int=711), space_id=UUID(int=712), owner_id="workspace-user"
    )
    await repository.create_conversation(conversation)

    class ClarifyingRun:
        status = ConversationRunStatus.WAITING_CLARIFICATION

    async def list_clarifying_runs(_conversation_id: UUID) -> tuple[ConversationRun, ...]:
        return cast(tuple[ConversationRun, ...], (ClarifyingRun(),))

    service = ConversationWorkspaceService(
        conversations=repository,
        resolve_path=WorkspaceRoot(root),
        list_runs=list_clarifying_runs,
    )
    with pytest.raises(ConversationWorkspaceError, match="WORKSPACE_RUN_ACTIVE"):
        await service.select(conversation.conversation_id, "project")
