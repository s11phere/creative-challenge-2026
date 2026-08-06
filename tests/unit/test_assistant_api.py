from uuid import UUID

import pytest
from api.main import create_app
from application.qa import InMemoryGroundedQARepository
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
