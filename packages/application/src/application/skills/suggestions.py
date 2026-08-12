"""Lightweight personal-Skill suggestions from distilled usage patterns.

Phase 4's prelude to Phase 6 (auto extraction): when a bounded usage pattern is
repeated often enough and no Skill already covers it, surface a "固化这个模式为
个人 skill?" candidate. Suggestions are deterministic, frequency-thresholded, and
always require explicit human confirmation before any draft is created.
"""

from __future__ import annotations

from dataclasses import dataclass

from domain.usage_traces import UsagePatternRepository

_IGNORED_CATEGORIES = frozenset({"general"})


@dataclass(frozen=True)
class SkillSuggestionView:
    """Body-free candidate Skill suggestion for the Web projection."""

    name: str
    category: str
    frequency: int
    last_seen_at: str
    description: str
    hint: str


class SkillSuggestionService:
    """Surface repeatable task patterns as human-confirmed Skill candidates."""

    def __init__(
        self,
        *,
        patterns: UsagePatternRepository,
        min_frequency: int = 3,
        max_suggestions: int = 5,
    ) -> None:
        if min_frequency < 1:
            raise ValueError("Skill suggestion frequency threshold must be positive")
        if max_suggestions < 1:
            raise ValueError("Skill suggestion cap must be positive")
        self._patterns = patterns
        self._min_frequency = min_frequency
        self._max_suggestions = max_suggestions

    async def suggest(
        self, *, existing_names: frozenset[str] = frozenset()
    ) -> tuple[SkillSuggestionView, ...]:
        """Return candidates whose category is frequent and not yet covered."""
        patterns = await self._patterns.list()
        candidates: list[SkillSuggestionView] = []
        for pattern in patterns:
            # A pattern already bound to a Skill is already consolidated; only
            # plain assistant-turn patterns are worth solidifying.
            if pattern.skill_name is not None:
                continue
            if pattern.task_category in _IGNORED_CATEGORIES:
                continue
            if pattern.frequency < self._min_frequency:
                continue
            name = _category_slug(pattern.task_category)
            if name in existing_names:
                continue
            candidates.append(
                SkillSuggestionView(
                    name=name,
                    category=pattern.task_category,
                    frequency=pattern.frequency,
                    last_seen_at=pattern.last_seen_at.isoformat(),
                    description=_description(pattern.task_category, pattern.frequency),
                    hint=f"固化「{pattern.task_category}」模式为一个个人 Skill，"
                    "点击后进入 creator 引导创建流程。",
                )
            )
        return tuple(
            sorted(candidates, key=lambda item: (-item.frequency, item.name))[
                : self._max_suggestions
            ]
        )


def _category_slug(category: str) -> str:
    return f"{category}_workflow"


def _description(category: str, frequency: int) -> str:
    return f"你最近常做「{category}」类任务（{frequency} 次），可以固化成个人 Skill。"


__all__ = ["SkillSuggestionService", "SkillSuggestionView"]
