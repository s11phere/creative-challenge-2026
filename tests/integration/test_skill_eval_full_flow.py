"""Full-flow Skill evaluation on a synthetic fixture Skill (RUN_INTEGRATION=1).

The fixture Skill needs no database: its probe runs through the production
``DeterministicWorkflowExecutor`` with injected handlers. This proves the whole
pipeline — manifest load, eval case load, probe execution, structural judging,
metric aggregation, and report writing — end to end.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml
from agent_runtime import (
    FileSystemSkillRegistry,
    NodeExecutionContext,
    NodeOutcome,
    NodeResult,
)
from application.skills import SkillEvalStatus
from model_gateway import GatewayConfig, create_model_gateway

from scripts.evaluate_skills import (
    FixtureProbe,
    SkillEvalProbeRunner,
    _report_dict,
    _write_report,
    evaluate_skills,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_INTEGRATION") != "1",
        reason="set RUN_INTEGRATION=1 with an isolated migrated PostgreSQL database",
    ),
]


def _manifest() -> dict[str, object]:
    return {
        "manifest_version": "1",
        "name": "fixture_skill",
        "version": "1.0.0",
        "description": "Synthetic eval fixture.",
        "input_schema": "schemas/input.json",
        "output_schema": "schemas/output.json",
        "required_tools": [],
        "required_capabilities": [],
        "permissions": ["read_knowledge"],
        "budgets": {
            "max_steps": 8,
            "max_tool_calls": 4,
            "max_input_tokens": 200,
            "max_output_tokens": 200,
            "timeout_seconds": 30,
        },
        "entrypoint": "workflow.yaml",
        "compatibility": {"runtime": ">=0.1.0,<1.0.0", "checkpoint_schema_versions": [1]},
        "prompts": ["prompts/system.md"],
        "evals": ["evals/cases.jsonl"],
    }


def _write_fixture_skill(root: Path, *, cases: str) -> None:
    package = root / "fixture_skill"
    (package / "schemas").mkdir(parents=True)
    (package / "prompts").mkdir()
    (package / "evals").mkdir()
    (package / "skill.yaml").write_text(
        yaml.safe_dump(_manifest(), sort_keys=False), encoding="utf-8"
    )
    (package / "schemas" / "input.json").write_text(
        json.dumps({"type": "object"}), encoding="utf-8"
    )
    (package / "schemas" / "output.json").write_text(
        json.dumps(
            {
                "type": "object",
                "required": ["status"],
                "properties": {
                    "status": {"type": "string"},
                    "citations": {"type": "array", "items": {"type": "object"}},
                },
            }
        ),
        encoding="utf-8",
    )
    (package / "workflow.yaml").write_text(
        "workflow_version: '1'\n"
        "start: fixture_plan\n"
        "nodes:\n"
        "  - id: fixture_plan\n"
        "    step: planning\n"
        "    handler: fixture_plan_handler\n"
        "    next: fixture_retrieve\n"
        "    max_retries: 0\n"
        "    required_permissions: []\n"
        "    reserve: {tool_calls: 0, input_tokens: 0, output_tokens: 0}\n"
        "  - id: fixture_retrieve\n"
        "    step: retrieving\n"
        "    handler: fixture_retrieve_handler\n"
        "    next: fixture_execute\n"
        "    max_retries: 0\n"
        "    required_permissions: []\n"
        "    reserve: {tool_calls: 0, input_tokens: 0, output_tokens: 0}\n"
        "  - id: fixture_execute\n"
        "    step: executing\n"
        "    handler: fixture_execute_handler\n"
        "    next: fixture_verify\n"
        "    max_retries: 0\n"
        "    required_permissions: []\n"
        "    reserve: {tool_calls: 0, input_tokens: 0, output_tokens: 0}\n"
        "  - id: fixture_verify\n"
        "    step: verifying\n"
        "    handler: fixture_verify_handler\n"
        "    next: null\n"
        "    max_retries: 0\n"
        "    required_permissions: []\n"
        "    reserve: {tool_calls: 0, input_tokens: 0, output_tokens: 0}\n",
        encoding="utf-8",
    )
    (package / "prompts" / "system.md").write_text("synthetic prompt\n", encoding="utf-8")
    (package / "evals" / "cases.jsonl").write_text(cases, encoding="utf-8")


async def _verify_handler(context: NodeExecutionContext) -> NodeResult:
    del context
    return NodeResult(
        outcome=NodeOutcome.COMPLETE,
        output={"status": "completed", "citations": [{"id": "c1"}, {"id": "c2"}]},
    )


async def _continue(context: NodeExecutionContext) -> NodeResult:
    del context
    return NodeResult()


def _runner(registry: FileSystemSkillRegistry) -> SkillEvalProbeRunner:
    return SkillEvalProbeRunner(
        registry=registry,
        gateway=create_model_gateway(GatewayConfig()),
        fixture_probes={
            "fixture_skill": FixtureProbe(
                handlers={
                    "fixture_plan_handler": _continue,
                    "fixture_retrieve_handler": _continue,
                    "fixture_execute_handler": _continue,
                    "fixture_verify_handler": _verify_handler,
                }
            )
        },
    )


async def test_full_flow_produces_report_on_fixture_skill(tmp_path: Path) -> None:
    cases = (
        json.dumps(
            {
                "case_id": "pass-001",
                "checks": [
                    {"type": "output_matches_schema"},
                    {"type": "cites_sources", "min": 2},
                    {"type": "trace_tool_called", "tool": "fixture_verify"},
                    {"type": "finalized"},
                ],
            }
        )
        + "\n"
        + json.dumps({"case_id": "thin-001"})
        + "\n"
        + json.dumps({"case_id": "fail-001", "checks": [{"type": "cites_sources", "min": 9}]})
        + "\n"
    )
    _write_fixture_skill(tmp_path, cases=cases)
    registry = FileSystemSkillRegistry(tmp_path)
    runner = _runner(registry)

    reports = await evaluate_skills(registry, ["fixture_skill"], runner)
    assert len(reports) == 1
    by_case = {result.case_id: result for result in reports[0].cases}
    assert by_case["pass-001"].status is SkillEvalStatus.PASSED
    assert by_case["thin-001"].status is SkillEvalStatus.INCONCLUSIVE
    assert by_case["fail-001"].status is SkillEvalStatus.FAILED

    bundle = _report_dict(reports)
    report_path = tmp_path / "reports" / "skill-eval.json"
    _write_report(report_path, bundle)
    written = json.loads(report_path.read_text(encoding="utf-8"))
    assert written["schema_version"] == "skill-eval-report-bundle-v1"
    assert written["metrics"]["total"] == 3
    assert written["metrics"]["passed"] == 1
    assert written["metrics"]["failed"] == 1
    assert written["metrics"]["inconclusive"] == 1
    assert report_path.with_suffix(".md").is_file()
