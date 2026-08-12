from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from application.usage_traces import (
    UsagePatternDistiller,
    UsagePatternService,
    classify_input_type,
    classify_task_category,
    tool_sequence,
)
from domain.conversation_context import ConversationSensitivity
from domain.usage_traces import (
    UsageOutcome,
    UsagePatternSnapshot,
    UsageTrace,
)

_RUN_BASE = UUID(int=1_000)


class InMemoryUsageTraceRepository:
    def __init__(self) -> None:
        self._traces: dict[UUID, UsageTrace] = {}

    async def save(self, trace: UsageTrace) -> UsageTrace:
        self._traces[trace.run_id] = trace
        return trace

    async def get(self, run_id: UUID) -> UsageTrace | None:
        return self._traces.get(run_id)

    async def list(
        self, *, limit: int | None = None, since: datetime | None = None
    ) -> tuple[UsageTrace, ...]:
        values = (
            trace for trace in self._traces.values() if since is None or trace.created_at >= since
        )
        selected = tuple(values)
        return selected[:limit] if limit is not None else selected


class InMemoryUsagePatternRepository:
    def __init__(self) -> None:
        self._patterns: dict[str, UsagePatternSnapshot] = {}

    async def replace_all(self, patterns: tuple[UsagePatternSnapshot, ...]) -> None:
        self._patterns = {pattern.key: pattern for pattern in patterns}

    async def list(self, *, limit: int | None = None) -> tuple[UsagePatternSnapshot, ...]:
        selected = tuple(self._patterns.values())
        return selected[:limit] if limit is not None else selected


def _trace(
    *,
    index: int,
    input_summary: str,
    tools: tuple[str, ...] = (),
    skill_name: str | None = None,
    command: str | None = None,
    outcome: UsageOutcome = UsageOutcome.COMPLETED,
) -> UsageTrace:
    return UsageTrace(
        run_id=UUID(int=_RUN_BASE.int + index),
        conversation_id=UUID(int=_RUN_BASE.int + index),
        input_summary=input_summary,
        tools_used=tools,
        outcome=outcome,
        model="fake",
        sensitivity=ConversationSensitivity.PRIVATE_LOCAL,
        skill_name=skill_name,
        command="assistant_turn" if skill_name is None and command is None else command,
        created_at=datetime(2026, 8, 1 + index, tzinfo=UTC),
    )


class TestClassifyInputType:
    @pytest.mark.parametrize(
        ("summary", "expected"),
        [
            ("请总结这篇文档的重点", "zh"),
            ("Summarize the key points", "en"),
            ("def add(a, b): return a + b", "code"),
            ("中英 mixed text", "mixed"),
            ("1234 ...", "other"),
        ],
    )
    def test_classification(self, summary: str, expected: str) -> None:
        assert classify_input_type(summary) == expected


class TestClassifyTaskCategory:
    @pytest.mark.parametrize(
        ("summary", "skill", "expected"),
        [
            ("请总结这篇文档的重点", None, "summarize"),
            ("research the latest papers", None, "research"),
            ("帮我调试这段代码", None, "code"),
            ("写一份产品需求文档", None, "write"),
            ("把这段话翻译成英文", None, "translate"),
            ("who is the author?", None, "question"),
            ("random unrelated words", None, "general"),
            ("anything at all", "summarize_document", "summarize"),
        ],
    )
    def test_classification(self, summary: str, skill: str | None, expected: str) -> None:
        assert classify_task_category(summary, skill_name=skill) == expected


class TestToolSequence:
    def test_joins_tools(self) -> None:
        assert tool_sequence(("knowledge_search", "grounded_answer")) == (
            "knowledge_search,grounded_answer"
        )

    def test_empty_is_none(self) -> None:
        assert tool_sequence(()) == "none"


class TestUsagePatternDistiller:
    def test_groups_frequency_and_window(self) -> None:
        traces = (
            _trace(
                index=1,
                input_summary="请总结这篇文档",
                tools=("knowledge_search",),
                skill_name="summarize_document",
            ),
            _trace(
                index=2,
                input_summary="请总结另一篇文档",
                tools=("knowledge_search",),
                skill_name="summarize_document",
            ),
            _trace(index=3, input_summary="what is the plan?", command="assistant_turn"),
        )
        patterns = UsagePatternDistiller().distill(traces)

        assert len(patterns) == 2
        summarize = next(
            pattern for pattern in patterns if pattern.skill_name == "summarize_document"
        )
        assert summarize.frequency == 2
        assert summarize.task_category == "summarize"
        assert summarize.input_type == "zh"
        assert summarize.first_seen_at == datetime(2026, 8, 2, tzinfo=UTC)
        assert summarize.last_seen_at == datetime(2026, 8, 3, tzinfo=UTC)
        assistant = next(pattern for pattern in patterns if pattern.skill_name is None)
        assert assistant.frequency == 1
        assert assistant.tool_sequence == "none"

    def test_sorts_by_frequency_descending(self) -> None:
        traces = (
            _trace(
                index=1, input_summary="summarize doc", tools=("a",), skill_name="knowledge_agent"
            ),
            _trace(
                index=2,
                input_summary="summarize doc two",
                tools=("a",),
                skill_name="knowledge_agent",
            ),
            _trace(
                index=3,
                input_summary="another summarize",
                tools=("a",),
                skill_name="knowledge_agent",
            ),
            _trace(index=4, input_summary="one off", tools=("b",), skill_name="knowledge_agent"),
        )
        patterns = UsagePatternDistiller().distill(traces)

        assert patterns[0].frequency == 3
        assert patterns[1].frequency == 1

    def test_empty_input_produces_no_patterns(self) -> None:
        assert UsagePatternDistiller().distill(()) == ()


class TestUsagePatternService:
    async def test_distill_all_replaces_snapshot(self) -> None:
        traces = InMemoryUsageTraceRepository()
        patterns = InMemoryUsagePatternRepository()
        for index in range(3):
            await traces.save(
                _trace(index=index + 1, input_summary="summarize the document", tools=("a",))
            )
        service = UsagePatternService(traces=traces, patterns=patterns)

        first = await service.distill_all()
        second = await service.distill_all()

        assert len(first) == 1
        assert first[0].frequency == 3
        assert len(await patterns.list()) == 1
        assert second == first

    async def test_list_patterns_returns_persisted(self) -> None:
        traces = InMemoryUsageTraceRepository()
        patterns = InMemoryUsagePatternRepository()
        await traces.save(_trace(index=1, input_summary="summarize the document", tools=("a",)))
        service = UsagePatternService(traces=traces, patterns=patterns)

        await service.distill_all()
        listed = await service.list_patterns()

        assert len(listed) == 1
        assert listed[0].frequency == 1
