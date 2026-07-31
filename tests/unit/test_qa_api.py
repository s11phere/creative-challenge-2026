import json
from uuid import UUID

import pytest
from api.main import create_app
from application.qa import InMemoryGroundedQARepository
from domain.qa_sse import QAEventLog
from httpx import ASGITransport, AsyncClient
from model_gateway import FakeModelGateway


@pytest.mark.asyncio
async def test_provisional_qa_api_creates_run_cancels_and_replays_events() -> None:
    app = create_app(
        model_gateway=FakeModelGateway(),
        enable_qa_execution=False,
        qa_repository=InMemoryGroundedQARepository(),
        qa_event_store=QAEventLog(),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            "/api/v1/spaces/00000000-0000-0000-0000-000000000001/conversations",
            json={"owner_id": "local-user"},
        )
        assert created.status_code == 201
        conversation_id = created.json()["conversation_id"]

        submitted = await client.post(
            f"/api/v1/conversations/{conversation_id}/questions",
            json={"question": "What is supported?", "idempotency_key": "question-1"},
        )
        assert submitted.status_code == 202
        run_id = UUID(submitted.json()["run_id"])
        assert submitted.json()["status"] == "queued"

        replayed_submission = await client.post(
            f"/api/v1/conversations/{conversation_id}/questions",
            json={"question": "What is supported?", "idempotency_key": "question-1"},
        )
        assert replayed_submission.status_code == 202
        assert UUID(replayed_submission.json()["run_id"]) == run_id

        replay = await client.get(f"/api/v1/qa/runs/{run_id}/events")
        assert replay.status_code == 200
        assert "event: accepted" in replay.text
        assert "What is supported?" not in replay.text

        cancelled = await client.post(f"/api/v1/qa/runs/{run_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancel_requested"

        resumed = await client.get(
            f"/api/v1/qa/runs/{run_id}/events", headers={"Last-Event-ID": "1"}
        )
        assert "event: cancel_requested" in resumed.text
        assert "event: accepted" not in resumed.text

        feedback = await client.post(
            f"/api/v1/qa/runs/{run_id}/feedback",
            json={
                "decision": "negative",
                "idempotency_key": "feedback-1",
                "note": "Needs review",
            },
        )
        assert feedback.status_code == 409
        assert "Needs review" not in json.dumps(feedback.json())
