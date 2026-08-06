"""PostgreSQL event store for the versioned Assistant Run SSE contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from domain.assistant_sse import AssistantEventType, AssistantStreamEvent
from domain.grounded_qa import QAContractError
from sqlalchemy import desc, select

from .database import Database
from .orm import AssistantEventModel, ConversationRunModel


class PostgresAssistantEventStore:
    """Persist monotonic, content-free Assistant events under the parent Run lock."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def append(
        self, run_id: UUID, event_type: AssistantEventType, payload: Mapping[str, Any]
    ) -> AssistantStreamEvent:
        async with self._database.transaction() as session:
            run = await session.get(ConversationRunModel, run_id, with_for_update=True)
            if run is None:
                raise QAContractError("Assistant event ConversationRun does not exist")
            latest = (
                await session.execute(
                    select(AssistantEventModel)
                    .where(AssistantEventModel.run_id == run_id)
                    .order_by(desc(AssistantEventModel.sequence))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if latest is not None:
                latest_event = _event(latest)
                if latest_event.terminal:
                    return latest_event
                if latest_event.event_type is event_type and latest_event.payload == payload:
                    return latest_event
            event = AssistantStreamEvent(
                run_id=run_id,
                sequence=1 if latest is None else latest.sequence + 1,
                event_type=event_type,
                payload=dict(payload),
            )
            session.add(
                AssistantEventModel(
                    id=event.event_id,
                    run_id=run_id,
                    sequence=event.sequence,
                    event_type=event.event_type.value,
                    payload=dict(event.payload),
                    created_at=event.occurred_at,
                )
            )
            return event

    async def replay(
        self, run_id: UUID, after_sequence: int = 0
    ) -> tuple[AssistantStreamEvent, ...]:
        async with self._database.session() as session:
            models = (
                await session.execute(
                    select(AssistantEventModel)
                    .where(
                        AssistantEventModel.run_id == run_id,
                        AssistantEventModel.sequence > after_sequence,
                    )
                    .order_by(AssistantEventModel.sequence)
                )
            ).scalars()
            return tuple(_event(model) for model in models)


def _event(model: AssistantEventModel) -> AssistantStreamEvent:
    return AssistantStreamEvent(
        event_id=model.id,
        run_id=model.run_id,
        sequence=model.sequence,
        event_type=AssistantEventType(model.event_type),
        payload=model.payload,
        occurred_at=model.created_at,
    )


__all__ = ["PostgresAssistantEventStore"]
