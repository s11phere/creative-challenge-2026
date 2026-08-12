"""Body-free, deterministic Skill evaluation: checks, judge, and report types.

This module deliberately has no knowledge of JSONL, Skill registry loading, model
gateways, or execution. Case loading and probe execution belong to the evaluation
runner; the judge only maps a body-free execution observation onto structural
assertions. Behavior labels in eval cases (``expected``) are encoded as trace
assertions (``trace_tool_called`` / ``finalized``), never judged by an LLM.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, cast

from jsonschema import Draft202012Validator

_MISSING: object = object()
_CITATION_KEYS = frozenset({"citations", "sources", "references"})
_FINALIZED_RUN_STATUS = "complete"


class SkillEvalFailureCategory(StrEnum):
    SCHEMA_MISMATCH = "schema_mismatch"
    CHECK_FAILED = "check_failed"
    TRACE_MISSING = "trace_missing"
    CASE_INVALID = "case_invalid"
    CASE_TOO_THIN = "case_too_thin"
    RUN_ERROR = "run_error"


class SkillEvalStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"
    ERROR = "error"


@dataclass(frozen=True)
class SkillEvalCheck:
    """One structural assertion declared on an eval case."""

    type: str
    key: str | None = None
    tool: str | None = None
    minimum: int | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SkillEvalCheck:
        check_type = value.get("type")
        if not isinstance(check_type, str) or check_type not in _CHECK_EVALUATORS:
            raise ValueError(f"Unsupported Skill eval check type: {check_type}")
        key = value.get("key")
        tool = value.get("tool")
        minimum = value.get("min")
        return cls(
            type=check_type,
            key=key if isinstance(key, str) else None,
            tool=tool if isinstance(tool, str) else None,
            minimum=(
                minimum if isinstance(minimum, int) and not isinstance(minimum, bool) else None
            ),
        )


@dataclass(frozen=True)
class SkillEvalCase:
    """One parsed eval case; ``expected`` stays a survey label, never asserted directly."""

    case_id: str
    input: Mapping[str, object] | None
    expected: object
    checks: tuple[SkillEvalCheck, ...]
    fixture: str | None

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SkillEvalCase:
        case_id = value.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("Skill eval case requires a non-empty case_id")
        raw_input = value.get("input")
        raw_checks = value.get("checks")
        if raw_checks is None:
            checks: tuple[SkillEvalCheck, ...] = ()
        elif isinstance(raw_checks, list):
            checks = tuple(
                SkillEvalCheck.from_mapping(item) for item in raw_checks if isinstance(item, dict)
            )
        else:
            raise ValueError("Skill eval case checks must be a list")
        raw_fixture = value.get("fixture")
        return cls(
            case_id=case_id,
            input=raw_input if isinstance(raw_input, Mapping) else None,
            expected=value.get("expected"),
            checks=checks,
            fixture=raw_fixture if isinstance(raw_fixture, str) else None,
        )


@dataclass(frozen=True)
class SkillEvalObservation:
    """Body-free execution output of one probe run.

    ``output`` may carry synthetic fixture text in memory, but it is never
    serialized into a report; only the structured ``SkillEvalEvidence`` is.
    """

    case_id: str
    status: str
    output: object | None
    tool_calls: tuple[str, ...]
    latency_ms: float
    error: str | None = None

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("Skill eval observation case ID is required")
        if self.latency_ms < 0 or not math.isfinite(self.latency_ms):
            raise ValueError("Skill eval observation latency must be finite and non-negative")


@dataclass(frozen=True)
class SkillEvalEvidence:
    """One body-free check result; never carries output text."""

    check: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class SkillEvalCaseResult:
    case_id: str
    status: SkillEvalStatus
    failure_categories: tuple[SkillEvalFailureCategory, ...]
    evidence: tuple[SkillEvalEvidence, ...]
    executed: bool
    latency_ms: float
    expected: str | None = None
    fixture: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class SkillEvalMetrics:
    """Aggregate metrics for one Skill report or the whole evaluation run."""

    total: int
    passed: int
    failed: int
    inconclusive: int
    errored: int
    pass_rate: float | None
    failure_category_counts: dict[str, int]
    checks_executed: int
    checks_passed: int
    check_pass_rate: float | None
    latency_p50_ms: float | None
    latency_p95_ms: float | None


@dataclass(frozen=True)
class SkillEvalSkillReport:
    skill_name: str
    skill_version: str
    cases: tuple[SkillEvalCaseResult, ...]
    metrics: SkillEvalMetrics


@dataclass(frozen=True)
class AggregateSkillEvalReport:
    skills: tuple[SkillEvalSkillReport, ...]
    metrics: SkillEvalMetrics


class SkillEvalJudge(Protocol):
    """Protocol seam: a later LLM judge could consume the same observations."""

    def judge(
        self,
        case: SkillEvalCase,
        observation: SkillEvalObservation,
        *,
        output_schema: Mapping[str, object],
    ) -> SkillEvalCaseResult: ...


class StructuralSkillEvalJudge:
    """Deterministic judge mapping structural assertions onto an observation."""

    def judge(
        self,
        case: SkillEvalCase,
        observation: SkillEvalObservation,
        *,
        output_schema: Mapping[str, object],
    ) -> SkillEvalCaseResult:
        if observation.status == "error":
            return SkillEvalCaseResult(
                case_id=case.case_id,
                status=SkillEvalStatus.ERROR,
                failure_categories=(SkillEvalFailureCategory.RUN_ERROR,),
                evidence=(),
                executed=False,
                latency_ms=observation.latency_ms,
                expected=_expected_label(case.expected),
                fixture=case.fixture,
                error=observation.error,
            )
        if not case.checks:
            # No declared checks: record the minimal schema/finalized evidence but
            # never report a pass — the case is only a survey observation.
            evidence = _minimal_evidence(observation, output_schema)
            return SkillEvalCaseResult(
                case_id=case.case_id,
                status=SkillEvalStatus.INCONCLUSIVE,
                failure_categories=(SkillEvalFailureCategory.CASE_TOO_THIN,),
                evidence=evidence,
                executed=True,
                latency_ms=observation.latency_ms,
                expected=_expected_label(case.expected),
                fixture=case.fixture,
            )
        evidence = tuple(
            _evaluate_check(check, observation, output_schema) for check in case.checks
        )
        failures = _classify_failures(evidence)
        return SkillEvalCaseResult(
            case_id=case.case_id,
            status=SkillEvalStatus.PASSED if not failures else SkillEvalStatus.FAILED,
            failure_categories=failures,
            evidence=evidence,
            executed=True,
            latency_ms=observation.latency_ms,
            expected=_expected_label(case.expected),
            fixture=case.fixture,
        )


def invalid_case_result(case_id: str, *, reason: str) -> SkillEvalCaseResult:
    """Build a ``case_invalid`` result for a case that could not be parsed."""
    return SkillEvalCaseResult(
        case_id=case_id,
        status=SkillEvalStatus.ERROR,
        failure_categories=(SkillEvalFailureCategory.CASE_INVALID,),
        evidence=(),
        executed=False,
        latency_ms=0.0,
        error=reason,
    )


async def run_skill_evaluation(
    cases: Sequence[SkillEvalCase],
    execute: Callable[[SkillEvalCase], Awaitable[SkillEvalObservation]],
    judge: SkillEvalJudge,
    *,
    output_schema: Mapping[str, object],
) -> tuple[SkillEvalCaseResult, ...]:
    """Execute and judge one Skill without serializing any private input."""
    results: list[SkillEvalCaseResult] = []
    for case in cases:
        observation = await execute(case)
        results.append(judge.judge(case, observation, output_schema=output_schema))
    return tuple(results)


def build_skill_report(
    skill_name: str,
    skill_version: str,
    results: Sequence[SkillEvalCaseResult],
) -> SkillEvalSkillReport:
    return SkillEvalSkillReport(
        skill_name=skill_name,
        skill_version=skill_version,
        cases=tuple(results),
        metrics=aggregate_skill_eval_metrics(results),
    )


def aggregate_skill_reports(
    reports: Sequence[SkillEvalSkillReport],
) -> AggregateSkillEvalReport:
    combined = tuple(result for report in reports for result in report.cases)
    return AggregateSkillEvalReport(
        skills=tuple(reports),
        metrics=aggregate_skill_eval_metrics(combined),
    )


def aggregate_skill_eval_metrics(
    results: Sequence[SkillEvalCaseResult],
) -> SkillEvalMetrics:
    total = len(results)
    passed = sum(result.status is SkillEvalStatus.PASSED for result in results)
    failed = sum(result.status is SkillEvalStatus.FAILED for result in results)
    inconclusive = sum(result.status is SkillEvalStatus.INCONCLUSIVE for result in results)
    errored = sum(result.status is SkillEvalStatus.ERROR for result in results)
    judged = passed + failed
    evidence = tuple(item for result in results for item in result.evidence)
    latencies = sorted(result.latency_ms for result in results if result.latency_ms > 0)
    return SkillEvalMetrics(
        total=total,
        passed=passed,
        failed=failed,
        inconclusive=inconclusive,
        errored=errored,
        pass_rate=passed / judged if judged else None,
        failure_category_counts=dict(
            Counter(category.value for result in results for category in result.failure_categories)
        ),
        checks_executed=len(evidence),
        checks_passed=sum(item.passed for item in evidence),
        check_pass_rate=sum(item.passed for item in evidence) / len(evidence) if evidence else None,
        latency_p50_ms=_percentile(latencies, 0.50),
        latency_p95_ms=_percentile(latencies, 0.95),
    )


def _minimal_evidence(
    observation: SkillEvalObservation,
    output_schema: Mapping[str, object],
) -> tuple[SkillEvalEvidence, ...]:
    """The lowest judgment for survey cases: schema compliance plus finalization."""
    evidence: list[SkillEvalEvidence] = []
    if observation.output is not None:
        evidence.append(
            _evaluate_check(
                SkillEvalCheck(type="output_matches_schema"), observation, output_schema
            )
        )
    evidence.append(_evaluate_check(SkillEvalCheck(type="finalized"), observation, output_schema))
    return tuple(evidence)


def _classify_failures(
    evidence: Sequence[SkillEvalEvidence],
) -> tuple[SkillEvalFailureCategory, ...]:
    failures: list[SkillEvalFailureCategory] = []
    for item in evidence:
        if item.passed:
            continue
        if item.check == "output_matches_schema":
            failures.append(SkillEvalFailureCategory.SCHEMA_MISMATCH)
        elif item.check == "finalized":
            failures.append(SkillEvalFailureCategory.TRACE_MISSING)
        else:
            failures.append(SkillEvalFailureCategory.CHECK_FAILED)
    return tuple(dict.fromkeys(failures))


def _evaluate_check(
    check: SkillEvalCheck,
    observation: SkillEvalObservation,
    output_schema: Mapping[str, object],
) -> SkillEvalEvidence:
    evaluator = _CHECK_EVALUATORS.get(check.type)
    if evaluator is None:
        return SkillEvalEvidence(check=check.type, passed=False, detail="unsupported_check")
    passed, detail = evaluator(check, observation, output_schema)
    return SkillEvalEvidence(check=check.type, passed=passed, detail=detail)


def _check_output_matches_schema(
    check: SkillEvalCheck,
    observation: SkillEvalObservation,
    output_schema: Mapping[str, object],
) -> tuple[bool, str]:
    del check
    output = observation.output
    if output is None:
        return False, "output_missing"
    error = next(Draft202012Validator(output_schema).iter_errors(cast(Any, output)), None)
    if error is None:
        return True, "output_matches_schema"
    location = "/".join(str(part) for part in error.absolute_path) or "<root>"
    return False, f"schema_mismatch_at_{location}"


def _check_output_has_key(
    check: SkillEvalCheck,
    observation: SkillEvalObservation,
    output_schema: Mapping[str, object],
) -> tuple[bool, str]:
    del output_schema
    key = check.key
    if key is None:
        return False, "check_missing_key"
    found = _lookup_key(observation.output, key)
    if found is _MISSING:
        return False, f"output_key_missing_{key}"
    return True, f"output_key_{key}"


def _check_cites_sources(
    check: SkillEvalCheck,
    observation: SkillEvalObservation,
    output_schema: Mapping[str, object],
) -> tuple[bool, str]:
    del output_schema
    minimum = check.minimum or 1
    count = _count_citations(observation.output)
    return count >= minimum, f"citations={count},min={minimum}"


def _check_trace_tool_called(
    check: SkillEvalCheck,
    observation: SkillEvalObservation,
    output_schema: Mapping[str, object],
) -> tuple[bool, str]:
    del output_schema
    tool = check.tool
    if tool is None:
        return False, "check_missing_tool"
    minimum = check.minimum or 1
    count = sum(1 for name in observation.tool_calls if name == tool)
    return count >= minimum, f"tool={tool},calls={count},min={minimum}"


def _check_finalized(
    check: SkillEvalCheck,
    observation: SkillEvalObservation,
    output_schema: Mapping[str, object],
) -> tuple[bool, str]:
    del check, output_schema
    return (
        observation.status == _FINALIZED_RUN_STATUS,
        f"run_status={observation.status}",
    )


_CHECK_EVALUATORS: dict[str, Callable[..., tuple[bool, str]]] = {
    "output_matches_schema": _check_output_matches_schema,
    "output_has_key": _check_output_has_key,
    "cites_sources": _check_cites_sources,
    "trace_tool_called": _check_trace_tool_called,
    "finalized": _check_finalized,
}


def _lookup_key(value: object, key: str) -> object:
    current = value
    for part in key.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _count_citations(value: object) -> int:
    count = 0
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            for child_key, child in current.items():
                if child_key in _CITATION_KEYS and isinstance(child, list):
                    count += len(child)
                pending.append(child)
        elif isinstance(current, list):
            pending.extend(current)
    return count


def _expected_label(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip()[:280] or None
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return None
    return rendered[:280] or None


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    index = (len(values) - 1) * quantile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return float(values[lower])
    weight = index - lower
    return float(values[lower] * (1.0 - weight) + values[upper] * weight)


__all__ = [
    "AggregateSkillEvalReport",
    "SkillEvalCase",
    "SkillEvalCaseResult",
    "SkillEvalCheck",
    "SkillEvalEvidence",
    "SkillEvalFailureCategory",
    "SkillEvalJudge",
    "SkillEvalMetrics",
    "SkillEvalObservation",
    "SkillEvalSkillReport",
    "SkillEvalStatus",
    "StructuralSkillEvalJudge",
    "aggregate_skill_eval_metrics",
    "aggregate_skill_reports",
    "build_skill_report",
    "invalid_case_result",
    "run_skill_evaluation",
]
