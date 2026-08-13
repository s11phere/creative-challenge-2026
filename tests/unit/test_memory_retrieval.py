from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from application.memory import (
    MEMORY_RECENCY_HALF_LIFE_DAYS,
    MEMORY_RECENCY_WEIGHT,
    MEMORY_TOP_K,
    injectable,
    recency_factor,
    score_memory,
    select_memories,
)
from domain.conversation_context import ConversationSensitivity
from domain.memory_entries import MemoryEntry, MemoryEntryType

_NOW = datetime(2026, 8, 13, tzinfo=UTC)


def _entry(
    *,
    index: int = 0,
    content: str | None = None,
    sensitivity: ConversationSensitivity = ConversationSensitivity.PRIVATE_LOCAL,
    embedding: tuple[float, ...] | None = None,
    updated_at: datetime = _NOW,
) -> MemoryEntry:
    return MemoryEntry(
        entry_type=MemoryEntryType.FACT,
        content=content or f"fact {index}",
        source_conversation_id=None,
        sensitivity=sensitivity,
        content_sha256="",
        embedding=embedding,
        source_kind="summary",
        first_seen_at=updated_at,
        updated_at=updated_at,
    )


class TestInjectable:
    def test_restricted_never_injectable(self) -> None:
        entry = _entry(sensitivity=ConversationSensitivity.RESTRICTED)
        assert not injectable(entry, ConversationSensitivity.RESTRICTED)
        assert not injectable(entry, ConversationSensitivity.PRIVATE_LOCAL)

    def test_private_not_injectable_into_public_context(self) -> None:
        entry = _entry(sensitivity=ConversationSensitivity.PRIVATE_LOCAL)
        assert not injectable(entry, ConversationSensitivity.PUBLIC_DEMO)

    def test_public_injectable_into_private_context(self) -> None:
        entry = _entry(sensitivity=ConversationSensitivity.PUBLIC_DEMO)
        assert injectable(entry, ConversationSensitivity.PRIVATE_LOCAL)

    def test_same_sensitivity_injectable(self) -> None:
        entry = _entry(sensitivity=ConversationSensitivity.PRIVATE_LOCAL)
        assert injectable(entry, ConversationSensitivity.PRIVATE_LOCAL)


class TestRecencyFactor:
    def test_fresh_memory_is_one(self) -> None:
        assert recency_factor(_NOW, now=_NOW) == pytest.approx(1.0)

    def test_half_life_halves(self) -> None:
        old = _NOW - timedelta(days=MEMORY_RECENCY_HALF_LIFE_DAYS)
        assert recency_factor(old, now=_NOW) == pytest.approx(0.5)

    def test_double_half_life_quarters(self) -> None:
        old = _NOW - timedelta(days=2 * MEMORY_RECENCY_HALF_LIFE_DAYS)
        assert recency_factor(old, now=_NOW) == pytest.approx(0.25)

    def test_future_updated_at_clamped_to_one(self) -> None:
        future = _NOW + timedelta(days=1)
        assert recency_factor(future, now=_NOW) == pytest.approx(1.0)


class TestScoreMemory:
    def test_combines_similarity_and_recency(self) -> None:
        entry = _entry(embedding=(1.0, 0.0), updated_at=_NOW)
        score = score_memory(entry, (1.0, 0.0), now=_NOW)
        assert score == pytest.approx(1.0 + MEMORY_RECENCY_WEIGHT)

    def test_missing_embedding_contributes_zero_similarity(self) -> None:
        entry = _entry(embedding=None)
        score = score_memory(entry, (1.0, 0.0), now=_NOW)
        assert score == pytest.approx(MEMORY_RECENCY_WEIGHT)


class TestSelectMemories:
    def test_empty_candidates(self) -> None:
        selected = select_memories(
            (),
            (1.0, 0.0),
            sensitivity=ConversationSensitivity.PRIVATE_LOCAL,
            limit=3,
            now=_NOW,
        )
        assert selected == ()

    def test_ranks_by_similarity_descending(self) -> None:
        close = _entry(index=1, embedding=(1.0, 0.0), updated_at=_NOW - timedelta(days=30))
        far = _entry(index=2, embedding=(0.0, 1.0), updated_at=_NOW)
        selected = select_memories(
            (far, close),
            (1.0, 0.0),
            sensitivity=ConversationSensitivity.PRIVATE_LOCAL,
            limit=2,
            now=_NOW,
        )
        assert [entry.content for entry in selected] == [close.content, far.content]

    def test_bounds_to_limit(self) -> None:
        entries = tuple(_entry(index=index, embedding=(1.0, 0.0)) for index in range(12))
        selected = select_memories(
            entries,
            (1.0, 0.0),
            sensitivity=ConversationSensitivity.PRIVATE_LOCAL,
            limit=MEMORY_TOP_K,
            now=_NOW,
        )
        assert len(selected) == MEMORY_TOP_K

    def test_filters_restricted(self) -> None:
        entries = (
            _entry(index=1, sensitivity=ConversationSensitivity.RESTRICTED, embedding=(1.0, 0.0)),
            _entry(index=2, embedding=(1.0, 0.0)),
        )
        selected = select_memories(
            entries,
            (1.0, 0.0),
            sensitivity=ConversationSensitivity.PRIVATE_LOCAL,
            limit=2,
            now=_NOW,
        )
        assert len(selected) == 1
        assert selected[0].content == "fact 2"

    def test_filters_more_restrictive_than_snapshot(self) -> None:
        entries = (
            _entry(
                index=1,
                sensitivity=ConversationSensitivity.PRIVATE_LOCAL,
                embedding=(1.0, 0.0),
            ),
            _entry(
                index=2,
                sensitivity=ConversationSensitivity.PUBLIC_DEMO,
                embedding=(1.0, 0.0),
            ),
        )
        selected = select_memories(
            entries,
            (1.0, 0.0),
            sensitivity=ConversationSensitivity.PUBLIC_DEMO,
            limit=2,
            now=_NOW,
        )
        assert len(selected) == 1
        assert selected[0].sensitivity is ConversationSensitivity.PUBLIC_DEMO

    def test_rejects_nonpositive_limit(self) -> None:
        with pytest.raises(ValueError, match="limit"):
            select_memories(
                (),
                (1.0,),
                sensitivity=ConversationSensitivity.PRIVATE_LOCAL,
                limit=0,
                now=_NOW,
            )
