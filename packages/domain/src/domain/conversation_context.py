"""Immutable contracts for bounded, versioned conversation context."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from typing import Protocol
from uuid import UUID, uuid4


class ConversationSensitivity(StrEnum):
    """Sensitivity inherited by summaries and standalone Skill requests."""

    PUBLIC_DEMO = "public_demo"
    PRIVATE_LOCAL = "private_local"
    RESTRICTED = "restricted"


class ConversationSensitivityRank(IntEnum):
    PUBLIC_DEMO = 0
    PRIVATE_LOCAL = 1
    RESTRICTED = 2


def most_restrictive_sensitivity(
    values: tuple[ConversationSensitivity, ...],
) -> ConversationSensitivity:
    if not values:
        return ConversationSensitivity.PRIVATE_LOCAL
    return max(values, key=lambda value: ConversationSensitivityRank[value.name])


@dataclass(frozen=True)
class ConversationSummary:
    """Append-only rolling summary for a contiguous prefix of one conversation."""

    conversation_id: UUID
    space_id: UUID
    run_id: UUID
    covered_start_message_id: UUID
    covered_end_message_id: UUID
    covered_message_count: int
    content: str
    prompt_version: str
    model_identity: str
    sensitivity: ConversationSensitivity = ConversationSensitivity.PRIVATE_LOCAL
    summary_id: UUID = field(default_factory=uuid4)
    content_sha256: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.covered_message_count < 1:
            raise ValueError("Conversation summary must cover at least one message")
        if not self.content.strip() or len(self.content) > 16_000:
            raise ValueError("Conversation summary content must be bounded and non-empty")
        if not self.prompt_version.strip() or not self.model_identity.strip():
            raise ValueError("Conversation summary prompt and model versions are required")
        if self.created_at.tzinfo is None:
            raise ValueError("Conversation summary timestamp must be timezone-aware")
        digest = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        if self.content_sha256 and self.content_sha256 != digest:
            raise ValueError("Conversation summary digest does not match its content")
        object.__setattr__(self, "content_sha256", digest)


class ConversationSummaryRepository(Protocol):
    async def list_conversation_summaries(
        self, conversation_id: UUID
    ) -> tuple[ConversationSummary, ...]: ...

    async def create_conversation_summary(
        self, summary: ConversationSummary
    ) -> ConversationSummary: ...


@dataclass(frozen=True)
class ConversationToolHistoryItem:
    """Privacy-safe history for one observed Tool invocation."""

    iteration: int
    tool_name: str
    tool_version: str
    input_summary: str
    output_summary: str
    error_code: str | None = None
    retry_count: int = 0
    duration_ms: int = 0

    def __post_init__(self) -> None:
        if self.iteration < 1 or not self.tool_name.strip() or not self.tool_version.strip():
            raise ValueError("Tool history identity is invalid")
        if not self.input_summary.strip() or not self.output_summary.strip():
            raise ValueError("Tool history must contain bounded summaries")
        if len(self.input_summary) > 2_000 or len(self.output_summary) > 2_000:
            raise ValueError("Tool history summaries are too long")
        if self.retry_count < 0 or self.duration_ms < 0:
            raise ValueError("Tool history counters cannot be negative")


@dataclass(frozen=True)
class ConversationEvidenceCoverage:
    """Counts and safe identifiers describing evidence available to a Run."""

    candidate_count: int = 0
    covered_count: int = 0
    required_count: int = 0
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if min(self.candidate_count, self.covered_count, self.required_count) < 0:
            raise ValueError("Evidence coverage counts cannot be negative")
        if self.covered_count > self.candidate_count:
            raise ValueError("Covered evidence cannot exceed candidates")
        if (
            len(self.evidence_ids) > 100
            or len(set(self.evidence_ids)) != len(self.evidence_ids)
            or any(not item.strip() for item in self.evidence_ids)
        ):
            raise ValueError("Evidence identifiers must be unique and bounded")

    @property
    def ratio(self) -> float:
        if self.required_count <= 0:
            return 1.0
        return min(1.0, self.covered_count / self.required_count)


__all__ = [
    "ConversationSensitivity",
    "ConversationEvidenceCoverage",
    "ConversationSummary",
    "ConversationSummaryRepository",
    "ConversationToolHistoryItem",
    "most_restrictive_sensitivity",
]
