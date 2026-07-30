from application.qa.evaluation import (
    AnswerEvaluationObservation,
    AnswerFailureStage,
    aggregate_answer_metrics,
)


def test_answer_metrics_keep_infrastructure_failures_out_of_refusal_denominators() -> None:
    report = aggregate_answer_metrics(
        (
            AnswerEvaluationObservation(
                case_id="case-1",
                expected_behavior="answer",
                outcome="answer",
                supported_claims=2,
                total_claims=2,
                valid_citations=2,
                resolved_citations=2,
                total_citations=2,
                latency_ms=10,
                input_tokens=5,
                output_tokens=3,
            ),
            AnswerEvaluationObservation(
                case_id="case-2",
                expected_behavior="refuse",
                outcome="refuse",
                latency_ms=20,
            ),
            AnswerEvaluationObservation(
                case_id="case-3",
                expected_behavior="refuse",
                outcome="failed",
                latency_ms=30,
                failure_stage=AnswerFailureStage.INFRASTRUCTURE,
            ),
        )
    )
    metrics = report["metrics"]
    assert metrics["supported_claim_rate"] == {"numerator": 2, "denominator": 2, "value": 1.0}
    assert metrics["refusal_recall"] == {"numerator": 1, "denominator": 2, "value": 0.5}
    assert metrics["infrastructure_failure_rate"] == {
        "numerator": 1,
        "denominator": 3,
        "value": 1 / 3,
    }
    assert report["failure_attribution"]["infrastructure"] == 1
    assert report["latency_ms"] == {"p50": 20, "p95": 30}
