from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from agent_runtime import PersonalSkillRegistry, SkillRegistryError
from application.skills import (
    PersonalSkillError,
    PersonalSkillErrorCode,
    PersonalSkillStore,
)
from infrastructure.skill_lifecycle import InMemorySkillActivationStore

INPUT_SCHEMA = {"type": "object", "properties": {"value": {"type": "string"}}}
OUTPUT_SCHEMA = {"type": "object", "properties": {"status": {"type": "string"}}}


def manifest(*, name: str = "my_skill", version: str = "1.0.0") -> dict[str, object]:
    return {
        "manifest_version": "1",
        "name": name,
        "version": version,
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


def package_files(**overrides: object) -> dict[str, str]:
    data = manifest()
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
def store(tmp_path: Path) -> PersonalSkillStore:
    builtin_root = tmp_path / "builtin"
    builtin_root.mkdir()
    builtin_files = package_files(name="builtin_skill")
    for relative, content in builtin_files.items():
        path = builtin_root / "builtin_skill" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    registry = PersonalSkillRegistry(builtin_root, personal_root=tmp_path / "personal")
    registry.reload()
    return PersonalSkillStore(
        registry=registry,
        store=InMemorySkillActivationStore(),
    )


class TestPersonalSkillStoreCrud:
    def test_create_and_list(self, store: PersonalSkillStore) -> None:
        created = store.create("my_skill", package_files())
        assert created.name == "my_skill"
        assert created.active is False
        assert created.permissions == ("read_knowledge",)

        listed = store.list()
        assert [view.name for view in listed] == ["my_skill"]

    def test_get_missing_skill(self, store: PersonalSkillStore) -> None:
        with pytest.raises(PersonalSkillError) as excinfo:
            store.get("missing")
        assert excinfo.value.code is PersonalSkillErrorCode.NOT_FOUND

    def test_create_raises_registry_conflict_for_builtin_name(
        self, store: PersonalSkillStore
    ) -> None:
        with pytest.raises(SkillRegistryError):
            store.create("builtin_skill", package_files(name="builtin_skill"))

    async def test_update_replaces_content(self, store: PersonalSkillStore) -> None:
        store.create("my_skill", package_files())
        updated = await store.update("my_skill", package_files(description="Updated."))

        assert updated.description == "Updated."
        assert store.get("my_skill").description == "Updated."

    async def test_delete_removes(self, store: PersonalSkillStore) -> None:
        store.create("my_skill", package_files())
        await store.delete("my_skill")
        assert store.list() == ()

    async def test_delete_missing_skill(self, store: PersonalSkillStore) -> None:
        with pytest.raises(SkillRegistryError):
            await store.delete("missing")


class TestPersonalSkillStoreActivation:
    async def test_activate_persists_pointer(self, store: PersonalSkillStore) -> None:
        store.create("my_skill", package_files())
        activated = await store.activate("my_skill")

        assert activated.active is True
        assert store.get("my_skill").active is True

    async def test_activate_rejects_missing_skill(self, store: PersonalSkillStore) -> None:
        with pytest.raises(PersonalSkillError) as excinfo:
            await store.activate("missing")
        assert excinfo.value.code is PersonalSkillErrorCode.NOT_FOUND

    async def test_update_active_skill_refreshes_activation_pointer(
        self, store: PersonalSkillStore
    ) -> None:
        store.create("my_skill", package_files())
        await store.activate("my_skill")
        updated = await store.update("my_skill", package_files(description="Updated content"))

        assert updated.active is True
        assert store.get("my_skill").active is True
        pointer = await store._store.get("my_skill")
        assert pointer is not None
        assert pointer.content_sha256 == updated.content_sha256

    async def test_delete_active_skill_removes_activation_pointer(
        self, store: PersonalSkillStore
    ) -> None:
        store.create("my_skill", package_files())
        await store.activate("my_skill")
        await store.delete("my_skill")

        assert store.list() == ()
        assert await store._store.get("my_skill") is None
