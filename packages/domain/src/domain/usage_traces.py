"""Pure contracts for durable personal usage traces and distilled patterns.

Phase 2 (personalization) records one structured trace per finished
``ConversationRun`` and periodically distills deterministic pattern aggregates.
The data gate from the roadmap applies here: traces never store full prompts,
private bodies, or raw Provider responses — only bounded, sanitized summaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from .conversation_context import ConversationSensitivity

INPUT_SUMMARY_LIMIT = 1_024
_TOOLS_LIMIT = 32


class UsageOutcome(StrEnum):
    """One deterministic outcome projection for a finished user turn."""

    COMPLETED = "completed"
    FAILED = "failed"
    REFUSED = "refused"
    CLARIFIED = "clarified"


@dataclass(frozen=True)
class UsageTrace:
    """One sanitized usage trace derived from a finished ``ConversationRun``.

    Exactly one of ``skill_name`` / ``command`` is set: Skill Runs identify the
    activated Skill; every other Run carries its ``run_kind`` as the command
    (e.g. ``assistant_turn``, ``context_compaction``).
    """

    run_id: UUID
    conversation_id: UUID
    input_summary: str
    tools_used: tuple[str, ...]
    outcome: UsageOutcome
    model: str
    sensitivity: ConversationSensitivity
    skill_name: str | None = None
    command: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if (self.skill_name is None) == (self.command is None):
            raise ValueError("usage trace requires exactly one of skill name or command")
        if self.skill_name is not None and (
            not self.skill_name.strip() or len(self.skill_name) > 255
        ):
            raise ValueError("usage trace skill name is invalid")
        if self.command is not None and (not self.command.strip() or len(self.command) > 64):
            raise ValueError("usage trace command is invalid")
        if not self.input_summary.strip() or len(self.input_summary) > INPUT_SUMMARY_LIMIT:
            raise ValueError("usage trace input summary must be bounded and non-empty")
        if len(self.tools_used) > _TOOLS_LIMIT or any(
            not name.strip() or len(name) > 255 for name in self.tools_used
        ):
            raise ValueError("usage trace tools are invalid")
        if not self.model.strip() or len(self.model) > 255:
            raise ValueError("usage trace model identity is required")
        if self.created_at.tzinfo is None:
            raise ValueError("usage trace timestamp must be timezone-aware")


class UsageTraceRepository(Protocol):
    """Idempotent persistence port for one trace per Run identity."""

    async def save(self, trace: UsageTrace) -> UsageTrace: ...

    async def get(self, run_id: UUID) -> UsageTrace | None: ...

    async def list(
        self, *, limit: int | None = None, since: datetime | None = None
    ) -> tuple[UsageTrace, ...]: ...


@dataclass(frozen=True)
class UsagePatternSnapshot:
    """One distilled aggregate group across (skill, category, tools, input type)."""

    key: str
    task_category: str
    tool_sequence: str
    input_type: str
    frequency: int
    first_seen_at: datetime
    last_seen_at: datetime
    skill_name: str | None = None

    def __post_init__(self) -> None:
        if not self.key.strip() or len(self.key) > 512:
            raise ValueError("usage pattern key must be bounded and non-empty")
        if self.task_category and len(self.task_category) > 64:
            raise ValueError("usage pattern task category is too long")
        if len(self.tool_sequence) > 1_024:
            raise ValueError("usage pattern tool sequence is too long")
        if self.input_type and len(self.input_type) > 32:
            raise ValueError("usage pattern input type is too long")
        if self.skill_name is not None and len(self.skill_name) > 255:
            raise ValueError("usage pattern skill name is too long")
        if self.frequency < 1:
            raise ValueError("usage pattern frequency must be positive")
        if self.first_seen_at.tzinfo is None or self.last_seen_at.tzinfo is None:
            raise ValueError("usage pattern timestamps must be timezone-aware")
        if self.last_seen_at < self.first_seen_at:
            raise ValueError("usage pattern last_seen must follow first_seen")


def pattern_key(
    *,
    skill_name: str | None,
    task_category: str,
    tool_sequence: str,
    input_type: str,
) -> str:
    """Build the canonical, readable key that groups one usage pattern."""
    skill = skill_name or "none"
    return f"skill={skill}|category={task_category}|tools={tool_sequence}|input={input_type}"


class UsagePatternRepository(Protocol):
    """Replace-all persistence for the current aggregate snapshot."""

    async def replace_all(self, patterns: tuple[UsagePatternSnapshot, ...]) -> None: ...

    async def list(self, *, limit: int | None = None) -> tuple[UsagePatternSnapshot, ...]: ...


__all__ = [
    "INPUT_SUMMARY_LIMIT",
    "UsageOutcome",
    "UsagePatternRepository",
    "UsagePatternSnapshot",
    "UsageTrace",
    "UsageTraceRepository",
    "pattern_key",
]
