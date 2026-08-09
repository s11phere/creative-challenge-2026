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
        self.requests: list[ChatRequest] = []

    async def chat(
        self, _request: ChatRequest, *, capability: CapabilityAlias = CapabilityAlias.FAST_CHAT
    ) -> ChatResponse:
        self.calls += 1
        self.requests.append(_request)
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
async def test_finalizer_publishes_llm_synthesized_answer_once() -> None:
    repository = InMemoryGroundedQARepository()
    run = await _skill_run(repository)
    gateway = SynthesisGateway(response="LLM synthesized answer.")
    finalizer = ConversationFinalizer(runs=repository, gateway=gateway)
    skill_result = "Synthetic evidence supports this answer. [citation]"

    completed = await finalizer.execute(
        run,
        input=FinalizationInput(
            question="How do I use the architecture document?",
            skill_result=skill_result,
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
    assert message.content == "LLM synthesized answer."
    assert repeated == completed
    assert gateway.calls == 1
    assert "How do I use the architecture document?" in gateway.requests[0].messages[-1].content
    assert skill_result in gateway.requests[0].messages[-1].content
    assert len(await repository.list_messages(run.conversation_id)) == 2


@pytest.mark.asyncio
async def test_finalizer_uses_fallback_when_skill_result_is_empty() -> None:
    repository = InMemoryGroundedQARepository()
    run = await _skill_run(repository)
    gateway = SynthesisGateway(response="unused", scenario=FakeScenario.UNAVAILABLE)

    completed = await ConversationFinalizer(runs=repository, gateway=gateway).execute(
        run,
        input=FinalizationInput(question="Question", skill_result="   "),
    )

    assert completed.status is ConversationRunStatus.COMPLETED
    assert completed.result is not None
    message = await repository.get_message(completed.result.message_id)
    assert message is not None
    assert message.content == "工具执行已完成，但没有生成可展示的最终回答，请重试。"
