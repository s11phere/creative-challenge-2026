"""Skill Creator Tools tests: scaffold generation, handlers, registration, gate."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
import yaml
from agent_runtime import InMemoryToolRegistry, PersonalSkillRegistry, ToolExecutionContext
from application.skills import (
    DraftSkillEvalRunner,
    PersonalSkillStore,
    SkillCreatorTools,
    SkillDraftStore,
    register_skill_creator_tools,
    scaffold_skill_files,
)
from infrastructure.qa_execution import _skill_output_schema
from infrastructure.skill_lifecycle import InMemorySkillActivationStore

from tests.unit.test_skill_drafts import write_builtin_package


@pytest.fixture
def creator(tmp_path: Path) -> SkillCreatorTools:
    builtin_root = tmp_path / "builtin"
    builtin_root.mkdir()
    write_builtin_package(builtin_root, "builtin_skill")
    registry = PersonalSkillRegistry(builtin_root, personal_root=tmp_path / "personal")
    registry.reload()
    activation_store = InMemorySkillActivationStore()
    draft_store = SkillDraftStore(
        registry=registry,
        personal_store=PersonalSkillStore(registry=registry, store=activation_store),
        eval_runner=DraftSkillEvalRunner(registry=registry),
    )
    return SkillCreatorTools(draft_store=draft_store)


def _context() -> ToolExecutionContext:
    return cast(ToolExecutionContext, SimpleNamespace())


class TestScaffoldFiles:
    def test_generates_complete_loadable_package(self) -> None:
        files = scaffold_skill_files(
            name="my_skill",
            description="Summarize anything with citations.",
        )
        assert set(files) == {
            "skill.yaml",
            "workflow.yaml",
            "schemas/input.json",
            "schemas/output.json",
            "prompts/system.md",
            "evals/cases.jsonl",
        }
        manifest = yaml.safe_load(files["skill.yaml"])
        assert manifest["name"] == "my_skill"
        assert manifest["invocation"]["command"] == "my-skill"
        assert manifest["invocation"]["execution_mode"] == "projected"
        workflow = yaml.safe_load(files["workflow.yaml"])
        assert [node["handler"] for node in workflow["nodes"]] == [
            "grounded_qa_plan",
            "grounded_qa_plan",
            "grounded_qa_delegate",
            "grounded_qa_verify",
        ]
        output = json.loads(files["schemas/output.json"])
        assert "status" in output["properties"] and "result" in output["properties"]

    def test_creator_manifest_uses_native_tool_runtime(self) -> None:
        manifest = yaml.safe_load(
            Path("skills/skill_creator/skill.yaml").read_text(encoding="utf-8")
        )
        assert manifest["invocation"]["execution_mode"] == "native_tool_use"

    def test_merges_user_input_schema_with_question(self) -> None:
        files = scaffold_skill_files(
            name="my_skill",
            description="d",
            input_schema={"type": "object", "properties": {"topic": {"type": "string"}}},
            output_schema={
                "type": "object",
                "properties": {"finding": {"type": "string"}},
            },
        )
        merged_input = json.loads(files["schemas/input.json"])
        assert "question" in merged_input["properties"]
        merged_output = json.loads(files["schemas/output.json"])
        assert "finding" in merged_output["properties"]

    def test_scaffold_passes_draft_validation(self, creator: SkillCreatorTools) -> None:
        # The draft store lives behind the tools; this checks the registry path
        # directly through a scaffold -> validate round trip.
        view = creator._drafts.create(
            "my_skill", scaffold_skill_files(name="my_skill", description="d")
        )
        assert view.complete is True
        assert view.valid is True


class TestSkillScaffoldTool:
    async def test_creates_draft(self, creator: SkillCreatorTools) -> None:
        payload = await creator.skill_scaffold(
            {"name": "my_skill", "description": "Summarize documents."}, _context()
        )
        assert payload["ok"] is True
        assert payload["name"] == "my_skill"
        assert payload["complete"] is True
        assert payload["valid"] is True
        assert creator._drafts.get("my_skill").name == "my_skill"

    async def test_rejects_builtin_name(self, creator: SkillCreatorTools) -> None:
        payload = await creator.skill_scaffold(
            {"name": "builtin_skill", "description": "d"}, _context()
        )
        assert payload["ok"] is False
        assert payload["error"]

    async def test_invalid_name_returns_error(self, creator: SkillCreatorTools) -> None:
        payload = await creator.skill_scaffold(
            {"name": "Bad Name!", "description": "d"}, _context()
        )
        assert payload["ok"] is False


class TestSkillWriteTool:
    async def test_updates_draft_files(self, creator: SkillCreatorTools) -> None:
        await creator.skill_scaffold({"name": "my_skill", "description": "d"}, _context())
        payload = await creator.skill_write(
            {"name": "my_skill", "files": {"notes.md": "hello\n"}}, _context()
        )
        assert payload["ok"] is True
        assert "notes.md" in creator._drafts.get("my_skill").files

    async def test_missing_draft_returns_error(self, creator: SkillCreatorTools) -> None:
        payload = await creator.skill_write(
            {"name": "missing", "files": {"a.txt": "x"}}, _context()
        )
        assert payload["ok"] is False


class TestSkillValidateTool:
    async def test_valid_draft(self, creator: SkillCreatorTools) -> None:
        await creator.skill_scaffold({"name": "my_skill", "description": "d"}, _context())
        payload = await creator.skill_validate({"name": "my_skill"}, _context())
        assert payload["ok"] is True
        assert payload["valid"] is True

    async def test_missing_draft_returns_error(self, creator: SkillCreatorTools) -> None:
        payload = await creator.skill_validate({"name": "missing"}, _context())
        assert payload["ok"] is False


class TestSkillRunEvalTool:
    async def test_gate_passes_for_scaffold(self, creator: SkillCreatorTools) -> None:
        await creator.skill_scaffold({"name": "my_skill", "description": "d"}, _context())
        payload = await creator.skill_run_eval({"name": "my_skill"}, _context())
        assert payload["ok"] is True
        assert payload["total"] == 1
        assert payload["passed"] == 1
        assert payload["gate_passed"] is True

    async def test_custom_output_schema_generates_matching_eval_checks(
        self, creator: SkillCreatorTools
    ) -> None:
        await creator.skill_scaffold(
            {
                "name": "knowledge_outline",
                "description": "Create a knowledge outline.",
                "output_schema": {
                    "type": "object",
                    "properties": {"outline": {"type": "string"}},
                    "required": ["outline"],
                },
            },
            _context(),
        )
        payload = await creator.skill_run_eval({"name": "knowledge_outline"}, _context())
        assert payload["passed"] == 1
        assert payload["gate_passed"] is True

    async def test_missing_draft_returns_error(self, creator: SkillCreatorTools) -> None:
        payload = await creator.skill_run_eval({"name": "missing"}, _context())
        assert payload["ok"] is False


class TestSkillActivateTool:
    async def test_activates_scaffold(self, creator: SkillCreatorTools) -> None:
        await creator.skill_scaffold({"name": "my_skill", "description": "d"}, _context())
        payload = await creator.skill_activate({"name": "my_skill"}, _context())
        assert payload["ok"] is True
        assert payload["active"] is True
        assert payload["name"] == "my_skill"
        # Draft consumed by promotion.
        assert creator._drafts.list() == ()

    async def test_missing_draft_returns_error(self, creator: SkillCreatorTools) -> None:
        payload = await creator.skill_activate({"name": "missing"}, _context())
        assert payload["ok"] is False


class TestToolRegistration:
    def test_all_creator_tools_register(self, creator: SkillCreatorTools) -> None:
        registry = InMemoryToolRegistry(handlers=creator.handlers())
        definitions = register_skill_creator_tools(registry)
        names = {definition.name for definition in definitions}
        assert names == {
            "skill_scaffold",
            "skill_write",
            "skill_validate",
            "skill_run_eval",
            "skill_activate",
            "skill_draft",
        }
        write_tools = {d.name for d in definitions if "write_knowledge" in d.permissions}
        assert write_tools == {"skill_scaffold", "skill_write", "skill_draft", "skill_activate"}


class TestRuntimeGeneralization:
    def test_personal_skill_output_schema_falls_back(self) -> None:
        assert _skill_output_schema("my_personal_skill") == "personal-skill-output-v1"
        assert _skill_output_schema("summarize_document") == "summarize-document-skill-output-v1"
        assert _skill_output_schema("knowledge_agent") == "knowledge-agent-skill-output-v1"
