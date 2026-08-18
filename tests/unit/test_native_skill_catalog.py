from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from agent_runtime import FileSystemSkillRegistry, NativeSkillPin, ToolRef
from infrastructure.skill_catalog import FileSystemNativeSkillCatalog, FileSystemSkillCatalog


def _manifest() -> dict[str, object]:
    return {
        "manifest_version": "2",
        "name": "native_fixture",
        "version": "1.0.0",
        "description": "Synthetic native catalog fixture.",
        "input_schema": "schemas/input.json",
        "output_schema": "schemas/output.json",
        "required_tools": [],
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
            "checkpoint_schema_versions": [2],
        },
        "prompts": ["prompts/system.md"],
        "evals": ["evals/cases.jsonl"],
        "invocation": {
            "command": "native-fixture",
            "aliases": [],
            "argument_hint": "",
            "trigger": {
                "summary": "Use for synthetic native Tool-use tests.",
                "when": [],
                "avoid_when": [],
                "examples": [],
            },
            "input_mode": "question",
            "execution_mode": "native_tool_use",
        },
    }


def _write_package(root: Path) -> Path:
    package = root / "native-fixture"
    (package / "schemas").mkdir(parents=True)
    (package / "prompts").mkdir()
    (package / "evals").mkdir()
    (package / "skill.yaml").write_text(
        yaml.safe_dump(_manifest(), sort_keys=False), encoding="utf-8"
    )
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "properties": {"question": {"type": "string"}},
        "required": ["question"],
    }
    (package / "schemas" / "input.json").write_text(json.dumps(schema), encoding="utf-8")
    (package / "schemas" / "output.json").write_text(json.dumps(schema), encoding="utf-8")
    (package / "workflow.yaml").write_text("workflow_version: '1'\nnodes: []\n", encoding="utf-8")
    (package / "prompts" / "system.md").write_text("NATIVE_FIXTURE_PROMPT\n", encoding="utf-8")
    (package / "evals" / "cases.jsonl").write_text('{"case_id":"synthetic"}\n', encoding="utf-8")
    return package


def _catalog(tmp_path: Path, *, adapted: bool = True) -> tuple[FileSystemNativeSkillCatalog, Path]:
    package = _write_package(tmp_path)
    registry = FileSystemSkillRegistry(tmp_path)
    registry.register(registry.load(package.name))
    registry.activate("native_fixture", "1.0.0")
    adapters: dict[ToolRef, tuple[ToolRef, ...]] = {}
    if adapted:
        adapters[ToolRef("native_fixture", "1.0.0")] = (ToolRef("synthetic_lookup", "1.0.0"),)
    return (
        FileSystemNativeSkillCatalog(
            registry,
            FileSystemSkillCatalog(registry, include_manifest_v2=True),
            tool_adapters=adapters,
        ),
        package,
    )


def test_native_catalog_lists_thin_route_and_selects_hash_pinned_instructions(
    tmp_path: Path,
) -> None:
    catalog, _package = _catalog(tmp_path)

    route = catalog.list_routes()[0]
    selection = catalog.select("native_fixture")

    assert route.adapter_available is True
    assert route.description == "Use for synthetic native Tool-use tests."
    assert selection.instructions == "NATIVE_FIXTURE_PROMPT"
    assert selection.allowed_tools == (ToolRef("synthetic_lookup", "1.0.0"),)
    assert catalog.resolve(route.pin) == selection


def test_native_catalog_rejects_native_route_without_runtime_adapter(tmp_path: Path) -> None:
    catalog, _package = _catalog(tmp_path, adapted=False)

    with pytest.raises(ValueError, match="has no Runtime adapter"):
        catalog.list_routes()


def test_native_catalog_rejects_a_tampered_selected_pin(tmp_path: Path) -> None:
    catalog, _package = _catalog(tmp_path)
    route = catalog.list_routes()[0]

    with pytest.raises(ValueError):
        catalog.resolve(
            NativeSkillPin(
                name=route.pin.name,
                version=route.pin.version,
                content_sha256="0" * 64,
            )
        )


def test_native_catalog_rejects_prompt_tampering_during_resolve(tmp_path: Path) -> None:
    catalog, package = _catalog(tmp_path)
    route = catalog.list_routes()[0]
    (package / "prompts" / "system.md").write_text("TAMPERED\n", encoding="utf-8")

    with pytest.raises(ValueError):
        catalog.resolve(route.pin)
