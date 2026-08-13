from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator, ValidationError

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = (
    REPOSITORY_ROOT / "packages" / "application" / "src" / "application" / "assistant" / "contracts"
)
DATASET_ROOT = REPOSITORY_ROOT / "cases" / "evals" / "datasets" / "assistant-routing-v1"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(_read_json(CONTRACT_ROOT / name))


def test_frozen_assistant_contract_artifacts_match_manifest() -> None:
    manifest = _read_json(CONTRACT_ROOT / "manifest.json")
    assert manifest["contract_version"] == "v1"
    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, dict)
    assert set(artifacts) == {
        "base-system-prompt-v7.txt",
        "base-system-prompt-v8.txt",
        "router-decision-v1.schema.json",
        "command-catalog-v1.schema.json",
        "clarification-v1.schema.json",
        "error-codes-v1.json",
    }
    for name, expected_sha256 in artifacts.items():
        assert isinstance(expected_sha256, str)
        assert _sha256(CONTRACT_ROOT / name) == expected_sha256


def test_router_decision_schema_accepts_only_safe_declared_shapes() -> None:
    validator = _validator("router-decision-v1.schema.json")
    validator.validate(
        {
            "schema_version": "assistant-router-decision-v1",
            "action": "respond",
            "assistant_message": "A direct response.",
        }
    )
    validator.validate(
        {
            "schema_version": "assistant-router-decision-v1",
            "action": "clarify",
            "assistant_message": "Which workspace document do you mean?",
        }
    )
    validator.validate(
        {
            "schema_version": "assistant-router-decision-v1",
            "action": "invoke_skill",
            "skill_name": "knowledge_agent",
            "arguments": {"question": "Synthetic question."},
        }
    )

    with pytest.raises(ValidationError):
        validator.validate(
            {
                "schema_version": "assistant-router-decision-v1",
                "action": "invoke_skill",
                "skill_name": "knowledge_agent",
                "arguments": {"document_id": "model-chosen-id"},
            }
        )
    with pytest.raises(ValidationError):
        validator.validate(
            {
                "schema_version": "assistant-router-decision-v1",
                "action": "respond",
                "assistant_message": "A direct response.",
                "extra": "not allowed",
            }
        )


def test_command_catalog_schema_exposes_only_safe_invocation_metadata() -> None:
    validator = _validator("command-catalog-v1.schema.json")
    catalog = {
        "schema_version": "assistant-command-catalog-v1",
        "commands": [
            {
                "name": "ask",
                "aliases": [],
                "kind": "skill",
                "description": "Answer from the current workspace.",
                "argument_hint": "<question>",
                "input_mode": "question",
            }
        ],
    }
    validator.validate(catalog)

    catalog["commands"][0]["max_tool_calls"] = 4
    with pytest.raises(ValidationError):
        validator.validate(catalog)


def test_clarification_schema_allows_only_safe_server_candidate_metadata() -> None:
    validator = _validator("clarification-v1.schema.json")
    clarification = {
        "schema_version": "assistant-clarification-v1",
        "clarification_id": "clarify:synthetic:1",
        "kind": "resource_ambiguous",
        "message": "Choose one synthetic document.",
        "resource_candidates": [
            {
                "candidate_id": "candidate:1",
                "resource_type": "document",
                "label": "Synthetic design overview",
                "source_label": "Synthetic source",
                "version_label": "published",
            }
        ],
    }
    validator.validate(clarification)

    clarification["resource_candidates"][0]["content"] = "Document text is never catalog metadata."
    with pytest.raises(ValidationError):
        validator.validate(clarification)


def test_error_code_contract_keeps_resource_errors_stable() -> None:
    errors = _read_json(CONTRACT_ROOT / "error-codes-v1.json")
    codes = {entry["code"] for entry in errors["codes"]}
    assert {"RESOURCE_NOT_FOUND", "RESOURCE_CONFLICT", "RUN_AGENT_DECISION_INVALID"} <= codes
    assert all(isinstance(code, str) and code.isupper() for code in codes)


def test_routing_development_dataset_is_synthetic_and_schema_valid() -> None:
    manifest = yaml.safe_load((DATASET_ROOT / "manifest.yaml").read_text(encoding="utf-8"))
    assert isinstance(manifest, dict)
    assert manifest["status"] == "provisional"
    assert manifest["distribution_scope"] == "repository_fixture"
    assert manifest["content_policy"] == "synthetic_only"
    assert manifest["formal_runs_enabled"] is False
    assert _sha256(REPOSITORY_ROOT / manifest["schema_path"]) == manifest["schema_sha256"]
    assert _sha256(REPOSITORY_ROOT / manifest["cases_path"]) == manifest["cases_sha256"]

    schema = _read_json(DATASET_ROOT / "schema.json")
    validator = Draft202012Validator(schema)
    cases = [
        json.loads(line)
        for line in (DATASET_ROOT / "development.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert len(cases) == len({case["id"] for case in cases})
    for case in cases:
        validator.validate(case)
        assert case["content_policy"] == "synthetic_only"
        assert case["split"] == "development"

    assert {case["category"] for case in cases} == {
        "general_chat",
        "knowledge_request",
        "explicit_command",
        "ambiguous_resource",
        "prompt_injection",
        "cross_space",
        "write_approval",
        "no_skill_counterexample",
    }
