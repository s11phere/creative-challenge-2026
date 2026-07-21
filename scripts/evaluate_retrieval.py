"""Validate or execute a versioned retrieval evaluation configuration.

Stage 3 Step 0 provides validation only. The retrieval executor is connected in
later steps; until then this command refuses non-validation runs explicitly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from application.retrieval.evaluation import canonical_config_hash
from jsonschema import Draft202012Validator

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONFIG_SCHEMA_PATH = (
    REPOSITORY_ROOT / "cases" / "evals" / "configs" / "retrieval-eval-config-v1.schema.json"
)


class EvaluationConfigError(ValueError):
    """Raised when an evaluation input violates the frozen protocol."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _resolve_repository_path(raw_path: str) -> Path:
    candidate = (REPOSITORY_ROOT / raw_path).resolve()
    try:
        candidate.relative_to(REPOSITORY_ROOT)
    except ValueError as exc:
        raise EvaluationConfigError(f"Path escapes repository root: {raw_path}") from exc
    if not candidate.is_file():
        raise EvaluationConfigError(f"Required file does not exist: {raw_path}")
    return candidate


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise EvaluationConfigError(f"Expected a YAML object: {path}")
    return value


def _validate_sha(path: Path, expected: str, label: str) -> None:
    actual = _sha256(path)
    if actual != expected:
        raise EvaluationConfigError(f"{label} SHA-256 mismatch: expected {expected}, got {actual}")


def _split_content_hash(raw_lines: Sequence[str]) -> str:
    content = "\n".join(raw_lines)
    if raw_lines:
        content += "\n"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def validate_evaluation_config(
    config: Mapping[str, Any],
    *,
    config_schema: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate config, allowlist, source bytes, dataset schema, and splits."""
    schema_errors = sorted(
        Draft202012Validator(config_schema).iter_errors(config),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if schema_errors:
        first = schema_errors[0]
        location = ".".join(str(part) for part in first.absolute_path) or "<root>"
        raise EvaluationConfigError(f"Config schema error at {location}: {first.message}")

    corpus_config = config["corpus"]
    dataset_config = config["dataset"]
    gates = config["gates"]

    manifest_path = _resolve_repository_path(corpus_config["manifest_path"])
    _validate_sha(manifest_path, corpus_config["manifest_sha256"], "Corpus manifest")
    manifest = _load_yaml_mapping(manifest_path)
    if manifest.get("corpus_version") != corpus_config["version"]:
        raise EvaluationConfigError("Corpus version does not match evaluation config")

    source_by_key: dict[str, tuple[str, Mapping[str, Any]]] = {}
    sensitivity_counts: Counter[str] = Counter()
    repository_fixture_count = 0
    source_count = 0
    for space in manifest.get("spaces", []):
        if not isinstance(space, dict) or not isinstance(space.get("id"), str):
            raise EvaluationConfigError("Manifest contains an invalid Space entry")
        for source in space.get("sources", []):
            if not isinstance(source, dict):
                raise EvaluationConfigError("Manifest contains an invalid source entry")
            source_key = source.get("source_key")
            if not isinstance(source_key, str) or source_key in source_by_key:
                raise EvaluationConfigError(
                    f"Manifest source_key is invalid or duplicate: {source_key}"
                )
            allowed_uses = source.get("allowed_uses")
            required_use = corpus_config["required_allowed_use"]
            if not isinstance(allowed_uses, list) or required_use not in allowed_uses:
                raise EvaluationConfigError(f"Source {source_key} does not allow {required_use}")
            sensitivity = source.get("sensitivity")
            if not isinstance(sensitivity, str) or not sensitivity:
                raise EvaluationConfigError(
                    f"Source {source_key} has no sensitivity classification"
                )
            source_path = _resolve_repository_path(f"cases/{source.get('path', '')}")
            content_sha256 = source.get("content_sha256")
            if not isinstance(content_sha256, str):
                raise EvaluationConfigError(f"Source {source_key} has no content_sha256")
            _validate_sha(source_path, content_sha256, f"Source {source_key}")
            source_by_key[source_key] = (space["id"], source)
            sensitivity_counts[sensitivity] += 1
            repository_fixture_count += int("repository_fixture" in allowed_uses)
            source_count += 1

    cases_path = _resolve_repository_path(dataset_config["cases_path"])
    dataset_schema_path = _resolve_repository_path(dataset_config["schema_path"])
    _validate_sha(cases_path, dataset_config["cases_sha256"], "Dataset")
    _validate_sha(dataset_schema_path, dataset_config["schema_sha256"], "Dataset schema")
    dataset_schema = json.loads(dataset_schema_path.read_text(encoding="utf-8"))
    case_validator = Draft202012Validator(dataset_schema)

    raw_lines = [line for line in cases_path.read_text(encoding="utf-8").splitlines() if line]
    split_lines: dict[str, list[str]] = {"development": [], "holdout": []}
    split_ids: dict[str, set[str]] = {"development": set(), "holdout": set()}
    split_questions: dict[str, set[str]] = {"development": set(), "holdout": set()}
    category_counts: Counter[str] = Counter()
    evidenced_case_count = 0
    no_evidence_case_count = 0

    for line_number, raw_line in enumerate(raw_lines, start=1):
        try:
            case = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise EvaluationConfigError(f"Invalid dataset JSON on line {line_number}") from exc
        errors = sorted(case_validator.iter_errors(case), key=lambda error: list(error.path))
        if errors:
            raise EvaluationConfigError(
                f"Dataset schema error on line {line_number}: {errors[0].message}"
            )
        split = case["split"]
        case_id = case["id"]
        if case_id in split_ids[split]:
            raise EvaluationConfigError(f"Duplicate case ID: {case_id}")
        split_lines[split].append(raw_line)
        split_ids[split].add(case_id)
        split_questions[split].add(" ".join(case["question"].split()).casefold())
        category_counts[case["category"]] += 1
        evidenced_case_count += int(bool(case["evidence"]))
        no_evidence_case_count += int(not case["evidence"])

        for evidence in case["evidence"]:
            source_key = evidence["source_key"]
            source_entry = source_by_key.get(source_key)
            if source_entry is None:
                raise EvaluationConfigError(
                    f"Case {case_id} references source outside manifest: {source_key}"
                )
            source_space, source = source_entry
            if source_space != case["space_id"]:
                raise EvaluationConfigError(
                    f"Case {case_id} evidence crosses Space boundary: {source_key}"
                )
            if source["content_sha256"] != evidence["source_version"]:
                raise EvaluationConfigError(
                    f"Case {case_id} evidence version differs from manifest: {source_key}"
                )

    if split_ids["development"] & split_ids["holdout"]:
        raise EvaluationConfigError("Development and holdout case IDs overlap")
    if split_questions["development"] & split_questions["holdout"]:
        raise EvaluationConfigError("Development and holdout questions overlap")
    for split, expected in dataset_config["splits"].items():
        if len(split_lines[split]) != expected["count"]:
            raise EvaluationConfigError(f"Unexpected {split} case count")
        if _split_content_hash(split_lines[split]) != expected["content_sha256"]:
            raise EvaluationConfigError(f"Unexpected {split} split content hash")

    blockers: list[str] = []
    if manifest.get("status") != gates["formal_manifest_status"]:
        blockers.append(
            f"manifest_status={manifest.get('status')} (requires {gates['formal_manifest_status']})"
        )
    stage_2_acceptance_path = (REPOSITORY_ROOT / gates["stage_2_step_9_acceptance_path"]).resolve()
    if not stage_2_acceptance_path.is_file():
        blockers.append("stage_2_step_9_acceptance_missing")
    if not gates["formal_runs_enabled"]:
        blockers.append("formal_runs_disabled_by_config")

    return {
        "schema_version": "retrieval-eval-validation-v1",
        "config_hash": canonical_config_hash(config),
        "config_status": config["status"],
        "formal_run_eligible": not blockers,
        "formal_run_blockers": blockers,
        "corpus": {
            "version": manifest["corpus_version"],
            "manifest_status": manifest["status"],
            "source_count": source_count,
            "repository_fixture_count": repository_fixture_count,
            "sensitivity_counts": dict(sorted(sensitivity_counts.items())),
        },
        "dataset": {
            "version": dataset_config["version"],
            "case_count": len(raw_lines),
            "split_counts": {split: len(ids) for split, ids in sorted(split_ids.items())},
            "category_counts": dict(sorted(category_counts.items())),
            "evidenced_case_count": evidenced_case_count,
            "no_evidence_case_count": no_evidence_case_count,
        },
        "protocol": config["protocol"],
        "runtime": config["runtime"],
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Repository-relative evaluation YAML")
    parser.add_argument("--split", choices=("development", "holdout"), default="development")
    parser.add_argument("--output", help="Optional path for the validation summary")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate frozen inputs without executing retrieval",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        config_path = _resolve_repository_path(args.config)
        config = _load_yaml_mapping(config_path)
        config_schema = json.loads(CONFIG_SCHEMA_PATH.read_text(encoding="utf-8"))
        summary = validate_evaluation_config(config, config_schema=config_schema)
    except (EvaluationConfigError, OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
        print(f"evaluation config invalid: {exc}", file=sys.stderr)
        return 2

    summary["selected_split"] = args.split
    rendered = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output_path = Path(args.output).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
    print(rendered, end="")

    if not args.validate_only:
        print(
            "retrieval execution is not connected in Stage 3 Step 0; use --validate-only",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
