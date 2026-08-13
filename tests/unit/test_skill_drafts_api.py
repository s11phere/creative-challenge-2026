"""HTTP projection tests for Skill Creator drafts and the eval gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from api.main import create_app
from application.qa import InMemoryGroundedQARepository
from application.skills import scaffold_skill_files
from domain.agent_sse import AgentRunEventLog
from domain.assistant_sse import AssistantEventLog
from domain.qa_sse import QAEventLog
from httpx import ASGITransport, AsyncClient
from infrastructure.config import settings
from infrastructure.skill_lifecycle import InMemorySkillActivationStore
from model_gateway import FakeModelGateway

DRAFTS = "/api/v1/skills/personal/drafts"


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "personal_skills_dir", str(tmp_path / "personal"))
    return create_app(
        model_gateway=FakeModelGateway(),
        enable_qa_execution=False,
        qa_repository=InMemoryGroundedQARepository(),
        qa_event_store=QAEventLog(),
        assistant_event_store=AssistantEventLog(),
        agent_event_store=AgentRunEventLog(),
        skill_activation_store=InMemorySkillActivationStore(),
    )


def _scaffold_files(name: str = "my_skill") -> dict[str, str]:
    return scaffold_skill_files(name=name, description="A personal Skill fixture.")


async def _create_draft(client: AsyncClient, name: str = "my_skill") -> None:
    response = await client.post(DRAFTS, json={"name": name, "files": _scaffold_files(name)})
    assert response.status_code == 201, response.text


class TestDraftCrud:
    async def test_create_list_get_files(self, app) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await _create_draft(client)
            listed = await client.get(DRAFTS)
            assert listed.status_code == 200
            assert [item["name"] for item in listed.json()] == ["my_skill"]
            detail = await client.get(f"{DRAFTS}/my_skill")
            assert detail.json()["valid"] is True
            assert detail.json()["complete"] is True
            files = await client.get(f"{DRAFTS}/my_skill/files")
            assert "skill.yaml" in files.json()["files"]

    async def test_create_rejects_builtin_name(self, app) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                DRAFTS,
                json={"name": "knowledge_agent", "files": _scaffold_files("knowledge_agent")},
            )
            assert response.status_code == 409

    async def test_get_missing_draft_404(self, app) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"{DRAFTS}/missing")
            assert response.status_code == 404

    async def test_update_and_reject(self, app) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await _create_draft(client)
            updated = await client.put(
                f"{DRAFTS}/my_skill", json={"files": {"notes.md": "hello\n"}}
            )
            assert updated.status_code == 200
            assert "notes.md" in updated.json()["files"]
            rejected = await client.delete(f"{DRAFTS}/my_skill")
            assert rejected.status_code == 200
            assert rejected.json()["status"] == "rejected"
            assert (await client.get(DRAFTS)).json() == []


class TestDraftGate:
    async def test_validate_eval_activate_lifecycle(self, app) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await _create_draft(client)
            validated = await client.post(f"{DRAFTS}/my_skill/validate")
            assert validated.status_code == 200
            assert validated.json()["valid"] is True

            evaled = await client.post(f"{DRAFTS}/my_skill/eval")
            assert evaled.status_code == 200
            body = evaled.json()
            assert body["gate_passed"] is True
            assert body["metrics"]["passed"] == 1

            activated = await client.post(f"{DRAFTS}/my_skill/activate")
            assert activated.status_code == 200
            assert activated.json()["active"] is True
            # Draft is consumed by promotion.
            assert (await client.get(DRAFTS)).json() == []

    async def test_activate_rejected_when_draft_invalid(self, app) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post(DRAFTS, json={"name": "bad", "files": {"skill.yaml": "name: bad\n"}})
            response = await client.post(f"{DRAFTS}/bad/validate")
            assert response.json()["valid"] is False
            activated = await client.post(f"{DRAFTS}/bad/activate")
            assert activated.status_code == 400


class TestDraftEvidence:
    async def test_evidence_endpoint_returns_attached_evidence(self, app) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            files = _scaffold_files()
            files["evidence.json"] = json.dumps(
                {"frequency": 3, "exemplars": [{"run_id": "a" * 32}]}
            )
            response = await client.post(DRAFTS, json={"name": "my_skill", "files": files})
            assert response.status_code == 201
            evidence = await client.get(f"{DRAFTS}/my_skill/evidence")
            assert evidence.status_code == 200
            body = evidence.json()
            assert body["name"] == "my_skill"
            assert body["evidence"]["frequency"] == 3

    async def test_evidence_endpoint_returns_null_without_evidence(self, app) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await _create_draft(client)
            response = await client.get(f"{DRAFTS}/my_skill/evidence")
            assert response.status_code == 200
            assert response.json()["evidence"] is None

    async def test_evidence_endpoint_404_for_missing_draft(self, app) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"{DRAFTS}/missing/evidence")
            assert response.status_code == 404
