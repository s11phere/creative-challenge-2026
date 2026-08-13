from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml
from agent_runtime import PersonalSkillRegistry, SkillRegistryError, SkillRegistryErrorCode

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
        "compatibility": {"runtime": ">=0.1.0,<1.0.0", "checkpoint_schema_versions": [1]},
        "prompts": ["prompts/system.md"],
        "evals": ["evals/cases.jsonl"],
    }


def package_files(
    *, name: str = "my_skill", version: str = "1.0.0", **overrides: object
) -> dict[str, str]:
    data = manifest(name=name, version=version)
    data.update(overrides)
    return {
        "skill.yaml": yaml.safe_dump(data, sort_keys=False),
        "schemas/input.json": json.dumps(INPUT_SCHEMA),
        "schemas/output.json": json.dumps(OUTPUT_SCHEMA),
        "workflow.yaml": "version: 1\nsteps: []\n",
        "prompts/system.md": "system prompt\n",
        "evals/cases.jsonl": '{"case_id": "c1"}\n',
    }


def write_builtin_package(root: Path, directory: str) -> None:
    files = package_files(name=directory)
    package = root / directory
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


class TestCreatePersonal:
    def test_creates_and_registers(self, registry: PersonalSkillRegistry, tmp_path: Path) -> None:
        package = registry.create_personal("my_skill", package_files())

        assert package.manifest.name == "my_skill"
        assert registry.is_personal("my_skill")
        assert "my_skill" in registry.personal_names()
        assert (tmp_path / "personal" / "my_skill" / "skill.yaml").is_file()
        assert not registry.is_builtin("my_skill")

    def test_rejects_builtin_name(self, registry: PersonalSkillRegistry) -> None:
        with pytest.raises(SkillRegistryError) as excinfo:
            registry.create_personal("builtin_skill", package_files(name="builtin_skill"))
        assert excinfo.value.code is SkillRegistryErrorCode.NAME_CONFLICT

    def test_rejects_duplicate_personal_name(self, registry: PersonalSkillRegistry) -> None:
        registry.create_personal("my_skill", package_files())
        with pytest.raises(SkillRegistryError) as excinfo:
            registry.create_personal("my_skill", package_files())
        assert excinfo.value.code is SkillRegistryErrorCode.NAME_CONFLICT

    def test_rejects_invalid_name(self, registry: PersonalSkillRegistry) -> None:
        with pytest.raises(SkillRegistryError):
            registry.create_personal("Bad-Name", package_files(name="Bad-Name"))

    def test_rejects_path_traversal_in_file(self, registry: PersonalSkillRegistry) -> None:
        files = package_files()
        files["../escape.yaml"] = "version: 1\n"
        with pytest.raises(SkillRegistryError) as excinfo:
            registry.create_personal("my_skill", files)
        assert excinfo.value.code is SkillRegistryErrorCode.PATH_OUTSIDE_TRUSTED_ROOT
        assert not (registry._personal_root / "my_skill").exists()

    def test_rejects_absolute_file_path(self, registry: PersonalSkillRegistry) -> None:
        files = package_files()
        files["/etc/passwd"] = "root:x:0:0\n"
        with pytest.raises(SkillRegistryError) as excinfo:
            registry.create_personal("my_skill", files)
        assert excinfo.value.code is SkillRegistryErrorCode.PATH_OUTSIDE_TRUSTED_ROOT

    def test_rejects_invalid_manifest_and_cleans_up(self, registry: PersonalSkillRegistry) -> None:
        files = package_files()
        files["skill.yaml"] = "name: [unclosed\n"
        with pytest.raises(SkillRegistryError):
            registry.create_personal("my_skill", files)
        assert "my_skill" not in registry.personal_names()
        assert not (registry._personal_root / "my_skill").exists()

    def test_rejects_missing_manifest_file(self, registry: PersonalSkillRegistry) -> None:
        files = package_files()
        del files["skill.yaml"]
        with pytest.raises(SkillRegistryError):
            registry.create_personal("my_skill", files)

    def test_rejects_manifest_name_mismatch(self, registry: PersonalSkillRegistry) -> None:
        files = package_files(name="other_name")
        with pytest.raises(SkillRegistryError) as excinfo:
            registry.create_personal("my_skill", files)
        assert excinfo.value.code is SkillRegistryErrorCode.INVALID_MANIFEST

    def test_rejects_remote_schema_reference(self, registry: PersonalSkillRegistry) -> None:
        files = package_files()
        files["schemas/input.json"] = json.dumps(
            {"$ref": "https://example.com/schema.json", "type": "object"}
        )
        with pytest.raises(SkillRegistryError) as excinfo:
            registry.create_personal("my_skill", files)
        assert excinfo.value.code is SkillRegistryErrorCode.PATH_OUTSIDE_TRUSTED_ROOT

    def test_rejects_symbolic_link_inside_package(
        self, registry: PersonalSkillRegistry, tmp_path: Path
    ) -> None:
        if os.name == "nt":
            pytest.skip("symbolic links are not portable on this platform")
        registry.create_personal("my_skill", package_files())
        outside = tmp_path / "outside.txt"
        outside.write_text("secret\n", encoding="utf-8")
        link = registry._personal_root / "my_skill" / "schemas" / "link.json"
        link.symlink_to(outside)
        with pytest.raises(SkillRegistryError) as excinfo:
            registry.reload()
        assert excinfo.value.code is SkillRegistryErrorCode.LINK_NOT_ALLOWED


class TestUpdatePersonal:
    def test_update_replaces_content(self, registry: PersonalSkillRegistry) -> None:
        original = registry.create_personal("my_skill", package_files())
        updated = registry.update_personal(
            "my_skill", package_files(description="Updated personal Skill.")
        )

        assert updated.manifest.description == "Updated personal Skill."
        assert updated.content_sha256 != original.content_sha256
        assert registry.get("my_skill", "1.0.0").manifest.description == "Updated personal Skill."

    def test_update_allows_active_skill(self, registry: PersonalSkillRegistry) -> None:
        registry.create_personal("my_skill", package_files())
        registry.activate("my_skill", "1.0.0")
        updated = registry.update_personal("my_skill", package_files(description="Edited live."))

        assert updated.manifest.description == "Edited live."
        # The Skill stays active (same version), now pinned to the edited content.
        assert registry.active_version("my_skill") == "1.0.0"

    def test_update_rejects_missing_skill(self, registry: PersonalSkillRegistry) -> None:
        with pytest.raises(SkillRegistryError) as excinfo:
            registry.update_personal("missing", package_files())
        assert excinfo.value.code is SkillRegistryErrorCode.NOT_FOUND


class TestDeletePersonal:
    def test_delete_removes_skill(self, registry: PersonalSkillRegistry) -> None:
        registry.create_personal("my_skill", package_files())
        removed = registry.delete_personal("my_skill")

        assert removed is not None
        assert "my_skill" not in registry.personal_names()
        assert not (registry._personal_root / "my_skill").exists()

    def test_delete_allows_active_skill(self, registry: PersonalSkillRegistry) -> None:
        registry.create_personal("my_skill", package_files())
        registry.activate("my_skill", "1.0.0")
        registry.delete_personal("my_skill")

        assert "my_skill" not in registry.personal_names()
        assert not (registry._personal_root / "my_skill").exists()
        # The in-memory active pointer is cleared so a deleted Skill is never
        # reported active.
        with pytest.raises(SkillRegistryError) as excinfo:
            registry.active_version("my_skill")
        assert excinfo.value.code is SkillRegistryErrorCode.ACTIVE_VERSION_MISSING


class TestReloadAndActivation:
    def test_reload_loads_existing_personal_skills(
        self, registry: PersonalSkillRegistry, tmp_path: Path
    ) -> None:
        registry.create_personal("my_skill", package_files())
        fresh = PersonalSkillRegistry(tmp_path / "builtin", personal_root=tmp_path / "personal")
        fresh.reload()

        assert "my_skill" in fresh.personal_names()
        assert fresh.get("my_skill", "1.0.0").manifest.name == "my_skill"

    def test_reload_rejects_personal_shadowing_builtin(
        self, registry: PersonalSkillRegistry, tmp_path: Path
    ) -> None:
        write_builtin_package(tmp_path / "personal", "builtin_skill")
        with pytest.raises(SkillRegistryError) as excinfo:
            registry.reload()
        assert excinfo.value.code is SkillRegistryErrorCode.NAME_CONFLICT

    def test_activate_all_applies_persisted_versions(self, registry: PersonalSkillRegistry) -> None:
        registry.create_personal("my_skill", package_files())
        registry.activate_all({"my_skill": "1.0.0", "builtin_skill": "1.0.0"})

        assert registry.active_version("my_skill") == "1.0.0"
        # Built-in activation is not touched by activate_all (only personal names).
        with pytest.raises(SkillRegistryError):
            registry.active_version("builtin_skill")

    def test_activate_all_rejects_missing_personal_version(
        self, registry: PersonalSkillRegistry
    ) -> None:
        registry.create_personal("my_skill", package_files())
        with pytest.raises(SkillRegistryError) as excinfo:
            registry.activate_all({"my_skill": "9.9.9"})
        assert excinfo.value.code is SkillRegistryErrorCode.NOT_FOUND
