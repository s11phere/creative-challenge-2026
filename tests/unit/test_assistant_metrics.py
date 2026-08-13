from typing import cast

import pytest
from application.assistant import (
    AssistantAction,
    AssistantMetrics,
    AssistantRoutingObservation,
    aggregate_assistant_metrics,
)


def test_assistant_development_metrics_have_explicit_denominators() -> None:
    observations = (
        AssistantRoutingObservation(
            case_id="routing-001",
            expected_action=AssistantAction.RESPOND,
            actual_action=AssistantAction.RESPOND,
            input_tokens=4,
            output_tokens=2,
            latency_ms=10,
            termination_reason="respond",
        ),
        AssistantRoutingObservation(
            case_id="routing-002",
            expected_action=AssistantAction.INVOKE_SKILL,
            actual_action=AssistantAction.INVOKE_SKILL,
            expected_skill="knowledge_agent",
            actual_skill="knowledge_agent",
            input_tokens=8,
            output_tokens=4,
            latency_ms=30,
            termination_reason="invoke_skill",
        ),
        AssistantRoutingObservation(
            case_id="routing-004",
            expected_action=AssistantAction.CLARIFY,
            actual_action=AssistantAction.CLARIFY,
            clarification_requested=True,
            clarification_resolved=True,
            command_expected=True,
            command_matched=True,
            compaction_eligible=True,
            compaction_completed=True,
            latency_ms=20,
            termination_reason="clarify",
        ),
        AssistantRoutingObservation(
            case_id="routing-005",
            expected_action=AssistantAction.RESPOND,
            actual_action=AssistantAction.INVOKE_SKILL,
            actual_skill="create_review_cards",
            security_violations=1,
            latency_ms=40,
            termination_reason="RUN_AGENT_DECISION_INVALID",
        ),
    )

    report = aggregate_assistant_metrics(observations)

    assert report["quality_status"] == "provisional"
    metrics = cast(dict[str, object], report["metrics"])
    assert metrics["routing_precision"] == {"numerator": 1, "denominator": 2, "value": 0.5}
    assert metrics["routing_recall"] == {"numerator": 1, "denominator": 1, "value": 1.0}
    assert metrics["daily_chat_false_trigger_rate"] == {
        "numerator": 1,
        "denominator": 2,
        "value": 0.5,
    }
    assert metrics["clarification_success_rate"] == {"numerator": 1, "denominator": 1, "value": 1.0}
    assert metrics["command_hit_rate"] == {"numerator": 1, "denominator": 1, "value": 1.0}
    assert metrics["context_compaction_rate"] == {"numerator": 1, "denominator": 1, "value": 1.0}
    assert report["tokens"] == {"input": 12, "output": 6, "total": 18}
    termination_reasons = cast(dict[str, int], report["termination_reasons"])
    assert termination_reasons["RUN_AGENT_DECISION_INVALID"] == 1


def test_operational_metrics_are_labelled_and_body_free() -> None:
    metrics = AssistantMetrics()
    metrics.record_router_decision("respond")
    metrics.record_command("ask", matched=True)
    metrics.record_command("research", matched=True)
    metrics.record_usage(
        run_kind="assistant_turn", input_tokens=5, output_tokens=3, latency_ms=12.5
    )
    snapshot = metrics.snapshot()

    assert snapshot["quality_status"] == "provisional"
    counters = cast(dict[str, int], snapshot["counters"])
    assert counters["assistant.routing.decisions|action=respond"] == 1
    assert counters["assistant.commands|command=ask,matched=true"] == 1
    assert counters["assistant.commands|command=research,matched=true"] == 1
    observations = cast(dict[str, dict[str, object]], snapshot["observations"])
    assert observations["assistant.tokens.input|run_kind=assistant_turn"]["sum"] == 5
    assert observations["assistant.latency_ms|run_kind=assistant_turn"]["p50"] == 12.5


def test_operational_metrics_reject_untrusted_labels() -> None:
    metrics = AssistantMetrics()

    with pytest.raises(ValueError, match="not allowlisted"):
        metrics.record_command("a user supplied command", matched=True)

    # Skill Creator command (Phase 4) is allowlisted for metric recording.
    metrics.record_command("create-skill", matched=True)

    with pytest.raises(ValueError, match="not allowlisted"):
        metrics.increment(
            "assistant.routing.decisions",
            labels={"action": "user supplied response text"},
        )


def test_operational_metrics_accept_bounded_personal_skill_command() -> None:
    metrics = AssistantMetrics()
    # A personal-Skill command slug (Phase 3/6) is a bounded label: any slug
    # matching the safe command pattern must not crash metric recording.
    metrics.record_command("monthly-report", matched=True)
    metrics.record_command("review-sources", matched=True)
    snapshot = metrics.snapshot()
    counters = cast(dict[str, int], snapshot["counters"])
    assert counters["assistant.commands|command=monthly-report,matched=true"] == 1
    assert counters["assistant.commands|command=review-sources,matched=true"] == 1
