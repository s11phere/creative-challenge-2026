"""SkillSuggestionService tests: frequency threshold, dedup, category filtering."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from application.skills import SkillSuggestionService
from domain.usage_traces import UsagePatternSnapshot, pattern_key


class InMemoryUsagePatternRepository:
    def __init__(self, patterns: tuple[UsagePatternSnapshot, ...] = ()) -> None:
        self._patterns = list(patterns)

    async def replace_all(self, patterns: tuple[UsagePatternSnapshot, ...]) -> None:
        self._patterns = list(patterns)

    async def list(self, *, limit: int | None = None) -> tuple[UsagePatternSnapshot, ...]:
        return tuple(self._patterns[:limit])


def _pattern(
    *,
    category: str,
    frequency: int,
    skill_name: str | None = None,
    index: int = 0,
) -> UsagePatternSnapshot:
    day = 1 + index
    return UsagePatternSnapshot(
        key=pattern_key(
            skill_name=skill_name,
            task_category=category,
            tool_sequence="none",
            input_type="zh",
        ),
        skill_name=skill_name,
        task_category=category,
        tool_sequence="none",
        input_type="zh",
        frequency=frequency,
        first_seen_at=datetime(2026, 8, 1, tzinfo=UTC),
        last_seen_at=datetime(2026, 8, day, tzinfo=UTC),
    )


@pytest.fixture
def service() -> SkillSuggestionService:
    return SkillSuggestionService(patterns=InMemoryUsagePatternRepository(), min_frequency=3)


class TestSkillSuggestions:
    async def test_suggests_frequent_unconsolidated_patterns(self, service) -> None:
        service._patterns._patterns = [
            _pattern(category="summarize", frequency=6),
            _pattern(category="research", frequency=2),  # below threshold
        ]
        suggestions = await service.suggest()
        assert [item.category for item in suggestions] == ["summarize"]
        assert suggestions[0].frequency == 6
        assert suggestions[0].name == "summarize_workflow"

    async def test_ignores_general_and_skill_bound_patterns(self, service) -> None:
        service._patterns._patterns = [
            _pattern(category="general", frequency=9),
            _pattern(category="code", frequency=5, skill_name="summarize_document"),
        ]
        assert await service.suggest() == ()

    async def test_skips_existing_names(self, service) -> None:
        service._patterns._patterns = [
            _pattern(category="summarize", frequency=7),
        ]
        suggestions = await service.suggest(existing_names=frozenset({"summarize_workflow"}))
        assert suggestions == ()

    async def test_orders_by_frequency_and_caps(self, service) -> None:
        service._patterns._patterns = [
            _pattern(category="translate", frequency=4, index=1),
            _pattern(category="code", frequency=9, index=2),
            _pattern(category="write", frequency=5, index=3),
        ]
        service._max_suggestions = 2
        suggestions = await service.suggest()
        assert [item.category for item in suggestions] == ["code", "write"]

    def test_invalid_configuration(self) -> None:
        with pytest.raises(ValueError):
            SkillSuggestionService(patterns=InMemoryUsagePatternRepository(), min_frequency=0)
        with pytest.raises(ValueError):
            SkillSuggestionService(patterns=InMemoryUsagePatternRepository(), max_suggestions=0)
