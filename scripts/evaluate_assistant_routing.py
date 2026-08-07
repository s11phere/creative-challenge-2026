"""Evaluate synthetic Assistant routing predictions without calling a Provider.

This command accepts aggregate-safe prediction metadata only. It never executes
routes, reads the controlled corpus, or enables a formal holdout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from application.assistant import (
    AssistantAction,
    AssistantRoutingObservation,
    aggregate_assistant_metrics,
)
from jsonschema import Draft202012Validator

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST_PATH = Path("cases/evals/datasets/assistant-routing-v1/manifest.yaml")


class AssistantRoutingEvaluationError(ValueError):
    """Raised when synthetic development metadata violates the evaluation protocol."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _repository_path(value: str | Path) -> Path:
    path = (REPOSITORY_ROOT / value).resolve()
    try:
        path.relative_to(REPOSITORY_ROOT)
    except ValueError as exc:
        raise AssistantRoutingEvaluationError("path escapes repository root") from exc
    if not path.is_file():
        raise AssistantRoutingEvaluationError("required evaluation file does not exist")
    return path


def _load_jsonl(path: Path) -> tuple[dict[str, Any], ...]:
    values: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise AssistantRoutingEvaluationError("JSONL entries must be objects")
        values.append(value)
    return tuple(values)


def validate_dataset(
    manifest_path: str | Path = DEFAULT_MANIFEST_PATH,
) -> tuple[dict[str, Any], ...]:
    manifest_file = _repository_path(manifest_path)
    manifest = yaml.safe_load(manifest_file.read_text(encoding="utf-8"))
    if not isinstance(manifest, Mapping):
        raise AssistantRoutingEvaluationError("routing manifest must be an object")
    if manifest.get("status") != "provisional" or manifest.get("formal_runs_enabled") is not False:
        raise AssistantRoutingEvaluationError("routing dataset cannot enable formal evaluation")
    if manifest.get("content_policy") != "synthetic_only":
        raise AssistantRoutingEvaluationError("routing dataset must remain synthetic only")
    schema_path = _repository_path(str(manifest.get("schema_path", "")))
    cases_path = _repository_path(str(manifest.get("cases_path", "")))
    if _sha256(schema_path) != manifest.get("schema_sha256"):
        raise AssistantRoutingEvaluationError("routing schema SHA-256 mismatch")
    if _sha256(cases_path) != manifest.get("cases_sha256"):
        raise AssistantRoutingEvaluationError("routing cases SHA-256 mismatch")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    cases = _load_jsonl(cases_path)
    if not cases:
        raise AssistantRoutingEvaluationError("routing dataset must not be empty")
    for case in cases:
        validator.validate(case)
        if case.get("split") != "development" or case.get("content_policy") != "synthetic_only":
            raise AssistantRoutingEvaluationError("routing dataset contains a non-development case")
    return cases


def evaluate_predictions(
    cases: tuple[dict[str, Any], ...], predictions: tuple[dict[str, Any], ...]
) -> dict[str, object]:
    """Combine pinned synthetic expectations with body-free prediction observations."""

    by_id = {str(item["id"]): item for item in cases}
    prediction_ids = [str(item.get("id", "")) for item in predictions]
    if len(prediction_ids) != len(set(prediction_ids)) or set(prediction_ids) != set(by_id):
        raise AssistantRoutingEvaluationError(
            "predictions must contain each development case exactly once"
        )
    observations: list[AssistantRoutingObservation] = []
    for prediction in predictions:
        case = by_id[str(prediction["id"])]
        expected = case["expected"]
        try:
            actual_action = AssistantAction(str(prediction["actual_action"]))
            expected_action = AssistantAction(str(expected["action"]))
        except (KeyError, ValueError) as exc:
            raise AssistantRoutingEvaluationError("prediction action is unsupported") from exc
        expected_source = str(expected["selection_source"])
        actual_source = str(prediction.get("actual_selection_source", "none"))
        if actual_source not in {"auto", "command", "none"}:
            raise AssistantRoutingEvaluationError("prediction selection source is unsupported")
        observations.append(
            AssistantRoutingObservation(
                case_id=str(case["id"]),
                expected_action=expected_action,
                actual_action=actual_action,
                expected_skill=expected["skill_name"],
                actual_skill=prediction.get("actual_skill"),
                expected_selection_source=expected_source,
                actual_selection_source=actual_source,
                clarification_requested=bool(prediction.get("clarification_requested", False)),
                clarification_resolved=bool(prediction.get("clarification_resolved", False)),
                command_expected=expected_source == "command",
                command_matched=bool(prediction.get("command_matched", False)),
                compaction_eligible=bool(prediction.get("compaction_eligible", False)),
                compaction_completed=bool(prediction.get("compaction_completed", False)),
                security_violations=int(prediction.get("security_violations", 0)),
                input_tokens=int(prediction.get("input_tokens", 0)),
                output_tokens=int(prediction.get("output_tokens", 0)),
                latency_ms=float(prediction.get("latency_ms", 0)),
                termination_reason=prediction.get("termination_reason"),
            )
        )
    report = aggregate_assistant_metrics(tuple(observations))
    report["dataset_version"] = "assistant-routing-v1"
    report["content_policy"] = "synthetic_only"
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate synthetic Assistant routing predictions")
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
                        "schema_version": "assistant-metrics-report-v1",
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
            print("assistant routing evaluation blocked: predictions metadata is required")
            return 4
        predictions = _load_jsonl(_repository_path(args.predictions))
        print(json.dumps(evaluate_predictions(cases, predictions), sort_keys=True))
        return 0
    except (AssistantRoutingEvaluationError, json.JSONDecodeError, ValueError) as exc:
        print(f"assistant routing evaluation failed: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
