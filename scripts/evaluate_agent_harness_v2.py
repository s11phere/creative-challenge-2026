"""Validate the body-free, synthetic Agent Harness v2 development fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, ValidationError

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST_PATH = Path("cases/evals/datasets/agent-harness-v2/manifest.yaml")


class AgentHarnessV2EvaluationError(ValueError):
    """Raised when a v2 development artifact crosses its metadata-only boundary."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _repository_file(value: str | Path) -> Path:
    path = (REPOSITORY_ROOT / value).resolve()
    try:
        path.relative_to(REPOSITORY_ROOT)
    except ValueError as exc:
        raise AgentHarnessV2EvaluationError("path escapes repository root") from exc
    if not path.is_file():
        raise AgentHarnessV2EvaluationError("required evaluation file does not exist")
    return path


def _load_jsonl(path: Path) -> tuple[dict[str, Any], ...]:
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise AgentHarnessV2EvaluationError("JSONL entries must be objects")
        entries.append(value)
    return tuple(entries)


def validate_dataset(
    manifest_path: str | Path = DEFAULT_MANIFEST_PATH,
) -> tuple[dict[str, Any], ...]:
    manifest_file = _repository_file(manifest_path)
    manifest = yaml.safe_load(manifest_file.read_text(encoding="utf-8"))
    if not isinstance(manifest, Mapping):
        raise AgentHarnessV2EvaluationError("Agent Harness v2 manifest must be an object")
    if manifest.get("status") != "provisional" or manifest.get("formal_runs_enabled") is not False:
        raise AgentHarnessV2EvaluationError("Agent Harness v2 cannot enable formal evaluation")
    if manifest.get("content_policy") != "synthetic_metadata_only":
        raise AgentHarnessV2EvaluationError("Agent Harness v2 must remain metadata-only")
    schema_path = _repository_file(str(manifest.get("schema_path", "")))
    cases_path = _repository_file(str(manifest.get("cases_path", "")))
    if _sha256(schema_path) != manifest.get("schema_sha256"):
        raise AgentHarnessV2EvaluationError("Agent Harness v2 schema SHA-256 mismatch")
    if _sha256(cases_path) != manifest.get("cases_sha256"):
        raise AgentHarnessV2EvaluationError("Agent Harness v2 cases SHA-256 mismatch")
    validator = Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8")))
    cases = _load_jsonl(cases_path)
    if not cases or len(cases) != len({item.get("id") for item in cases}):
        raise AgentHarnessV2EvaluationError("Agent Harness v2 cases must be non-empty and unique")
    for case in cases:
        try:
            validator.validate(case)
        except ValidationError as exc:
            raise AgentHarnessV2EvaluationError(
                "Agent Harness v2 case schema validation failed"
            ) from exc
        if _contains_forbidden_body(case):
            raise AgentHarnessV2EvaluationError(
                "Agent Harness v2 case contains forbidden body data"
            )
    return cases


def _contains_forbidden_body(value: object) -> bool:
    forbidden = frozenset(
        {
            "request",
            "prompt",
            "answer",
            "citation",
            "document",
            "tool_payload",
            "provider_response",
            "credential",
            "secret",
            "internal_id",
        }
    )
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or key.lower() in forbidden:
                return True
            if _contains_forbidden_body(item):
                return True
        return False
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_forbidden_body(item) for item in value)
    return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST_PATH))
    args = parser.parse_args(argv)
    try:
        cases = validate_dataset(args.manifest)
    except (AgentHarnessV2EvaluationError, json.JSONDecodeError, ValueError) as exc:
        print(f"Agent Harness v2 evaluation failed: {exc}")
        return 2
    print(
        json.dumps(
            {
                "schema_version": "agent-harness-v2-metrics-report-v1",
                "quality_status": "provisional",
                "dataset_split": "development",
                "case_count": len(cases),
                "content_policy": "synthetic_metadata_only",
                "formal_run_eligible": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
