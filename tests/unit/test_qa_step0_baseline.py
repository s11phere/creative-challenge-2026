from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator, ValidationError

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = REPOSITORY_ROOT / "cases" / "evals" / "configs"


def _read_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _read_schema(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_controlled_input_hash_if_available(
    *,
    path: Path,
    expected_sha256: str,
    provisional: bool,
) -> None:
    if not path.is_file():
        assert provisional, f"required controlled input is unavailable: {path}"
        return
    assert _sha256(path) == expected_sha256


def test_provisional_qa_profile_matches_schema() -> None:
    profile = _read_yaml(CONFIG_ROOT / "qa-profile-v1.yaml")
    schema = _read_schema(CONFIG_ROOT / "qa-profile-v1.schema.json")

    Draft202012Validator(schema).validate(profile)


def test_controlled_input_may_be_absent_only_while_provisional(tmp_path: Path) -> None:
    missing_path = tmp_path / "controlled-input.jsonl"

    _assert_controlled_input_hash_if_available(
        path=missing_path,
        expected_sha256="0" * 64,
        provisional=True,
    )
    with pytest.raises(AssertionError, match="required controlled input is unavailable"):
        _assert_controlled_input_hash_if_available(
            path=missing_path,
            expected_sha256="0" * 64,
            provisional=False,
        )


def test_provisional_qa_evaluation_config_pins_inputs_and_matches_schema() -> None:
    config = _read_yaml(CONFIG_ROOT / "qa-v1.yaml")
    schema = _read_schema(CONFIG_ROOT / "qa-eval-config-v1.schema.json")

    Draft202012Validator(schema).validate(config)

    controlled_inputs_may_be_absent = (
        config["status"] == "provisional" and not config["gates"]["formal_runs_enabled"]
    )
    for section in ("corpus", "dataset"):
        for path_key, hash_key in (
            ("manifest_path", "manifest_sha256"),
            ("cases_path", "cases_sha256"),
            ("schema_path", "schema_sha256"),
        ):
            if path_key not in config[section]:
                continue
            _assert_controlled_input_hash_if_available(
                path=REPOSITORY_ROOT / config[section][path_key],
                expected_sha256=config[section][hash_key],
                provisional=controlled_inputs_may_be_absent,
            )
    assert _sha256(REPOSITORY_ROOT / config["profile"]["path"]) == config["profile"]["sha256"]
    assert _sha256(REPOSITORY_ROOT / config["prompt"]["path"]) == config["prompt"]["sha256"]


def test_formal_runs_are_rejected_by_the_provisional_schema() -> None:
    config = _read_yaml(CONFIG_ROOT / "qa-v1.yaml")
    schema = _read_schema(CONFIG_ROOT / "qa-eval-config-v1.schema.json")
    attempted_formal_config = deepcopy(config)
    attempted_formal_config["gates"]["formal_runs_enabled"] = True

    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(attempted_formal_config)


def test_dataset_baseline_has_required_slices_without_reading_corpus_content() -> None:
    cases_path = (
        REPOSITORY_ROOT / "cases" / "evals" / "datasets" / "knowledge-qa-v0" / "cases.jsonl"
    )
    if not cases_path.is_file():
        pytest.skip("controlled Stage 0 dataset is not distributed in a clean checkout")
    cases = [
        json.loads(line) for line in cases_path.read_text(encoding="utf-8").splitlines() if line
    ]

    assert {case["split"] for case in cases} == {"development", "holdout"}
    assert sum(case["split"] == "development" for case in cases) == 20
    assert sum(case["split"] == "holdout" for case in cases) == 10
    assert {case["expected_behavior"] for case in cases} == {"answer", "refuse"}
    categories = {case["category"] for case in cases}
    assert {
        "single_document_factual",
        "cross_document_synthesis",
        "version_or_conflict",
        "no_answer",
        "adversarial_document",
        "bilingual",
    } <= categories
