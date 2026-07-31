import json
from uuid import UUID

import pytest
from api.main import create_app
from application.qa import InMemoryGroundedQARepository
from domain.grounded_qa import Citation, CitationResolution, CitationStatus
from domain.qa_persistence import QARetrievalScope
from domain.qa_sse import QAEventLog
from domain.retrieval import LocatorKind, SearchLocator
from httpx import ASGITransport, AsyncClient
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


@pytest.mark.asyncio
async def test_provisional_qa_api_creates_run_cancels_and_replays_events() -> None:
    app = create_app(
        model_gateway=FakeModelGateway(),
        enable_qa_execution=False,
        qa_repository=InMemoryGroundedQARepository(),
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
        assert submitted.json()["skill"]["name"] == "knowledge_qa"
        assert submitted.json()["skill"]["version"] == "0.1.0"
        assert len(submitted.json()["skill"]["content_sha256"]) == 64

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
        versions = await client.get("/api/v1/skills/knowledge_qa/versions")
        missing = await client.get("/api/v1/skills/not_installed/versions")

    assert listed.status_code == 200
    assert {item["name"] for item in listed.json()} == {
        "compare_sources",
        "create_review_cards",
        "knowledge_agent",
        "knowledge_qa",
        "summarize_document",
    }
    knowledge_qa = next(item for item in listed.json() if item["name"] == "knowledge_qa")
    assert knowledge_qa == {
        "name": "knowledge_qa",
        "active_version": "0.1.0",
        "active_revision": 1,
        "versions": ["0.1.0"],
    }
    assert versions.status_code == 200
    payload = versions.json()[0]
    assert payload["active"] is True
    assert payload["content_sha256"]
    assert payload["permissions"] == ["model", "read_knowledge"]
    assert payload["budget"] == {
        "max_steps": 4,
        "max_tool_calls": 1,
        "max_input_tokens": 8192,
        "max_output_tokens": 4096,
        "timeout_seconds": 60,
    }
    assert missing.status_code == 404
    assert missing.json()["code"] == "SKILL_NOT_FOUND"


@pytest.mark.asyncio
async def test_skill_activation_uses_revision_cas_and_only_installed_versions() -> None:
    app = create_app(
        model_gateway=FakeModelGateway(),
        enable_qa_execution=False,
        qa_repository=InMemoryGroundedQARepository(),
        qa_event_store=QAEventLog(),
        skill_activation_store=InMemorySkillActivationStore(),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        activated = await client.put(
            "/api/v1/skills/knowledge_qa/active",
            json={"version": "0.1.0", "expected_revision": 1},
        )
        stale = await client.post(
            "/api/v1/skills/knowledge_qa/rollback",
            json={"version": "0.1.0", "expected_revision": 1},
        )
        missing = await client.put(
            "/api/v1/skills/knowledge_qa/active",
            json={"version": "9.9.9", "expected_revision": 2},
        )
        listed = await client.get("/api/v1/skills")

    assert activated.status_code == 200
    assert activated.json()["revision"] == 2
    assert stale.status_code == 409
    assert stale.json()["code"] == "SKILL_ACTIVATION_CONFLICT"
    assert missing.status_code == 404
    assert missing.json()["code"] == "SKILL_NOT_FOUND"
    knowledge_qa = next(item for item in listed.json() if item["name"] == "knowledge_qa")
    assert knowledge_qa["active_revision"] == 2


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
    assert submitted.json()["skill"]["version"] == "0.1.0"
    assert submitted.json()["fixed_scope"] == {
        "source_ids": [str(UUID(int=30))],
        "document_ids": [str(UUID(int=31))],
        "version_ids": [str(UUID(int=32))],
    }
    assert preview.status_code == 202
    assert preview.json()["write"] == {
        "status": "blocked",
        "code": "SKILL_WRITE_PORT_UNAVAILABLE",
        "side_effects": 0,
    }


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
    assert submitted.json()["skill"]["version"] == "0.1.0"
    assert submitted.json()["fixed_scope"] == {
        "source_ids": [],
        "document_ids": [],
        "version_ids": [],
    }
