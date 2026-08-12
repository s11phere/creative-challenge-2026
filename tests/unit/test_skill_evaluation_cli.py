from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from agent_runtime import (
    DeterministicWorkflowExecutor,
    FileSystemSkillRegistry,
    InMemoryRuntimeStateStore,
    NodeExecutionContext,
    NodeOutcome,
    NodeResult,
)
from application.skills import SkillEvalStatus
from model_gateway import GatewayConfig, create_model_gateway

from scripts.evaluate_skills import (
    FixtureProbe,
    SkillEvalProbeRunner,
    _build_run,
    _input_data,
    _load_cases,
    _report_dict,
    _runtime_observation,
    _target_directories,
    evaluate_skills,
)


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


def _write_fixture_skill(root: Path, directory: str, *, cases: str) -> Path:
    package = root / directory
    (package / "schemas").mkdir(parents=True)
    (package / "prompts").mkdir()
    (package / "evals").mkdir()
    (package / "skill.yaml").write_text(
        yaml.safe_dump(_manifest(), sort_keys=False), encoding="utf-8"
    )
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["status"],
        "properties": {
            "status": {"type": "string"},
            "citations": {"type": "array", "items": {"type": "object"}},
        },
    }
    (package / "schemas" / "input.json").write_text(
        json.dumps({"type": "object"}), encoding="utf-8"
    )
    (package / "schemas" / "output.json").write_text(json.dumps(schema), encoding="utf-8")
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
    return package


async def _plan_handler(context: NodeExecutionContext) -> NodeResult:
    del context
    return NodeResult()


async def _retrieve_handler(context: NodeExecutionContext) -> NodeResult:
    del context
    return NodeResult(state_updates={"retrieved": True})


async def _execute_handler(context: NodeExecutionContext) -> NodeResult:
    del context
    return NodeResult(state_updates={"result": {"status": "completed"}})


async def _verify_handler(context: NodeExecutionContext) -> NodeResult:
    del context
    return NodeResult(
        outcome=NodeOutcome.COMPLETE,
        output={
            "status": "completed",
            "citations": [{"id": "c1"}, {"id": "c2"}, {"id": "c3"}],
        },
    )


def _fixture_probe() -> FixtureProbe:
    return FixtureProbe(
        handlers={
            "fixture_plan_handler": _plan_handler,
            "fixture_retrieve_handler": _retrieve_handler,
            "fixture_execute_handler": _execute_handler,
            "fixture_verify_handler": _verify_handler,
        }
    )


def _runner(registry: FileSystemSkillRegistry, *, fixture: bool = True) -> SkillEvalProbeRunner:
    gateway = create_model_gateway(GatewayConfig())
    probes = {"fixture_skill": _fixture_probe()} if fixture else None
    return SkillEvalProbeRunner(registry=registry, gateway=gateway, fixture_probes=probes)


def test_target_directories_all_includes_template_and_skips_underscore() -> None:
    class _Args:
        all = True
        skills = ""

    targets = _target_directories(_Args())
    assert "assistant_agent" in targets
    assert "_template" in targets
    assert not any(name.startswith("_") and name != "_template" for name in targets)


def test_target_directories_skills_parses_comma_list() -> None:
    class _Args:
        all = False
        skills = "a, b ,c"

    assert _target_directories(_Args()) == ["a", "b", "c"]


def test_load_cases_parses_valid_and_marks_invalid_lines(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text(
        '{"case_id":"a"}\n{"case_id":"b","checks":[{"type":"finalized"}]}\n{broken\n["array"]\n',
        encoding="utf-8",
    )
    parsed = _load_cases(path)
    assert len(parsed) == 4
    assert [case.case_id if case is not None else None for case in parsed] == ["a", "b", None, None]


async def test_runtime_observation_maps_complete_run_trace(tmp_path: Path) -> None:
    _write_fixture_skill(tmp_path, "fixture", cases='{"case_id":"a"}\n')
    registry = FileSystemSkillRegistry(tmp_path)
    package = registry.register(registry.load("fixture"))
    pin = registry.pin("fixture_skill", "1.0.0")
    case = next(iter(_load_cases(package.root / "evals" / "cases.jsonl")))
    assert case is not None

    run = _build_run(package, pin, case)
    assert _input_data(case, package) == {"question": "a"}

    executor = DeterministicWorkflowExecutor(
        skill_registry=registry,
        model_gateway=create_model_gateway(GatewayConfig()),
        handlers=_fixture_probe().handlers,
        state_store=InMemoryRuntimeStateStore(),
    )
    result = await executor.execute(run, pin, _input_data(case, package))
    observation = _runtime_observation(result, case, 0.0)

    assert observation.status == "complete"
    assert observation.output == {
        "status": "completed",
        "citations": [{"id": "c1"}, {"id": "c2"}, {"id": "c3"}],
    }
    assert observation.tool_calls == (
        "fixture_plan",
        "fixture_retrieve",
        "fixture_execute",
        "fixture_verify",
    )


def test_report_dict_is_body_free(tmp_path: Path) -> None:
    _write_fixture_skill(
        tmp_path,
        "fixture",
        cases=json.dumps({"case_id": "a", "checks": [{"type": "finalized"}]}) + "\n",
    )
    registry = FileSystemSkillRegistry(tmp_path)
    runner = _runner(registry)
    reports = _sync_evaluate(registry, ["fixture"], runner)
    bundle = _report_dict(reports)
    serialized = json.dumps(bundle)
    assert bundle["schema_version"] == "skill-eval-report-bundle-v1"
    for forbidden in ("query", "question", "text", "output", "input", "prompt"):
        assert forbidden not in serialized


def test_evaluate_skills_full_flow_with_fixture_probe(tmp_path: Path) -> None:
    cases = (
        json.dumps(
            {
                "case_id": "pass-001",
                "checks": [
                    {"type": "output_matches_schema"},
                    {"type": "output_has_key", "key": "status"},
                    {"type": "cites_sources", "min": 3},
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
    _write_fixture_skill(tmp_path, "fixture", cases=cases)
    registry = FileSystemSkillRegistry(tmp_path)
    runner = _runner(registry)
    reports = _sync_evaluate(registry, ["fixture"], runner)

    assert len(reports) == 1
    report = reports[0]
    assert report.skill_name == "fixture_skill"
    assert report.skill_version == "1.0.0"
    by_case = {result.case_id: result for result in report.cases}
    assert by_case["pass-001"].status is SkillEvalStatus.PASSED
    assert by_case["pass-001"].failure_categories == ()
    assert by_case["thin-001"].status is SkillEvalStatus.INCONCLUSIVE
    assert by_case["fail-001"].status is SkillEvalStatus.FAILED
    assert report.metrics.passed == 1
    assert report.metrics.failed == 1
    assert report.metrics.inconclusive == 1
    assert report.metrics.pass_rate == pytest.approx(0.5)


def test_skill_without_adapter_reports_run_error(tmp_path: Path) -> None:
    _write_fixture_skill(tmp_path, "fixture", cases='{"case_id":"a"}\n')
    registry = FileSystemSkillRegistry(tmp_path)
    runner = _runner(registry, fixture=False)
    reports = _sync_evaluate(registry, ["fixture"], runner)
    result = reports[0].cases[0]
    assert result.status is SkillEvalStatus.ERROR
    assert result.failure_categories[0].value == "run_error"


def test_runner_reports_database_required_for_qa_skill(tmp_path: Path) -> None:
    _write_fixture_skill(tmp_path, "fixture", cases='{"case_id":"a"}\n')
    # Rename the fixture to a QA-backed skill identity by overriding the manifest.
    manifest = _manifest()
    manifest["name"] = "summarize_document"
    manifest["version"] = "1.0.0"
    package = _write_fixture_skill(tmp_path, "qa-skill", cases='{"case_id":"a"}\n')
    (package / "skill.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    registry = FileSystemSkillRegistry(tmp_path)
    runner = SkillEvalProbeRunner(
        registry=registry,
        gateway=create_model_gateway(GatewayConfig()),
        database=None,
    )
    reports = _sync_evaluate(registry, ["qa-skill"], runner)
    result = reports[0].cases[0]
    assert result.status is SkillEvalStatus.ERROR
    assert result.error == "SKILL_EVAL_DATABASE_REQUIRED"


def _sync_evaluate(
    registry: FileSystemSkillRegistry,
    targets: list[str],
    runner: SkillEvalProbeRunner,
) -> Any:
    return asyncio.run(evaluate_skills(registry, targets, runner))
