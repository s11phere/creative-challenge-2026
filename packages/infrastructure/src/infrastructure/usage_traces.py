"""PostgreSQL adapters for usage traces and distilled pattern aggregates."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from domain.conversation_context import ConversationSensitivity
from domain.usage_traces import UsageOutcome, UsagePatternSnapshot, UsageTrace
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .database import Database
from .orm import UsagePatternModel, UsageTraceModel


class PostgresUsageTraceRepository:
    """Idempotent append-only persistence for one trace per Run identity."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def save(self, trace: UsageTrace) -> UsageTrace:
        async with self._database.transaction() as session:
            statement = pg_insert(UsageTraceModel).values(_trace_values(trace))
            statement = statement.on_conflict_do_nothing(index_elements=[UsageTraceModel.run_id])
            await session.execute(statement)
        return trace

    async def get(self, run_id: UUID) -> UsageTrace | None:
        async with self._database.session() as session:
            model = await session.get(UsageTraceModel, run_id)
            return _trace(model) if model is not None else None

    async def list(
        self, *, limit: int | None = None, since: datetime | None = None
    ) -> tuple[UsageTrace, ...]:
        if limit is not None and limit < 1:
            raise ValueError("usage trace page limit must be positive")
        async with self._database.session() as session:
            statement = select(UsageTraceModel).order_by(
                UsageTraceModel.created_at, UsageTraceModel.run_id
            )
            if since is not None:
                statement = statement.where(UsageTraceModel.created_at >= since)
            if limit is not None:
                statement = statement.limit(limit)
            models = (await session.execute(statement)).scalars()
            return tuple(_trace(model) for model in models)


class PostgresUsagePatternRepository:
    """Replace-all snapshot persistence for distilled usage patterns."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def replace_all(self, patterns: tuple[UsagePatternSnapshot, ...]) -> None:
        async with self._database.transaction() as session:
            await session.execute(delete(UsagePatternModel))
            session.add_all(_pattern_model(pattern) for pattern in patterns)
            await session.flush()

    async def list(self, *, limit: int | None = None) -> tuple[UsagePatternSnapshot, ...]:
        if limit is not None and limit < 1:
            raise ValueError("usage pattern page limit must be positive")
        async with self._database.session() as session:
            statement = select(UsagePatternModel).order_by(
                UsagePatternModel.frequency.desc(), UsagePatternModel.key
            )
            if limit is not None:
                statement = statement.limit(limit)
            models = (await session.execute(statement)).scalars()
            return tuple(_pattern(model) for model in models)


def _trace_values(trace: UsageTrace) -> dict[str, Any]:
    return {
        "run_id": trace.run_id,
        "conversation_id": trace.conversation_id,
        "skill_name": trace.skill_name,
        "command": trace.command,
        "input_summary": trace.input_summary,
        "tools_used": list(trace.tools_used),
        "outcome": trace.outcome.value,
        "model": trace.model,
        "sensitivity": trace.sensitivity.value,
        "created_at": trace.created_at,
    }


def _trace(model: UsageTraceModel) -> UsageTrace:
    return UsageTrace(
        run_id=model.run_id,
        conversation_id=model.conversation_id,
        input_summary=model.input_summary,
        tools_used=tuple(model.tools_used),
        outcome=UsageOutcome(model.outcome),
        model=model.model,
        sensitivity=ConversationSensitivity(model.sensitivity),
        skill_name=model.skill_name,
        command=model.command,
        created_at=model.created_at,
    )


def _pattern_model(pattern: UsagePatternSnapshot) -> UsagePatternModel:
    return UsagePatternModel(
        key=pattern.key,
        skill_name=pattern.skill_name,
        task_category=pattern.task_category,
        tool_sequence=pattern.tool_sequence,
        input_type=pattern.input_type,
        frequency=pattern.frequency,
        first_seen_at=pattern.first_seen_at,
        last_seen_at=pattern.last_seen_at,
    )


def _pattern(model: UsagePatternModel) -> UsagePatternSnapshot:
    return UsagePatternSnapshot(
        key=model.key,
        skill_name=model.skill_name,
        task_category=model.task_category,
        tool_sequence=model.tool_sequence,
        input_type=model.input_type,
        frequency=model.frequency,
        first_seen_at=model.first_seen_at,
        last_seen_at=model.last_seen_at,
    )


__all__ = ["PostgresUsagePatternRepository", "PostgresUsageTraceRepository"]
