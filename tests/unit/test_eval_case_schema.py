from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from agent_runtime.skills import (
    EVAL_CASE_SCHEMA,
    FileSystemSkillRegistry,
    SkillRegistryError,
    SkillRegistryErrorCode,
)
from jsonschema import Draft202012Validator

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

_CASE_VALIDATOR = Draft202012Validator(EVAL_CASE_SCHEMA)


def _manifest() -> dict[str, object]:
    return {
        "manifest_version": "1",
        "name": "test_skill",
        "version": "1.0.0",
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
        "compatibility": {"runtime": ">=0.1.0,<1.0.0", "checkpoint_schema_versions": [1]},
        "prompts": ["prompts/system.md"],
        "evals": ["evals/cases.jsonl"],
    }


def _write_package(root: Path, directory: str, *, cases: str) -> Path:
    package = root / directory
    (package / "schemas").mkdir(parents=True)
    (package / "prompts").mkdir()
    (package / "evals").mkdir()
    (package / "skill.yaml").write_text(
        yaml.safe_dump(_manifest(), sort_keys=False), encoding="utf-8"
    )
    for name in ("input.json", "output.json"):
        (package / "schemas" / name).write_text(
            json.dumps(
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                }
            ),
            encoding="utf-8",
        )
    (package / "workflow.yaml").write_text("workflow_version: '1'\nnodes: []\n", encoding="utf-8")
    (package / "prompts" / "system.md").write_text("synthetic prompt\n", encoding="utf-8")
    (package / "evals" / "cases.jsonl").write_text(cases, encoding="utf-8")
    return package


def _assert_case_valid(case: dict[str, Any]) -> None:
    errors = list(_CASE_VALIDATOR.iter_errors(case))
    assert not errors, f"case {case.get('case_id')} must be valid: {errors[0].message}"


def _assert_case_invalid(case: dict[str, Any], *, path: tuple[object, ...] = ()) -> None:
    errors = list(_CASE_VALIDATOR.iter_errors(case))
    assert errors, f"case {case} must be invalid"
    if path:
        assert any(list(error.absolute_path)[: len(path)] == list(path) for error in errors)


def test_case_schema_accepts_new_style_case_with_all_standard_fields() -> None:
    _assert_case_valid(
        {
            "case_id": "fixture-001",
            "input": {"question": "What is the extractive answer?"},
            "expected": "model_directed_retrieval_with_verified_finalization",
            "fixture": "synthetic_only",
            "checks": [
                {"type": "output_matches_schema"},
                {"type": "output_has_key", "key": "result"},
                {"type": "cites_sources", "min": 2},
                {"type": "trace_tool_called", "tool": "knowledge_search", "min": 1},
                {"type": "finalized"},
            ],
        }
    )


def test_case_schema_accepts_legacy_survey_cases_without_checks() -> None:
    # Existing repository cases carry survey-only labels and no structured input/checks.
    _assert_case_valid(
        {
            "case_id": "research-deep-read-one-paper",
            "scenario": "normal",
            "mode": "deep_read",
            "expected": "structure_and_teaching_with_citations",
            "fixture": "synthetic_only",
        }
    )
    _assert_case_valid(
        {"case_id": "exam-line-one", "expected_question_count": 3, "fixture": "synthetic_only"}
    )
    _assert_case_valid({"case_id": "minimal"})


@pytest.mark.parametrize(
    "check",
    [
        {"type": "output_matches_schema"},
        {"type": "output_has_key", "key": "answer"},
        {"type": "cites_sources"},
        {"type": "cites_sources", "min": 1},
        {"type": "trace_tool_called", "tool": "grounded_answer"},
        {"type": "trace_tool_called", "tool": "verify_answer", "min": 2},
        {"type": "finalized"},
    ],
)
def test_case_schema_accepts_each_supported_check(check: dict[str, Any]) -> None:
    _assert_case_valid({"case_id": "checks", "checks": [check]})


@pytest.mark.parametrize(
    "case",
    [
        {"checks": []},
        {"case_id": ""},
        {"case_id": "x", "input": ["not", "an", "object"]},
        {"case_id": "x", "expected": 3},
        {"case_id": "x", "fixture": ""},
    ],
)
def test_case_schema_rejects_invalid_case_level_fields(case: dict[str, Any]) -> None:
    _assert_case_invalid(case)


@pytest.mark.parametrize(
    "check",
    [
        {"type": "unknown_check"},
        {"type": "output_has_key"},
        {"type": "trace_tool_called"},
        {"type": "trace_tool_called", "tool": "x", "min": 0},
        {"type": "finalized", "surprise": True},
        {"type": "cites_sources", "min": "many"},
        "not-an-object",
    ],
)
def test_case_schema_rejects_invalid_check_entries(check: Any) -> None:
    _assert_case_invalid({"case_id": "x", "checks": [check]}, path=("checks", 0))


def test_manifest_load_accepts_valid_eval_case_file(tmp_path: Path) -> None:
    _write_package(
        tmp_path,
        "valid",
        cases=json.dumps({"case_id": "a", "checks": [{"type": "finalized"}]}) + "\n",
    )
    registry = FileSystemSkillRegistry(tmp_path)
    package = registry.load("valid")
    assert package.manifest.evals == ("evals/cases.jsonl",)


def test_manifest_load_rejects_malformed_eval_case_json(tmp_path: Path) -> None:
    _write_package(tmp_path, "bad-json", cases='{"case_id": "a"}\n{"broken"\n')
    registry = FileSystemSkillRegistry(tmp_path)
    with pytest.raises(SkillRegistryError) as captured:
        registry.load("bad-json")
    assert captured.value.code == SkillRegistryErrorCode.INVALID_MANIFEST
    assert "line 2" in captured.value.args[0]


def test_manifest_load_rejects_non_object_eval_case(tmp_path: Path) -> None:
    _write_package(tmp_path, "non-object", cases='["an", "array"]\n')
    registry = FileSystemSkillRegistry(tmp_path)
    with pytest.raises(SkillRegistryError) as captured:
        registry.load("non-object")
    assert captured.value.code == SkillRegistryErrorCode.INVALID_MANIFEST


def test_manifest_load_rejects_case_missing_case_id(tmp_path: Path) -> None:
    _write_package(tmp_path, "no-id", cases='{"expected": "label"}\n')
    registry = FileSystemSkillRegistry(tmp_path)
    with pytest.raises(SkillRegistryError) as captured:
        registry.load("no-id")
    assert captured.value.code == SkillRegistryErrorCode.INVALID_MANIFEST


def test_manifest_load_rejects_invalid_check_config(tmp_path: Path) -> None:
    _write_package(
        tmp_path,
        "bad-check",
        cases='{"case_id": "a", "checks": [{"type": "output_has_key"}]}\n',
    )
    registry = FileSystemSkillRegistry(tmp_path)
    with pytest.raises(SkillRegistryError) as captured:
        registry.load("bad-check")
    assert captured.value.code == SkillRegistryErrorCode.INVALID_MANIFEST


def test_all_repository_skill_eval_cases_pass_schema() -> None:
    registry = FileSystemSkillRegistry(REPOSITORY_ROOT / "skills")
    packages = registry.reload()
    assert packages, "expected at least one repository Skill"
    for package in packages:
        for reference in package.manifest.evals:
            path = package.root / reference
            assert path.is_file(), f"missing eval file {reference}"
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                case = json.loads(line)
                _assert_case_valid(case)
