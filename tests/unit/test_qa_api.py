import json
from uuid import UUID

import pytest
from api.main import _active_skill_versions, create_app
from application.qa import InMemoryGroundedQARepository
from domain.grounded_qa import Citation, CitationResolution, CitationStatus, QAEvent
from domain.qa_persistence import QARetrievalScope
from domain.qa_sse import QAEventLog
from domain.retrieval import LocatorKind, SearchLocator
from httpx import ASGITransport, AsyncClient
from infrastructure.config import settings
from infrastructure.qa_execution import _assistant_loop_decision
from infrastructure.skill_lifecycle import InMemorySkillActivationStore
from model_gateway import FakeModelGateway


class FakeCitationService:
    async def resolve(self, run_id: UUID, evidence_id: UUID) -> CitationResolution | None:
        if run_id != UUID(int=20) or evidence_id != UUID(int=21):
            return None
        citation = Citation(
            evidence_id=evidence_id,
            space_id=UUID(int=22),
            source_id=UUID(int=23),
            document_id=UUID(int=24),
            version_id=UUID(int=25),
            chunk_id=UUID(int=26),
            locator=SearchLocator(LocatorKind.LINES, 4, 8),
            excerpt_sha256="a" * 64,
        )
        return CitationResolution(citation, CitationStatus.VALID, "minimal excerpt")


class FakeOrganizationScope:
    async def document_scope(
        self, *, space_id: UUID, document_id: UUID, version_id: UUID
    ) -> QARetrievalScope:
        assert space_id
        return QARetrievalScope(
            source_ids=frozenset({UUID(int=30)}),
            document_ids=frozenset({document_id}),
            version_ids=frozenset({version_id}),
        )

    async def sources_scope(
        self, *, space_id: UUID, source_ids: frozenset[UUID]
    ) -> QARetrievalScope:
        assert space_id
        return QARetrievalScope(source_ids=source_ids)


@pytest.fixture(autouse=True)
def enable_current_agent_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "knowledge_agent_skill_version", "1.0.0")


def test_active_skill_versions_exclude_legacy_recovery_package() -> None:
    assert _active_skill_versions() == {
        "knowledge_agent": "1.0.0",
        "summarize_document": "1.0.0",
        "compare_sources": "1.0.0",
        "create_review_cards": "1.0.0",
        "research_reading_workflow": "1.1.0",
    }


def test_fake_assistant_harness_probes_a_chinese_named_subject_before_artifact_work() -> None:
    decision = _assistant_loop_decision(
        json.dumps(
            {
                "input": {"question": "介绍一下 omnistudio 的主要模块，并保存为 markdown 文件"},
                "state": {"observations": []},
            },
            ensure_ascii=False,
        )
    )

    assert decision == {
        "action": "call_tool",
        "tool_name": "knowledge_search",
        "arguments": {"query": "介绍一下 omnistudio 的主要模块"},
    }


def test_fake_assistant_harness_routes_research_topic_to_safe_discovery() -> None:
    decision = _assistant_loop_decision(
        json.dumps(
            {
                "input": {"question": "/research 图神经网络鲁棒性文献综述"},
                "state": {"observations": []},
            },
            ensure_ascii=False,
        )
    )

    assert decision == {
        "action": "call_tool",
        "tool_name": "research_discover",
        "arguments": {"topic": "图神经网络鲁棒性文献综述"},
    }


def test_fake_assistant_harness_routes_an_explicit_filename_to_deep_read() -> None:
    decision = _assistant_loop_decision(
        json.dumps(
            {
                "input": {
                    "question": "/research 请精读 research-workflow-paper-a.md，面向本科生。"
                },
                "state": {"observations": []},
            },
            ensure_ascii=False,
        )
    )

    assert decision == {
        "action": "call_tool",
        "tool_name": "research_prepare",
        "arguments": {
            "mode": "deep_read",
            "document_references": ["research-workflow-paper-a.md"],
        },
    }


def test_fake_assistant_harness_selects_and_writes_a_non_conflicting_markdown_artifact() -> None:
    prefix = {
        "input": {
            "question": "介绍一下 omnistudio 的主要模块，并保存为 markdown 文件",
            "workspace": {"tools_enabled": True},
        },
        "state": {
            "observations": [
                {
                    "tool_name": "finalize_answer",
                    "output": {"ready": True, "outcome": "answer"},
                }
            ]
        },
    }
    listed = _assistant_loop_decision(json.dumps(prefix, ensure_ascii=False))
    assert listed == {
        "action": "call_tool",
        "tool_name": "fs_list",
        "arguments": {"path": "."},
    }

    prefix["state"]["observations"].append(
        {
            "tool_name": "fs_list",
            "output": {"entries": [{"path": "omnistudio-modules.md", "kind": "file"}]},
        }
    )
    written = _assistant_loop_decision(json.dumps(prefix, ensure_ascii=False))
    assert written == {
        "action": "call_tool",
        "tool_name": "fs_write",
        "arguments": {
            "path": "omnistudio-modules-2.md",
            "content": "{{current_grounded_qa_answer}}",
        },
    }


@pytest.mark.asyncio
async def test_provisional_qa_api_creates_run_cancels_and_replays_events() -> None:
    repository = InMemoryGroundedQARepository()
    app = create_app(
        model_gateway=FakeModelGateway(),
        enable_qa_execution=False,
        qa_repository=repository,
        qa_event_store=QAEventLog(),
        skill_activation_store=InMemorySkillActivationStore(),
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
        assert submitted.json()["skill"]["name"] == "knowledge_agent"
        assert submitted.json()["skill"]["version"] == "1.0.0"
        assert len(submitted.json()["skill"]["content_sha256"]) == 64
        parent = await repository.get_conversation_run(run_id)
        assert parent is not None
        assert parent.run_id == run_id
        assert parent.run_kind.value == "skill"

        history = await client.get(
            "/api/v1/spaces/00000000-0000-0000-0000-000000000001/conversations",
            params={"owner_id": "local-user"},
        )
        assert history.status_code == 200
        assert len(history.json()["conversations"]) == 1
        assert history.json()["conversations"][0]["messages"][0]["content"] == (
            "What is supported?"
        )
        assert history.json()["conversations"][0]["runs"][0]["run_id"] == str(run_id)

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
        assert "id: 1" in replay.text

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


@pytest.mark.asyncio
async def test_generic_run_facade_reuses_qa_identity_and_retry_attempts() -> None:
    repository = InMemoryGroundedQARepository()
    app = create_app(
        model_gateway=FakeModelGateway(),
        enable_qa_execution=False,
        qa_repository=repository,
        qa_event_store=QAEventLog(),
        skill_activation_store=InMemorySkillActivationStore(),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            f"/api/v1/spaces/{UUID(int=51)}/conversations",
            json={"owner_id": "local-user"},
        )
        conversation_id = created.json()["conversation_id"]
        submitted = await client.post(
            "/api/v1/runs",
            json={
                "conversation_id": conversation_id,
                "skill_name": "knowledge_agent",
                "question": "retry me",
                "idempotency_key": "retry-question",
            },
        )
        run_id = UUID(submitted.json()["run_id"])
        await repository.transition_run(run_id, QAEvent.START)
        failed = await repository.transition_run(run_id, QAEvent.FAIL, error_code="QA_TIMED_OUT")
        assert failed.status.value == "failed"
        retried = await client.post(f"/api/v1/runs/{run_id}/retry")
        current = await client.get(f"/api/v1/runs/{run_id}")

    assert submitted.status_code == 202
    assert retried.status_code == 202, retried.text
    assert retried.json()["run_id"] == str(run_id)
    assert retried.json()["attempt_id"] != str(failed.attempt.attempt_id)
    assert current.status_code == 200
    assert current.json()["status"] == "queued"


@pytest.mark.asyncio
async def test_citation_endpoint_returns_only_the_resolved_published_excerpt() -> None:
    app = create_app(
        model_gateway=FakeModelGateway(),
        enable_qa_execution=False,
        qa_repository=InMemoryGroundedQARepository(),
        qa_event_store=QAEventLog(),
        qa_citation_service=FakeCitationService(),
        skill_activation_store=InMemorySkillActivationStore(),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/v1/qa/runs/{UUID(int=20)}/citations/{UUID(int=21)}")
        missing = await client.get(f"/api/v1/qa/runs/{UUID(int=20)}/citations/{UUID(int=99)}")

    assert response.status_code == 200
    assert response.json() == {
        "evidence_id": str(UUID(int=21)),
        "source_id": str(UUID(int=23)),
        "document_id": str(UUID(int=24)),
        "version_id": str(UUID(int=25)),
        "chunk_id": str(UUID(int=26)),
        "locator": {"kind": "lines", "start": 4, "end": 8},
        "status": "valid",
        "excerpt": "minimal excerpt",
    }
    assert missing.status_code == 404
    assert missing.json()["code"] == "HTTP_404"
    assert "minimal excerpt" not in missing.text


@pytest.mark.asyncio
async def test_skill_catalog_exposes_only_installed_versions_and_fixed_budget() -> None:
    app = create_app(
        model_gateway=FakeModelGateway(),
        enable_qa_execution=False,
        qa_repository=InMemoryGroundedQARepository(),
        qa_event_store=QAEventLog(),
        skill_activation_store=InMemorySkillActivationStore(),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        listed = await client.get("/api/v1/skills")

    assert listed.status_code == 200
    assert {item["name"] for item in listed.json()} == {
        "compare_sources",
        "create_review_cards",
        "knowledge_agent",
        "summarize_document",
        "research_reading_workflow",
    }
    payload = next(item for item in listed.json() if item["name"] == "knowledge_agent")
    assert payload["version"] == "1.0.0"
    assert payload["content_sha256"]
    assert payload["permissions"] == ["model", "read_knowledge"]
    assert payload["budget"] == {
        "max_steps": 32,
        "max_tool_calls": 24,
        "max_input_tokens": 131072,
        "max_output_tokens": 32768,
        "timeout_seconds": 600,
    }


@pytest.mark.asyncio
async def test_document_organization_skill_fixes_scope_on_the_shared_qa_run() -> None:
    app = create_app(
        model_gateway=FakeModelGateway(),
        enable_qa_execution=False,
        qa_repository=InMemoryGroundedQARepository(),
        qa_event_store=QAEventLog(),
        skill_activation_store=InMemorySkillActivationStore(),
    )
    app.state.organization_scope = FakeOrganizationScope()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            f"/api/v1/spaces/{UUID(int=1)}/conversations",
            json={"owner_id": "local-user"},
        )
        submitted = await client.post(
            f"/api/v1/conversations/{created.json()['conversation_id']}"
            "/skills/summarize_document/runs",
            json={
                "document_id": str(UUID(int=31)),
                "version_id": str(UUID(int=32)),
                "focus": "key constraints",
                "idempotency_key": "summary-1",
            },
        )
        preview = await client.post(
            f"/api/v1/conversations/{created.json()['conversation_id']}"
            "/skills/create_review_cards/runs",
            json={
                "document_id": str(UUID(int=31)),
                "version_id": str(UUID(int=32)),
                "idempotency_key": "cards-1",
            },
        )

    assert submitted.status_code == 202
    assert submitted.json()["skill"]["name"] == "summarize_document"
    assert submitted.json()["skill"]["version"] == "1.0.0"
    assert submitted.json()["fixed_scope"] == {
        "source_ids": [str(UUID(int=30))],
        "document_ids": [str(UUID(int=31))],
        "version_ids": [str(UUID(int=32))],
    }
    assert preview.status_code == 202
    assert preview.json()["write"] == {
        "status": "blocked",
        "code": "SKILL_WRITE_REQUIRES_APPROVAL",
        "side_effects": 0,
    }


@pytest.mark.asyncio
async def test_generic_run_facade_accepts_organization_skill_inputs() -> None:
    app = create_app(
        model_gateway=FakeModelGateway(),
        enable_qa_execution=False,
        qa_repository=InMemoryGroundedQARepository(),
        qa_event_store=QAEventLog(),
        skill_activation_store=InMemorySkillActivationStore(),
    )
    app.state.organization_scope = FakeOrganizationScope()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            f"/api/v1/spaces/{UUID(int=2)}/conversations",
            json={"owner_id": "local-user"},
        )
        conversation_id = created.json()["conversation_id"]
        summary = await client.post(
            "/api/v1/runs",
            json={
                "conversation_id": conversation_id,
                "skill_name": "summarize_document",
                "document_id": str(UUID(int=31)),
                "version_id": str(UUID(int=32)),
                "idempotency_key": "generic-summary-1",
            },
        )
        comparison = await client.post(
            "/api/v1/runs",
            json={
                "conversation_id": conversation_id,
                "skill_name": "compare_sources",
                "source_ids": [str(UUID(int=41)), str(UUID(int=42))],
                "idempotency_key": "generic-compare-1",
            },
        )

    assert summary.status_code == 202
    assert summary.json()["skill"]["name"] == "summarize_document"
    assert summary.json()["fixed_scope"]["document_ids"] == [str(UUID(int=31))]
    assert comparison.status_code == 202
    assert comparison.json()["skill"]["name"] == "compare_sources"
    assert comparison.json()["fixed_scope"]["source_ids"] == [
        str(UUID(int=41)),
        str(UUID(int=42)),
    ]


@pytest.mark.asyncio
async def test_knowledge_agent_uses_the_shared_qa_run_and_fixed_skill_identity() -> None:
    app = create_app(
        model_gateway=FakeModelGateway(),
        enable_qa_execution=False,
        qa_repository=InMemoryGroundedQARepository(),
        qa_event_store=QAEventLog(),
        skill_activation_store=InMemorySkillActivationStore(),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            f"/api/v1/spaces/{UUID(int=1)}/conversations",
            json={"owner_id": "local-user"},
        )
        submitted = await client.post(
            f"/api/v1/conversations/{created.json()['conversation_id']}"
            "/skills/knowledge_agent/runs",
            json={"question": "Use the bounded Agent.", "idempotency_key": "agent-1"},
        )

    assert submitted.status_code == 202
    assert submitted.json()["skill"]["name"] == "knowledge_agent"
    assert submitted.json()["skill"]["version"] == "1.0.0"
    assert submitted.json()["fixed_scope"] == {
        "source_ids": [],
        "document_ids": [],
        "version_ids": [],
    }


@pytest.mark.asyncio
async def test_conversation_history_can_be_archived_by_owner() -> None:
    app = create_app(
        model_gateway=FakeModelGateway(),
        enable_qa_execution=False,
        qa_repository=InMemoryGroundedQARepository(),
        qa_event_store=QAEventLog(),
        skill_activation_store=InMemorySkillActivationStore(),
    )
    space_id = UUID(int=40)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            f"/api/v1/spaces/{space_id}/conversations",
            json={"owner_id": "local-user"},
        )
        conversation_id = created.json()["conversation_id"]
        deleted = await client.delete(
            f"/api/v1/spaces/{space_id}/conversations/{conversation_id}",
            params={"owner_id": "local-user"},
        )
        repeated = await client.delete(
            f"/api/v1/spaces/{space_id}/conversations/{conversation_id}",
            params={"owner_id": "local-user"},
        )
        history = await client.get(
            f"/api/v1/spaces/{space_id}/conversations",
            params={"owner_id": "local-user"},
        )

    assert deleted.status_code == 200
    assert deleted.json() == {"conversation_id": conversation_id, "status": "deleted"}
    assert repeated.status_code == 200
    assert repeated.json()["status"] == "already_deleted"
    assert history.json() == {"conversations": []}
