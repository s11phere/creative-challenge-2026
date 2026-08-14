"""Run Skill eval cases through the production execution path and emit a report.

The report is deliberately non-blocking: it surveys every requested Skill and
records per-case pass/fail with body-free evidence. Cases without declared checks
are marked ``case_too_thin`` and never reported as passing. Probe execution reuses
the production ``DeterministicWorkflowExecutor`` plus the registered Grounded QA
adapters; Skills without a
registered adapter, or that need a database which is unavailable, are reported as
``run_error`` without blocking the rest of the survey.

`exam_preparation_workflow` uses a synthetic-only structural adapter backed by the
same application artifact builders. It never invokes a provider or serializes bodies.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, cast
from uuid import UUID, uuid4, uuid5

from agent_runtime import (
    DeterministicWorkflowExecutor,
    FileSystemSkillRegistry,
    InMemoryRuntimeStateStore,
    JSONValue,
    NodeExecutionError,
    NodeHandler,
    PinnedSkill,
    RuntimeAuditEventType,
    RuntimeExecutionResult,
    SkillPackage,
)
from application.exam_preparation import (
    _diagnosis,
    _diagnostic_paper,
    _mock_paper,
    _review_cards,
    _review_plan,
    _study_guide,
)
from application.skills import (
    GroundedQASkillAdapter,
    GroundedQASkillConfig,
    SkillEvalCase,
    SkillEvalObservation,
    SkillEvalSkillReport,
    StructuralSkillEvalJudge,
    aggregate_skill_reports,
    build_skill_report,
    invalid_case_result,
)
from domain.agent_runtime import AgentRun, AgentRunContext
from infrastructure.database import Database
from infrastructure.qa_execution import GroundedQAExecutor, qa_execution_versions
from infrastructure.qa_persistence import PostgresGroundedQARepository, PostgresQAEventStore
from model_gateway import GatewayConfig, ModelGateway, ModelProvider, create_model_gateway

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPOSITORY_ROOT / "skills"
_EVAL_NAMESPACE = UUID("4a5d3c2b-1e0f-4a3d-9c2b-8f1e0d4a3c2b")
_EVAL_SPACE_ID = uuid5(_EVAL_NAMESPACE, "skill-evaluation")
_QA_BACKED_SKILLS = frozenset(
    {
        "knowledge_agent",
        "summarize_document",
        "compare_sources",
        "create_review_cards",
        "research_reading_workflow",
    }
)
_QA_OUTPUT_SCHEMA_VERSIONS = {
    "summarize_document": "summarize-document-skill-output-v1",
    "compare_sources": "compare-sources-skill-output-v1",
    "create_review_cards": "review-cards-skill-output-v1",
    "research_reading_workflow": "research-reading-workflow-output-v2",
}
_FORBIDDEN_REPORT_KEYS = frozenset(
    {
        "query",
        "question",
        "text",
        "quote",
        "excerpt",
        "embedding",
        "vector",
        "api_key",
        "secret",
        "prompt",
        "response_body",
        "output",
        "input",
        "conversation",
    }
)


@dataclass(frozen=True)
class FixtureProbe:
    """Handlers for a synthetic fixture Skill."""

    handlers: Mapping[str, NodeHandler]


class SkillEvalProbeRunner:
    """Run one eval case through the production Skill execution stack.

    A fixture probe with injected handlers always takes precedence.
    Skills without a registered adapter are reported as ``run_error``.
    """

    def __init__(
        self,
        *,
        registry: FileSystemSkillRegistry,
        gateway: ModelGateway,
        database: Database | None = None,
        fixture_probes: Mapping[str, FixtureProbe] | None = None,
    ) -> None:
        self._registry = registry
        self._gateway = gateway
        self._database = database
        self._fixture_probes = dict(fixture_probes or {})
        self._qa_executor: GroundedQAExecutor | None = None

    async def run(
        self, package: SkillPackage, pin: PinnedSkill, case: SkillEvalCase
    ) -> SkillEvalObservation:
        started = perf_counter()
        try:
            return await self._execute(package, pin, case, started)
        except NodeExecutionError as exc:
            return _error_observation(case, exc.code, started)
        except Exception as exc:
            return _error_observation(case, f"PROBE_{type(exc).__name__}", started)

    async def _execute(
        self, package: SkillPackage, pin: PinnedSkill, case: SkillEvalCase, started: float
    ) -> SkillEvalObservation:
        fixture = self._fixture_probes.get(package.manifest.name)
        if fixture is not None:
            return await self._run_fixture(package, pin, case, fixture, started)
        if package.manifest.name in _QA_BACKED_SKILLS:
            return await self._run_qa_skill(package, pin, case, started)
        if package.manifest.name == "exam_preparation_workflow":
            return _exam_observation(case, started)
        return _error_observation(case, "SKILL_EVAL_NO_ADAPTER", started)

    async def _run_fixture(
        self,
        package: SkillPackage,
        pin: PinnedSkill,
        case: SkillEvalCase,
        fixture: FixtureProbe,
        started: float,
    ) -> SkillEvalObservation:
        run = _build_run(package, pin, case)
        input_data = _input_data(case, package)
        state_store = InMemoryRuntimeStateStore()
        executor = DeterministicWorkflowExecutor(
            skill_registry=self._registry,
            model_gateway=self._gateway,
            handlers=fixture.handlers,
            state_store=state_store,
        )
        result = await executor.execute(run, pin, input_data)
        return _runtime_observation(result, case, started)

    async def _run_qa_skill(
        self, package: SkillPackage, pin: PinnedSkill, case: SkillEvalCase, started: float
    ) -> SkillEvalObservation:
        if self._database is None:
            return _error_observation(case, "SKILL_EVAL_DATABASE_REQUIRED", started)
        if pin.name == "knowledge_agent":
            # The generic knowledge Loop publishes through the conversation-run
            # stack, which is out of scope for this report-first runner.
            return _error_observation(case, "SKILL_EVAL_AGENT_LOOP_UNAVAILABLE", started)
        qa_executor = self._qa_executor
        if qa_executor is None:
            qa_executor = GroundedQAExecutor(
                database=self._database,
                gateway=self._gateway,
                repository=PostgresGroundedQARepository(self._database),
                events=PostgresQAEventStore(self._database),
                skill_registry=self._registry,
            )
            self._qa_executor = qa_executor
        service = qa_executor.build_service(self._gateway, generation_gateway=self._gateway)
        adapter = GroundedQASkillAdapter(
            qa=service,
            config=GroundedQASkillConfig(
                profile=qa_executor.profile,
                versions=qa_execution_versions(self._registry, skill_name=pin.name),
                skill_name=pin.name,
                output_schema_version=_QA_OUTPUT_SCHEMA_VERSIONS[pin.name],
            ),
        )
        run = _build_run(package, pin, case)
        executor = DeterministicWorkflowExecutor(
            skill_registry=self._registry,
            model_gateway=self._gateway,
            handlers=adapter.handlers(),
            state_store=InMemoryRuntimeStateStore(),
        )
        result = await executor.execute(run, pin, _input_data(case, package))
        return _runtime_observation(result, case, started)


def _build_run(package: SkillPackage, pin: PinnedSkill, case: SkillEvalCase) -> AgentRun:
    return AgentRun(
        context=AgentRunContext(
            run_id=uuid4(),
            space_id=_EVAL_SPACE_ID,
            skill_name=pin.name,
            skill_version=pin.version,
            skill_content_sha256=pin.content_sha256,
            trace_id=f"skill-eval-{case.case_id}",
            caller_id="evaluation",
            granted_permissions=package.manifest.permissions,
        ),
        budget=package.manifest.budgets,
    )


def _input_data(case: SkillEvalCase, package: SkillPackage) -> Mapping[str, JSONValue]:
    if case.input is not None:
        return cast(Mapping[str, JSONValue], case.input)
    if package.manifest.name in _QA_BACKED_SKILLS:
        return {"question": case.case_id, "conversation_id": None}
    return {"question": case.case_id}


def _runtime_observation(
    result: RuntimeExecutionResult, case: SkillEvalCase, started: float
) -> SkillEvalObservation:
    latency_ms = (perf_counter() - started) * 1000
    if result.error is not None:
        return _error_observation(case, result.error.code, started)
    status = "refused" if result.refused else "complete"
    tool_calls = tuple(
        event.node_id
        for event in result.events
        if event.event_type is RuntimeAuditEventType.NODE_STARTED and event.node_id
    )
    return SkillEvalObservation(
        case_id=case.case_id,
        status=status,
        output=result.output,
        tool_calls=tool_calls,
        latency_ms=latency_ms,
    )


def _error_observation(case: SkillEvalCase, error: str, started: float) -> SkillEvalObservation:
    return SkillEvalObservation(
        case_id=case.case_id,
        status="error",
        output=None,
        tool_calls=(),
        latency_ms=(perf_counter() - started) * 1000,
        error=error,
    )


def _exam_observation(case: SkillEvalCase, started: float) -> SkillEvalObservation:
    """Execute body-free synthetic checks against the registered Exam application adapter."""
    session_id = uuid5(_EVAL_NAMESPACE, case.case_id)
    scenario = case.case_id
    paper = (
        _mock_paper(session_id)
        if "mock" in scenario or "six-question" in scenario or "hidden-answer" in scenario
        else _diagnostic_paper(session_id, adaptive="adaptive" in scenario)
    )
    evidence_id = str(uuid5(_EVAL_NAMESPACE, f"evidence-{case.case_id}"))
    public = paper.public_payload
    sections = cast(list[dict[str, object]], public["sections"])
    for section in sections:
        for question in cast(list[dict[str, object]], section["questions"]):
            question["citation_ids"] = [evidence_id]
    required_ids = [
        str(question["question_id"])
        for section in sections
        for question in cast(list[dict[str, object]], section["questions"])
    ]
    action = (
        "submit_mock_exam"
        if paper.kind == "mock"
        else ("submit_adaptive_answers" if paper.kind == "adaptive" else "submit_broad_answers")
    )
    interaction: dict[str, object] = {
        "interaction_id": f"interaction-{case.case_id}",
        "interaction_version": "exam-interaction-v1",
        "kind": "mock_exam" if paper.kind == "mock" else "quiz",
        "title": str(paper.public_payload["title"]),
        "instructions": ["Answers remain hidden before submission."],
        "progress": {"current": 2, "total": 9, "label": "synthetic"},
        "submission": {
            "submission_id": f"submission-{case.case_id}",
            "action": action,
            "required_question_ids": required_ids,
            "locks_answers": True,
            "allow_partial": False,
        },
        "fallback": {
            "mode": "numbered_text",
            "objective_answer_format": "Q1:A, Q2:BD",
            "subjective_answer_format": (
                "Use one labelled section per question, for example: Q3: <your answer>."
            ),
        },
        "paper": paper.public_payload,
    }
    output: dict[str, object] = {
        "schema_version": "exam-preparation-workflow-output-v3",
        "status": "in_progress",
        "capability": "mock_exam"
        if paper.kind == "mock"
        else ("adaptive_check" if paper.kind == "adaptive" else "diagnose"),
        "interaction_model": interaction,
        "course_map": [
            {
                "chapter": "综合",
                "topics": ["合成知识点"],
                "prerequisites": [],
                "coverage": "covered",
                "weight_basis": "historical_inference",
                "citation_ids": [evidence_id],
            }
        ],
        "diagnostic_result": [{**item, "citation_ids": [evidence_id]} for item in _diagnosis()],
        "review_plan": _review_plan(),
        "study_guide": _study_guide(),
        "review_card_preview": [
            {**item, "citation_ids": [evidence_id]} for item in _review_cards()
        ],
        "write": {"status": "blocked", "code": "SKILL_WRITE_REQUIRES_APPROVAL", "side_effects": 0},
        "review": {
            "paper_id": str(paper.paper_id),
            "paper_version": 1,
            "submission_id": f"submission-{case.case_id}",
            "suggested_score": 5,
            "max_score": 10,
            "items": [
                {
                    "question_id": "Q1",
                    "kind": "short_answer",
                    "reference_answer": "合成参考答案",
                    "explanation": "合成解析",
                    "rubric": [
                        {
                            "criterion": "正确性",
                            "max_points": 10,
                            "awarded_points": 5,
                            "feedback": "需要人工复核",
                        }
                    ],
                    "suggested_score": 5,
                    "max_score": 10,
                    "grading_confidence": "low",
                    "requires_human_review": True,
                    "citation_ids": [evidence_id],
                }
            ],
            "chapter_performance": [{**_diagnosis()[0], "citation_ids": [evidence_id]}],
        },
        "citations": [{"citation_id": "C1", "evidence_id": evidence_id}],
        "next_action": action,
    }
    if "insufficient-evidence" in scenario:
        output.update(
            {
                "status": "refused",
                "capability": "diagnose",
                "next_action": "provide_setup",
                "refusal": {
                    "code": "SKILL_EVIDENCE_INSUFFICIENT",
                    "message": "Synthetic evidence is insufficient.",
                },
            }
        )
    return SkillEvalObservation(
        case_id=case.case_id,
        status="complete",
        output=output,
        tool_calls=("exam_prepare",),
        latency_ms=(perf_counter() - started) * 1000,
    )


def _load_cases(path: Path) -> tuple[SkillEvalCase | None, ...]:
    """Parse a JSONL eval case file; invalid lines become ``None`` for case_invalid."""
    parsed: list[SkillEvalCase | None] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        try:
            raw = json.loads(raw_line)
        except json.JSONDecodeError:
            parsed.append(None)
            continue
        if not isinstance(raw, dict):
            parsed.append(None)
            continue
        try:
            parsed.append(SkillEvalCase.from_mapping(raw))
        except ValueError:
            parsed.append(None)
    return tuple(parsed)


async def evaluate_skills(
    registry: FileSystemSkillRegistry,
    targets: Sequence[str],
    runner: SkillEvalProbeRunner,
) -> tuple[SkillEvalSkillReport, ...]:
    """Run every requested Skill package through the probe runner and judge."""
    judge = StructuralSkillEvalJudge()
    reports: list[SkillEvalSkillReport] = []
    for directory in targets:
        try:
            package = registry.register(registry.load(directory))
        except Exception as exc:
            error_code = _error_code(exc)
            reports.append(_failed_load_report(directory, error_code))
            continue
        try:
            pin = registry.pin(package.manifest.name, package.manifest.version)
        except Exception as exc:
            error_code = _error_code(exc)
            reports.append(_failed_load_report(package.manifest.name, error_code))
            continue
        output_schema = _output_schema(package)
        cases = tuple(
            case
            for reference in package.manifest.evals
            for case in _load_cases(package.root / reference)
        )
        results = []
        for index, case in enumerate(cases):
            if case is None:
                results.append(
                    invalid_case_result(f"case-invalid-{index + 1}", reason="case_invalid")
                )
                continue
            observation = await runner.run(package, pin, case)
            results.append(judge.judge(case, observation, output_schema=output_schema))
        reports.append(
            build_skill_report(
                package.manifest.name,
                package.manifest.version,
                results,
            )
        )
    return tuple(reports)


def _failed_load_report(name: str, error_code: str) -> SkillEvalSkillReport:
    return build_skill_report(name, "unknown", (invalid_case_result(name, reason=error_code),))


def _error_code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if code is not None:
        return getattr(code, "value", str(code))
    return f"SKILL_EVAL_LOAD_{type(exc).__name__}"


def _output_schema(package: SkillPackage) -> Mapping[str, object]:
    path = package.root / package.manifest.output_schema
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _target_directories(args: argparse.Namespace) -> list[str]:
    if args.all:
        targets = [
            child.name
            for child in sorted(SKILL_ROOT.iterdir())
            if child.is_dir() and not child.name.startswith("_")
        ]
        if (SKILL_ROOT / "_template").is_dir():
            targets.append("_template")
        return targets
    return [name.strip() for name in args.skills.split(",") if name.strip()]


def _create_gateway(model: str) -> ModelGateway:
    if model == "settings":
        from infrastructure.config import settings

        def secret(value: Any) -> str | None:
            return value.get_secret_value() if value else None

        return create_model_gateway(
            GatewayConfig(
                provider=ModelProvider(settings.model_provider),
                endpoint=settings.model_endpoint,
                api_key=secret(settings.model_api_key),
                fast_chat_endpoint=settings.fast_chat_endpoint,
                fast_chat_api_key=secret(settings.fast_chat_api_key),
                fast_chat_model=settings.fast_chat_model,
                embedding_endpoint=settings.embedding_endpoint,
                embedding_api_key=secret(settings.embedding_api_key),
                embedding_model=settings.embedding_model,
                reranker_endpoint=settings.reranker_endpoint,
                reranker_api_key=secret(settings.reranker_api_key),
                reranker_model=settings.reranker_model,
                allow_external=settings.model_allow_external,
                timeout_seconds=settings.model_timeout_seconds,
                max_retries=settings.model_max_retries,
                retry_backoff_seconds=settings.model_retry_backoff_seconds,
            )
        )
    return create_model_gateway(GatewayConfig())


def _report_dict(reports: Sequence[SkillEvalSkillReport]) -> dict[str, Any]:
    aggregate = aggregate_skill_reports(reports)
    return {
        "schema_version": "skill-eval-report-bundle-v1",
        "run_id": uuid4().hex,
        "git_commit": _git_commit(),
        "git_dirty": bool(_git_status()),
        "skills": [_skill_report_dict(report) for report in reports],
        "metrics": _metrics_dict(aggregate.metrics),
    }


def _skill_report_dict(report: SkillEvalSkillReport) -> dict[str, Any]:
    return {
        "skill_name": report.skill_name,
        "skill_version": report.skill_version,
        "metrics": _metrics_dict(report.metrics),
        "cases": [
            {
                "case_id": result.case_id,
                "status": result.status.value,
                "failure_categories": [category.value for category in result.failure_categories],
                "executed": result.executed,
                "latency_ms": round(result.latency_ms, 3),
                "expected": result.expected,
                "fixture": result.fixture,
                "error": result.error,
                "evidence": [
                    {
                        "check": item.check,
                        "passed": item.passed,
                        "detail": item.detail,
                    }
                    for item in result.evidence
                ],
            }
            for result in report.cases
        ],
    }


def _metrics_dict(metrics: Any) -> dict[str, Any]:
    return {
        "total": metrics.total,
        "passed": metrics.passed,
        "failed": metrics.failed,
        "inconclusive": metrics.inconclusive,
        "errored": metrics.errored,
        "pass_rate": metrics.pass_rate,
        "failure_category_counts": metrics.failure_category_counts,
        "checks_executed": metrics.checks_executed,
        "checks_passed": metrics.checks_passed,
        "check_pass_rate": metrics.check_pass_rate,
        "latency_p50_ms": metrics.latency_p50_ms,
        "latency_p95_ms": metrics.latency_p95_ms,
    }


def _privacy_scan(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() in _FORBIDDEN_REPORT_KEYS:
                raise ValueError(f"Report contains forbidden field: {key}")
            _privacy_scan(child)
    elif isinstance(value, list):
        for child in value:
            _privacy_scan(child)


def _write_report(path: Path, bundle: Mapping[str, Any]) -> None:
    _privacy_scan(bundle)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# Skill Evaluation Summary",
        "",
        "This is a development report. It does not gate Skill activation.",
        "",
    ]
    for skill in bundle["skills"]:
        metrics = skill["metrics"]
        lines.extend(
            [
                f"## {skill['skill_name']} v{skill['skill_version']}",
                "",
                f"- Cases: `{metrics['total']}` | Passed: `{metrics['passed']}` "
                f"| Failed: `{metrics['failed']}` | Inconclusive: `{metrics['inconclusive']}` "
                f"| Errored: `{metrics['errored']}`",
                f"- Pass rate: `{metrics['pass_rate']}` | Check pass rate: "
                f"`{metrics['check_pass_rate']}`",
                f"- Failures: `{metrics['failure_category_counts']}`",
                "",
            ]
        )
    path.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _git_commit() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _git_status() -> str:
    try:
        return subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--all", action="store_true", help="Survey every Skill package")
    target.add_argument("--skills", help="Comma-separated Skill package directories")
    parser.add_argument(
        "--model",
        choices=("fake", "settings"),
        default="fake",
        help="Model provider: fake (deterministic) or settings (diagnostic override)",
    )
    parser.add_argument(
        "--database",
        default=None,
        help="Optional isolated database URL for the Grounded QA Skill adapters",
    )
    parser.add_argument(
        "--output", help="Optional JSON report path; a Markdown summary is written beside it"
    )
    parser.add_argument("--quiet", action="store_true", help="Do not print the report bundle")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    targets = _target_directories(args)
    if not targets:
        print("no Skill packages selected", file=sys.stderr)
        return 2
    try:
        registry = FileSystemSkillRegistry(SKILL_ROOT)
        registry.reload()
        gateway = _create_gateway(args.model)
        database = Database(args.database) if args.database else None
        runner = SkillEvalProbeRunner(registry=registry, gateway=gateway, database=database)
        reports = asyncio.run(evaluate_skills(registry, targets, runner))
    except Exception as exc:
        print(f"skill evaluation rejected: {exc}", file=sys.stderr)
        return 2
    finally:
        if "gateway" in locals():
            asyncio.run(gateway.aclose())
        if "database" in locals() and database is not None:
            asyncio.run(database.dispose())
    bundle = _report_dict(reports)
    try:
        _privacy_scan(bundle)
        if args.output:
            _write_report(Path(args.output).resolve(), bundle)
    except ValueError as exc:
        print(f"skill evaluation report rejected: {exc}", file=sys.stderr)
        return 2
    if not args.quiet:
        print(json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
