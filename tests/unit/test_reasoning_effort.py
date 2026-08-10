from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest
from application.assistant import (
    AssistantCommandCatalog,
    AssistantCommandParser,
    AssistantCommandService,
    AssistantTurnSubmission,
    CommandParseError,
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
    GatewayStatus,
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


class ReasoningDisabledOpenAICompatibleGateway(FakeModelGateway):
    @property
    def status(self) -> GatewayStatus:
        return replace(
            super().status,
            provider=ModelProvider.OPENAI_COMPATIBLE,
            model_identity="chat-model",
            reasoning_enabled_by_default=False,
        )


def test_capability_registry_maps_native_coarse_and_unsupported_effort() -> None:
    registry = default_model_capability_registry()

    native = registry.map(
        provider=ModelProvider.FAKE,
        model="fake-fast-chat-v1",
        requested_effort=ReasoningEffort.HIGH,
    )
    assert native.effective_effort is ReasoningEffort.HIGH
    assert native.mode is ReasoningMode.NATIVE

    deepseek = registry.map(
        provider=ModelProvider.OPENAI_COMPATIBLE,
        model="deepseek-v4-flash",
        requested_effort=ReasoningEffort.XHIGH,
    )
    assert deepseek.effective_effort is ReasoningEffort.HIGH
    assert deepseek.mode is ReasoningMode.NATIVE
    assert deepseek.mapping_version == "reasoning-mapping-v2"

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


def test_deepseek_capability_maps_every_selectable_effort() -> None:
    registry = default_model_capability_registry()

    expected = {
        ReasoningEffort.LOW: ReasoningEffort.LOW,
        ReasoningEffort.MEDIUM: ReasoningEffort.MEDIUM,
        ReasoningEffort.HIGH: ReasoningEffort.HIGH,
        ReasoningEffort.XHIGH: ReasoningEffort.HIGH,
        ReasoningEffort.MAX: ReasoningEffort.MAX,
    }

    for requested_effort, effective_effort in expected.items():
        profile = registry.map(
            provider=ModelProvider.OPENAI_COMPATIBLE,
            model="deepseek-v4-flash",
            requested_effort=requested_effort,
        )

        assert profile.effective_effort is effective_effort
        assert profile.mode is ReasoningMode.NATIVE
        assert profile.mapping_version == "reasoning-mapping-v2"


def test_auto_effort_uses_provider_default_even_when_legacy_flag_is_disabled() -> None:
    profile = ReasoningProfileResolver(gateway=ReasoningDisabledOpenAICompatibleGateway()).resolve(
        ReasoningEffort.AUTO
    )

    assert profile.requested_effort is ReasoningEffort.AUTO
    assert profile.effective_effort is ReasoningEffort.MEDIUM
    assert profile.mode is ReasoningMode.COARSE


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
        reasoning=ReasoningProfileResolver(gateway=FakeModelGateway()),
    )

    initial = await service.effort(conversation.conversation_id, requested_effort=None)
    assert initial.content == "Model: fake-fast-chat-v1 | reasoning effort: low (default)."

    parsed = service.parser.parse("/effort high")
    assert parsed.arguments == {"effort": "high"}
    updated = await service.effort(
        conversation.conversation_id,
        requested_effort=str(parsed.arguments["effort"]),
    )
    assert updated.content == "Model: fake-fast-chat-v1 | reasoning effort: high."

    queried = await service.effort(conversation.conversation_id, requested_effort=None)
    assert queried.content == "Model: fake-fast-chat-v1 | reasoning effort: high."

    with pytest.raises(CommandParseError) as invalid:
        await service.effort(
            conversation.conversation_id,
            requested_effort="auto",
        )
    assert invalid.value.code == "RUN_REASONING_EFFORT_INVALID"

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


@pytest.mark.asyncio
async def test_effort_command_keeps_legacy_default_in_the_selectable_menu() -> None:
    repository = InMemoryGroundedQARepository()
    conversation = ConversationRecord(
        conversation_id=UUID(int=911), space_id=UUID(int=912), owner_id="effort-user"
    )
    await repository.create_conversation(conversation)
    catalog = AssistantCommandCatalog(EmptySkillCatalog())
    service = AssistantCommandService(
        catalog=catalog,
        parser=AssistantCommandParser(catalog),
        turns=ConversationRunService(
            conversations=repository,
            runs=repository,
            reasoning=ReasoningProfileResolver(gateway=FakeModelGateway()),
        ),
        runs=repository,
        conversations=repository,
        qa=repository,
        reasoning=ReasoningProfileResolver(gateway=ReasoningDisabledOpenAICompatibleGateway()),
    )

    result = await service.effort(conversation.conversation_id, requested_effort=None)

    assert result.content == "Model: chat-model | reasoning effort: medium (default)."
