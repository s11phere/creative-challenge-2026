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


__all__ = ["QAPlanningProfileV1", "load_qa_planning_profile"]
