"""Validate the guarded Grounded QA answer-evaluation protocol.

This command deliberately has no execution path while Stage 4 is provisional.
It validates pinned inputs without sending questions or document content to a model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONFIG_SCHEMA_PATH = REPOSITORY_ROOT / "cases/evals/configs/qa-eval-config-v1.schema.json"


class AnswerEvaluationConfigError(ValueError):
    """Raised for an invalid or unsafe answer-evaluation configuration."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve_path(raw_path: str) -> Path:
    path = (REPOSITORY_ROOT / raw_path).resolve()
    try:
        path.relative_to(REPOSITORY_ROOT)
    except ValueError as exc:
        raise AnswerEvaluationConfigError(f"path escapes repository root: {raw_path}") from exc
    if not path.is_file():
        raise AnswerEvaluationConfigError(f"required file does not exist: {raw_path}")
    return path


def _load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AnswerEvaluationConfigError(f"expected YAML object: {path}")
    return value


def _split_hash(lines: Sequence[str]) -> str:
    content = "\n".join(lines)
    if lines:
        content += "\n"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def validate_answer_evaluation_config(
    config: Mapping[str, Any], *, config_schema: Mapping[str, Any]
) -> dict[str, Any]:
    errors = sorted(
        Draft202012Validator(config_schema).iter_errors(config),
        key=lambda error: tuple(str(item) for item in error.absolute_path),
    )
    if errors:
        first = errors[0]
        location = ".".join(str(item) for item in first.absolute_path) or "<root>"
        raise AnswerEvaluationConfigError(f"config schema error at {location}: {first.message}")

    checked: dict[str, Path] = {}
    for section, path_key, hash_key in (
        ("corpus", "manifest_path", "manifest_sha256"),
        ("dataset", "cases_path", "cases_sha256"),
        ("dataset", "schema_path", "schema_sha256"),
        ("profile", "path", "sha256"),
        ("prompt", "path", "sha256"),
    ):
        value = config[section]
        path = _resolve_path(value[path_key])
        if _sha256(path) != value[hash_key]:
            raise AnswerEvaluationConfigError(f"{section} {path_key} SHA-256 mismatch")
        checked[f"{section}.{path_key}"] = path

    manifest = _load_yaml(checked["corpus.manifest_path"])
    if manifest.get("status") != config["gates"]["formal_manifest_status"]:
        raise AnswerEvaluationConfigError("manifest status does not match configured formal gate")
    if manifest.get("corpus_version") != config["corpus"]["version"]:
        raise AnswerEvaluationConfigError("manifest corpus version does not match config")

    dataset_schema = json.loads(checked["dataset.schema_path"].read_text(encoding="utf-8"))
    validator = Draft202012Validator(dataset_schema)
    lines = tuple(
        line
        for line in checked["dataset.cases_path"].read_text(encoding="utf-8").splitlines()
        if line
    )
    split_lines: dict[str, list[str]] = {"development": [], "holdout": []}
    for line in lines:
        case = json.loads(line)
        validator.validate(case)
        split = case.get("split")
        if split not in split_lines:
            raise AnswerEvaluationConfigError("dataset contains an unsupported split")
        split_lines[split].append(line)
    for split, pinned in config["dataset"]["splits"].items():
        if len(split_lines[split]) != pinned["count"]:
            raise AnswerEvaluationConfigError(f"dataset {split} count does not match config")
        if _split_hash(split_lines[split]) != pinned["content_sha256"]:
            raise AnswerEvaluationConfigError(f"dataset {split} content SHA-256 mismatch")

    return {
        "report_schema_version": "answer-report-v1",
        "status": config["status"],
        "formal_run_eligible": bool(config["gates"]["formal_runs_enabled"]),
        "pinned_inputs": {
            key: path.relative_to(REPOSITORY_ROOT).as_posix() for key, path in checked.items()
        },
        "dataset": {"split_counts": {key: len(value) for key, value in split_lines.items()}},
        "runtime": dict(config["runtime"]),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Grounded QA answer evaluation inputs")
    parser.add_argument("--config", default="cases/evals/configs/qa-v1.yaml")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        config_path = _resolve_path(args.config)
        config = _load_yaml(config_path)
        schema = json.loads(CONFIG_SCHEMA_PATH.read_text(encoding="utf-8"))
        summary = validate_answer_evaluation_config(config, config_schema=schema)
    except (AnswerEvaluationConfigError, json.JSONDecodeError) as exc:
        print(f"answer evaluation validation failed: {exc}")
        return 2
    print(json.dumps(summary, sort_keys=True))
    if args.validate_only:
        return 0
    print("answer evaluation execution blocked: formal_runs_enabled is false")
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
