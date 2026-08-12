from __future__ import annotations

from collections.abc import Mapping

import pytest
from application.skills import (
    SkillEvalCase,
    SkillEvalCaseResult,
    SkillEvalCheck,
    SkillEvalEvidence,
    SkillEvalFailureCategory,
    SkillEvalObservation,
    SkillEvalStatus,
    StructuralSkillEvalJudge,
    aggregate_skill_eval_metrics,
    aggregate_skill_reports,
    build_skill_report,
    invalid_case_result,
    run_skill_evaluation,
)

OUTPUT_SCHEMA: Mapping[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["status"],
    "properties": {
        "status": {"type": "string"},
        "citations": {"type": "array", "items": {"type": "object"}},
    },
}

VALID_OUTPUT = {"status": "completed", "citations": [{"id": "a"}, {"id": "b"}]}

JUDGE = StructuralSkillEvalJudge()


def _observation(
    *,
    status: str = "complete",
    output: object | None = VALID_OUTPUT,
    tool_calls: tuple[str, ...] = ("knowledge_search", "grounded_answer"),
    latency_ms: float = 10.0,
    error: str | None = None,
) -> SkillEvalObservation:
    return SkillEvalObservation(
        case_id="case-1",
        status=status,
        output=output,
        tool_calls=tool_calls,
        latency_ms=latency_ms,
        error=error,
    )


def _case(*checks: dict[str, object]) -> SkillEvalCase:
    return SkillEvalCase.from_mapping({"case_id": "case-1", "checks": list(checks)})


def test_case_parsing_maps_standard_fields_and_check_min() -> None:
    case = SkillEvalCase.from_mapping(
        {
            "case_id": "qa-001",
            "input": {"question": "x"},
            "expected": "model_directed_retrieval",
            "fixture": "synthetic_only",
            "checks": [
                {"type": "trace_tool_called", "tool": "knowledge_search", "min": 2},
                {"type": "cites_sources"},
            ],
        }
    )
    assert case.case_id == "qa-001"
    assert case.input == {"question": "x"}
    assert case.expected == "model_directed_retrieval"
    assert case.fixture == "synthetic_only"
    assert case.checks == (
        SkillEvalCheck(type="trace_tool_called", tool="knowledge_search", minimum=2),
        SkillEvalCheck(type="cites_sources", minimum=None),
    )


@pytest.mark.parametrize(
    ("value", "match"),
    [
        ({"case_id": ""}, "case_id"),
        ({"case_id": 3}, "case_id"),
        ({"case_id": "x", "checks": "not-a-list"}, "checks"),
    ],
)
def test_case_parsing_rejects_invalid_input(value: Mapping[str, object], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        SkillEvalCase.from_mapping(value)


def test_check_parsing_rejects_unsupported_type() -> None:
    with pytest.raises(ValueError, match="Unsupported Skill eval check"):
        SkillEvalCheck.from_mapping({"type": "llm_judge"})


def test_output_matches_schema_check() -> None:
    passed = JUDGE.judge(
        _case({"type": "output_matches_schema"}),
        _observation(),
        output_schema=OUTPUT_SCHEMA,
    )
    assert passed.status is SkillEvalStatus.PASSED
    assert passed.evidence[0].detail == "output_matches_schema"

    mismatch = JUDGE.judge(
        _case({"type": "output_matches_schema"}),
        _observation(output={"status": 3}),
        output_schema=OUTPUT_SCHEMA,
    )
    assert mismatch.status is SkillEvalStatus.FAILED
    assert SkillEvalFailureCategory.SCHEMA_MISMATCH in mismatch.failure_categories
    assert mismatch.evidence[0].detail.startswith("schema_mismatch_at_")

    missing = JUDGE.judge(
        _case({"type": "output_matches_schema"}),
        _observation(output=None),
        output_schema=OUTPUT_SCHEMA,
    )
    assert missing.evidence[0].detail == "output_missing"


def test_output_has_key_check_supports_nested_paths() -> None:
    passed = JUDGE.judge(
        _case({"type": "output_has_key", "key": "citations"}),
        _observation(output={"status": "ok", "citations": []}),
        output_schema=OUTPUT_SCHEMA,
    )
    assert passed.status is SkillEvalStatus.PASSED

    nested = JUDGE.judge(
        _case({"type": "output_has_key", "key": "result.claims"}),
        _observation(output={"result": {"claims": []}}),
        output_schema=OUTPUT_SCHEMA,
    )
    assert nested.status is SkillEvalStatus.PASSED

    missing = JUDGE.judge(
        _case({"type": "output_has_key", "key": "result.claims"}),
        _observation(output={"result": {}}),
        output_schema=OUTPUT_SCHEMA,
    )
    assert missing.status is SkillEvalStatus.FAILED
    assert missing.evidence[0].detail == "output_key_missing_result.claims"
    assert SkillEvalFailureCategory.CHECK_FAILED in missing.failure_categories


def test_null_value_at_key_counts_as_present() -> None:
    passed = JUDGE.judge(
        _case({"type": "output_has_key", "key": "nullable"}),
        _observation(output={"nullable": None}),
        output_schema=OUTPUT_SCHEMA,
    )
    assert passed.status is SkillEvalStatus.PASSED


def test_cites_sources_counts_citation_arrays_anywhere() -> None:
    one = JUDGE.judge(
        _case({"type": "cites_sources", "min": 1}),
        _observation(output={"citations": [{"id": "a"}]}),
        output_schema=OUTPUT_SCHEMA,
    )
    assert one.status is SkillEvalStatus.PASSED

    nested = JUDGE.judge(
        _case({"type": "cites_sources", "min": 3}),
        _observation(output={"result": {"citations": [1, 2], "sources": [3]}}),
        output_schema=OUTPUT_SCHEMA,
    )
    assert nested.status is SkillEvalStatus.PASSED

    short = JUDGE.judge(
        _case({"type": "cites_sources", "min": 3}),
        _observation(output={"citations": [1, 2]}),
        output_schema=OUTPUT_SCHEMA,
    )
    assert short.status is SkillEvalStatus.FAILED
    assert short.evidence[0].detail == "citations=2,min=3"

    no_output = JUDGE.judge(
        _case({"type": "cites_sources", "min": 1}),
        _observation(output=None),
        output_schema=OUTPUT_SCHEMA,
    )
    assert no_output.status is SkillEvalStatus.FAILED


def test_trace_tool_called_check() -> None:
    passed = JUDGE.judge(
        _case({"type": "trace_tool_called", "tool": "knowledge_search", "min": 1}),
        _observation(tool_calls=("knowledge_search", "grounded_answer")),
        output_schema=OUTPUT_SCHEMA,
    )
    assert passed.status is SkillEvalStatus.PASSED

    repeated = JUDGE.judge(
        _case({"type": "trace_tool_called", "tool": "knowledge_search", "min": 2}),
        _observation(tool_calls=("knowledge_search", "verify_answer", "knowledge_search")),
        output_schema=OUTPUT_SCHEMA,
    )
    assert repeated.status is SkillEvalStatus.PASSED

    never = JUDGE.judge(
        _case({"type": "trace_tool_called", "tool": "finalize_answer"}),
        _observation(tool_calls=("knowledge_search",)),
        output_schema=OUTPUT_SCHEMA,
    )
    assert never.status is SkillEvalStatus.FAILED
    assert never.evidence[0].detail == "tool=finalize_answer,calls=0,min=1"


@pytest.mark.parametrize("status", ["refused", "clarify", "timed_out", "failed"])
def test_finalized_requires_complete_run(status: str) -> None:
    result = JUDGE.judge(
        _case({"type": "finalized"}),
        _observation(status=status),
        output_schema=OUTPUT_SCHEMA,
    )
    assert result.status is SkillEvalStatus.FAILED
    assert SkillEvalFailureCategory.TRACE_MISSING in result.failure_categories
    assert result.evidence[0].detail == f"run_status={status}"


def test_all_checks_passing_yields_passed() -> None:
    result = JUDGE.judge(
        _case(
            {"type": "output_matches_schema"},
            {"type": "output_has_key", "key": "citations"},
            {"type": "cites_sources", "min": 2},
            {"type": "trace_tool_called", "tool": "knowledge_search"},
            {"type": "finalized"},
        ),
        _observation(),
        output_schema=OUTPUT_SCHEMA,
    )
    assert result.status is SkillEvalStatus.PASSED
    assert result.failure_categories == ()
    assert result.executed is True
    assert len(result.evidence) == 5


def test_thin_case_is_inconclusive_never_reported_passed() -> None:
    # Even when the minimal schema + finalized evidence passes, a case without
    # declared checks must stay inconclusive (case_too_thin), never pass.
    result = JUDGE.judge(
        _case(),
        _observation(),
        output_schema=OUTPUT_SCHEMA,
    )
    assert result.status is SkillEvalStatus.INCONCLUSIVE
    assert result.failure_categories == (SkillEvalFailureCategory.CASE_TOO_THIN,)
    assert result.executed is True
    assert {item.check for item in result.evidence} == {
        "output_matches_schema",
        "finalized",
    }

    refused = JUDGE.judge(
        _case(),
        _observation(status="refused", output=None),
        output_schema=OUTPUT_SCHEMA,
    )
    assert refused.status is SkillEvalStatus.INCONCLUSIVE
    assert refused.evidence == (
        SkillEvalEvidence(check="finalized", passed=False, detail="run_status=refused"),
    )


def test_probe_error_is_run_error_and_not_executed() -> None:
    result = JUDGE.judge(
        _case({"type": "finalized"}),
        _observation(status="error", error="NO_HANDLER"),
        output_schema=OUTPUT_SCHEMA,
    )
    assert result.status is SkillEvalStatus.ERROR
    assert result.failure_categories == (SkillEvalFailureCategory.RUN_ERROR,)
    assert result.executed is False
    assert result.error == "NO_HANDLER"
    assert result.evidence == ()


def test_invalid_case_result_marks_case_invalid() -> None:
    result = invalid_case_result("line-3", reason="json_parse_error")
    assert result.status is SkillEvalStatus.ERROR
    assert result.failure_categories == (SkillEvalFailureCategory.CASE_INVALID,)
    assert result.executed is False
    assert result.error == "json_parse_error"


async def test_run_skill_evaluation_judges_every_case() -> None:
    case = _case({"type": "finalized"})
    case_fail = _case({"type": "finalized"})
    observations = [
        _observation(status="complete"),
        _observation(status="clarify"),
    ]

    async def execute(c: SkillEvalCase) -> SkillEvalObservation:
        return observations[0] if c is case else observations[1]

    results = await run_skill_evaluation(
        (case, case_fail), execute, JUDGE, output_schema=OUTPUT_SCHEMA
    )

    assert [result.status for result in results] == [
        SkillEvalStatus.PASSED,
        SkillEvalStatus.FAILED,
    ]


def test_aggregate_metrics_exclude_inconclusive_and_errors_from_pass_rate() -> None:
    results = [
        _result(SkillEvalStatus.PASSED, ()),
        _result(SkillEvalStatus.PASSED, ()),
        _result(SkillEvalStatus.FAILED, (SkillEvalFailureCategory.CHECK_FAILED,)),
        _result(SkillEvalStatus.INCONCLUSIVE, (SkillEvalFailureCategory.CASE_TOO_THIN,)),
        _result(SkillEvalStatus.ERROR, (SkillEvalFailureCategory.RUN_ERROR,)),
    ]
    metrics = aggregate_skill_eval_metrics(results)
    assert metrics.total == 5
    assert metrics.passed == 2
    assert metrics.failed == 1
    assert metrics.inconclusive == 1
    assert metrics.errored == 1
    assert metrics.pass_rate == pytest.approx(2 / 3)
    assert metrics.failure_category_counts == {
        "check_failed": 1,
        "case_too_thin": 1,
        "run_error": 1,
    }


def test_aggregate_metrics_track_check_pass_rate_and_latency() -> None:
    passed = SkillEvalCaseResult(
        case_id="a",
        status=SkillEvalStatus.PASSED,
        failure_categories=(),
        evidence=(
            SkillEvalEvidence(check="finalized", passed=True, detail="run_status=complete"),
            SkillEvalEvidence(check="cites_sources", passed=True, detail="citations=2"),
        ),
        executed=True,
        latency_ms=10.0,
    )
    failed = SkillEvalCaseResult(
        case_id="b",
        status=SkillEvalStatus.FAILED,
        failure_categories=(SkillEvalFailureCategory.CHECK_FAILED,),
        evidence=(SkillEvalEvidence(check="cites_sources", passed=False, detail="citations=0"),),
        executed=True,
        latency_ms=30.0,
    )
    metrics = aggregate_skill_eval_metrics((passed, failed))
    assert metrics.checks_executed == 3
    assert metrics.checks_passed == 2
    assert metrics.check_pass_rate == pytest.approx(2 / 3)
    assert metrics.latency_p50_ms == 20.0
    assert metrics.latency_p95_ms == pytest.approx(29.0)


def test_aggregate_metrics_empty_run_has_none_rates() -> None:
    metrics = aggregate_skill_eval_metrics(())
    assert metrics.total == 0
    assert metrics.pass_rate is None
    assert metrics.check_pass_rate is None
    assert metrics.latency_p50_ms is None
    assert metrics.latency_p95_ms is None
    assert metrics.failure_category_counts == {}


def test_skill_report_and_aggregate_report_bundle_metrics() -> None:
    results = (
        SkillEvalCaseResult(
            case_id="a",
            status=SkillEvalStatus.PASSED,
            failure_categories=(),
            evidence=(),
            executed=True,
            latency_ms=5.0,
        ),
        SkillEvalCaseResult(
            case_id="b",
            status=SkillEvalStatus.INCONCLUSIVE,
            failure_categories=(SkillEvalFailureCategory.CASE_TOO_THIN,),
            evidence=(),
            executed=True,
            latency_ms=5.0,
        ),
    )
    report = build_skill_report("research_reading_workflow", "1.1.0", results)
    assert report.skill_name == "research_reading_workflow"
    assert report.skill_version == "1.1.0"
    assert report.metrics.passed == 1

    aggregate = aggregate_skill_reports((report,))
    assert aggregate.metrics.total == 2
    assert aggregate.skills[0] is report


def _result(
    status: SkillEvalStatus,
    failure_categories: tuple[SkillEvalFailureCategory, ...],
) -> SkillEvalCaseResult:
    return SkillEvalCaseResult(
        case_id=f"case-{status.value}",
        status=status,
        failure_categories=failure_categories,
        evidence=(),
        executed=True,
        latency_ms=1.0,
    )
