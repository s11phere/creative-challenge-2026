"""PostgreSQL persistence for generic Agent Loop v3 event history."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from domain.agent_sse import (
    AgentRunEventConflictError,
    AgentRunEventPage,
    AgentRunEventType,
    AgentRunStreamEvent,
)
from domain.grounded_qa import QAContractError
from sqlalchemy import desc, select

from .database import Database
from .orm import AgentRunEventModel, ConversationRunModel


class PostgresAgentRunEventStore:
    """Persist idempotent, redacted v3 events under the parent Run lock."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def append(
        self,
        run_id: UUID,
        event_type: AgentRunEventType,
        payload: Mapping[str, Any],
        *,
        event_key: str,
        schema_version: str = "agent-run-sse-v3",
    ) -> AgentRunStreamEvent:
        # Validate before the transaction so an unsafe payload never reaches the ORM.
        candidate = AgentRunStreamEvent(
            run_id=run_id,
            sequence=1,
            event_type=event_type,
            payload=dict(payload),
            event_key=event_key,
            schema_version=schema_version,
        )
        async with self._database.transaction() as session:
            run = await session.get(ConversationRunModel, run_id, with_for_update=True)
            if run is None:
                raise QAContractError("Agent Run event ConversationRun does not exist")
            existing = (
                await session.execute(
                    select(AgentRunEventModel).where(
                        AgentRunEventModel.run_id == run_id,
                        AgentRunEventModel.event_key == event_key,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                stored = _event(existing)
                if stored.event_type is event_type and stored.payload == candidate.payload:
                    return stored
                raise AgentRunEventConflictError("Agent Run event key conflicts with history")
            latest = (
                await session.execute(
                    select(AgentRunEventModel)
                    .where(AgentRunEventModel.run_id == run_id)
                    .order_by(desc(AgentRunEventModel.sequence))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if latest is not None and _event(latest).terminal:
                raise AgentRunEventConflictError("Agent Run already has a terminal event")
            event = AgentRunStreamEvent(
                run_id=run_id,
                sequence=1 if latest is None else latest.sequence + 1,
                event_type=event_type,
                payload=dict(payload),
                event_key=event_key,
                schema_version=schema_version,
            )
            session.add(
                AgentRunEventModel(
                    id=event.event_id,
                    run_id=run_id,
                    sequence=event.sequence,
                    event_key=event.event_key,
                    schema_version=event.schema_version,
                    event_type=event.event_type.value,
                    payload=dict(event.payload),
                    created_at=event.occurred_at,
                )
            )
            return event

    async def page(
        self, run_id: UUID, *, after_sequence: int = 0, limit: int = 100
    ) -> AgentRunEventPage:
        if after_sequence < 0 or not 1 <= limit <= 200:
            raise ValueError("Agent Run event page is invalid")
        async with self._database.session() as session:
            models = list(
                (
                    await session.execute(
                        select(AgentRunEventModel)
                        .where(
                            AgentRunEventModel.run_id == run_id,
                            AgentRunEventModel.sequence > after_sequence,
                        )
                        .order_by(AgentRunEventModel.sequence)
                        .limit(limit + 1)
                    )
                ).scalars()
            )
        selected = tuple(_event(model) for model in models[:limit])
        return AgentRunEventPage(
            events=selected,
            next_sequence=selected[-1].sequence if selected else None,
            has_more=len(models) > len(selected),
        )


def _event(model: AgentRunEventModel) -> AgentRunStreamEvent:
    return AgentRunStreamEvent(
        event_id=model.id,
        run_id=model.run_id,
        sequence=model.sequence,
        event_key=model.event_key,
        schema_version=model.schema_version,
        event_type=AgentRunEventType(model.event_type),
        payload=model.payload,
        occurred_at=model.created_at,
    )


__all__ = ["PostgresAgentRunEventStore"]
