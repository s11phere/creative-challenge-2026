from __future__ import annotations

import json
from uuid import UUID

import pytest
from api.assistant_runtime import AssistantWorkerDispatcher
from application.assistant import (
    AssistantAgentService,
    AssistantTurnSubmission,
    ConversationRunService,
)
from application.qa import InMemoryGroundedQARepository
from domain.assistant_sse import AssistantEventLog, AssistantEventType
from domain.conversation_run import ConversationRunStatus
from domain.qa_persistence import ConversationRecord
from model_gateway import (
    CapabilityAlias,
    ChatRequest,
    ChatResponse,
    FakeModelGateway,
    FakeScenario,
    ModelUsage,
)


class DecisionGateway(FakeModelGateway):
    def __init__(self, decision: str) -> None:
        super().__init__()
        self.decision = decision
        self.calls = 0

    async def chat(
        self, _request: ChatRequest, *, capability: CapabilityAlias = CapabilityAlias.FAST_CHAT
    ) -> ChatResponse:
        self.calls += 1
        return ChatResponse(
            text=self.decision,
            finish_reason="stop",
            usage=ModelUsage(input_tokens=4, output_tokens=2),
            capability=capability,
            latency_ms=1.5,
        )


async def _turn(
    gateway: FakeModelGateway,
) -> tuple[InMemoryGroundedQARepository, AssistantAgentService, AssistantEventLog, UUID]:
    repository = InMemoryGroundedQARepository()
    conversation = ConversationRecord(
        conversation_id=UUID(int=401), space_id=UUID(int=402), owner_id="synthetic-user"
    )
    await repository.create_conversation(conversation)
    submitted = await ConversationRunService(conversations=repository, runs=repository).submit(
        AssistantTurnSubmission(
            conversation_id=conversation.conversation_id,
            content="Synthetic ordinary conversation.",
            idempotency_key="assistant-agent-1",
        )
    )
    await repository.claim_conversation_run(
        submitted.run_id, lease_owner="test-worker", lease_seconds=60
    )
    events = AssistantEventLog()
    agent = AssistantAgentService(
        runs=repository,
        messages=repository,
        gateway=gateway,
        events=events,
    )
    return repository, agent, events, submitted.run_id


@pytest.mark.asyncio
async def test_fake_respond_publishes_one_assistant_message_atomically() -> None:
    repository, agent, events, run_id = await _turn(FakeModelGateway())

    completed = await agent.execute(run_id)

    assert completed is not None
    assert completed.status is ConversationRunStatus.COMPLETED
    assert completed.result is not None
    assert completed.result.message_id is not None
    message = await repository.get_message(completed.result.message_id)
    assert message is not None
    assert message.content.startswith("fake-response-")
    assert len(await repository.list_messages(completed.conversation_id)) == 2
    assert [event.event_type for event in await events.replay(run_id)] == [
        AssistantEventType.ROUTING,
        AssistantEventType.COMPLETED,
    ]
    serialized = json.dumps([event.as_dict() for event in await events.replay(run_id)])
    assert "Synthetic ordinary conversation." not in serialized
    assert "fake-response" not in serialized


@pytest.mark.asyncio
async def test_clarify_is_server_authored_and_does_not_create_fake_assistant_message() -> None:
    decision = json.dumps(
        {
            "schema_version": "assistant-router-decision-v1",
            "action": "clarify",
            "assistant_message": "Model text is not persisted as an SSE payload.",
        }
    )
    gateway = DecisionGateway(decision)
    repository, agent, events, run_id = await _turn(gateway)

    clarified = await agent.execute(run_id)

    assert clarified is not None
    assert clarified.status is ConversationRunStatus.WAITING_CLARIFICATION
    assert clarified.result is not None
    assert clarified.result.clarification is not None
    assert clarified.result.clarification.clarification_id == f"clarify:{run_id.hex}"
    assert len(await repository.list_messages(clarified.conversation_id)) == 1
    assert [event.event_type for event in await events.replay(run_id)] == [
        AssistantEventType.ROUTING,
        AssistantEventType.CLARIFICATION,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "gateway",
    [
        DecisionGateway("not json"),
        DecisionGateway(
            json.dumps(
                {
                    "schema_version": "assistant-router-decision-v1",
                    "action": "invoke_skill",
                    "skill_name": "knowledge_qa",
                    "arguments": {"question": "Synthetic"},
                }
            )
        ),
        FakeModelGateway(scenario=FakeScenario.TIMEOUT),
    ],
)
async def test_invalid_or_failed_decision_never_publishes_assistant_message(
    gateway: FakeModelGateway,
) -> None:
    repository, agent, events, run_id = await _turn(gateway)

    failed = await agent.execute(run_id)

    assert failed is not None
    assert failed.status is ConversationRunStatus.FAILED
    assert failed.result is None
    assert len(await repository.list_messages(failed.conversation_id)) == 1
    terminal = (await events.replay(run_id))[-1]
    assert terminal.event_type is AssistantEventType.FAILED
    assert "content" not in json.dumps(terminal.as_dict())


@pytest.mark.asyncio
async def test_cancel_before_execution_does_not_call_model() -> None:
    gateway = DecisionGateway(
        json.dumps(
            {
                "schema_version": "assistant-router-decision-v1",
                "action": "respond",
                "assistant_message": "Should never be reached.",
            }
        )
    )
    repository, agent, events, run_id = await _turn(gateway)
    await repository.request_conversation_cancel(run_id)

    cancelled = await agent.execute(run_id)

    assert cancelled is not None
    assert cancelled.status is ConversationRunStatus.CANCELLED
    assert gateway.calls == 0
    assert cancelled.result is None
    assert len(await repository.list_messages(cancelled.conversation_id)) == 1
    assert (await events.replay(run_id))[-1].event_type is AssistantEventType.CANCELLED


@pytest.mark.asyncio
async def test_assistant_dispatcher_enqueues_control_metadata_only() -> None:
    captured: dict[str, object] = {}

    class Message:
        message_id = "assistant-message-1"

    def enqueue(**kwargs: object) -> Message:
        captured.update(kwargs)
        return Message()

    dispatcher = AssistantWorkerDispatcher(
        repository=InMemoryGroundedQARepository(),
        enqueuer=enqueue,
    )

    run_id = UUID(int=403)
    assert dispatcher.start(run_id) is True
    assert captured["run_id"] == str(run_id)
    assert captured["event_version"] == 2
    assert len(str(captured["trace_id"])) == 32
    assert set(captured) == {"run_id", "trace_id", "event_version"}
