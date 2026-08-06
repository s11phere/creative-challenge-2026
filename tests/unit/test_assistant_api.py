import json
from uuid import UUID

import pytest
from api.assistant_runtime import AssistantWorkerDispatcher
from api.main import create_app
from application.qa import InMemoryGroundedQARepository
from domain.assistant_sse import AssistantEventLog, AssistantEventType
from domain.conversation_run import ConversationRunStatus
from domain.qa_sse import QAEventLog
from httpx import ASGITransport, AsyncClient
from infrastructure.skill_lifecycle import InMemorySkillActivationStore
from model_gateway import FakeModelGateway


@pytest.mark.asyncio
async def test_v2_turn_skeleton_persists_and_cancels_a_model_free_turn() -> None:
    repository = InMemoryGroundedQARepository()
    app = create_app(
        model_gateway=FakeModelGateway(),
        enable_qa_execution=False,
        qa_repository=repository,
        qa_event_store=QAEventLog(),
        assistant_event_store=AssistantEventLog(),
        skill_activation_store=InMemorySkillActivationStore(),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        conversation = await client.post(
            f"/api/v1/spaces/{UUID(int=201)}/conversations", json={"owner_id": "local-user"}
        )
        conversation_id = conversation.json()["conversation_id"]
        created = await client.post(
            f"/api/v2/conversations/{conversation_id}/turns",
            json={"content": "Hello.", "idempotency_key": "turn-1"},
        )
        run_id = UUID(created.json()["run_id"])
        replayed = await client.post(
            f"/api/v2/conversations/{conversation_id}/turns",
            json={"content": "Hello.", "idempotency_key": "turn-1"},
        )
        conflict = await client.post(
            f"/api/v2/conversations/{conversation_id}/turns",
            json={"content": "Different.", "idempotency_key": "turn-1"},
        )
        recovered = await client.get(f"/api/v2/runs/{run_id}")
        cancelled = await client.post(f"/api/v2/runs/{run_id}/cancel")

    assert conversation.status_code == 201
    assert created.status_code == 202
    assert created.json() == {
        "run_id": str(run_id),
        "status": "created",
        "run_kind": "assistant_turn",
        "selection": {"source": "none", "skill": None},
        "assistant_message": None,
        "clarification": None,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "model_latency_ms": 0.0,
        },
    }
    assert replayed.status_code == 202
    assert replayed.json()["run_id"] == str(run_id)
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "CONVERSATION_RUN_CONFLICT"
    assert recovered.status_code == 200
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == ConversationRunStatus.CANCEL_REQUESTED.value


@pytest.mark.asyncio
async def test_v2_ordinary_multi_turns_complete_without_retrieval_and_sse_is_content_free() -> None:
    repository = InMemoryGroundedQARepository()
    enqueued: list[UUID] = []

    class Message:
        message_id = "message-1"

    def enqueue(**kwargs: object) -> Message:
        enqueued.append(UUID(str(kwargs["run_id"])))
        return Message()

    events = AssistantEventLog()
    runtime = AssistantWorkerDispatcher(repository=repository, enqueuer=enqueue)
    app = create_app(
        model_gateway=FakeModelGateway(),
        enable_qa_execution=True,
        qa_repository=repository,
        qa_event_store=QAEventLog(),
        assistant_event_store=events,
        assistant_runtime=runtime,
        skill_activation_store=InMemorySkillActivationStore(),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        conversation = await client.post(
            f"/api/v1/spaces/{UUID(int=211)}/conversations", json={"owner_id": "local-user"}
        )
        conversation_id = conversation.json()["conversation_id"]
        first = await client.post(
            f"/api/v2/conversations/{conversation_id}/turns",
            json={"content": "Synthetic first turn.", "idempotency_key": "ordinary-1"},
        )
        first_id = UUID(first.json()["run_id"])
        await repository.claim_conversation_run(
            first_id, lease_owner="test-worker", lease_seconds=60
        )
        await app.state.assistant_agent_service.execute(first_id)
        first_completed = await client.get(f"/api/v2/runs/{first_id}")
        second = await client.post(
            f"/api/v2/conversations/{conversation_id}/turns",
            json={"content": "Synthetic second turn.", "idempotency_key": "ordinary-2"},
        )
        second_id = UUID(second.json()["run_id"])
        await repository.claim_conversation_run(
            second_id, lease_owner="test-worker", lease_seconds=60
        )
        await app.state.assistant_agent_service.execute(second_id)
        second_completed = await client.get(f"/api/v2/runs/{second_id}")
        stream = await client.get(f"/api/v2/runs/{second_id}/events")

    assert enqueued == [first_id, second_id]
    assert first_completed.json()["status"] == ConversationRunStatus.COMPLETED.value
    assert second_completed.json()["status"] == ConversationRunStatus.COMPLETED.value
    assert first_completed.json()["assistant_message"]["content"].startswith("fake-response-")
    assert second_completed.json()["assistant_message"]["content"].startswith("fake-response-")
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    stream_body = stream.text
    assert "Synthetic second turn." not in stream_body
    assert "fake-response-" not in stream_body
    payloads = [
        json.loads(line.removeprefix("data: "))
        for line in stream_body.splitlines()
        if line.startswith("data: ")
    ]
    assert [payload["type"] for payload in payloads] == [
        AssistantEventType.ACCEPTED.value,
        AssistantEventType.ROUTING.value,
        AssistantEventType.COMPLETED.value,
    ]
