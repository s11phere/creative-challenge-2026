from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from agent_runtime import FileSystemSkillRegistry
from infrastructure.qa_execution import assistant_skill_registry
from infrastructure.skill_catalog import FileSystemSkillCatalog

SKILL_ROOT = Path(__file__).resolve().parents[2] / "skills"
WORKFLOW_SKILLS = {
    "research_reading_workflow": "2.0.0",
    "exam_preparation_workflow": "2.0.0",
    "course_project_workflow": "2.0.0",
}


def _schema(package: str) -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads((SKILL_ROOT / package / "schemas/output.json").read_text(encoding="utf-8")),
    )


def test_student_workflow_packages_load_pin_and_are_active_by_default() -> None:
    registry = FileSystemSkillRegistry(SKILL_ROOT)
    registry.reload()
    for name, version in WORKFLOW_SKILLS.items():
        package = registry.get(name, version)
        assert package.manifest.manifest_version == "2"
        assert registry.pin(name, version).content_sha256 == package.content_sha256
    active = FileSystemSkillCatalog(assistant_skill_registry(), include_manifest_v2=True)
    assert set(WORKFLOW_SKILLS).issubset({item.name for item in active.list_active_invocations()})


def test_upgraded_workflows_share_native_complexity_budget() -> None:
    registry = FileSystemSkillRegistry(SKILL_ROOT)
    registry.reload()
    commands: set[str] = set()
    for name, version in WORKFLOW_SKILLS.items():
        package = registry.get(name, version)
        invocation = package.manifest.invocation
        assert invocation is not None and invocation.execution_mode == "native_tool_use"
        budget = package.manifest.budgets
        assert (budget.max_steps, budget.max_tool_calls) == (16, 12)
        assert (budget.max_input_tokens, budget.max_output_tokens) == (65536, 16384)
        assert budget.timeout_seconds == 600
        assert 2 <= len(package.manifest.required_tools) <= 4
        assert not commands.intersection(invocation.commands)
        commands.update(invocation.commands)


def test_upgraded_workflow_outputs_use_small_terminal_envelope() -> None:
    expected = {"schema_version", "status", "outcome", "publication", "next_action"}
    for name in WORKFLOW_SKILLS:
        schema = _schema(name)
        assert set(cast(list[str], schema["required"])) == expected
        assert set(cast(dict[str, object], schema["properties"])) == expected | {"artifact_refs"}


def test_security_boundaries_remain_in_skill_prompts() -> None:
    exam = (SKILL_ROOT / "exam_preparation_workflow/prompts/boundary.md").read_text(
        encoding="utf-8"
    )
    project = (SKILL_ROOT / "course_project_workflow/prompts/boundary.md").read_text(
        encoding="utf-8"
    )
    assert "never expose correct options" in exam
    assert "Never fabricate results" in project


def test_eval_cases_are_synthetic_only() -> None:
    for name in WORKFLOW_SKILLS:
        cases = [
            json.loads(line)
            for line in (SKILL_ROOT / name / "evals/cases.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        assert cases and all(case.get("fixture") == "synthetic_only" for case in cases)
