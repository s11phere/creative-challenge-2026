"""Durable usage-trace capture from finished ConversationRuns.

Phase 2 only records; nothing here injects patterns back into the Agent loop.
Sanitization follows the personalization data gate: traces store a bounded
summary (never full prompts, private bodies, or raw Provider responses).
"""

from __future__ import annotations

import re
from datetime import datetime
from uuid import UUID

from domain.agent_sse import AgentRunEventStore
from domain.conversation_context import (
    ConversationSensitivity,
    most_restrictive_sensitivity,
)
from domain.conversation_run import (
    ConversationRun,
    ConversationRunRepository,
    ConversationRunStatus,
)
from domain.qa_persistence import GroundedQARepository
from domain.usage_traces import UsageOutcome, UsageTrace, UsageTraceRepository

from application.assistant.context import ConversationContextDataPort

_SECRET_PATTERN = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_FINISHED_OUTCOMES: dict[ConversationRunStatus, UsageOutcome] = {
    ConversationRunStatus.COMPLETED: UsageOutcome.COMPLETED,
    ConversationRunStatus.REFUSED: UsageOutcome.REFUSED,
    ConversationRunStatus.WAITING_CLARIFICATION: UsageOutcome.CLARIFIED,
    ConversationRunStatus.FAILED: UsageOutcome.FAILED,
    ConversationRunStatus.CANCELLED: UsageOutcome.FAILED,
    ConversationRunStatus.TIMED_OUT: UsageOutcome.FAILED,
}


def classify_outcome(status: ConversationRunStatus) -> UsageOutcome | None:
    """Map a Run status to a trace outcome; unfinished Runs yield ``None``."""
    return _FINISHED_OUTCOMES.get(status)


def sanitize_input_summary(content: str, *, max_chars: int = 512) -> str:
    """Bounded, secret-redacted input summary that is safe to persist.

    Full user content never reaches the trace store: control characters are
    stripped, long hex tokens (probable secrets) are redacted, whitespace is
    collapsed, and the result is truncated.
    """
    cleaned = "".join(
        character if character.isprintable() or character in "\n\t" else " "
        for character in content
    )
    cleaned = _SECRET_PATTERN.sub("[redacted]", cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned[:max_chars].strip() or "(empty)"


def build_usage_trace(
    run: ConversationRun,
    *,
    input_summary: str,
    tools_used: tuple[str, ...],
    sensitivity: ConversationSensitivity,
    max_summary_chars: int = 512,
) -> UsageTrace:
    """Assemble a validated trace from a finished Run; requires a final outcome."""
    outcome = classify_outcome(run.status)
    if outcome is None:
        raise ValueError("usage trace requires a finished ConversationRun")
    if run.skill is not None:
        skill_name: str | None = run.skill.name
        command: str | None = None
    else:
        skill_name = None
        command = run.run_kind.value
    return UsageTrace(
        run_id=run.run_id,
        conversation_id=run.conversation_id,
        input_summary=sanitize_input_summary(input_summary, max_chars=max_summary_chars),
        tools_used=tools_used,
        outcome=outcome,
        model=run.model_identity,
        sensitivity=sensitivity,
        skill_name=skill_name,
        command=command,
        created_at=run.updated_at,
    )


class UsageTraceService:
    """Persistence facade for callers that already hold a finished trace."""

    def __init__(self, *, repository: UsageTraceRepository) -> None:
        self._repository = repository

    async def record(self, trace: UsageTrace) -> UsageTrace:
        return await self._repository.save(trace)

    async def get(self, run_id: UUID) -> UsageTrace | None:
        return await self._repository.get(run_id)

    async def list(
        self, *, limit: int | None = None, since: datetime | None = None
    ) -> tuple[UsageTrace, ...]:
        return await self._repository.list(limit=limit, since=since)


class UsageTraceRecorder:
    """Assemble and persist one trace when a Run reaches a finished state.

    Recording is best-effort and idempotent: an existing trace for the Run is
    returned unchanged, and unfinished Runs are skipped. The Agent loop core is
    untouched — this hook lives in the Application/Worker layers.
    """

    def __init__(
        self,
        *,
        runs: ConversationRunRepository,
        data: ConversationContextDataPort,
        qa: GroundedQARepository,
        agent_events: AgentRunEventStore,
        traces: UsageTraceRepository,
        max_summary_chars: int = 512,
    ) -> None:
        if max_summary_chars < 64:
            raise ValueError("usage trace summary bound is too small")
        self._runs = runs
        self._data = data
        self._qa = qa
        self._agent_events = agent_events
        self._traces = traces
        self._max_summary_chars = max_summary_chars

    async def record_run(self, run_id: UUID) -> UsageTrace | None:
        run = await self._runs.get_conversation_run(run_id)
        if run is None or classify_outcome(run.status) is None:
            return None
        existing = await self._traces.get(run_id)
        if existing is not None:
            return existing
        message = await self._data.get_message(run.user_message_id)
        tools = await self._collect_tools(run_id)
        sensitivity = await self._derive_sensitivity(run)
        trace = build_usage_trace(
            run,
            input_summary=message.content if message is not None else "",
            tools_used=tools,
            sensitivity=sensitivity,
            max_summary_chars=self._max_summary_chars,
        )
        return await self._traces.save(trace)

    async def _collect_tools(self, run_id: UUID) -> tuple[str, ...]:
        page = await self._agent_events.page(run_id, limit=200)
        seen: list[str] = []
        for event in page.events:
            name = event.payload.get("tool_name")
            if isinstance(name, str) and name and name not in seen:
                seen.append(name)
        return tuple(seen)

    async def _derive_sensitivity(self, run: ConversationRun) -> ConversationSensitivity:
        qa_run = await self._qa.get_run(run.run_id)
        if qa_run is not None and qa_run.context_sensitivity:
            try:
                return ConversationSensitivity(qa_run.context_sensitivity)
            except ValueError:
                pass
        summaries = await self._data.list_conversation_summaries(run.conversation_id)
        if summaries:
            return most_restrictive_sensitivity(tuple(item.sensitivity for item in summaries))
        return ConversationSensitivity.PRIVATE_LOCAL


__all__ = [
    "UsageTraceRecorder",
    "UsageTraceService",
    "build_usage_trace",
    "classify_outcome",
    "sanitize_input_summary",
]
