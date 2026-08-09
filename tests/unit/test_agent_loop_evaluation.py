from __future__ import annotations

import json
from typing import Any, cast

import pytest
from application.agent_evaluation import (
    AgentLoopEvaluationAction,
    AgentLoopEvaluationObservation,
    aggregate_agent_loop_metrics,
)

from scripts import evaluate_agent_loop


def _prediction(case: dict[str, Any], *, recovery: bool = False) -> dict[str, object]:
    expected = cast(dict[str, object], case["expected"])
    terminal_action = str(expected["terminal_action"])
    tool_calls = int(expected["min_tool_calls"])
    needs_evidence = tool_calls > 0
    return {
        "id": case["id"],
        "first_action": expected["first_action"],
        "terminal_action": terminal_action,
        "tool_calls": tool_calls,
        "repeated_tool_calls": 0,
        "approval_requested": bool(expected.get("requires_approval", False)),
        "goal_complete": terminal_action == "finalize",
        "subquestions_total": tool_calls,
        "subquestions_covered": tool_calls,
        "evidence_required": int(needs_evidence),
        "evidence_covered": int(needs_evidence),
        "citations_required": int(needs_evidence and terminal_action == "finalize"),
        "citations_covered": int(needs_evidence and terminal_action == "finalize"),
        "total_claims": int(terminal_action == "finalize"),
        "supported_claims": int(terminal_action == "finalize"),
        "security_assertions_passed": expected["security_assertions"],
        "input_tokens": 10,
        "output_tokens": 4,
        "latency_ms": 12.5,
        "recovery_attempted": recovery,
        "recovery_succeeded": recovery,
        "stop_reason": {
            "finalize": "goal_complete",
            "clarify": "user_clarification",
            "refuse": "policy_refusal",
        }[terminal_action],
    }


def test_agent_loop_evaluator_aggregates_safe_synthetic_metadata() -> None:
    cases = evaluate_agent_loop.validate_dataset()
    report = evaluate_agent_loop.evaluate_predictions(
        cases,
        tuple(_prediction(case, recovery=index == 0) for index, case in enumerate(cases)),
    )

    assert report["schema_version"] == "agent-loop-metrics-report-v1"
    assert report["quality_status"] == "provisional"
    assert report["dataset_split"] == "development"
    assert report["case_count"] == 8
    metrics = cast(dict[str, object], report["metrics"])
    assert metrics["first_action_accuracy"] == {"numerator": 8, "denominator": 8, "value": 1.0}
    assert metrics["tool_selection_rate"] == {"numerator": 5, "denominator": 5, "value": 1.0}
    assert metrics["repeated_tool_call_rate"] == {"numerator": 0, "denominator": 7, "value": 0.0}
    assert metrics["recovery_success_rate"] == {"numerator": 1, "denominator": 1, "value": 1.0}
    assert "fictional" not in json.dumps(report)


def test_agent_loop_evaluator_rejects_body_or_unknown_prediction_fields() -> None:
    cases = evaluate_agent_loop.validate_dataset()
    predictions = [_prediction(case) for case in cases]
    predictions[0]["request"] = "must never become prediction metadata"

    with pytest.raises(evaluate_agent_loop.AgentLoopEvaluationError, match="fields are invalid"):
        evaluate_agent_loop.evaluate_predictions(cases, tuple(predictions))


def test_agent_loop_evaluator_validate_only_remains_provisional(capsys: object) -> None:
    result = evaluate_agent_loop.main(["--validate-only"])
    captured = capsys.readouterr()  # type: ignore[attr-defined]

    assert result == 0
    assert json.loads(captured.out) == {
        "case_count": 8,
        "dataset_split": "development",
        "formal_run_eligible": False,
        "quality_status": "provisional",
        "schema_version": "agent-loop-metrics-report-v1",
    }


def test_agent_loop_metrics_reject_invalid_recovery_or_security_claims() -> None:
    with pytest.raises(ValueError, match="recovery"):
        AgentLoopEvaluationObservation(
            case_id="synthetic-case",
            expected_first_action=AgentLoopEvaluationAction.CALL_TOOL,
            expected_terminal_action=AgentLoopEvaluationAction.FINALIZE,
            expected_min_tool_calls=1,
            expected_requires_approval=False,
            expected_security_assertions=frozenset({"finalize_only_terminal"}),
            actual_first_action=AgentLoopEvaluationAction.CALL_TOOL,
            actual_terminal_action=AgentLoopEvaluationAction.FINALIZE,
            tool_calls=1,
            repeated_tool_calls=0,
            approval_requested=False,
            goal_complete=True,
            subquestions_total=1,
            subquestions_covered=1,
            evidence_required=1,
            evidence_covered=1,
            citations_required=1,
            citations_covered=1,
            total_claims=1,
            supported_claims=1,
            passed_security_assertions=frozenset({"finalize_only_terminal"}),
            input_tokens=1,
            output_tokens=1,
            latency_ms=1.0,
            recovery_attempted=False,
            recovery_succeeded=True,
            stop_reason="goal_complete",
        )


def test_agent_loop_metric_report_has_explicit_zero_denominators() -> None:
    observation = AgentLoopEvaluationObservation(
        case_id="synthetic-case",
        expected_first_action=AgentLoopEvaluationAction.CONTINUE,
        expected_terminal_action=AgentLoopEvaluationAction.FINALIZE,
        expected_min_tool_calls=0,
        expected_requires_approval=False,
        expected_security_assertions=frozenset({"finalize_only_terminal"}),
        actual_first_action=AgentLoopEvaluationAction.CONTINUE,
        actual_terminal_action=AgentLoopEvaluationAction.FINALIZE,
        tool_calls=0,
        repeated_tool_calls=0,
        approval_requested=False,
        goal_complete=True,
        subquestions_total=0,
        subquestions_covered=0,
        evidence_required=0,
        evidence_covered=0,
        citations_required=0,
        citations_covered=0,
        total_claims=0,
        supported_claims=0,
        passed_security_assertions=frozenset({"finalize_only_terminal"}),
        input_tokens=0,
        output_tokens=0,
        latency_ms=0.0,
        recovery_attempted=False,
        recovery_succeeded=False,
        stop_reason="goal_complete",
    )

    report = aggregate_agent_loop_metrics((observation,))

    metrics = cast(dict[str, dict[str, object]], report["metrics"])
    assert metrics["citation_completeness"]["value"] is None
    assert metrics["unsupported_claim_rate"]["value"] is None
