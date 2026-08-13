"""Pure contracts for cross-session long-term memory (personalization Phase 5).

Phase 5 distills durable facts, preferences, and work patterns from
``conversation_summaries`` and Phase 2 ``usage_patterns`` into ``memory_entries``,
then injects a bounded, sensitivity-filtered slice back into new Assistant turns.
The data gate from the roadmap applies here too: memory content is a distilled,
standalone statement — never a full prompt, private body, or raw Provider response.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

from .conversation_context import ConversationSensitivity

MEMORY_CONTENT_LIMIT = 2_048
_MEMORY_EMBEDDING_MAX_DIMENSIONS = 4_096


class MemoryEntryType(StrEnum):
    """One durable memory category distilled across sessions."""

    FACT = "fact"
    PREFERENCE = "preference"
    PATTERN = "pattern"


@dataclass(frozen=True)
class MemoryEntry:
    """One distilled, content-addressed memory entry for cross-session injection."""

    entry_type: MemoryEntryType
    content: str
    source_conversation_id: UUID | None
    sensitivity: ConversationSensitivity
    content_sha256: str
    embedding: tuple[float, ...] | None = None
    source_kind: str = "summary"
    source_summary_id: UUID | None = None
    expires_at: datetime | None = None
    first_seen_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    frequency: int = 1
    memory_id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if self.entry_type not in MemoryEntryType:
            raise ValueError("memory entry type is invalid")
        if not self.content.strip() or len(self.content) > MEMORY_CONTENT_LIMIT:
            raise ValueError("memory entry content must be bounded and non-empty")
        if self.source_kind not in {"summary", "pattern"}:
            raise ValueError("memory entry source kind is invalid")
        if self.sensitivity not in ConversationSensitivity:
            raise ValueError("memory entry sensitivity is invalid")
        if self.embedding is not None and (
            not self.embedding
            or len(self.embedding) > _MEMORY_EMBEDDING_MAX_DIMENSIONS
            or any(not math.isfinite(value) for value in self.embedding)
        ):
            raise ValueError("memory entry embedding is invalid")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise ValueError("memory entry expiration must be timezone-aware")
        if self.first_seen_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("memory entry timestamps must be timezone-aware")
        if self.updated_at < self.first_seen_at:
            raise ValueError("memory entry updated_at must follow first_seen_at")
        if self.frequency < 1:
            raise ValueError("memory entry frequency must be positive")
        digest = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        if self.content_sha256 and self.content_sha256 != digest:
            raise ValueError("memory entry digest does not match its content")
        object.__setattr__(self, "content_sha256", digest)


class MemoryEntryRepository(Protocol):
    """Persistence port for distilled memory entries."""

    async def save(self, entry: MemoryEntry) -> MemoryEntry: ...

    async def get(self, memory_id: UUID) -> MemoryEntry | None: ...

    async def get_by_hash(self, content_sha256: str) -> MemoryEntry | None: ...

    async def update(self, entry: MemoryEntry) -> MemoryEntry: ...

    async def find_similar(
        self,
        embedding: tuple[float, ...],
        *,
        limit: int,
        entry_type: MemoryEntryType | None = None,
    ) -> tuple[MemoryEntry, ...]: ...

    async def list_all(self, *, limit: int | None = None) -> tuple[MemoryEntry, ...]: ...


def normalize_vector(values: Sequence[float]) -> tuple[float, ...]:
    """Return *values* scaled to unit length, or an all-zero vector when empty."""
    if not values:
        return ()
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0.0:
        return (0.0,) * len(values)
    return tuple(value / norm for value in values)


def cosine_similarity(left: Sequence[float] | None, right: Sequence[float] | None) -> float:
    """Cosine similarity between two vectors; zero when either side is unavailable."""
    if left is None or right is None or not left or not right or len(left) != len(right):
        return 0.0
    lhs = normalize_vector(left)
    rhs = normalize_vector(right)
    return sum(a * b for a, b in zip(lhs, rhs, strict=True))


__all__ = [
    "MEMORY_CONTENT_LIMIT",
    "MemoryEntry",
    "MemoryEntryRepository",
    "MemoryEntryType",
    "cosine_similarity",
    "normalize_vector",
]
