"""Trusted QA planning/context profile projection."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from domain.grounded_qa import MAX_QUESTION_CHARS, QAContractError


@dataclass(frozen=True)
class QAPlanningProfileV1:
    profile_id: str = "grounded-qa-provisional-v1"
    max_question_chars: int = MAX_QUESTION_CHARS
    max_history_messages: int = 12
    max_subqueries: int = 3
    rewrite_timeout_seconds: float = 5.0
    rewrite_enabled: bool = False
    max_evidence_items: int = 10
    max_input_tokens: int = 12_000
    max_history_tokens: int = 2_000
    max_tokens_per_evidence: int = 1_200
    max_evidence_per_source: int = 4
    max_chunks_per_document: int = 3

    def __post_init__(self) -> None:
        counts = (
            self.max_question_chars,
            self.max_subqueries,
            self.max_evidence_items,
            self.max_input_tokens,
            self.max_tokens_per_evidence,
            self.max_evidence_per_source,
            self.max_chunks_per_document,
        )
        if not self.profile_id or any(value < 1 for value in counts):
            raise QAContractError("QA planning profile identifiers and limits must be positive")
        if self.max_question_chars > MAX_QUESTION_CHARS:
            raise QAContractError("QA profile cannot exceed the domain question limit")
        if self.max_history_messages < 0 or self.max_history_tokens < 0:
            raise QAContractError("QA history limits cannot be negative")
        if not math.isfinite(self.rewrite_timeout_seconds) or self.rewrite_timeout_seconds <= 0:
            raise QAContractError("QA rewrite timeout must be finite and positive")


@dataclass(frozen=True)
class QAGenerationProfileV1:
    profile_id: str = "grounded-qa-provisional-v1"
    retrieval_profile_reference: str = "stage3-default-pending-formal-freeze"
    capability_alias: str = "fast_chat"
    model_identity: str = "fake-fast-chat-v1"
    prompt_template_id: str = "grounded-qa-v1-provisional"
    structured_output_schema: str = "grounded-answer-v1"
    temperature: float = 0.0
    max_output_tokens: int = 2_048
    max_repair_attempts: int = 1
    timeout_seconds: float = 45.0
    min_claim_support_rate: float = 1.0
    min_citation_completeness_rate: float = 1.0
    below_threshold_outcome: str = "refuse"
    max_model_calls: int = 2

    def __post_init__(self) -> None:
        identities = (
            self.profile_id,
            self.retrieval_profile_reference,
            self.model_identity,
            self.prompt_template_id,
            self.structured_output_schema,
        )
        if any(not value for value in identities):
            raise QAContractError("QA generation identities must not be blank")
        if self.capability_alias != "fast_chat":
            raise QAContractError("QA generation must use the fast_chat capability")
        if self.temperature != 0.0:
            raise QAContractError("Provisional QA generation temperature must be zero")
        if self.max_output_tokens < 1:
            raise QAContractError("QA output token limit must be positive")
        if self.max_repair_attempts not in {0, 1}:
            raise QAContractError("QA generation allows at most one repair attempt")
        if self.max_model_calls != 1 + self.max_repair_attempts:
            raise QAContractError("QA model call budget must equal the initial call plus repairs")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise QAContractError("QA chat timeout must be finite and positive")
        rates = (self.min_claim_support_rate, self.min_citation_completeness_rate)
        if any(not math.isfinite(value) or not 0 <= value <= 1 for value in rates):
            raise QAContractError("QA verification rates must be finite values from zero to one")
        if self.below_threshold_outcome != "refuse":
            raise QAContractError("Provisional below-threshold answers must become refusals")


def load_qa_planning_profile(data: Mapping[str, object]) -> QAPlanningProfileV1:
    """Load only the trusted planning fields from a schema-validated QA profile."""
    if data.get("schema_version") != "qa-profile-v1":
        raise QAContractError("Unsupported QA profile schema version")
    query = _mapping(data, "query")
    context = _mapping(data, "context")
    return QAPlanningProfileV1(
        profile_id=_string(data, "profile_id"),
        max_question_chars=_integer(query, "max_question_chars"),
        max_history_messages=_integer(query, "max_history_messages"),
        max_subqueries=_integer(query, "max_subqueries"),
        rewrite_timeout_seconds=_number(query, "rewrite_timeout_seconds"),
        rewrite_enabled=_boolean(query, "rewrite_enabled"),
        max_evidence_items=_integer(context, "max_evidence_items"),
        max_input_tokens=_integer(context, "max_input_tokens"),
        max_history_tokens=_integer(context, "max_history_tokens"),
        max_tokens_per_evidence=_integer(context, "max_tokens_per_evidence"),
        max_evidence_per_source=_integer(context, "max_evidence_per_source"),
        max_chunks_per_document=_integer(context, "max_chunks_per_document"),
    )


def load_qa_generation_profile(data: Mapping[str, object]) -> QAGenerationProfileV1:
    """Load trusted generation and verification fields after schema validation."""
    if data.get("schema_version") != "qa-profile-v1":
        raise QAContractError("Unsupported QA profile schema version")
    retrieval = _mapping(data, "retrieval")
    generation = _mapping(data, "generation")
    verification = _mapping(data, "verification")
    runtime = _mapping(data, "runtime")
    return QAGenerationProfileV1(
        profile_id=_string(data, "profile_id"),
        retrieval_profile_reference=_string(retrieval, "profile_reference"),
        capability_alias=_string(generation, "capability_alias"),
        model_identity=_string(generation, "model_identity"),
        prompt_template_id=_string(generation, "prompt_template_id"),
        structured_output_schema=_string(generation, "structured_output_schema"),
        temperature=_number(generation, "temperature"),
        max_output_tokens=_integer(generation, "max_output_tokens"),
        max_repair_attempts=_integer(generation, "max_repair_attempts"),
        timeout_seconds=_number(generation, "timeout_seconds"),
        min_claim_support_rate=_number(verification, "min_claim_support_rate"),
        min_citation_completeness_rate=_number(verification, "min_citation_completeness_rate"),
        below_threshold_outcome=_string(verification, "below_threshold_outcome"),
        max_model_calls=_integer(runtime, "max_model_calls"),
    )


def _mapping(values: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = values.get(key)
    if not isinstance(value, Mapping):
        raise QAContractError(f"QA profile {key} must be an object")
    return value


def _string(values: Mapping[str, object], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value:
        raise QAContractError(f"QA profile {key} must be a non-empty string")
    return value


def _integer(values: Mapping[str, object], key: str) -> int:
    value = values.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise QAContractError(f"QA profile {key} must be an integer")
    return value


def _number(values: Mapping[str, object], key: str) -> float:
    value = values.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise QAContractError(f"QA profile {key} must be a number")
    return float(value)


def _boolean(values: Mapping[str, object], key: str) -> bool:
    value = values.get(key)
    if not isinstance(value, bool):
        raise QAContractError(f"QA profile {key} must be a boolean")
    return value


__all__ = [
    "QAGenerationProfileV1",
    "QAPlanningProfileV1",
    "load_qa_generation_profile",
    "load_qa_planning_profile",
]
