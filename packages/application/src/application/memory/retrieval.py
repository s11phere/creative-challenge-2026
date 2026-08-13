"""Bounded, sensitivity-filtered retrieval of long-term memory for injection.

The snapshot assembler depends only on the ``MemoryRetrievalPort``; the concrete
retriever (embedding + vector search) lives in the infrastructure layer. The pure
scoring helpers here are deterministic and unit-tested: candidate entries are
ranked by cosine similarity plus a recency half-life and bounded to top-K.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from domain.conversation_context import (
    ConversationSensitivity,
    ConversationSensitivityRank,
)
from domain.memory_entries import MemoryEntry, cosine_similarity

MEMORY_TOP_K = 5
MEMORY_RECENCY_HALF_LIFE_DAYS = 14.0
MEMORY_RECENCY_WEIGHT = 0.25


class MemoryRetrievalPort(Protocol):
    """Best-effort retrieval of relevant memory entries for one Assistant turn."""

    async def retrieve(
        self,
        *,
        query: str,
        conversation_id: UUID,
        sensitivity: ConversationSensitivity,
        limit: int,
    ) -> tuple[MemoryEntry, ...]: ...


def injectable(entry: MemoryEntry, snapshot_sensitivity: ConversationSensitivity) -> bool:
    """A memory is injectable only when it is not restricted and never raises the
    snapshot's protection level (memory cannot leak into a lower-sensitivity context)."""
    if entry.sensitivity is ConversationSensitivity.RESTRICTED:
        return False
    return (
        ConversationSensitivityRank[entry.sensitivity.name]
        <= ConversationSensitivityRank[snapshot_sensitivity.name]
    )


def recency_factor(updated_at: datetime, *, now: datetime) -> float:
    """Half-life decay toward zero for memories that were observed long ago."""
    age_seconds = max(0.0, (now - updated_at).total_seconds())
    age_days = age_seconds / 86_400.0
    return float(0.5 ** (age_days / MEMORY_RECENCY_HALF_LIFE_DAYS))


def score_memory(entry: MemoryEntry, query_embedding: tuple[float, ...], *, now: datetime) -> float:
    """Combine vector similarity with a recency bonus for ranking candidates."""
    similarity = cosine_similarity(entry.embedding, query_embedding)
    return similarity + MEMORY_RECENCY_WEIGHT * recency_factor(entry.updated_at, now=now)


def select_memories(
    candidates: tuple[MemoryEntry, ...],
    query_embedding: tuple[float, ...],
    *,
    sensitivity: ConversationSensitivity,
    limit: int,
    now: datetime,
) -> tuple[MemoryEntry, ...]:
    """Filter by sensitivity, rank by similarity+recency, and bound to *limit*."""
    if limit < 1:
        raise ValueError("memory retrieval limit must be positive")
    eligible = tuple(entry for entry in candidates if injectable(entry, sensitivity))
    ranked = sorted(
        eligible,
        key=lambda entry: score_memory(entry, query_embedding, now=now),
        reverse=True,
    )
    return tuple(ranked[:limit])


__all__ = [
    "MEMORY_RECENCY_HALF_LIFE_DAYS",
    "MEMORY_RECENCY_WEIGHT",
    "MEMORY_TOP_K",
    "MemoryRetrievalPort",
    "injectable",
    "recency_factor",
    "score_memory",
    "select_memories",
]
