from __future__ import annotations

from uuid import UUID

import pytest
from application.assistant import (
    AssistantCommandCatalog,
    AssistantCommandParser,
    AssistantCommandService,
    AssistantSkillInvocationService,
    AssistantTurnSubmission,
    CommandCatalogError,
    CommandParseError,
    ConversationContextMessage,
    ConversationContextSnapshot,
    ConversationRunService,
)
from application.qa import InMemoryGroundedQARepository
from application.skills import SkillInvocationView
from domain.conversation_context import ConversationSensitivity
from domain.conversation_run import (
    ConversationRun,
    ConversationRunSelectionSource,
    ConversationRunStatus,
)
from domain.qa_persistence import ConversationRecord, MessageRole
from infrastructure.qa_execution import assistant_skill_registry
from infrastructure.skill_catalog import FileSystemSkillCatalog


class FakeSkillCatalog:
    def __init__(self, *entries: SkillInvocationView) -> None:
        self.entries = entries

    def list_active_invocations(self) -> tuple[SkillInvocationView, ...]:
        return self.entries

    def list_skills(self) -> tuple[object, ...]:
        return ()

    def list_versions(self, _name: str) -> tuple[object, ...]:
        return ()


def skill(*, name: str = "knowledge_agent", command: str = "ask") -> SkillInvocationView:
    return SkillInvocationView(
        name=name,
        version="0.3.0",
        content_sha256="a" * 64,
        command=command,
        aliases=("qa",),
        description="Synthetic Skill.",
        argument_hint="<question>",
        input_mode="question",
    )


def test_parser_is_case_insensitive_and_preserves_escaped_text() -> None:
    catalog = AssistantCommandCatalog(FakeSkillCatalog(skill()))
    parser = AssistantCommandParser(catalog)

    parsed = parser.parse("  /QA\tWhat is retrieval?")
    assert parsed.descriptor is not None
    assert parsed.descriptor.name == "ask"
    assert parsed.arguments == {"question": "What is retrieval?"}
    escaped = parser.parse("//ask keep this literal")
    assert escaped.escaped is True
    assert escaped.content == "/ask keep this literal"


def test_unknown_command_returns_candidates_and_empty_skill_args_are_clarifiable() -> None:
    parser = AssistantCommandParser(AssistantCommandCatalog(FakeSkillCatalog(skill())))
    with pytest.raises(CommandParseError) as unknown:
        parser.parse("/as")
    assert unknown.value.code == "RUN_COMMAND_UNKNOWN"
    assert unknown.value.candidates[0].name == "ask"
    empty = parser.parse("/ask")
    assert empty.descriptor is not None
    assert empty.arguments == {"question": ""}


def test_workspace_command_accepts_its_alias_and_requires_a_folder() -> None:
    parser = AssistantCommandParser(AssistantCommandCatalog(FakeSkillCatalog(skill())))
    parsed = parser.parse("/ws project/src")
    assert parsed.descriptor is not None
    assert parsed.descriptor.name == "workspace"
    assert parsed.arguments == {"workspace_path": "project/src"}
    with pytest.raises(CommandParseError, match="Workspace path is required"):
        parser.parse("/workspace")


def test_base_and_skill_alias_collision_is_rejected() -> None:
    catalog = AssistantCommandCatalog(FakeSkillCatalog(skill(command="help")))
    with pytest.raises(CommandCatalogError):
        catalog.list()


@pytest.mark.asyncio
async def test_explicit_skill_dispatch_reuses_turn_port_and_command_selection() -> None:
    repository = InMemoryGroundedQARepository()
    conversation = ConversationRecord(
        conversation_id=UUID(int=601), space_id=UUID(int=602), owner_id="synthetic-user"
    )
    await repository.create_conversation(conversation)
    skill_view = skill()
    catalog = AssistantCommandCatalog(FakeSkillCatalog(skill_view))

    class Invoker:
        catalog = FakeSkillCatalog(skill_view)

        async def invoke(
            self,
            run: ConversationRun,
            *,
            skill: SkillInvocationView,
            arguments: dict[str, object],
            selection_source: ConversationRunSelectionSource,
        ) -> ConversationRun:
            assert skill.name == "knowledge_agent"
            assert arguments == {"question": "Synthetic question."}
            assert selection_source is ConversationRunSelectionSource.COMMAND
            return run

    service = AssistantCommandService(
        catalog=catalog,
        parser=AssistantCommandParser(catalog),
        turns=ConversationRunService(conversations=repository, runs=repository),
        runs=repository,
        conversations=repository,
        qa=repository,
        skill_invoker=Invoker(),
    )
    parsed = service.parser.parse("/ask Synthetic question.")
    result = await service.invoke_skill(
        conversation.conversation_id, parsed, idempotency_key="command-1"
    )
    assert result.run is not None
    assert result.run.selection_source is ConversationRunSelectionSource.COMMAND
    message = await repository.get_message(result.run.user_message_id)
    assert message is not None
    assert message.content == "/ask Synthetic question."


@pytest.mark.asyncio
async def test_empty_skill_command_is_idempotently_clarified_without_a_projection() -> None:
    repository = InMemoryGroundedQARepository()
    conversation = ConversationRecord(
        conversation_id=UUID(int=611), space_id=UUID(int=612), owner_id="synthetic-user"
    )
    await repository.create_conversation(conversation)
    registry = assistant_skill_registry()
    active_catalog = FileSystemSkillCatalog(registry, include_manifest_v2=True)
    catalog = AssistantCommandCatalog(active_catalog)
    projected: list[UUID] = []

    class Projection:
        async def create(
            self,
            run: ConversationRun,
            *,
            skill: object,
            arguments: object,
            resource_scope: object,
        ) -> ConversationRun:
            _ = skill, arguments, resource_scope
            projected.append(run.run_id)
            return run

    service = AssistantCommandService(
        catalog=catalog,
        parser=AssistantCommandParser(catalog),
        turns=ConversationRunService(conversations=repository, runs=repository),
        runs=repository,
        conversations=repository,
        qa=repository,
        skill_invoker=AssistantSkillInvocationService(
            runs=repository,
            catalog=active_catalog,
            registry=registry,
            projection=Projection(),
        ),
    )
    parsed = service.parser.parse("/ask")
    first = await service.invoke_skill(
        conversation.conversation_id, parsed, idempotency_key="empty-command"
    )
    second = await service.invoke_skill(
        conversation.conversation_id, parsed, idempotency_key="empty-command"
    )

    assert first.run is not None
    assert second.run is not None
    assert first.run.run_id == second.run.run_id
    assert first.run.status is ConversationRunStatus.WAITING_CLARIFICATION
    assert first.run.selection_source is ConversationRunSelectionSource.COMMAND
    assert projected == []


@pytest.mark.asyncio
async def test_skill_projection_uses_only_the_current_question_for_qa_retrieval() -> None:
    repository = InMemoryGroundedQARepository()
    conversation = ConversationRecord(
        conversation_id=UUID(int=621), space_id=UUID(int=622), owner_id="synthetic-user"
    )
    await repository.create_conversation(conversation)
    run = await ConversationRunService(conversations=repository, runs=repository).submit(
        AssistantTurnSubmission(
            conversation_id=conversation.conversation_id,
            content="Current question?",
            idempotency_key="projection-question",
        )
    )
    registry = assistant_skill_registry()
    catalog = FileSystemSkillCatalog(registry, include_manifest_v2=True)
    selected = next(
        item for item in catalog.list_active_invocations() if item.name == "knowledge_agent"
    )
    projected: dict[str, object] = {}

    class Projection:
        async def create(
            self,
            promoted: ConversationRun,
            *,
            skill: object,
            arguments: dict[str, object],
            resource_scope: object,
        ) -> ConversationRun:
            _ = skill, resource_scope
            projected.update(arguments)
            return promoted

    context = ConversationContextSnapshot(
        conversation_id=conversation.conversation_id,
        space_id=conversation.space_id,
        current_message_id=run.user_message_id,
        current_content="Current question?",
        summary=None,
        recent_messages=(
            # The router may see prior content, but QA retrieval must not receive it as query text.
            # This value represents a prior persisted assistant answer.
            ConversationContextMessage(
                UUID(int=623), MessageRole.ASSISTANT, "Previous answer from a document."
            ),
        ),
        sensitivity=ConversationSensitivity.PRIVATE_LOCAL,
        estimated_input_tokens=10,
        soft_limit_exceeded=False,
    )
    invoker = AssistantSkillInvocationService(
        runs=repository,
        catalog=catalog,
        registry=registry,
        projection=Projection(),
    )

    await invoker.invoke(
        run,
        skill=selected,
        arguments={"question": "Current question?"},
        context=context,
    )

    assert projected["standalone_request"] == "Current question?"
    assert "Previous answer" not in str(projected["standalone_request"])

    run_without_context = await ConversationRunService(
        conversations=repository, runs=repository
    ).submit(
        AssistantTurnSubmission(
            conversation_id=conversation.conversation_id,
            content="Current question without context?",
            idempotency_key="projection-question-without-context",
        )
    )
    await invoker.invoke(
        run_without_context,
        skill=selected,
        arguments={"question": "Current question without context?"},
    )

    assert projected["standalone_request"] == "Current question without context?"


def test_active_assistant_catalog_exposes_only_knowledge_agent_for_knowledge_requests() -> None:
    registry = assistant_skill_registry()
    catalog = FileSystemSkillCatalog(registry, include_manifest_v2=True)

    entries = catalog.list_active_invocations()
    knowledge_entries = [entry for entry in entries if entry.command == "ask"]

    assert [(entry.name, entry.version, entry.aliases) for entry in knowledge_entries] == [
        ("knowledge_agent", "0.9.0", ("qa",))
    ]
    assert all(entry.name != "knowledge_qa" for entry in entries)
