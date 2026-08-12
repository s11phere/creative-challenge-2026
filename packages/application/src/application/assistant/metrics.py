"""Privacy-safe Assistant operational metrics and development report aggregation."""

from __future__ import annotations

import logging
import math
import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from statistics import median
from threading import Lock

logger = logging.getLogger(__name__)

_SAFE_COMMANDS = frozenset(
    {
        "ask",
        "cards",
        "compare",
        "compact",
        "effort",
        "help",
        "new",
        "research",
        "skills",
        "stop",
        "summarize",
        "workspace",
    }
)
_SAFE_ACTIONS = frozenset({"respond", "clarify", "invoke_skill"})
_SAFE_CLARIFICATION_KINDS = frozenset(
    {"input_required", "resource", "resource_ambiguous", "resource_missing"}
)
_SAFE_RUN_KINDS = frozenset({"assistant_turn", "context_compaction", "grounded_qa", "skill"})
_SAFE_RUN_STATUSES = frozenset(
    {
        "cancel_requested",
        "cancelled",
        "completed",
        "created",
        "failed",
        "queued",
        "refused",
        "running",
        "timed_out",
        "waiting_approval",
        "waiting_clarification",
    }
)
_SAFE_TERMINATION_REASONS = frozenset({"cancel_requested", "clarify", "invoke_skill", "respond"})
_SAFE_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_COUNTER_LABELS = {
    "assistant.routing.decisions": frozenset({"action"}),
    "assistant.commands": frozenset({"command", "matched"}),
    "assistant.clarifications": frozenset({"kind", "resolved"}),
    "assistant.context_compactions": frozenset({"mode", "status"}),
    "assistant.terminations": frozenset({"run_kind", "status", "reason"}),
}
_OBSERVATION_LABELS = {
    "assistant.tokens.input": frozenset({"run_kind"}),
    "assistant.tokens.output": frozenset({"run_kind"}),
    "assistant.latency_ms": frozenset({"run_kind"}),
}


class AssistantAction(StrEnum):
    RESPOND = "respond"
    CLARIFY = "clarify"
    INVOKE_SKILL = "invoke_skill"


@dataclass(frozen=True)
class AssistantRoutingObservation:
    """One synthetic development observation; it never contains request or response text."""

    case_id: str
    expected_action: AssistantAction
    actual_action: AssistantAction
    expected_skill: str | None = None
    actual_skill: str | None = None
    expected_selection_source: str = "none"
    actual_selection_source: str = "none"
    clarification_requested: bool = False
    clarification_resolved: bool = False
    command_expected: bool = False
    command_matched: bool = False
    compaction_eligible: bool = False
    compaction_completed: bool = False
    security_violations: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    termination_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("Assistant observation requires a case ID")
        if self.expected_skill is not None and not self.expected_skill.strip():
            raise ValueError("expected Skill cannot be empty")
        if self.actual_skill is not None and not self.actual_skill.strip():
            raise ValueError("actual Skill cannot be empty")
        if self.security_violations < 0 or self.input_tokens < 0 or self.output_tokens < 0:
            raise ValueError("Assistant metric counts cannot be negative")
        if not math.isfinite(self.latency_ms) or self.latency_ms < 0:
            raise ValueError("Assistant latency must be finite and non-negative")
        if self.command_matched and not self.command_expected:
            raise ValueError("a command cannot match when none was expected")
        if self.clarification_resolved and not self.clarification_requested:
            raise ValueError("clarification resolution requires a request")
        if self.compaction_completed and not self.compaction_eligible:
            raise ValueError("compaction completion requires an eligible observation")


def _rate(numerator: int, denominator: int) -> dict[str, int | float | None]:
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


def aggregate_assistant_metrics(
    observations: tuple[AssistantRoutingObservation, ...],
) -> dict[str, object]:
    """Aggregate a reproducible development report with an explicit provisional label."""

    expected_invocations = tuple(
        item for item in observations if item.expected_action is AssistantAction.INVOKE_SKILL
    )
    actual_invocations = tuple(
        item for item in observations if item.actual_action is AssistantAction.INVOKE_SKILL
    )
    true_invocations = tuple(
        item
        for item in observations
        if item.expected_action is AssistantAction.INVOKE_SKILL
        and item.actual_action is AssistantAction.INVOKE_SKILL
        and item.expected_skill == item.actual_skill
    )
    ordinary = tuple(
        item for item in observations if item.expected_action is AssistantAction.RESPOND
    )
    clarifications = tuple(item for item in observations if item.clarification_requested)
    commands = tuple(item for item in observations if item.command_expected)
    compactions = tuple(item for item in observations if item.compaction_eligible)
    latencies = sorted(item.latency_ms for item in observations)

    return {
        "schema_version": "assistant-metrics-report-v1",
        "quality_status": "provisional",
        "dataset_split": "development",
        "case_count": len(observations),
        "metrics": {
            "routing_precision": _rate(len(true_invocations), len(actual_invocations)),
            "routing_recall": _rate(len(true_invocations), len(expected_invocations)),
            "daily_chat_false_trigger_rate": _rate(
                sum(item.actual_action is not AssistantAction.RESPOND for item in ordinary),
                len(ordinary),
            ),
            "clarification_success_rate": _rate(
                sum(item.clarification_resolved for item in clarifications), len(clarifications)
            ),
            "command_hit_rate": _rate(
                sum(item.command_matched for item in commands), len(commands)
            ),
            "context_compaction_rate": _rate(
                sum(item.compaction_completed for item in compactions), len(compactions)
            ),
            "security_violation_rate": _rate(
                sum(item.security_violations > 0 for item in observations), len(observations)
            ),
        },
        "tokens": {
            "input": sum(item.input_tokens for item in observations),
            "output": sum(item.output_tokens for item in observations),
            "total": sum(item.input_tokens + item.output_tokens for item in observations),
        },
        "latency_ms": {
            "p50": median(latencies) if latencies else None,
            "p95": _percentile(latencies, 0.95),
        },
        "termination_reasons": dict(
            Counter(
                item.termination_reason
                for item in observations
                if item.termination_reason is not None
            )
        ),
    }


def _label_text(labels: Mapping[str, str]) -> str:
    return ",".join(f"{key}={labels[key]}" for key in sorted(labels))


def _validate_safe_labels(
    name: str,
    labels: Mapping[str, str],
    *,
    allowed_metrics: Mapping[str, frozenset[str]],
) -> None:
    expected_keys = allowed_metrics.get(name)
    if expected_keys is None or set(labels) != expected_keys:
        raise ValueError("Assistant metric name or labels are not allowlisted")
    if any(not isinstance(value, str) for value in labels.values()):
        raise ValueError("Assistant metric labels must be strings")

    if "action" in labels and labels["action"] not in _SAFE_ACTIONS:
        raise ValueError("Assistant metric action is not allowlisted")
    if "command" in labels and labels["command"] not in _SAFE_COMMANDS:
        raise ValueError("Assistant metric command is not allowlisted")
    if "matched" in labels and labels["matched"] not in {"true", "false"}:
        raise ValueError("Assistant metric command match label is invalid")
    if "kind" in labels and labels["kind"] not in _SAFE_CLARIFICATION_KINDS:
        raise ValueError("Assistant metric clarification kind is not allowlisted")
    if "resolved" in labels and labels["resolved"] not in {"true", "false"}:
        raise ValueError("Assistant metric clarification resolution label is invalid")
    if "mode" in labels and labels["mode"] not in {"automatic", "worker"}:
        raise ValueError("Assistant metric compaction mode is not allowlisted")
    if "run_kind" in labels and labels["run_kind"] not in _SAFE_RUN_KINDS:
        raise ValueError("Assistant metric Run kind is not allowlisted")
    if "status" in labels and labels["status"] not in _SAFE_RUN_STATUSES:
        raise ValueError("Assistant metric Run status is not allowlisted")
    if "reason" in labels and (
        labels["reason"] not in _SAFE_TERMINATION_REASONS
        and _SAFE_ERROR_CODE.fullmatch(labels["reason"]) is None
    ):
        raise ValueError("Assistant metric termination reason is not allowlisted")


class AssistantMetrics:
    """Process-local counters and samples suitable for safe structured log export."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: Counter[str] = Counter()
        self._samples: defaultdict[str, list[float]] = defaultdict(list)

    def increment(
        self, name: str, *, amount: int = 1, labels: Mapping[str, str] | None = None
    ) -> None:
        if amount < 0:
            raise ValueError("metric increments cannot be negative")
        safe_labels = labels or {}
        _validate_safe_labels(name, safe_labels, allowed_metrics=_COUNTER_LABELS)
        key = f"{name}|{_label_text(safe_labels)}"
        with self._lock:
            self._counters[key] += amount
        logger.info(
            "assistant_metric_counter",
            extra={"metric": name, "count": amount, "metric_labels": _label_text(safe_labels)},
        )

    def observe(
        self,
        name: str,
        value: float,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        if not math.isfinite(value) or value < 0:
            raise ValueError("metric observations must be finite and non-negative")
        safe_labels = labels or {}
        _validate_safe_labels(name, safe_labels, allowed_metrics=_OBSERVATION_LABELS)
        key = f"{name}|{_label_text(safe_labels)}"
        with self._lock:
            self._samples[key].append(value)
        logger.info(
            "assistant_metric_observation",
            extra={"metric": name, "value": value, "metric_labels": _label_text(safe_labels)},
        )

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            counters = dict(self._counters)
            samples = {key: tuple(values) for key, values in self._samples.items()}
        return {
            "schema_version": "assistant-operational-metrics-v1",
            "quality_status": "provisional",
            "counters": counters,
            "observations": {
                key: {
                    "count": len(values),
                    "sum": sum(values),
                    "p50": median(values) if values else None,
                    "p95": _percentile(sorted(values), 0.95),
                }
                for key, values in samples.items()
            },
        }

    def record_router_decision(self, action: str) -> None:
        self.increment("assistant.routing.decisions", labels={"action": action})

    def record_command(self, command: str, *, matched: bool) -> None:
        self.increment(
            "assistant.commands", labels={"command": command, "matched": str(matched).lower()}
        )

    def record_clarification(self, kind: str, *, resolved: bool | None = None) -> None:
        labels = {"kind": kind}
        if resolved is not None:
            labels["resolved"] = str(resolved).lower()
        self.increment("assistant.clarifications", labels=labels)

    def record_compaction(self, *, mode: str, status: str) -> None:
        self.increment("assistant.context_compactions", labels={"mode": mode, "status": status})

    def record_usage(
        self,
        *,
        run_kind: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: float,
    ) -> None:
        labels = {"run_kind": run_kind}
        self.observe("assistant.tokens.input", input_tokens, labels=labels)
        self.observe("assistant.tokens.output", output_tokens, labels=labels)
        self.observe("assistant.latency_ms", latency_ms, labels=labels)

    def record_termination(self, *, run_kind: str, status: str, reason: str) -> None:
        self.increment(
            "assistant.terminations",
            labels={"run_kind": run_kind, "status": status, "reason": reason},
        )


__all__ = [
    "AssistantAction",
    "AssistantMetrics",
    "AssistantRoutingObservation",
    "aggregate_assistant_metrics",
]
