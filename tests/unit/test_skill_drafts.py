"""Draft lifecycle tests: registry ops, application store, and the creator eval gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from agent_runtime import PersonalSkillRegistry, SkillRegistryError, SkillRegistryErrorCode
from application.skills import (
    DraftSkillEvalRunner,
    PersonalSkillStore,
    SkillDraftError,
    SkillDraftErrorCode,
    SkillDraftStore,
)
from infrastructure.skill_lifecycle import InMemorySkillActivationStore

INPUT_SCHEMA = {"type": "object", "properties": {"question": {"type": "string"}}}
OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"status": {"type": "string"}, "result": {"type": "string"}},
    "required": ["status", "result"],
}

WORKFLOW = """workflow_version: "1"
start: plan
nodes:
  - id: plan
    step: planning
    handler: grounded_qa_plan
    next: retrieve
    max_retries: 0
    required_permissions: []
    reserve: {tool_calls: 0, input_tokens: 0, output_tokens: 0}
  - id: retrieve
    step: retrieving
    handler: grounded_qa_plan
    next: delegate
    max_retries: 0
    required_permissions: []
    reserve: {tool_calls: 0, input_tokens: 0, output_tokens: 0}
  - id: delegate
    step: executing
    handler: grounded_qa_delegate
    next: verify
    max_retries: 0
    required_permissions: [read_knowledge]
    reserve: {tool_calls: 0, input_tokens: 0, output_tokens: 0}
  - id: verify
    step: verifying
    handler: grounded_qa_verify
    next: null
    max_retries: 0
    required_permissions: []
    reserve: {tool_calls: 0, input_tokens: 0, output_tokens: 0}
"""


def manifest(
    *, name: str = "my_skill", description: str = "My personal Skill."
) -> dict[str, object]:
    return {
        "manifest_version": "1",
        "name": name,
        "version": "1.0.0",
        "description": description,
        "input_schema": "schemas/input.json",
        "output_schema": "schemas/output.json",
        "required_tools": [],
        "required_capabilities": [],
        "permissions": ["read_knowledge"],
        "budgets": {
            "max_steps": 8,
            "max_tool_calls": 4,
            "max_input_tokens": 4096,
            "max_output_tokens": 2048,
            "timeout_seconds": 60,
        },
        "entrypoint": "workflow.yaml",
        "compatibility": {"runtime": ">=0.1.0,<1.0.0", "checkpoint_schema_versions": [2]},
        "prompts": ["prompts/system.md"],
        "evals": ["evals/cases.jsonl"],
    }


def scaffold_files(
    *, name: str = "my_skill", description: str = "My personal Skill.", checks: bool = True
) -> dict[str, str]:
    cases = (
        '{"case_id":"scaffold-001","input":{"question":"fixture question"},'
        '"expected":"complete","checks":['
        '{"type":"output_has_key","key":"status"},'
        '{"type":"output_has_key","key":"result"},'
        '{"type":"finalized"}]}\n'
        if checks
        else '{"case_id":"scaffold-thin","expected":"complete"}\n'
    )
    data = manifest(name=name, description=description)
    return {
        "skill.yaml": yaml.safe_dump(data, sort_keys=False),
        "schemas/input.json": json.dumps(INPUT_SCHEMA),
        "schemas/output.json": json.dumps(OUTPUT_SCHEMA),
        "workflow.yaml": WORKFLOW,
        "prompts/system.md": "Return only the structure declared by the output schema.\n",
        "evals/cases.jsonl": cases,
    }


def broken_files(*, name: str = "broken_skill") -> dict[str, str]:
    data = manifest(name=name)
    data["evals"] = ["evals/missing.jsonl"]
    return {
        "skill.yaml": yaml.safe_dump(data, sort_keys=False),
        "schemas/input.json": json.dumps(INPUT_SCHEMA),
        "schemas/output.json": json.dumps(OUTPUT_SCHEMA),
        "workflow.yaml": WORKFLOW,
        "prompts/system.md": "prompt\n",
    }


def write_builtin_package(root: Path, directory: str) -> None:
    data = manifest(name=directory)
    package = root / directory
    files = {
        "skill.yaml": yaml.safe_dump(data, sort_keys=False),
        "schemas/input.json": json.dumps(INPUT_SCHEMA),
        "schemas/output.json": json.dumps(OUTPUT_SCHEMA),
        "workflow.yaml": WORKFLOW,
        "prompts/system.md": "prompt\n",
        "evals/cases.jsonl": '{"case_id": "c1"}\n',
    }
    for relative, content in files.items():
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


@pytest.fixture
def registry(tmp_path: Path) -> PersonalSkillRegistry:
    builtin_root = tmp_path / "builtin"
    builtin_root.mkdir()
    write_builtin_package(builtin_root, "builtin_skill")
    personal_root = tmp_path / "personal"
    registry = PersonalSkillRegistry(builtin_root, personal_root=personal_root)
    registry.reload()
    return registry


@pytest.fixture
def store(registry: PersonalSkillRegistry) -> SkillDraftStore:
    activation_store = InMemorySkillActivationStore()
    personal_store = PersonalSkillStore(registry=registry, store=activation_store)
    return SkillDraftStore(
        registry=registry,
        personal_store=personal_store,
        eval_runner=DraftSkillEvalRunner(registry=registry),
    )


class TestRegistryDrafts:
    def test_create_writes_underscored_draft_dir(self, registry: PersonalSkillRegistry) -> None:
        registry.create_draft("my_skill", scaffold_files())
        assert registry.is_draft("my_skill")
        assert registry.draft_names() == ("my_skill",)
        assert not registry.is_personal("my_skill")
        files = registry.read_draft_files("my_skill")
        assert "skill.yaml" in files and "workflow.yaml" in files


class TestDraftExecutionModeNormalization:
    def test_creator_draft_native_mode_is_normalized_before_validation(
        self, store: SkillDraftStore
    ) -> None:
        files = scaffold_files()
        manifest_data = yaml.safe_load(files["skill.yaml"])
        manifest_data["manifest_version"] = "2"
        manifest_data["invocation"] = {
            "command": "my-skill",
            "aliases": [],
            "argument_hint": "<question>",
            "trigger": {"summary": "My Skill", "when": [], "avoid_when": [], "examples": []},
            "input_mode": "question",
            "execution_mode": "native_tool_use",
        }
        files["skill.yaml"] = yaml.safe_dump(manifest_data, sort_keys=False)

        view = store.create("my_skill", files)

        assert view.valid is True
        stored = store.read_files("my_skill")
        stored_manifest = yaml.safe_load(stored["skill.yaml"])
        assert stored_manifest["invocation"]["execution_mode"] == "projected"

    def test_rejects_builtin_name(self, registry: PersonalSkillRegistry) -> None:
        with pytest.raises(SkillRegistryError) as excinfo:
            registry.create_draft("builtin_skill", scaffold_files(name="builtin_skill"))
        assert excinfo.value.code is SkillRegistryErrorCode.NAME_CONFLICT

    def test_rejects_existing_personal_name(self, registry: PersonalSkillRegistry) -> None:
        registry.create_personal("installed", scaffold_files(name="installed"))
        with pytest.raises(SkillRegistryError) as excinfo:
            registry.create_draft("installed", scaffold_files(name="installed"))
        assert excinfo.value.code is SkillRegistryErrorCode.NAME_CONFLICT

    def test_rejects_duplicate_draft(self, registry: PersonalSkillRegistry) -> None:
        registry.create_draft("my_skill", scaffold_files())
        with pytest.raises(SkillRegistryError) as excinfo:
            registry.create_draft("my_skill", scaffold_files())
        assert excinfo.value.code is SkillRegistryErrorCode.NAME_CONFLICT

    def test_path_traversal_rejected(self, registry: PersonalSkillRegistry) -> None:
        with pytest.raises(SkillRegistryError) as excinfo:
            registry.create_draft(
                "my_skill",
                {"skill.yaml": "name: my_skill\n", "evil/../../escape": "x"},
            )
        assert excinfo.value.code is SkillRegistryErrorCode.PATH_OUTSIDE_TRUSTED_ROOT

    def test_update_and_delete(self, registry: PersonalSkillRegistry) -> None:
        registry.create_draft("my_skill", scaffold_files())
        registry.update_draft("my_skill", {"notes.md": "updated\n"})
        assert "notes.md" in registry.read_draft_files("my_skill")
        registry.delete_draft("my_skill")
        assert not registry.is_draft("my_skill")

    def test_validate_draft_passes_valid_and_fails_incomplete(
        self, registry: PersonalSkillRegistry
    ) -> None:
        registry.create_draft("my_skill", scaffold_files())
        package = registry.validate_draft("my_skill")
        assert package.manifest.name == "my_skill"
        registry.create_draft("broken_skill", broken_files())
        with pytest.raises(SkillRegistryError):
            registry.validate_draft("broken_skill")

    def test_promote_draft_installs_and_removes(self, registry: PersonalSkillRegistry) -> None:
        registry.create_draft("my_skill", scaffold_files())
        package = registry.promote_draft("my_skill")
        assert registry.is_personal("my_skill")
        assert package.manifest.name == "my_skill"
        assert not registry.is_draft("my_skill")

    def test_promote_invalid_draft_rejected(self, registry: PersonalSkillRegistry) -> None:
        registry.create_draft("broken_skill", broken_files())
        with pytest.raises(SkillRegistryError):
            registry.promote_draft("broken_skill")
        assert registry.is_draft("broken_skill")


class TestDraftStore:
    def test_create_list_get_view(self, store: SkillDraftStore) -> None:
        view = store.create("my_skill", scaffold_files())
        assert view.name == "my_skill"
        assert view.complete is True
        assert view.valid is True
        assert "workflow.yaml" in view.files
        listed = store.list()
        assert [item.name for item in listed] == ["my_skill"]
        assert store.get("my_skill") == view

    def test_validate_result(self, store: SkillDraftStore) -> None:
        store.create("my_skill", scaffold_files())
        result = store.validate("my_skill")
        assert result.valid is True
        assert result.description == "My personal Skill."
        store.create("broken_skill", broken_files())
        broken = store.validate("broken_skill")
        assert broken.valid is False
        assert broken.error

    async def test_run_eval_passes_gate(self, store: SkillDraftStore) -> None:
        store.create("my_skill", scaffold_files())
        report = await store.run_eval("my_skill")
        assert report.metrics.total == 1
        assert report.metrics.passed == 1
        assert report.metrics.failed == 0

    async def test_activate_promotes_and_activates(self, store: SkillDraftStore) -> None:
        store.create("my_skill", scaffold_files())
        activated = await store.activate("my_skill")
        assert activated.name == "my_skill"
        assert activated.active is True
        assert not store.list()  # draft consumed by promotion

    async def test_activate_rejected_when_eval_fails(self, store: SkillDraftStore) -> None:
        store.create("broken_skill", broken_files())
        with pytest.raises(SkillDraftError) as excinfo:
            await store.activate("broken_skill")
        assert excinfo.value.code is SkillDraftErrorCode.INVALID

    async def test_activate_rejected_when_no_checks(self, store: SkillDraftStore) -> None:
        store.create("my_skill", scaffold_files(checks=False))
        with pytest.raises(SkillDraftError) as excinfo:
            await store.activate("my_skill")
        assert excinfo.value.code is SkillDraftErrorCode.EVAL_FAILED

    def test_get_missing_draft(self, store: SkillDraftStore) -> None:
        with pytest.raises(SkillDraftError) as excinfo:
            store.get("missing")
        assert excinfo.value.code is SkillDraftErrorCode.NOT_FOUND


class TestDraftEvalRunner:
    async def test_echo_fixture_satisfies_declared_checks(
        self, registry: PersonalSkillRegistry
    ) -> None:
        registry.create_draft("my_skill", scaffold_files())
        runner = DraftSkillEvalRunner(registry=registry)
        report = await runner.evaluate("my_skill")
        assert report.metrics.passed == 1
        case = report.cases[0]
        assert case.status.value == "passed"
        assert all(item.passed for item in case.evidence)
