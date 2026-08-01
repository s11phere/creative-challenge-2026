from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from agent_runtime.skills import (
    FileSystemSkillRegistry,
    SkillRegistryError,
    SkillRegistryErrorCode,
)


def manifest(*, name: str = "test_skill", version: str = "1.0.0") -> dict[str, object]:
    return {
        "manifest_version": "1",
        "name": name,
        "version": version,
        "description": "Synthetic Skill fixture.",
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
        "compatibility": {
            "runtime": ">=0.1.0,<1.0.0",
            "checkpoint_schema_versions": [1],
        },
        "prompts": ["prompts/system.md"],
        "evals": ["evals/cases.jsonl"],
    }


def write_package(
    root: Path,
    directory: str,
    *,
    data: dict[str, object] | None = None,
    prompt: str = "synthetic prompt\n",
    newline: str = "\n",
) -> Path:
    package = root / directory
    (package / "schemas").mkdir(parents=True)
    (package / "prompts").mkdir()
    (package / "evals").mkdir()
    manifest_text = yaml.safe_dump(data or manifest(), sort_keys=False).replace("\n", newline)
    (package / "skill.yaml").write_text(manifest_text, encoding="utf-8", newline="")
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }
    (package / "schemas" / "input.json").write_text(json.dumps(schema), encoding="utf-8")
    (package / "schemas" / "output.json").write_text(json.dumps(schema), encoding="utf-8")
    (package / "workflow.yaml").write_text("workflow_version: '1'\nnodes: []\n", encoding="utf-8")
    (package / "prompts" / "system.md").write_text(prompt, encoding="utf-8")
    (package / "evals" / "cases.jsonl").write_text('{"case_id":"synthetic"}\n', encoding="utf-8")
    (package / "README.md").write_text("# Synthetic fixture\n", encoding="utf-8")
    return package


def test_load_register_activate_and_pin(tmp_path: Path) -> None:
    write_package(tmp_path, "skill-v1")
    registry = FileSystemSkillRegistry(tmp_path)
    package = registry.register(registry.load("skill-v1"))
    assert package.manifest.name == "test_skill"
    assert len(package.content_sha256) == 64
    assert registry.is_available("test_skill", "1.0.0", package.content_sha256)
    registry.activate("test_skill", "1.0.0")
    pin = registry.pin("test_skill")
    assert pin.content_sha256 == package.content_sha256
    assert pin.entrypoint_sha256
    assert registry.validate_pin(pin) is package


def test_duplicate_is_idempotent_and_changed_content_conflicts(tmp_path: Path) -> None:
    write_package(tmp_path, "first")
    write_package(tmp_path, "same")
    write_package(tmp_path, "changed", prompt="changed prompt\n")
    registry = FileSystemSkillRegistry(tmp_path)
    first = registry.register(registry.load("first"))
    assert registry.register(registry.load("same")) is first
    with pytest.raises(SkillRegistryError) as captured:
        registry.register(registry.load("changed"))
    assert captured.value.code == SkillRegistryErrorCode.VERSION_CONFLICT


def test_active_switch_does_not_change_existing_pin(tmp_path: Path) -> None:
    write_package(tmp_path, "v1")
    write_package(tmp_path, "v2", data=manifest(version="2.0.0"))
    registry = FileSystemSkillRegistry(tmp_path)
    registry.register(registry.load("v1"))
    registry.register(registry.load("v2"))
    registry.activate("test_skill", "1.0.0")
    old_pin = registry.pin("test_skill")
    registry.activate("test_skill", "2.0.0")
    assert registry.pin("test_skill").version == "2.0.0"
    assert old_pin.version == "1.0.0"
    assert registry.validate_pin(old_pin).manifest.version == "1.0.0"


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda data: data.pop("budgets"), SkillRegistryErrorCode.INVALID_MANIFEST),
        (lambda data: data.update({"unknown": True}), SkillRegistryErrorCode.INVALID_MANIFEST),
        (
            lambda data: data.update({"input_schema": "../outside.json"}),
            SkillRegistryErrorCode.PATH_OUTSIDE_TRUSTED_ROOT,
        ),
    ],
)
def test_invalid_manifest_and_path_are_rejected(
    tmp_path: Path, mutate: object, code: SkillRegistryErrorCode
) -> None:
    data = manifest()
    callable_mutate = mutate
    assert callable(callable_mutate)
    callable_mutate(data)
    write_package(tmp_path, "invalid", data=data)
    registry = FileSystemSkillRegistry(tmp_path)
    with pytest.raises(SkillRegistryError) as captured:
        registry.load("invalid")
    assert captured.value.code == code


def test_remote_schema_reference_and_invalid_schema_are_rejected(tmp_path: Path) -> None:
    package = write_package(tmp_path, "remote")
    remote = {"$ref": "https://example.test/schema.json"}
    (package / "schemas" / "input.json").write_text(json.dumps(remote), encoding="utf-8")
    registry = FileSystemSkillRegistry(tmp_path)
    with pytest.raises(SkillRegistryError) as captured:
        registry.load("remote")
    assert captured.value.code == SkillRegistryErrorCode.PATH_OUTSIDE_TRUSTED_ROOT

    package = write_package(tmp_path, "invalid-schema")
    (package / "schemas" / "input.json").write_text(
        json.dumps({"type": "unknown"}), encoding="utf-8"
    )
    with pytest.raises(SkillRegistryError) as invalid:
        registry.load("invalid-schema")
    assert invalid.value.code == SkillRegistryErrorCode.INVALID_SCHEMA


def test_referenced_schema_is_recursively_validated(tmp_path: Path) -> None:
    package = write_package(tmp_path, "referenced")
    (package / "schemas" / "input.json").write_text(
        json.dumps({"$ref": "shared.json"}), encoding="utf-8"
    )
    (package / "schemas" / "shared.json").write_text(
        json.dumps({"$ref": "https://example.test/remote.json"}), encoding="utf-8"
    )
    registry = FileSystemSkillRegistry(tmp_path)
    with pytest.raises(SkillRegistryError) as captured:
        registry.load("referenced")
    assert captured.value.code == SkillRegistryErrorCode.PATH_OUTSIDE_TRUSTED_ROOT


def test_executable_entrypoint_is_rejected_without_execution(tmp_path: Path) -> None:
    data = manifest()
    data["entrypoint"] = "workflow.py"
    package = write_package(tmp_path, "executable", data=data)
    (package / "workflow.py").write_text("raise RuntimeError('must not run')\n", encoding="utf-8")
    registry = FileSystemSkillRegistry(tmp_path)
    with pytest.raises(SkillRegistryError) as captured:
        registry.load("executable")
    assert captured.value.code == SkillRegistryErrorCode.INVALID_MANIFEST


def test_compatibility_and_missing_active_version_are_explicit(tmp_path: Path) -> None:
    incompatible = manifest()
    incompatible["compatibility"] = {
        "runtime": ">=2.0.0",
        "checkpoint_schema_versions": [1],
    }
    write_package(tmp_path, "incompatible", data=incompatible)
    registry = FileSystemSkillRegistry(tmp_path)
    with pytest.raises(SkillRegistryError) as captured:
        registry.load("incompatible")
    assert captured.value.code == SkillRegistryErrorCode.INCOMPATIBLE
    with pytest.raises(SkillRegistryError) as missing:
        registry.get("test_skill")
    assert missing.value.code == SkillRegistryErrorCode.ACTIVE_VERSION_MISSING


def test_digest_normalizes_line_endings_and_pin_detects_mismatch(tmp_path: Path) -> None:
    write_package(tmp_path, "lf", newline="\n")
    write_package(tmp_path, "crlf", newline="\r\n")
    registry = FileSystemSkillRegistry(tmp_path)
    lf = registry.register(registry.load("lf"))
    crlf = registry.load("crlf")
    assert lf.content_sha256 == crlf.content_sha256
    pin = registry.pin("test_skill", "1.0.0")
    changed_pin = pin.__class__(**{**pin.__dict__, "content_sha256": "0" * 64})
    with pytest.raises(SkillRegistryError) as captured:
        registry.validate_pin(changed_pin)
    assert captured.value.code == SkillRegistryErrorCode.DIGEST_MISMATCH


def test_pin_revalidation_detects_package_tampering(tmp_path: Path) -> None:
    package_root = write_package(tmp_path, "installed")
    registry = FileSystemSkillRegistry(tmp_path)
    package = registry.register(registry.load("installed"))
    registry.activate("test_skill", "1.0.0")
    pin = registry.pin("test_skill")
    (package_root / "prompts" / "system.md").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(SkillRegistryError) as captured:
        registry.validate_pin(pin)
    assert captured.value.code == SkillRegistryErrorCode.DIGEST_MISMATCH
    assert package.content_sha256 == pin.content_sha256


def test_cleanup_removes_only_matching_non_active_version(tmp_path: Path) -> None:
    write_package(tmp_path, "installed-v1")
    write_package(tmp_path, "installed-v2", data=manifest(version="2.0.0"))
    registry = FileSystemSkillRegistry(tmp_path)
    package = registry.register(registry.load("installed-v1"))
    registry.register(registry.load("installed-v2"))
    registry.activate("test_skill", "2.0.0")
    with pytest.raises(SkillRegistryError) as captured:
        registry.remove(
            "test_skill", "2.0.0", content_sha256=registry.get("test_skill", "2.0.0").content_sha256
        )
    assert captured.value.code == SkillRegistryErrorCode.CLEANUP_BLOCKED
    removed = registry.remove("test_skill", "1.0.0", content_sha256=package.content_sha256)
    assert removed.content_sha256 == package.content_sha256
    assert registry.versions("test_skill") == ("2.0.0",)


def test_symbolic_link_package_is_rejected_when_supported(tmp_path: Path) -> None:
    write_package(tmp_path, "real")
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(tmp_path / "real", target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("symbolic link creation is unavailable in this Windows environment")
    registry = FileSystemSkillRegistry(tmp_path)
    with pytest.raises(SkillRegistryError) as captured:
        registry.load("linked")
    assert captured.value.code == SkillRegistryErrorCode.LINK_NOT_ALLOWED


def test_load_all_skips_template_directories(tmp_path: Path) -> None:
    write_package(tmp_path, "_template")
    write_package(tmp_path, "installed")
    registry = FileSystemSkillRegistry(tmp_path)
    loaded = registry.load_all()
    assert [package.root.name for package in loaded] == ["installed"]


def test_repository_skill_template_is_valid() -> None:
    trusted_root = Path(__file__).parents[2] / "skills"
    package = FileSystemSkillRegistry(trusted_root).load("_template")
    assert package.manifest.name == "skill_template"
    assert package.manifest.entrypoint == "workflow.yaml"


def test_yaml_alias_is_rejected_before_manifest_construction(tmp_path: Path) -> None:
    package = write_package(tmp_path, "alias")
    manifest_path = package / "skill.yaml"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8") + "alias_test: &value [*value]\n",
        encoding="utf-8",
    )
    registry = FileSystemSkillRegistry(tmp_path)
    with pytest.raises(SkillRegistryError) as captured:
        registry.load("alias")
    assert captured.value.code == SkillRegistryErrorCode.INVALID_MANIFEST
