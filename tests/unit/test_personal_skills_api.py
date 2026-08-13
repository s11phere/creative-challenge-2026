from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from api.main import create_app
from application.qa import InMemoryGroundedQARepository
from domain.agent_sse import AgentRunEventLog
from domain.assistant_sse import AssistantEventLog
from domain.qa_sse import QAEventLog
from httpx import ASGITransport, AsyncClient
from infrastructure.config import settings
from infrastructure.skill_lifecycle import InMemorySkillActivationStore
from model_gateway import FakeModelGateway

INPUT_SCHEMA = {"type": "object", "properties": {"value": {"type": "string"}}}
OUTPUT_SCHEMA = {"type": "object", "properties": {"status": {"type": "string"}}}


def package_files(*, name: str = "my_skill", **overrides: object) -> dict[str, str]:
    data = {
        "manifest_version": "1",
        "name": name,
        "version": "1.0.0",
        "description": "Personal Skill fixture.",
        "input_schema": "schemas/input.json",
        "output_schema": "schemas/output.json",
        "required_tools": [{"name": "search_knowledge", "version": "1.0.0"}],
        "required_capabilities": ["retrieval"],
        "permissions": ["read_knowledge"],
        "budgets": {
            "max_steps": 4,
            "max_tool_calls": 2,
            "max_input_tokens": 100,
            "max_output_tokens": 100,
            "timeout_seconds": 30,
        },
        "entrypoint": "workflow.yaml",
        "compatibility": {"runtime": ">=0.1.0,<1.0.0", "checkpoint_schema_versions": [2]},
        "prompts": ["prompts/system.md"],
        "evals": ["evals/cases.jsonl"],
    }
    data.update(overrides)
    return {
        "skill.yaml": yaml.safe_dump(data, sort_keys=False),
        "schemas/input.json": json.dumps(INPUT_SCHEMA),
        "schemas/output.json": json.dumps(OUTPUT_SCHEMA),
        "workflow.yaml": "version: 1\nsteps: []\n",
        "prompts/system.md": "system prompt\n",
        "evals/cases.jsonl": '{"case_id": "c1"}\n',
    }


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


@pytest.mark.asyncio
async def test_personal_skill_crud_and_activation(app) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            "/api/v1/skills/personal", json={"name": "my_skill", "files": package_files()}
        )
        assert created.status_code == 201
        assert created.json()["name"] == "my_skill"
        assert created.json()["active"] is False

        listed = await client.get("/api/v1/skills/personal")
        assert listed.status_code == 200
        assert [skill["name"] for skill in listed.json()] == ["my_skill"]

        got = await client.get("/api/v1/skills/personal/my_skill")
        assert got.status_code == 200
        assert got.json()["description"] == "Personal Skill fixture."

        updated = await client.put(
            "/api/v1/skills/personal/my_skill",
            json={"files": package_files(description="Updated personal Skill.")},
        )
        assert updated.status_code == 200
        assert updated.json()["description"] == "Updated personal Skill."

        activated = await client.post("/api/v1/skills/personal/my_skill/activate")
        assert activated.status_code == 200
        assert activated.json()["active"] is True

        # An active personal Skill can be edited directly; the durable
        # activation pointer is refreshed to the new content.
        reedited = await client.put(
            "/api/v1/skills/personal/my_skill",
            json={"files": package_files(description="Re-edited while active.")},
        )
        assert reedited.status_code == 200
        assert reedited.json()["active"] is True
        assert reedited.json()["description"] == "Re-edited while active."

        # An active personal Skill can be deleted (activation removed too).
        deleted = await client.delete("/api/v1/skills/personal/my_skill")
        assert deleted.status_code == 200
        assert deleted.json()["status"] == "deleted"
        listed_after = await client.get("/api/v1/skills/personal")
        assert [skill["name"] for skill in listed_after.json()] == []


@pytest.mark.asyncio
async def test_personal_skill_validation_and_conflict_errors(app) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # A built-in Skill name cannot be shadowed.
        conflict = await client.post(
            "/api/v1/skills/personal",
            json={"name": "knowledge_agent", "files": package_files(name="knowledge_agent")},
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "SKILL_NAME_CONFLICT"

        # Invalid manifest content is rejected with 400.
        invalid = await client.post(
            "/api/v1/skills/personal", json={"name": "bad_skill", "files": {"skill.yaml": "nope"}}
        )
        assert invalid.status_code == 400

        # Missing skill returns 404.
        missing = await client.get("/api/v1/skills/personal/missing")
        assert missing.status_code == 404

        # A path-traversal file path is rejected.
        traversal = await client.post(
            "/api/v1/skills/personal",
            json={"name": "evil_skill", "files": {"../escape.yaml": "x"}},
        )
        assert traversal.status_code == 400
