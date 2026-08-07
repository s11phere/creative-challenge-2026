from __future__ import annotations

from uuid import UUID

import pytest
from application.assistant import (
    AssistantTurnSubmission,
    ConversationFinalizer,
    ConversationRunService,
    FinalizationInput,
)
from application.qa import InMemoryGroundedQARepository
from domain.conversation_run import (
    AssistantResultKind,
    ConversationRunKind,
    ConversationRunStatus,
    FixedSkillIdentity,
)
from domain.qa_persistence import ConversationRecord, MessageRole
from model_gateway import (
    CapabilityAlias,
    ChatRequest,
    ChatResponse,
    FakeModelGateway,
    FakeScenario,
    ModelUsage,
)


class SynthesisGateway(FakeModelGateway):
    def __init__(self, *, response: str, scenario: FakeScenario = FakeScenario.NORMAL) -> None:
        super().__init__(scenario=scenario)
        self.response = response
        self.calls = 0

    async def chat(
        self, _request: ChatRequest, *, capability: CapabilityAlias = CapabilityAlias.FAST_CHAT
    ) -> ChatResponse:
        self.calls += 1
        if self.scenario is not FakeScenario.NORMAL:
            return await super().chat(_request, capability=capability)
        return ChatResponse(
            text=self.response,
            finish_reason="stop",
            usage=ModelUsage(input_tokens=3, output_tokens=4),
            capability=capability,
            latency_ms=1.0,
        )


async def _skill_run(repository: InMemoryGroundedQARepository):
    conversation = ConversationRecord(
        conversation_id=UUID(int=901), space_id=UUID(int=902), owner_id="finalizer-user"
    )
    await repository.create_conversation(conversation)
    run = await ConversationRunService(conversations=repository, runs=repository).submit(
        AssistantTurnSubmission(
            conversation_id=conversation.conversation_id,
            content="How do I use the architecture document?",
            idempotency_key="finalizer-turn",
        )
    )
    await repository.claim_conversation_run(run.run_id, lease_owner="worker", lease_seconds=60)
    return await repository.promote_to_skill(
        run.run_id,
        run_kind=ConversationRunKind.SKILL,
        selection_source=run.selection_source,
        skill=FixedSkillIdentity("knowledge_agent", "0.3.0", "a" * 64),
        core_prompt_version="assistant-base-prompt-v3",
    )


@pytest.mark.asyncio
async def test_finalizer_publishes_independent_synthesized_message_once() -> None:
    repository = InMemoryGroundedQARepository()
    run = await _skill_run(repository)
    gateway = SynthesisGateway(response="Use the architecture document to trace the data flow.")
    finalizer = ConversationFinalizer(runs=repository, gateway=gateway)

    completed = await finalizer.execute(
        run,
        input=FinalizationInput(
            question="How do I use the architecture document?",
            skill_result="Raw retrieval output with redundant metadata and repeated paragraphs.",
        ),
    )
    repeated = await finalizer.execute(
        completed,
        input=FinalizationInput(question="ignored", skill_result="ignored"),
    )

    assert completed.status is ConversationRunStatus.COMPLETED
    assert completed.result is not None
    assert completed.result.kind is AssistantResultKind.DIRECT_MESSAGE
    message = await repository.get_message(completed.result.message_id)
    assert message is not None
    assert message.role is MessageRole.ASSISTANT
    assert message.content == "Use the architecture document to trace the data flow."
    assert message.content != (
        "Raw retrieval output with redundant metadata and repeated paragraphs."
    )
    assert repeated == completed
    assert gateway.calls == 1
    assert len(await repository.list_messages(run.conversation_id)) == 2


@pytest.mark.asyncio
async def test_finalizer_failure_publishes_safe_fallback_without_tool_result() -> None:
    repository = InMemoryGroundedQARepository()
    run = await _skill_run(repository)
    gateway = SynthesisGateway(response="unused", scenario=FakeScenario.UNAVAILABLE)

    completed = await ConversationFinalizer(runs=repository, gateway=gateway).execute(
        run,
        input=FinalizationInput(question="Question", skill_result="Sensitive raw result"),
    )

    assert completed.status is ConversationRunStatus.COMPLETED
    assert completed.result is not None
    message = await repository.get_message(completed.result.message_id)
    assert message is not None
    assert "Sensitive raw result" not in message.content
