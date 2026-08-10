import json
from uuid import UUID

import pytest
from api.assistant_runtime import AssistantWorkerDispatcher
from api.main import create_app
from application.assistant import (
    ResolvedResource,
    ResourceResolutionError,
    ResourceResolutionErrorCode,
)
from application.qa import InMemoryGroundedQARepository
from domain.agent_sse import AgentRunEventLog, AgentRunEventType
from domain.assistant_sse import AssistantEventLog, AssistantEventType
from domain.conversation_run import ConversationRunStatus, ResourceCandidate
from domain.qa_persistence import QARetrievalScope
from domain.qa_sse import QAEventLog
from httpx import ASGITransport, AsyncClient
from infrastructure.config import settings
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
    created_payload = created.json()
    assert UUID(created_payload.pop("user_message_id"))
    assert created_payload == {
        "run_id": str(run_id),
        "status": "created",
        "run_kind": "assistant_turn",
        "error_code": None,
        "selection": {"source": "none", "skill": None},
        "model_identity": "unselected",
        "reasoning_profile": {
            "schema_version": "reasoning-profile-v1",
            "requested_effort": "auto",
            "effective_effort": "low",
            "provider": "fake",
            "model": "fake-fast-chat-v1",
            "mapping_version": "reasoning-mapping-v1",
            "mode": "native",
            "downgrade_reason": "none",
        },
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
async def test_v3_agent_event_page_and_sse_reconnect_are_redacted() -> None:
    repository = InMemoryGroundedQARepository()
    events = AgentRunEventLog()
    app = create_app(
        model_gateway=FakeModelGateway(),
        enable_qa_execution=False,
        qa_repository=repository,
        qa_event_store=QAEventLog(),
        assistant_event_store=AssistantEventLog(),
        agent_event_store=events,
        skill_activation_store=InMemorySkillActivationStore(),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        conversation = await client.post(
            f"/api/v1/spaces/{UUID(int=203)}/conversations", json={"owner_id": "local-user"}
        )
        conversation_id = conversation.json()["conversation_id"]
        created = await client.post(
            f"/api/v2/conversations/{conversation_id}/turns",
            json={"content": "Synthetic request.", "idempotency_key": "v3-events-1"},
        )
        run_id = UUID(created.json()["run_id"])
        await events.append(
            run_id,
            AgentRunEventType.ACCEPTED,
            {"status": "accepted"},
            event_key="accepted",
        )
        await events.append(
            run_id,
            AgentRunEventType.TOOL_OUTPUT,
            {
                "status": "succeeded",
                "iteration": 1,
                "tool_name": "knowledge_search",
                "tool_version": "1.0.0",
                "input_summary": "sha256:input",
                "output_summary": "sha256:output",
                "retry_count": 0,
                "duration_ms": 4,
            },
            event_key="tool-output",
        )
        await events.append(
            run_id,
            AgentRunEventType.COMPLETED,
            {"status": "completed", "stop_reason": "goal_complete", "iteration": 1},
            event_key="terminal",
        )
        first_page = await client.get(f"/api/v3/runs/{run_id}/events?limit=2")
        reconnect = await client.get(
            f"/api/v3/runs/{run_id}/events/stream",
            headers={"Last-Event-ID": "2"},
        )

    assert first_page.status_code == 200
    assert [event["event_type"] for event in first_page.json()["events"]] == [
        "accepted",
        "tool_output",
    ]
    assert first_page.json()["next_sequence"] == 2
    assert first_page.json()["has_more"] is True
    assert reconnect.status_code == 200
    assert "event: completed" in reconnect.text
    assert "Synthetic request." not in reconnect.text
    assert "sha256:input" not in reconnect.text


@pytest.mark.asyncio
async def test_v2_effort_command_updates_only_future_conversation_runs() -> None:
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
        created = await client.post(
            f"/api/v1/spaces/{UUID(int=205)}/conversations", json={"owner_id": "local-user"}
        )
        conversation_id = created.json()["conversation_id"]
        changed = await client.post(
            f"/api/v2/conversations/{conversation_id}/turns",
            json={"content": "/effort high", "idempotency_key": "effort-command-1"},
        )
        queried = await client.post(
            f"/api/v2/conversations/{conversation_id}/turns",
            json={"content": "/effort", "idempotency_key": "effort-command-2"},
        )
        turn = await client.post(
            f"/api/v2/conversations/{conversation_id}/turns",
            json={"content": "Use high effort.", "idempotency_key": "effort-turn-1"},
        )

    assert changed.status_code == 202
    assert changed.json()["content"] == "Model: fake-fast-chat-v1 | reasoning effort: high."
    assert queried.status_code == 202
    assert queried.json()["content"] == "Model: fake-fast-chat-v1 | reasoning effort: high."
    assert turn.status_code == 202
    profile = turn.json()["reasoning_profile"]
    assert profile["requested_effort"] == "high"
    assert profile["effective_effort"] == "high"
    assert profile["mode"] == "native"


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


@pytest.mark.asyncio
async def test_v2_command_catalog_and_base_commands_do_not_create_business_runs() -> None:
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
            f"/api/v1/spaces/{UUID(int=221)}/conversations", json={"owner_id": "local-user"}
        )
        conversation_id = conversation.json()["conversation_id"]
        catalog = await client.get("/api/v2/commands")
        help_result = await client.post(
            f"/api/v2/conversations/{conversation_id}/turns",
            json={"content": "/help", "idempotency_key": "help-1", "command": "help"},
        )
        new_result = await client.post(
            f"/api/v2/conversations/{conversation_id}/turns",
            json={"content": "/new", "idempotency_key": "new-1"},
        )
        mismatch = await client.post(
            f"/api/v2/conversations/{conversation_id}/turns",
            json={"content": "/help", "idempotency_key": "bad-command", "command": "ask"},
        )

    assert catalog.status_code == 200
    names = {item["name"] for item in catalog.json()["commands"]}
    assert {
        "help",
        "skills",
        "new",
        "compact",
        "stop",
        "ask",
        "summarize",
        "compare",
        "cards",
    } <= names
    assert all(
        "content_sha256" not in item and "budget" not in item for item in catalog.json()["commands"]
    )
    assert help_result.status_code == 202
    assert help_result.json()["command"] == "help"
    assert help_result.json()["run"] is None
    assert new_result.status_code == 202
    assert new_result.json()["conversation_id"] != conversation_id
    assert mismatch.status_code == 400
    assert mismatch.json()["code"] == "RUN_COMMAND_UNKNOWN"
    assert await repository.list_conversation_runs(UUID(conversation_id)) == ()


@pytest.mark.asyncio
async def test_v2_stop_and_escaped_slash_reuse_the_existing_turn_lifecycle() -> None:
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
            f"/api/v1/spaces/{UUID(int=231)}/conversations", json={"owner_id": "local-user"}
        )
        conversation_id = conversation.json()["conversation_id"]
        escaped = await client.post(
            f"/api/v2/conversations/{conversation_id}/turns",
            json={"content": "//ask literal", "idempotency_key": "escaped-1"},
        )
        stopped = await client.post(
            f"/api/v2/conversations/{conversation_id}/turns",
            json={"content": "/stop", "idempotency_key": "stop-1"},
        )

    assert escaped.status_code == 202
    assert escaped.json()["run_kind"] == "assistant_turn"
    assert escaped.json()["selection"]["source"] == "none"
    assert stopped.status_code == 202
    assert stopped.json()["command"] == "stop"
    assert stopped.json()["run"]["run_id"] == escaped.json()["run_id"]
    assert stopped.json()["run"]["status"] == ConversationRunStatus.CANCEL_REQUESTED.value


@pytest.mark.asyncio
async def test_v2_resource_clarification_resumes_the_same_run() -> None:
    class Resolver:
        candidate = ResourceCandidate(
            candidate_id="candidate:synthetic-document",
            resource_type="document",
            label="Architecture notes",
            source_label="notes.md",
            version_label="published",
        )

        async def resolve(self, **_kwargs: object) -> ResolvedResource:
            raise ResourceResolutionError(
                ResourceResolutionErrorCode.CONFLICT,
                "Several resources matched.",
                (self.candidate,),
            )

        async def select_candidate(self, **kwargs: object) -> ResolvedResource:
            if kwargs.get("candidate_id") != self.candidate.candidate_id:
                raise ResourceResolutionError(
                    ResourceResolutionErrorCode.NOT_FOUND,
                    "Candidate is unavailable.",
                )
            return ResolvedResource(
                candidate=self.candidate,
                scope=QARetrievalScope(),
            )

    repository = InMemoryGroundedQARepository()
    app = create_app(
        model_gateway=FakeModelGateway(),
        enable_qa_execution=False,
        qa_repository=repository,
        qa_event_store=QAEventLog(),
        assistant_event_store=AssistantEventLog(),
        skill_activation_store=InMemorySkillActivationStore(),
    )
    app.state.assistant_skill_invoker._resources = Resolver()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        conversation = await client.post(
            f"/api/v1/spaces/{UUID(int=237)}/conversations", json={"owner_id": "local-user"}
        )
        conversation_id = conversation.json()["conversation_id"]
        clarified = await client.post(
            f"/api/v2/conversations/{conversation_id}/turns",
            json={"content": "/summarize Architecture", "idempotency_key": "clarify-1"},
        )
        run_id = clarified.json()["run"]["run_id"]
        clarification_id = clarified.json()["run"]["clarification"]["clarification_id"]
        listed = await client.get(f"/api/v2/conversations/{conversation_id}/runs")
        invalid = await client.post(
            f"/api/v2/runs/{run_id}/clarifications/{clarification_id}",
            json={"candidate_id": "candidate:forged"},
        )
        resumed = await client.post(
            f"/api/v2/runs/{run_id}/clarifications/{clarification_id}",
            json={"candidate_id": Resolver.candidate.candidate_id},
        )

    assert clarified.status_code == 202
    assert clarified.json()["run"]["status"] == ConversationRunStatus.WAITING_CLARIFICATION.value
    assert listed.status_code == 200
    assert [item["run_id"] for item in listed.json()["runs"]] == [run_id]
    assert "continuation" not in json.dumps(listed.json())
    assert invalid.status_code == 409
    assert resumed.status_code == 202
    assert resumed.json()["run_id"] == run_id
    assert resumed.json()["run_kind"] == "skill"
    assert resumed.json()["selection"]["skill"]["name"] == "summarize_document"


@pytest.mark.asyncio
async def test_v2_explicit_skill_command_bypasses_model_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NoChatGateway(FakeModelGateway):
        async def chat(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("Explicit command must not use the Assistant router model")

    repository = InMemoryGroundedQARepository()
    monkeypatch.setattr(settings, "knowledge_agent_skill_version", "0.5.0")
    monkeypatch.setattr(settings, "agent_loop_v5_enabled", True)
    events = AssistantEventLog()
    app = create_app(
        model_gateway=NoChatGateway(),
        enable_qa_execution=False,
        qa_repository=repository,
        qa_event_store=QAEventLog(),
        assistant_event_store=events,
        skill_activation_store=InMemorySkillActivationStore(),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        conversation = await client.post(
            f"/api/v1/spaces/{UUID(int=241)}/conversations", json={"owner_id": "local-user"}
        )
        conversation_id = conversation.json()["conversation_id"]
        first = await client.post(
            f"/api/v2/conversations/{conversation_id}/turns",
            json={"content": "/ask Synthetic question.", "idempotency_key": "ask-1"},
        )
        second = await client.post(
            f"/api/v2/conversations/{conversation_id}/turns",
            json={"content": "/ask Synthetic question.", "idempotency_key": "ask-1"},
        )

    assert first.status_code == 202
    assert first.json()["command"] == "ask"
    assert first.json()["run"]["run_kind"] == "skill"
    assert first.json()["run"]["selection"]["source"] == "command"
    assert first.json()["run"]["selection"]["skill"]["version"] == "0.5.0"
    assert second.json()["run"]["run_id"] == first.json()["run"]["run_id"]
    replayed = await events.replay(UUID(first.json()["run"]["run_id"]))
    assert [event.event_type for event in replayed] == [
        AssistantEventType.ACCEPTED,
        AssistantEventType.SKILL_STARTED,
    ]
