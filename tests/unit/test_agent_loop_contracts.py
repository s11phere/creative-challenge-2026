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
    REPOSITORY_ROOT / "packages" / "agent_runtime" / "src" / "agent_runtime" / "contracts"
)
V2_DATASET_ROOT = REPOSITORY_ROOT / "cases" / "evals" / "datasets" / "agent-harness-v2"


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(_json(CONTRACT_ROOT / name))


def test_v2_contract_manifest_hashes_and_native_tool_messages_are_frozen() -> None:
    manifest = _json(CONTRACT_ROOT / "manifest-v2.json")
    assert manifest["schema_version"] == "agent-runtime-contract-manifest-v1"
    assert manifest["contract_version"] == "v2"
    assert manifest["status"] == "provisional"
    assert set(manifest["artifacts"]) == {
        "agent-loop-v2.schema.json",
        "agent-model-context-v2.schema.json",
        "native-tool-use-v2.schema.json",
    }
    for name, expected in manifest["artifacts"].items():
        assert _sha256(CONTRACT_ROOT / name) == expected
        Draft202012Validator.check_schema(_json(CONTRACT_ROOT / name))

    native_validator = _validator("native-tool-use-v2.schema.json")
    native_validator.validate(
        {
            "schema_version": "native-tool-use-v2",
            "message_kind": "tool_call",
            "call_id": "call_1",
            "tool_name": "invoke_skill",
            "arguments": {"name": "knowledge_agent"},
        }
    )
    native_validator.validate(
        {
            "schema_version": "native-tool-use-v2",
            "message_kind": "terminal_text",
            "text": "Synthetic terminal response.",
        }
    )
    with pytest.raises(ValidationError):
        native_validator.validate(
            {
                "schema_version": "native-tool-use-v2",
                "message_kind": "tool_call",
                "call_id": "call_1",
                "tool_name": "invoke_skill",
                "arguments": {"space_id": "model-chosen"},
            }
        )


def test_v2_model_context_remains_bounded_and_body_free() -> None:
    validator = _validator("agent-model-context-v2.schema.json")
    validator.validate(
        {
            "schema_version": "agent-model-context-v2",
            "goal": "Synthetic goal.",
            "selected_skills": [
                {
                    "name": "knowledge_agent",
                    "version": "1.0.0",
                    "content_sha256": "a" * 64,
                }
            ],
            "decision_history": [
                {
                    "iteration": 1,
                    "kind": "tool",
                    "tool_name": "invoke_skill",
                    "status": "succeeded",
                    "summary": "Selected a trusted Skill.",
                }
            ],
            "observations": [
                {
                    "iteration": 1,
                    "tool_name": "invoke_skill",
                    "status": "succeeded",
                    "summary": "Skill is available.",
                }
            ],
            "progress_summary": "One selected Skill; no unresolved items.",
        }
    )
    with pytest.raises(ValidationError):
        validator.validate(
            {
                "schema_version": "agent-model-context-v2",
                "goal": "Synthetic goal.",
                "selected_skills": [],
                "decision_history": [],
                "observations": [],
                "progress_summary": "x" * 4_001,
            }
        )


def test_tool_and_sse_contracts_reject_scope_or_private_payload_fields() -> None:
    tool_validator = _validator("tool-invocation-v1.schema.json")
    tool_validator.validate(
        {
            "schema_version": "tool-invocation-v1",
            "tool_name": "fs_read",
            "tool_version": "1.0.0",
            "arguments": {"path": "docs/README.md"},
        }
    )
    with pytest.raises(ValidationError):
        tool_validator.validate(
            {
                "schema_version": "tool-invocation-v1",
                "tool_name": "fs_read",
                "tool_version": "1.0.0",
                "arguments": {"space_id": "server-chosen"},
            }
        )

    sse_validator = _validator("agent-run-sse-v3.schema.json")
    sse_validator.validate(
        {
            "schema_version": "agent-run-sse-v3",
            "run_id": "00000000-0000-0000-0000-000000000001",
            "sequence": 1,
            "event_type": "tool_output",
            "payload": {"tool_name": "fs_read", "output_summary": "sha256:abc", "duration_ms": 4},
        }
    )
    with pytest.raises(ValidationError):
        sse_validator.validate(
            {
                "schema_version": "agent-run-sse-v3",
                "run_id": "00000000-0000-0000-0000-000000000001",
                "sequence": 2,
                "event_type": "tool_output",
                "payload": {"prompt": "private"},
            }
        )


def test_reasoning_and_final_answer_contracts_keep_provider_neutral_fields() -> None:
    _validator("reasoning-profile-v1.schema.json").validate(
        {
            "schema_version": "reasoning-profile-v1",
            "requested_effort": "auto",
            "effective_effort": "low",
            "provider": "fake",
            "model": "synthetic-chat",
            "mapping_version": "reasoning-mapping-v1",
            "mode": "native",
            "downgrade_reason": "none",
        }
    )
    _validator("assistant-final-answer-v2.schema.json").validate(
        {
            "schema_version": "assistant-final-answer-v2",
            "status": "refused",
            "message": "The synthetic evidence is insufficient.",
            "stop_reason": "evidence_insufficient",
            "publication_id": "assistant-publication:synthetic-1",
            "verified_evidence_count": 0,
            "citation_count": 0,
        }
    )


def test_agent_harness_v2_development_dataset_is_body_free_and_hash_pinned() -> None:
    manifest = yaml.safe_load((V2_DATASET_ROOT / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["status"] == "provisional"
    assert manifest["distribution_scope"] == "repository_fixture"
    assert manifest["content_policy"] == "synthetic_metadata_only"
    assert manifest["formal_runs_enabled"] is False
    assert _sha256(REPOSITORY_ROOT / manifest["schema_path"]) == manifest["schema_sha256"]
    assert _sha256(REPOSITORY_ROOT / manifest["cases_path"]) == manifest["cases_sha256"]

    validator = Draft202012Validator(_json(V2_DATASET_ROOT / "schema.json"))
    cases = [
        json.loads(line)
        for line in (V2_DATASET_ROOT / "development.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert len(cases) == 10
    assert len(cases) == len({case["id"] for case in cases})
    serialized = json.dumps(cases, sort_keys=True)
    assert all(
        f'"{field}"' not in serialized
        for field in (
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
        )
    )
    for case in cases:
        validator.validate(case)
        assert case["content_policy"] == "synthetic_metadata_only"
        assert case["split"] == "development"
