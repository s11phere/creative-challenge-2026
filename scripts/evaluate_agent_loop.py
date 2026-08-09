"""Score synthetic Agent Loop metadata without running a model or reading the corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from application.agent_evaluation import (
    AgentLoopEvaluationAction,
    AgentLoopEvaluationObservation,
    aggregate_agent_loop_metrics,
)
from jsonschema import Draft202012Validator, ValidationError

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST_PATH = Path("cases/evals/datasets/agent-loop-v1/manifest.yaml")

_PREDICTION_FIELDS = frozenset(
    {
        "id",
        "first_action",
        "terminal_action",
        "tool_calls",
        "repeated_tool_calls",
        "approval_requested",
        "goal_complete",
        "subquestions_total",
        "subquestions_covered",
        "evidence_required",
        "evidence_covered",
        "citations_required",
        "citations_covered",
        "total_claims",
        "supported_claims",
        "security_assertions_passed",
        "input_tokens",
        "output_tokens",
        "latency_ms",
        "recovery_attempted",
        "recovery_succeeded",
        "stop_reason",
    }
)


class AgentLoopEvaluationError(ValueError):
    """Raised when development-only metadata crosses the evaluation boundary."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _repository_path(value: str | Path) -> Path:
    path = (REPOSITORY_ROOT / value).resolve()
    try:
        path.relative_to(REPOSITORY_ROOT)
    except ValueError as exc:
        raise AgentLoopEvaluationError("path escapes repository root") from exc
    if not path.is_file():
        raise AgentLoopEvaluationError("required evaluation file does not exist")
    return path


def _load_jsonl(path: Path) -> tuple[dict[str, Any], ...]:
    values: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise AgentLoopEvaluationError("JSONL entries must be objects")
        values.append(value)
    return tuple(values)


def validate_dataset(
    manifest_path: str | Path = DEFAULT_MANIFEST_PATH,
) -> tuple[dict[str, Any], ...]:
    manifest_file = _repository_path(manifest_path)
    manifest = yaml.safe_load(manifest_file.read_text(encoding="utf-8"))
    if not isinstance(manifest, Mapping):
        raise AgentLoopEvaluationError("Agent Loop manifest must be an object")
    if manifest.get("status") != "provisional" or manifest.get("formal_runs_enabled") is not False:
        raise AgentLoopEvaluationError("Agent Loop dataset cannot enable formal evaluation")
    if manifest.get("content_policy") != "synthetic_only":
        raise AgentLoopEvaluationError("Agent Loop dataset must remain synthetic only")
    schema_path = _repository_path(str(manifest.get("schema_path", "")))
    cases_path = _repository_path(str(manifest.get("cases_path", "")))
    if _sha256(schema_path) != manifest.get("schema_sha256"):
        raise AgentLoopEvaluationError("Agent Loop schema SHA-256 mismatch")
    if _sha256(cases_path) != manifest.get("cases_sha256"):
        raise AgentLoopEvaluationError("Agent Loop cases SHA-256 mismatch")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    cases = _load_jsonl(cases_path)
    if not cases:
        raise AgentLoopEvaluationError("Agent Loop dataset must not be empty")
    for case in cases:
        try:
            validator.validate(case)
        except ValidationError as exc:
            raise AgentLoopEvaluationError("Agent Loop dataset schema validation failed") from exc
        if case.get("split") != "development" or case.get("content_policy") != "synthetic_only":
            raise AgentLoopEvaluationError("Agent Loop dataset contains a non-development case")
    return cases


def _action(value: object, *, field: str) -> AgentLoopEvaluationAction:
    try:
        return AgentLoopEvaluationAction(str(value))
    except ValueError as exc:
        raise AgentLoopEvaluationError(f"Agent Loop prediction {field} is unsupported") from exc


def _integer(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AgentLoopEvaluationError(f"Agent Loop prediction {field} is invalid")
    return value


def _boolean(value: object, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise AgentLoopEvaluationError(f"Agent Loop prediction {field} is invalid")
    return value


def _number(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AgentLoopEvaluationError(f"Agent Loop prediction {field} is invalid")
    return float(value)


def _passed_assertions(value: object) -> frozenset[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise AgentLoopEvaluationError("Agent Loop prediction security assertions are invalid")
    values = frozenset(value)
    if len(values) != len(value):
        raise AgentLoopEvaluationError("Agent Loop prediction security assertions must be unique")
    return values


def _observation(
    case: Mapping[str, object], prediction: Mapping[str, object]
) -> AgentLoopEvaluationObservation:
    if set(prediction) != _PREDICTION_FIELDS:
        raise AgentLoopEvaluationError("Agent Loop prediction fields are invalid")
    expected = case.get("expected")
    if not isinstance(expected, Mapping):
        raise AgentLoopEvaluationError("Agent Loop case expectation is invalid")
    prediction_id = prediction.get("id")
    if not isinstance(prediction_id, str) or prediction_id != case.get("id"):
        raise AgentLoopEvaluationError("Agent Loop prediction case ID is invalid")
    assertions = expected.get("security_assertions")
    if not isinstance(assertions, list) or any(not isinstance(item, str) for item in assertions):
        raise AgentLoopEvaluationError("Agent Loop case security assertions are invalid")
    stop_reason = prediction.get("stop_reason")
    if not isinstance(stop_reason, str):
        raise AgentLoopEvaluationError("Agent Loop prediction stop reason is invalid")
    return AgentLoopEvaluationObservation(
        case_id=prediction_id,
        expected_first_action=_action(expected.get("first_action"), field="expected first action"),
        expected_terminal_action=_action(
            expected.get("terminal_action"), field="expected terminal action"
        ),
        expected_min_tool_calls=_integer(
            expected.get("min_tool_calls"), field="expected minimum Tool calls"
        ),
        expected_requires_approval=_boolean(
            expected.get("requires_approval", False), field="expected approval"
        ),
        expected_security_assertions=frozenset(assertions),
        actual_first_action=_action(prediction.get("first_action"), field="first action"),
        actual_terminal_action=_action(prediction.get("terminal_action"), field="terminal action"),
        tool_calls=_integer(prediction.get("tool_calls"), field="Tool calls"),
        repeated_tool_calls=_integer(
            prediction.get("repeated_tool_calls"), field="repeated Tool calls"
        ),
        approval_requested=_boolean(
            prediction.get("approval_requested"), field="approval requested"
        ),
        goal_complete=_boolean(prediction.get("goal_complete"), field="goal completion"),
        subquestions_total=_integer(
            prediction.get("subquestions_total"), field="subquestion total"
        ),
        subquestions_covered=_integer(
            prediction.get("subquestions_covered"), field="subquestion coverage"
        ),
        evidence_required=_integer(prediction.get("evidence_required"), field="evidence required"),
        evidence_covered=_integer(prediction.get("evidence_covered"), field="evidence coverage"),
        citations_required=_integer(
            prediction.get("citations_required"), field="citations required"
        ),
        citations_covered=_integer(prediction.get("citations_covered"), field="citation coverage"),
        total_claims=_integer(prediction.get("total_claims"), field="total claims"),
        supported_claims=_integer(prediction.get("supported_claims"), field="supported claims"),
        passed_security_assertions=_passed_assertions(prediction.get("security_assertions_passed")),
        input_tokens=_integer(prediction.get("input_tokens"), field="input tokens"),
        output_tokens=_integer(prediction.get("output_tokens"), field="output tokens"),
        latency_ms=_number(prediction.get("latency_ms"), field="latency"),
        recovery_attempted=_boolean(
            prediction.get("recovery_attempted"), field="recovery attempted"
        ),
        recovery_succeeded=_boolean(
            prediction.get("recovery_succeeded"), field="recovery succeeded"
        ),
        stop_reason=stop_reason,
    )


def evaluate_predictions(
    cases: tuple[dict[str, Any], ...], predictions: tuple[dict[str, Any], ...]
) -> dict[str, object]:
    """Combine hash-pinned synthetic expectations with body-free observations."""

    by_id = {str(item["id"]): item for item in cases}
    prediction_ids = [str(item.get("id", "")) for item in predictions]
    if len(prediction_ids) != len(set(prediction_ids)) or set(prediction_ids) != set(by_id):
        raise AgentLoopEvaluationError(
            "predictions must contain each development case exactly once"
        )
    report = aggregate_agent_loop_metrics(
        tuple(_observation(by_id[str(prediction["id"])], prediction) for prediction in predictions)
    )
    report["dataset_version"] = "agent-loop-v1"
    report["content_policy"] = "synthetic_only"
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate synthetic Agent Loop prediction metadata"
    )
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST_PATH))
    parser.add_argument("--predictions")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        cases = validate_dataset(args.manifest)
        if args.validate_only:
            print(
                json.dumps(
                    {
                        "schema_version": "agent-loop-metrics-report-v1",
                        "quality_status": "provisional",
                        "dataset_split": "development",
                        "case_count": len(cases),
                        "formal_run_eligible": False,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if not args.predictions:
            print("Agent Loop evaluation blocked: predictions metadata is required")
            return 4
        predictions = _load_jsonl(_repository_path(args.predictions))
        print(json.dumps(evaluate_predictions(cases, predictions), sort_keys=True))
        return 0
    except (AgentLoopEvaluationError, json.JSONDecodeError, ValueError) as exc:
        print(f"Agent Loop evaluation failed: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
