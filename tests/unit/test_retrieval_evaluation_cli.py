from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from pytest import MonkeyPatch

from scripts import evaluate_retrieval


def _write_validation_fixture(root: Path, monkeypatch: MonkeyPatch) -> tuple[dict[str, Any], Path]:
    source_bytes = b"public retrieval fixture\n"
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    source_path = root / "cases" / "evals" / "source.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(source_bytes)

    manifest = {
        "schema_version": "1.0",
        "corpus_version": "v0",
        "status": "draft_pending_license_review",
        "spaces": [
            {
                "id": "test_space",
                "sources": [
                    {
                        "source_key": "test_space/source",
                        "path": "evals/source.txt",
                        "allowed_uses": ["local_evaluation", "repository_fixture"],
                        "sensitivity": "public_demo",
                        "content_sha256": source_hash,
                    }
                ],
            }
        ],
    }
    manifest_path = root / "cases" / "evals" / "manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")

    dataset_schema = {"type": "object"}
    dataset_schema_path = root / "cases" / "evals" / "dataset.schema.json"
    dataset_schema_path.write_text(json.dumps(dataset_schema), encoding="utf-8")
    cases = (
        {
            "id": "qa-001",
            "split": "development",
            "category": "single_document_factual",
            "space_id": "test_space",
            "question": "Where is the fixture?",
            "evidence": [
                {
                    "source_key": "test_space/source",
                    "source_version": source_hash,
                }
            ],
        },
        {
            "id": "qa-002",
            "split": "holdout",
            "category": "no_answer",
            "space_id": "test_space",
            "question": "Is there unsupported content?",
            "evidence": [],
        },
    )
    raw_lines = tuple(json.dumps(case, separators=(",", ":")) for case in cases)
    cases_path = root / "cases" / "evals" / "cases.jsonl"
    cases_path.write_text("\n".join(raw_lines) + "\n", encoding="utf-8")

    config = {
        "schema_version": "retrieval-eval-config-v1",
        "status": "provisional",
        "corpus": {
            "version": "v0",
            "manifest_path": "cases/evals/manifest.yaml",
            "manifest_sha256": evaluate_retrieval._sha256(manifest_path),
            "required_allowed_use": "local_evaluation",
        },
        "dataset": {
            "version": "v0",
            "cases_path": "cases/evals/cases.jsonl",
            "cases_sha256": evaluate_retrieval._sha256(cases_path),
            "schema_path": "cases/evals/dataset.schema.json",
            "schema_sha256": evaluate_retrieval._sha256(dataset_schema_path),
            "splits": {
                "development": {
                    "count": 1,
                    "content_sha256": evaluate_retrieval._split_content_hash((raw_lines[0],)),
                },
                "holdout": {
                    "count": 1,
                    "content_sha256": evaluate_retrieval._split_content_hash((raw_lines[1],)),
                },
            },
        },
        "protocol": {
            "evidence_match": "source_key+source_version+locator_overlap-v1",
            "metrics": [
                "evidence_recall",
                "mrr",
                "evidence_ndcg",
                "full_evidence_coverage",
                "must_exclude_violations",
                "latency_p50_p95",
                "failure_rate",
            ],
            "recall_k": 5,
            "ndcg_k": 5,
            "no_evidence_policy": "exclude_from_recall_and_ranking_denominators",
            "safety_slices": [
                "no_answer",
                "cross_space",
                "withdrawn_version",
                "adversarial_document",
            ],
        },
        "runtime": {
            "target": "test",
            "concurrency": 1,
            "warmup_queries": 0,
            "sampling": "one_complete_pass",
            "retrieval_p95_budget_ms": 1000,
        },
        "gates": {
            "formal_manifest_status": "frozen",
            "stage_2_step_9_acceptance_path": "docs/stage-2-acceptance.md",
            "formal_runs_enabled": False,
        },
    }
    config_path = root / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setattr(evaluate_retrieval, "REPOSITORY_ROOT", root)
    return config, config_path


def test_repository_provisional_config_matches_schema() -> None:
    config_path = (
        evaluate_retrieval.REPOSITORY_ROOT / "cases" / "evals" / "configs" / "retrieval-v1.yaml"
    )
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    schema = json.loads(evaluate_retrieval.CONFIG_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(config)


def test_fixture_config_validates_as_provisional(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    config, _ = _write_validation_fixture(tmp_path, monkeypatch)
    schema = json.loads(evaluate_retrieval.CONFIG_SCHEMA_PATH.read_text(encoding="utf-8"))

    summary = evaluate_retrieval.validate_evaluation_config(config, config_schema=schema)

    assert summary["formal_run_eligible"] is False
    assert summary["corpus"]["source_count"] == 1
    assert summary["corpus"]["repository_fixture_count"] == 1
    assert summary["dataset"]["split_counts"] == {"development": 1, "holdout": 1}
    assert summary["dataset"]["evidenced_case_count"] == 1
    assert summary["dataset"]["no_evidence_case_count"] == 1


def test_cli_requires_validate_only(tmp_path: Path, monkeypatch: MonkeyPatch, capsys: Any) -> None:
    _, config_path = _write_validation_fixture(tmp_path, monkeypatch)
    result = evaluate_retrieval.main(["--config", config_path.name, "--split", "development"])
    captured = capsys.readouterr()
    assert result == 3
    assert "not connected" in captured.err


def test_split_hash_preserves_jsonl_line_order() -> None:
    lines = ('{"id":"a"}', '{"id":"b"}')
    assert evaluate_retrieval._split_content_hash(lines) != evaluate_retrieval._split_content_hash(
        tuple(reversed(lines))
    )


def test_repository_path_cannot_escape_workspace(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(evaluate_retrieval, "REPOSITORY_ROOT", tmp_path)
    try:
        evaluate_retrieval._resolve_repository_path("../outside")
    except evaluate_retrieval.EvaluationConfigError as exc:
        assert "escapes repository root" in str(exc)
    else:
        raise AssertionError("repository path traversal was accepted")
