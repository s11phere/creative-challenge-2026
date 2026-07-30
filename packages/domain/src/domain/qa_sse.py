"""Versioned, privacy-safe SSE contracts for Grounded QA runs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


class QAEventType(StrEnum):
    ACCEPTED = "accepted"
    STARTED = "started"
    PHASE = "phase"
    EVIDENCE = "evidence"
    ANSWER_DELTA = "answer_delta"
    COMPLETED = "completed"
    REFUSED = "refused"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    HEARTBEAT = "heartbeat"


TERMINAL_EVENT_TYPES = frozenset(
    {
        QAEventType.COMPLETED,
        QAEventType.REFUSED,
        QAEventType.FAILED,
        QAEventType.CANCELLED,
        QAEventType.TIMED_OUT,
    }
)
_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {"question", "answer", "content", "excerpt", "prompt", "model_output", "raw_response"}
)


@dataclass(frozen=True)
class QAStreamEvent:
    run_id: UUID
    sequence: int
    event_type: QAEventType
    payload: Mapping[str, Any]
    event_id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = "qa-sse-v1"

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ValueError("SSE sequence must be positive")
        if self.occurred_at.tzinfo is None:
            raise ValueError("SSE timestamp must be timezone-aware")
        if self.event_type in TERMINAL_EVENT_TYPES and "status" not in self.payload:
            raise ValueError("terminal SSE events require a safe status")
        if _contains_forbidden_key(self.payload):
            raise ValueError("SSE payload contains private content")

    @property
    def terminal(self) -> bool:
        return self.event_type in TERMINAL_EVENT_TYPES

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


class QAEventLog:
    """In-memory event authority used by provisional API and contract tests."""

    def __init__(self) -> None:
        self._events: dict[UUID, list[QAStreamEvent]] = {}

    def append(
        self, run_id: UUID, event_type: QAEventType, payload: Mapping[str, Any]
    ) -> QAStreamEvent:
        events = self._events.setdefault(run_id, [])
        if events and events[-1].terminal:
            return events[-1]
        if events and events[-1].event_type is event_type and events[-1].payload == payload:
            return events[-1]
        event = QAStreamEvent(
            run_id=run_id, sequence=len(events) + 1, event_type=event_type, payload=dict(payload)
        )
        events.append(event)
        return event

    def replay(self, run_id: UUID, after_sequence: int = 0) -> tuple[QAStreamEvent, ...]:
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


__all__ = ["QAEventLog", "QAEventType", "QAStreamEvent", "TERMINAL_EVENT_TYPES"]
