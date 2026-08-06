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


__all__ = [
    "ConversationSensitivity",
    "ConversationSummary",
    "ConversationSummaryRepository",
    "most_restrictive_sensitivity",
]
