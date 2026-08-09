from __future__ import annotations

from uuid import UUID

import pytest
from application.assistant import (
    AssistantCommandCatalog,
    AssistantCommandParser,
    AssistantCommandService,
    AssistantTurnSubmission,
    ConversationRunService,
    ReasoningProfileResolver,
)
from application.qa import InMemoryGroundedQARepository
from domain.qa_persistence import ConversationRecord
from domain.reasoning import (
    ReasoningDowngradeReason,
    ReasoningEffort,
    ReasoningMode,
)
from model_gateway import (
    FakeModelGateway,
    ModelProvider,
    ReasoningMappingError,
    default_model_capability_registry,
)


class EmptySkillCatalog:
    def list_active_invocations(self) -> tuple[object, ...]:
        return ()

    def list_skills(self) -> tuple[object, ...]:
        return ()

    def list_versions(self, _name: str) -> tuple[object, ...]:
        return ()


def test_capability_registry_maps_native_coarse_and_unsupported_effort() -> None:
    registry = default_model_capability_registry()

    native = registry.map(
        provider=ModelProvider.FAKE,
        model="fake-fast-chat-v1",
        requested_effort=ReasoningEffort.HIGH,
    )
    assert native.effective_effort is ReasoningEffort.HIGH
    assert native.mode is ReasoningMode.NATIVE

    coarse = registry.map(
        provider=ModelProvider.OPENAI_COMPATIBLE,
        model="chat-model",
        requested_effort=ReasoningEffort.HIGH,
    )
    assert coarse.effective_effort is ReasoningEffort.HIGH
    assert coarse.mode is ReasoningMode.COARSE

    downgraded = registry.map(
        provider=ModelProvider.DISABLED,
        model="disabled",
        requested_effort=ReasoningEffort.AUTO,
    )
    assert downgraded.effective_effort is ReasoningEffort.NONE
    assert downgraded.downgrade_reason is ReasoningDowngradeReason.PROVIDER_UNSUPPORTED

    with pytest.raises(ReasoningMappingError):
        registry.map(
            provider=ModelProvider.DISABLED,
            model="disabled",
            requested_effort=ReasoningEffort.HIGH,
        )


@pytest.mark.asyncio
async def test_effort_command_persists_conversation_default_and_next_run_profile() -> None:
    repository = InMemoryGroundedQARepository()
    conversation = ConversationRecord(
        conversation_id=UUID(int=901), space_id=UUID(int=902), owner_id="effort-user"
    )
    await repository.create_conversation(conversation)
    catalog = AssistantCommandCatalog(EmptySkillCatalog())
    turns = ConversationRunService(
        conversations=repository,
        runs=repository,
        reasoning=ReasoningProfileResolver(gateway=FakeModelGateway()),
    )
    service = AssistantCommandService(
        catalog=catalog,
        parser=AssistantCommandParser(catalog),
        turns=turns,
        runs=repository,
        conversations=repository,
        qa=repository,
    )

    parsed = service.parser.parse("/effort high")
    assert parsed.arguments == {"effort": "high"}
    updated = await service.effort(
        conversation.conversation_id,
        requested_effort=str(parsed.arguments["effort"]),
    )
    assert updated.content == "Default reasoning effort: high."

    queried = await service.effort(conversation.conversation_id, requested_effort=None)
    assert queried.content == "Current reasoning effort: high."

    run = await turns.submit(
        AssistantTurnSubmission(
            conversation_id=conversation.conversation_id,
            content="Use the selected effort.",
            idempotency_key="effort-run-1",
        )
    )
    assert run.reasoning_profile.requested_effort is ReasoningEffort.HIGH
    assert run.reasoning_profile.effective_effort is ReasoningEffort.HIGH
    assert run.reasoning_profile.provider == "fake"
    assert run.reasoning_profile.mode is ReasoningMode.NATIVE
