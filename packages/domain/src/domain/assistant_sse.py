"""Versioned, privacy-safe SSE contracts for product-level Assistant runs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid4


class AssistantEventType(StrEnum):
    ACCEPTED = "accepted"
    ROUTING = "routing"
    CLARIFICATION = "clarification"
    SKILL_STARTED = "skill_started"
    PHASE = "phase"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


ASSISTANT_TERMINAL_EVENT_TYPES = frozenset(
    {AssistantEventType.COMPLETED, AssistantEventType.FAILED, AssistantEventType.CANCELLED}
)
_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {"question", "answer", "content", "excerpt", "prompt", "model_output", "raw_response"}
)


@dataclass(frozen=True)
class AssistantStreamEvent:
    run_id: UUID
    sequence: int
    event_type: AssistantEventType
    payload: Mapping[str, Any]
    event_id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = "agent-run-sse-v2"

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ValueError("Assistant SSE sequence must be positive")
        if self.occurred_at.tzinfo is None:
            raise ValueError("Assistant SSE timestamp must be timezone-aware")
        if self.event_type in ASSISTANT_TERMINAL_EVENT_TYPES and "status" not in self.payload:
            raise ValueError("terminal Assistant SSE events require a safe status")
        if _contains_forbidden_key(self.payload):
            raise ValueError("Assistant SSE payload contains private content")

    @property
    def terminal(self) -> bool:
        return self.event_type in ASSISTANT_TERMINAL_EVENT_TYPES

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": str(self.event_id),
            "run_id": str(self.run_id),
            "sequence": self.sequence,
            "occurred_at": self.occurred_at.isoformat(),
            "type": self.event_type.value,
            "payload": dict(self.payload),
        }


class AssistantEventStore(Protocol):
    async def append(
        self, run_id: UUID, event_type: AssistantEventType, payload: Mapping[str, Any]
    ) -> AssistantStreamEvent: ...

    async def replay(
        self, run_id: UUID, after_sequence: int = 0
    ) -> tuple[AssistantStreamEvent, ...]: ...


class AssistantEventLog:
    """In-memory Assistant event authority for unit tests."""

    def __init__(self) -> None:
        self._events: dict[UUID, list[AssistantStreamEvent]] = {}

    async def append(
        self, run_id: UUID, event_type: AssistantEventType, payload: Mapping[str, Any]
    ) -> AssistantStreamEvent:
        events = self._events.setdefault(run_id, [])
        if events and events[-1].terminal:
            return events[-1]
        if events and events[-1].event_type is event_type and events[-1].payload == payload:
            return events[-1]
        event = AssistantStreamEvent(
            run_id=run_id,
            sequence=len(events) + 1,
            event_type=event_type,
            payload=dict(payload),
        )
        events.append(event)
        return event

    async def replay(
        self, run_id: UUID, after_sequence: int = 0
    ) -> tuple[AssistantStreamEvent, ...]:
        return tuple(
            event for event in self._events.get(run_id, ()) if event.sequence > after_sequence
        )


def _contains_forbidden_key(value: Mapping[str, Any]) -> bool:
    for key, item in value.items():
        if key.lower() in _FORBIDDEN_PAYLOAD_KEYS:
            return True
        if isinstance(item, Mapping) and _contains_forbidden_key(item):
            return True
    return False


__all__ = [
    "ASSISTANT_TERMINAL_EVENT_TYPES",
    "AssistantEventLog",
    "AssistantEventStore",
    "AssistantEventType",
    "AssistantStreamEvent",
]
