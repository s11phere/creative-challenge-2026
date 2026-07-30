"""Deterministic Grounded QA answer metrics with explicit denominators."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from statistics import median


class AnswerFailureStage(StrEnum):
    PARSER = "parser"
    RETRIEVAL = "retrieval"
    CONTEXT = "context"
    GENERATION = "generation"
    VERIFICATION = "verification"
    CITATION = "citation"
    REFUSAL = "refusal"
    POLICY = "policy"
    INFRASTRUCTURE = "infrastructure"


@dataclass(frozen=True)
class AnswerEvaluationObservation:
    case_id: str
    expected_behavior: str
    outcome: str
    supported_claims: int = 0
    total_claims: int = 0
    valid_citations: int = 0
    total_citations: int = 0
    resolved_citations: int = 0
    forbidden_claims: int = 0
    safety_violations: int = 0
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    failure_stage: AnswerFailureStage | None = None

    def __post_init__(self) -> None:
        if self.expected_behavior not in {"answer", "refuse", "conflict"}:
            raise ValueError("unsupported expected behavior")
        if self.outcome not in {"answer", "refuse", "conflict", "failed"}:
            raise ValueError("unsupported answer outcome")
        counts = (
            self.supported_claims,
            self.total_claims,
            self.valid_citations,
            self.total_citations,
            self.resolved_citations,
            self.forbidden_claims,
            self.safety_violations,
            self.input_tokens,
            self.output_tokens,
        )
        if any(value < 0 for value in counts):
            raise ValueError("answer evaluation counts must be non-negative")
        if self.supported_claims > self.total_claims:
            raise ValueError("supported claims cannot exceed total claims")
        if self.valid_citations > self.total_citations:
            raise ValueError("valid citations cannot exceed total citations")
        if self.resolved_citations > self.total_citations:
            raise ValueError("resolved citations cannot exceed total citations")
        if not math.isfinite(self.latency_ms) or self.latency_ms < 0:
            raise ValueError("latency must be finite and non-negative")
        if self.outcome == "failed" and self.failure_stage is None:
            raise ValueError("failed observations require failure attribution")
        if self.outcome != "failed" and self.failure_stage is not None:
            raise ValueError("business outcomes cannot have failure attribution")


def aggregate_answer_metrics(
    observations: tuple[AnswerEvaluationObservation, ...],
) -> dict[str, object]:
    answer_cases = tuple(item for item in observations if item.expected_behavior == "answer")
    refusal_cases = tuple(item for item in observations if item.expected_behavior == "refuse")
    conflict_cases = tuple(item for item in observations if item.expected_behavior == "conflict")
    business = tuple(item for item in observations if item.outcome != "failed")
    total_claims = sum(item.total_claims for item in answer_cases if item.outcome == "answer")
    total_citations = sum(item.total_citations for item in answer_cases if item.outcome == "answer")
    predicted_refusals = tuple(item for item in business if item.outcome == "refuse")
    predicted_conflicts = tuple(item for item in business if item.outcome == "conflict")
    latencies = sorted(item.latency_ms for item in observations)

    return {
        "case_count": len(observations),
        "metrics": {
            "supported_claim_rate": _metric(
                sum(item.supported_claims for item in answer_cases if item.outcome == "answer"),
                total_claims,
            ),
            "citation_precision": _metric(
                sum(item.valid_citations for item in answer_cases if item.outcome == "answer"),
                total_citations,
            ),
            "citation_target_resolution": _metric(
                sum(item.resolved_citations for item in answer_cases if item.outcome == "answer"),
                total_citations,
            ),
            "refusal_precision": _metric(
                sum(item.expected_behavior == "refuse" for item in predicted_refusals),
                len(predicted_refusals),
            ),
            "refusal_recall": _metric(
                sum(item.outcome == "refuse" for item in refusal_cases), len(refusal_cases)
            ),
            "conflict_precision": _metric(
                sum(item.expected_behavior == "conflict" for item in predicted_conflicts),
                len(predicted_conflicts),
            ),
            "conflict_recall": _metric(
                sum(item.outcome == "conflict" for item in conflict_cases), len(conflict_cases)
            ),
            "forbidden_claim_rate": _metric(
                sum(item.forbidden_claims for item in business),
                sum(item.total_claims for item in business),
            ),
            "safety_violation_rate": _metric(
                sum(item.safety_violations > 0 for item in observations), len(observations)
            ),
            "infrastructure_failure_rate": _metric(
                sum(item.outcome == "failed" for item in observations), len(observations)
            ),
        },
        "latency_ms": {
            "p50": median(latencies) if latencies else None,
            "p95": _percentile(latencies, 0.95),
        },
        "tokens": {
            "input": sum(item.input_tokens for item in observations),
            "output": sum(item.output_tokens for item in observations),
        },
        "failure_attribution": {
            stage.value: sum(item.failure_stage is stage for item in observations)
            for stage in AnswerFailureStage
        },
    }


def _metric(numerator: int, denominator: int) -> dict[str, int | float | None]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
    }


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    index = max(0, math.ceil(len(values) * percentile) - 1)
    return values[index]


__all__ = ["AnswerEvaluationObservation", "AnswerFailureStage", "aggregate_answer_metrics"]
