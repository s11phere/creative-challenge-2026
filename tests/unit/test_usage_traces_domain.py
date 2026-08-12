from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from domain.conversation_context import ConversationSensitivity
from domain.usage_traces import (
    UsageOutcome,
    UsagePatternSnapshot,
    UsageTrace,
    pattern_key,
)

_RUN_ID = UUID(int=1)
_CONVERSATION_ID = UUID(int=2)


def _trace(**overrides: object) -> UsageTrace:
    values: dict[str, object] = {
        "run_id": _RUN_ID,
        "conversation_id": _CONVERSATION_ID,
        "input_summary": "Summarize this document",
        "tools_used": ("knowledge_search", "grounded_answer"),
        "outcome": UsageOutcome.COMPLETED,
        "model": "fake",
        "sensitivity": ConversationSensitivity.PRIVATE_LOCAL,
        "skill_name": "knowledge_agent",
    }
    values.update(overrides)
    return UsageTrace(**values)


class TestUsageTraceValidation:
    def test_valid_skill_trace(self) -> None:
        trace = _trace()
        assert trace.skill_name == "knowledge_agent"
        assert trace.command is None

    def test_valid_command_trace(self) -> None:
        trace = _trace(skill_name=None, command="context_compaction")
        assert trace.skill_name is None
        assert trace.command == "context_compaction"

    @pytest.mark.parametrize(
        ("skill_name", "command"),
        [
            (None, None),
            ("knowledge_agent", "assistant_turn"),
        ],
    )
    def test_requires_exactly_one_identity(
        self, skill_name: str | None, command: str | None
    ) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            _trace(skill_name=skill_name, command=command)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("skill_name", "   "),
            ("skill_name", "x" * 256),
            ("command", "   "),
            ("command", "x" * 65),
            ("input_summary", ""),
            ("input_summary", "x" * 1025),
            ("model", ""),
            ("model", "x" * 256),
        ],
    )
    def test_rejects_invalid_field(self, field: str, value: object) -> None:
        with pytest.raises(ValueError):
            _trace(**{field: value})

    def test_rejects_invalid_tools(self) -> None:
        with pytest.raises(ValueError, match="tools"):
            _trace(tools_used=("x" * 256,))

    def test_rejects_too_many_tools(self) -> None:
        with pytest.raises(ValueError, match="tools"):
            _trace(tools_used=tuple(f"tool-{i}" for i in range(33)))

    def test_rejects_naive_timestamp(self) -> None:
        with pytest.raises(ValueError, match="timezone"):
            _trace(created_at=datetime(2026, 8, 12))


class TestPatternKey:
    def test_skill_key(self) -> None:
        assert (
            pattern_key(
                skill_name="knowledge_agent",
                task_category="question",
                tool_sequence="knowledge_search,grounded_answer",
                input_type="zh",
            )
            == "skill=knowledge_agent|category=question|"
            "tools=knowledge_search,grounded_answer|input=zh"
        )

    def test_none_skill_becomes_none_label(self) -> None:
        key = pattern_key(
            skill_name=None,
            task_category="general",
            tool_sequence="none",
            input_type="en",
        )
        assert key.startswith("skill=none|category=general|")

    def test_same_dimensions_share_key(self) -> None:
        first = pattern_key(skill_name="a", task_category="c", tool_sequence="t", input_type="en")
        second = pattern_key(skill_name="a", task_category="c", tool_sequence="t", input_type="en")
        assert first == second


class TestUsagePatternSnapshot:
    def test_valid_pattern(self) -> None:
        pattern = UsagePatternSnapshot(
            key="skill=knowledge_agent|category=question",
            task_category="question",
            tool_sequence="knowledge_search",
            input_type="zh",
            frequency=3,
            first_seen_at=datetime(2026, 8, 1, tzinfo=UTC),
            last_seen_at=datetime(2026, 8, 12, tzinfo=UTC),
            skill_name="knowledge_agent",
        )
        assert pattern.frequency == 3

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("key", ""),
            ("key", "x" * 513),
            ("task_category", "x" * 65),
            ("tool_sequence", "x" * 1025),
            ("input_type", "x" * 33),
            ("skill_name", "x" * 256),
        ],
    )
    def test_rejects_invalid_field(self, field: str, value: object) -> None:
        base: dict[str, object] = {
            "key": "k",
            "task_category": "question",
            "tool_sequence": "t",
            "input_type": "zh",
            "frequency": 1,
            "first_seen_at": datetime(2026, 8, 1, tzinfo=UTC),
            "last_seen_at": datetime(2026, 8, 2, tzinfo=UTC),
        }
        base[field] = value
        with pytest.raises(ValueError):
            UsagePatternSnapshot(**base)

    def test_rejects_zero_frequency(self) -> None:
        with pytest.raises(ValueError, match="frequency"):
            UsagePatternSnapshot(
                key="k",
                task_category="question",
                tool_sequence="t",
                input_type="zh",
                frequency=0,
                first_seen_at=datetime(2026, 8, 1, tzinfo=UTC),
                last_seen_at=datetime(2026, 8, 2, tzinfo=UTC),
            )

    def test_rejects_reversed_window(self) -> None:
        with pytest.raises(ValueError, match="follow"):
            UsagePatternSnapshot(
                key="k",
                task_category="question",
                tool_sequence="t",
                input_type="zh",
                frequency=1,
                first_seen_at=datetime(2026, 8, 2, tzinfo=UTC),
                last_seen_at=datetime(2026, 8, 1, tzinfo=UTC),
            )
