from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from agent_runtime.skills import (
    FileSystemSkillRegistry,
    PinnedSkill,
    SkillRegistryError,
    SkillRegistryErrorCode,
    SkillRegistryEventType,
)
from application.skills import (
    SkillActivation,
    SkillLifecycleService,
)
from domain.agent_runtime import RunCheckpoint
from infrastructure.skill_lifecycle import InMemorySkillActivationStore


class FakeToolRegistry:
    def __init__(self, available: bool = True) -> None:
        self.available = available

    def is_available(self, name: str, version: str) -> bool:
        return self.available and (name, version) == ("search_knowledge", "1.0.0")


def write_package(
    root: Path,
    directory: str,
    *,
    version: str,
    prompt: str | None = None,
    valid: bool = True,
) -> Path:
    package = root / directory
    (package / "schemas").mkdir(parents=True)
    (package / "prompts").mkdir()
    (package / "evals").mkdir()
    manifest = {
        "manifest_version": "1",
        "name": "lifecycle_skill",
        "version": version,
        "description": "Synthetic lifecycle fixture.",
        "input_schema": "schemas/input.json",
        "output_schema": "schemas/output.json",
        "required_tools": [{"name": "search_knowledge", "version": "1.0.0"}],
        "required_capabilities": [],
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
    (package / "skill.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    schema = {"type": "object", "additionalProperties": False}
    (package / "schemas" / "input.json").write_text(json.dumps(schema), encoding="utf-8")
    if valid:
        (package / "schemas" / "output.json").write_text(json.dumps(schema), encoding="utf-8")
    (package / "workflow.yaml").write_text(
        "workflow_version: '1'\nstart: plan\nnodes: []\n", encoding="utf-8"
    )
    (package / "prompts" / "system.md").write_text(
        prompt or f"version {version}\n", encoding="utf-8"
    )
    (package / "evals" / "cases.jsonl").write_text('{"case_id":"synthetic"}\n', encoding="utf-8")
    (package / "README.md").write_text("# Lifecycle fixture\n", encoding="utf-8")
    return package


def checkpoint_for(
    pin: PinnedSkill, *, schema_version: int = 1, verified: bool = True
) -> RunCheckpoint:
    return RunCheckpoint(
        run_id=UUID("00000000-0000-4000-8000-000000000010"),
        sequence=1,
        schema_version=schema_version,
        skill_name=pin.name,
        skill_version=pin.version,
        skill_content_sha256=pin.content_sha256,
        verified=verified,
    )


def test_reload_is_transactional_when_one_package_is_invalid(tmp_path: Path) -> None:
    write_package(tmp_path, "valid", version="1.0.0")
    write_package(tmp_path, "invalid", version="2.0.0", valid=False)
    registry = FileSystemSkillRegistry(tmp_path)
    with pytest.raises(SkillRegistryError):
        registry.reload()
    assert registry.versions("lifecycle_skill") == ()
    assert registry.events == ()


def test_upgrade_keeps_old_pin_and_rollback_changes_only_new_default(tmp_path: Path) -> None:
    write_package(tmp_path, "v1", version="1.0.0")
    registry = FileSystemSkillRegistry(tmp_path)
    registry.reload()
    registry.activate("lifecycle_skill", "1.0.0")
    old_pin = registry.pin("lifecycle_skill")

    write_package(tmp_path, "v2", version="2.0.0")
    registry.reload()
    registry.activate("lifecycle_skill", "2.0.0")
    assert registry.pin("lifecycle_skill").version == "2.0.0"
    assert registry.validate_pin(old_pin).manifest.version == "1.0.0"

    registry.rollback("lifecycle_skill", "1.0.0")
    assert registry.active_version("lifecycle_skill") == "1.0.0"
    assert registry.pin("lifecycle_skill").version == "1.0.0"
    assert [event.event_type for event in registry.events] == [
        SkillRegistryEventType.INSTALLED,
        SkillRegistryEventType.ACTIVATED,
        SkillRegistryEventType.INSTALLED,
        SkillRegistryEventType.ACTIVATED,
        SkillRegistryEventType.ROLLED_BACK,
    ]


def test_reload_conflict_and_rollback_failure_preserve_active_version(tmp_path: Path) -> None:
    package = write_package(tmp_path, "v1", version="1.0.0")
    registry = FileSystemSkillRegistry(tmp_path)
    registry.reload()
    registry.activate("lifecycle_skill", "1.0.0")

    (package / "prompts" / "system.md").write_text("mutated\n", encoding="utf-8")
    with pytest.raises(SkillRegistryError) as conflict:
        registry.reload()
    assert conflict.value.code == SkillRegistryErrorCode.VERSION_CONFLICT
    assert registry.active_version("lifecycle_skill") == "1.0.0"
    with pytest.raises(SkillRegistryError) as rollback:
        registry.rollback("lifecycle_skill", "9.0.0")
    assert rollback.value.code == SkillRegistryErrorCode.NOT_FOUND
    assert registry.active_version("lifecycle_skill") == "1.0.0"


def test_checkpoint_recovery_validates_pin_schema_and_tool_versions(tmp_path: Path) -> None:
    write_package(tmp_path, "v1", version="1.0.0")
    registry = FileSystemSkillRegistry(tmp_path)
    registry.reload()
    registry.activate("lifecycle_skill", "1.0.0")
    pin = registry.pin("lifecycle_skill")
    checkpoint = checkpoint_for(pin)
    package = registry.validate_checkpoint_compatibility(
        pin, checkpoint, tool_registry=FakeToolRegistry()
    )
    assert package.manifest.version == "1.0.0"

    with pytest.raises(SkillRegistryError) as schema:
        registry.validate_checkpoint_compatibility(
            pin,
            checkpoint_for(pin, schema_version=2),
            tool_registry=FakeToolRegistry(),
        )
    assert schema.value.code == SkillRegistryErrorCode.CHECKPOINT_INCOMPATIBLE

    with pytest.raises(SkillRegistryError) as tool:
        registry.validate_checkpoint_compatibility(
            pin, checkpoint, tool_registry=FakeToolRegistry(False)
        )
    assert tool.value.code == SkillRegistryErrorCode.TOOL_INCOMPATIBLE


def test_checkpoint_recovery_rejects_unverified_or_changed_package(tmp_path: Path) -> None:
    package = write_package(tmp_path, "v1", version="1.0.0")
    registry = FileSystemSkillRegistry(tmp_path)
    registry.reload()
    pin = registry.pin("lifecycle_skill", "1.0.0")
    with pytest.raises(SkillRegistryError) as unverified:
        registry.validate_checkpoint_compatibility(
            pin,
            checkpoint_for(pin, verified=False),
            tool_registry=FakeToolRegistry(),
        )
    assert unverified.value.code == SkillRegistryErrorCode.CHECKPOINT_INCOMPATIBLE

    (package / "prompts" / "system.md").write_text("changed after pin\n", encoding="utf-8")
    with pytest.raises(SkillRegistryError) as changed:
        registry.validate_checkpoint_compatibility(
            pin, checkpoint_for(pin), tool_registry=FakeToolRegistry()
        )
    assert changed.value.code == SkillRegistryErrorCode.DIGEST_MISMATCH


def test_concurrent_activation_and_pinning_return_only_complete_versions(tmp_path: Path) -> None:
    write_package(tmp_path, "v1", version="1.0.0")
    write_package(tmp_path, "v2", version="2.0.0")
    registry = FileSystemSkillRegistry(tmp_path)
    registry.reload()
    registry.activate("lifecycle_skill", "1.0.0")

    def switch_and_pin(index: int) -> PinnedSkill:
        registry.activate("lifecycle_skill", "1.0.0" if index % 2 == 0 else "2.0.0")
        return registry.pin("lifecycle_skill")

    with ThreadPoolExecutor(max_workers=4) as pool:
        pins = list(pool.map(switch_and_pin, range(24)))
    assert {pin.version for pin in pins}.issubset({"1.0.0", "2.0.0"})
    for pin in pins:
        registry.validate_pin(pin)


@pytest.mark.asyncio
async def test_current_skill_replaces_a_stale_persisted_activation(
    tmp_path: Path,
) -> None:
    write_package(tmp_path, "v1", version="1.0.0")
    write_package(tmp_path, "v2", version="2.0.0")
    store = InMemorySkillActivationStore()

    first_registry = FileSystemSkillRegistry(tmp_path)
    first_registry.reload()
    first = SkillLifecycleService(
        registry=first_registry,
        store=store,
        defaults={"lifecycle_skill": "1.0.0"},
    )
    initial = await first.current("lifecycle_skill")
    assert (initial.version, initial.revision) == ("1.0.0", 1)
    stale_pin = first_registry.pin("lifecycle_skill", "2.0.0")
    stale = await store.compare_and_set(
        SkillActivation("lifecycle_skill", "2.0.0", stale_pin.content_sha256, 2),
        expected_revision=1,
    )
    assert stale is not None

    second_registry = FileSystemSkillRegistry(tmp_path)
    second_registry.reload()
    second = SkillLifecycleService(
        registry=second_registry,
        store=store,
        defaults={"lifecycle_skill": "1.0.0"},
    )
    restored = await second.current("lifecycle_skill")
    assert (restored.version, restored.revision) == ("1.0.0", 3)
    assert second_registry.active_version("lifecycle_skill") == "1.0.0"
