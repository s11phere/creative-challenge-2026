"""Deterministic distillation of raw usage traces into pattern aggregates.

Phase 2 only produces these aggregates; nothing consumes them yet. The
classifiers below are deliberately keyword/script heuristics (no LLM) so every
run of the distillation is reproducible and unit-testable.
"""

from __future__ import annotations

import re
from collections import defaultdict

from domain.usage_traces import (
    UsagePatternRepository,
    UsagePatternSnapshot,
    UsageTrace,
    UsageTraceRepository,
    pattern_key,
)

_CJK_CHAR = re.compile(r"[㐀-䶿一-鿿぀-ヿ가-힯]")
_CODE_MARKERS = ("```", "def ", "import ", "return ", "function", "=>")
_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("summarize", ("summar", "总结", "摘要", "概括", "要点", "归纳", "纪要")),
    ("research", ("research", "调查", "研究", "调研", "文献", "literature")),
    ("code", ("代码", "编写", "函数", "报错", "debug", "python", "脚本", "bug", "function")),
    ("write", ("draft", "write a", "撰写", "起草", "写一")),
    ("translate", ("translate", "翻译")),
    ("question", ("?", "？", "what", "why", "how", "什么", "怎么", "为什么", "是否", "能否")),
)


def classify_input_type(summary: str) -> str:
    """Classify the dominant input script: code / zh / en / mixed / other."""
    if any(marker in summary for marker in _CODE_MARKERS):
        return "code"
    cjk = len(_CJK_CHAR.findall(summary))
    latin = sum(character.isalpha() and ord(character) < 128 for character in summary)
    if cjk and latin == 0:
        return "zh"
    if latin and cjk == 0:
        return "en"
    if cjk and latin:
        return "mixed"
    return "other"


def classify_task_category(summary: str, *, skill_name: str | None) -> str:
    """One deterministic intent bucket from a bounded input summary."""
    if skill_name == "summarize_document":
        return "summarize"
    normalized = summary.casefold()
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(keyword.casefold() in normalized for keyword in keywords):
            return category
    return "general"


def tool_sequence(tools: tuple[str, ...]) -> str:
    return ",".join(tools) if tools else "none"


class UsagePatternDistiller:
    """Group raw traces along the (skill, category, tools, input type) dimensions."""

    def distill(self, traces: tuple[UsageTrace, ...]) -> tuple[UsagePatternSnapshot, ...]:
        groups: dict[str, list[UsageTrace]] = defaultdict(list)
        for trace in traces:
            category = classify_task_category(trace.input_summary, skill_name=trace.skill_name)
            input_type = classify_input_type(trace.input_summary)
            key = pattern_key(
                skill_name=trace.skill_name,
                task_category=category,
                tool_sequence=tool_sequence(trace.tools_used),
                input_type=input_type,
            )
            groups[key].append(trace)
        patterns: list[UsagePatternSnapshot] = []
        for key, members in groups.items():
            category = classify_task_category(
                members[0].input_summary, skill_name=members[0].skill_name
            )
            patterns.append(
                UsagePatternSnapshot(
                    key=key,
                    skill_name=members[0].skill_name,
                    task_category=category,
                    tool_sequence=tool_sequence(members[0].tools_used),
                    input_type=classify_input_type(members[0].input_summary),
                    frequency=len(members),
                    first_seen_at=min(trace.created_at for trace in members),
                    last_seen_at=max(trace.created_at for trace in members),
                )
            )
        return tuple(sorted(patterns, key=lambda pattern: (-pattern.frequency, pattern.key)))


class UsagePatternService:
    """Recompute the full aggregate snapshot from raw traces and replace the persisted one."""

    def __init__(
        self,
        *,
        traces: UsageTraceRepository,
        patterns: UsagePatternRepository,
        distiller: UsagePatternDistiller | None = None,
    ) -> None:
        self._traces = traces
        self._patterns = patterns
        self._distiller = distiller or UsagePatternDistiller()

    async def distill_all(self) -> tuple[UsagePatternSnapshot, ...]:
        snapshot = self._distiller.distill(await self._traces.list())
        await self._patterns.replace_all(snapshot)
        return snapshot

    async def list_patterns(self, *, limit: int | None = None) -> tuple[UsagePatternSnapshot, ...]:
        return await self._patterns.list(limit=limit)


__all__ = [
    "UsagePatternDistiller",
    "UsagePatternService",
    "classify_input_type",
    "classify_task_category",
    "tool_sequence",
]
