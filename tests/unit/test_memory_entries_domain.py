from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest
from domain.conversation_context import ConversationSensitivity
from domain.memory_entries import (
    MEMORY_CONTENT_LIMIT,
    MemoryEntry,
    MemoryEntryType,
    cosine_similarity,
    normalize_vector,
)


def _entry(**overrides: object) -> MemoryEntry:
    values: dict[str, object] = dict(
        entry_type=MemoryEntryType.FACT,
        content="The user prefers concise answers.",
        source_conversation_id=None,
        sensitivity=ConversationSensitivity.PRIVATE_LOCAL,
        content_sha256="",
    )
    values.update(overrides)
    return MemoryEntry(**values)


class TestMemoryEntryValidation:
    def test_auto_digests_content(self) -> None:
        entry = _entry(content="stable fact")
        assert entry.content_sha256
        assert len(entry.content_sha256) == 64

    def test_rejects_mismatched_digest(self) -> None:
        with pytest.raises(ValueError, match="digest"):
            _entry(content="stable fact", content_sha256="a" * 64)

    def test_rejects_blank_content(self) -> None:
        with pytest.raises(ValueError, match="content"):
            _entry(content="   ")

    def test_rejects_oversized_content(self) -> None:
        with pytest.raises(ValueError, match="content"):
            _entry(content="x" * (MEMORY_CONTENT_LIMIT + 1))

    def test_rejects_unknown_entry_type(self) -> None:
        with pytest.raises(ValueError, match="entry type"):
            _entry(entry_type="bogus")  # type: ignore[arg-type]

    def test_rejects_unknown_sensitivity(self) -> None:
        with pytest.raises(ValueError, match="sensitivity"):
            _entry(sensitivity="bogus")  # type: ignore[arg-type]

    def test_rejects_unknown_source_kind(self) -> None:
        with pytest.raises(ValueError, match="source kind"):
            _entry(source_kind="query")

    def test_rejects_zero_frequency(self) -> None:
        with pytest.raises(ValueError, match="frequency"):
            _entry(frequency=0)

    def test_rejects_naive_first_seen(self) -> None:
        with pytest.raises(ValueError, match="timezone"):
            _entry(first_seen_at=datetime(2026, 8, 1))

    def test_rejects_updated_before_first_seen(self) -> None:
        with pytest.raises(ValueError, match="follow"):
            _entry(
                first_seen_at=datetime(2026, 8, 2, tzinfo=UTC),
                updated_at=datetime(2026, 8, 1, tzinfo=UTC),
            )

    def test_accepts_bounded_embedding(self) -> None:
        entry = _entry(embedding=(0.5, 0.5, -0.5))
        assert entry.embedding == (0.5, 0.5, -0.5)

    def test_rejects_nonfinite_embedding(self) -> None:
        with pytest.raises(ValueError, match="embedding"):
            _entry(embedding=(float("nan"), 0.0))

    def test_accepts_expiration(self) -> None:
        entry = _entry(expires_at=datetime(2026, 9, 1, tzinfo=UTC))
        assert entry.expires_at is not None

    def test_accepts_pattern_source_kind(self) -> None:
        entry = _entry(entry_type=MemoryEntryType.PATTERN, source_kind="pattern")
        assert entry.source_kind == "pattern"


class TestVectorHelpers:
    def test_normalize_unit_vector(self) -> None:
        vector = normalize_vector((3.0, 4.0))
        assert math.isclose(vector[0], 0.6)
        assert math.isclose(vector[1], 0.8)

    def test_normalize_empty(self) -> None:
        assert normalize_vector(()) == ()

    def test_normalize_zero_vector(self) -> None:
        assert normalize_vector((0.0, 0.0)) == (0.0, 0.0)

    def test_cosine_identical_is_one(self) -> None:
        assert cosine_similarity((1.0, 0.0), (1.0, 0.0)) == pytest.approx(1.0)

    def test_cosine_opposite_is_minus_one(self) -> None:
        assert cosine_similarity((1.0, 0.0), (-1.0, 0.0)) == pytest.approx(-1.0)

    def test_cosine_orthogonal_is_zero(self) -> None:
        assert cosine_similarity((1.0, 0.0), (0.0, 1.0)) == pytest.approx(0.0)

    def test_cosine_discards_dimension_mismatch(self) -> None:
        assert cosine_similarity((1.0,), (1.0, 0.0)) == 0.0

    def test_cosine_none_side_is_zero(self) -> None:
        assert cosine_similarity(None, (1.0, 0.0)) == 0.0
        assert cosine_similarity((1.0, 0.0), None) == 0.0
