"""Versioned, redacted event history for generic Agent Loop runs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid4


class AgentRunEventContractError(ValueError):
    """Raised when a v3 event cannot be safely persisted or projected."""


class AgentRunEventVersionError(AgentRunEventContractError):
    """Raised instead of interpreting a future event version as v3."""


class AgentRunEventConflictError(AgentRunEventContractError):
    """Raised when an event key or terminal outcome conflicts with history."""


class AgentRunEventType(StrEnum):
    ACCEPTED = "accepted"
    SKILL_ACTIVATED = "skill_activated"
    ITERATION_STARTED = "iteration_started"
    TOOL_REQUESTED = "tool_requested"
    TOOL_STARTED = "tool_started"
    TOOL_OUTPUT = "tool_output"
    APPROVAL_REQUIRED = "approval_required"
    CHECKPOINT_SAVED = "checkpoint_saved"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    CLARIFYING = "clarifying"
    REFUSED = "refused"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    CACHE_USED = "cache_used"


AGENT_RUN_TERMINAL_EVENT_TYPES = frozenset(
    {
        AgentRunEventType.COMPLETED,
        AgentRunEventType.CLARIFYING,
        AgentRunEventType.REFUSED,
        AgentRunEventType.FAILED,
        AgentRunEventType.CANCELLED,
        AgentRunEventType.TIMED_OUT,
    }
)

_SCHEMA_VERSION = "agent-run-sse-v3"
AGENT_RUN_SSE_V4 = "agent-run-sse-v4"
_SCHEMA_VERSIONS = frozenset({_SCHEMA_VERSION, AGENT_RUN_SSE_V4})
_PAYLOAD_KEYS = frozenset(
    {
        "checkpoint_sequence",
        "checkpoint_sha256",
        "approval_id",
        "command",
        "continuation",
        "downgrade_reason",
        "duration_ms",
        "effective_effort",
        "error_code",
        "exit_code",
        "evidence_sufficient",
        "goal_complete",
        "has_conflict",
        "input_summary",
        "iteration",
        "mapping_version",
        "mode",
        "model",
        "observation_count",
        "output_summary",
        "output_preview",
        "output_truncated",
        "path",
        "cwd",
        "cache_mode",
        "cache_read_tokens",
        "cache_write_tokens",
        "context_digest",
        "decision_summary",
        "harness_version",
        "provider",
        "publication_id",
        "query_preview",
        "resource_reference",
        "reasoning_profile_schema_version",
        "requested_effort",
        "retry_count",
        "status",
        "stop_reason",
        "skill_name",
        "skill_version",
        "tool_call_count",
        "tool_name",
        "tool_version",
        "tool_family",
        "tool_count",
        "terminal_kind",
        "visible_observation_bytes",
    }
)
_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "answer",
        "content",
        "credential",
        "document_body",
        "excerpt",
        "prompt",
        "raw",
        "raw_response",
        "secret",
        "stderr",
        "stdout",
    }
)
_INTEGER_PAYLOAD_KEYS = frozenset(
    {
        "checkpoint_sequence",
        "duration_ms",
        "iteration",
        "observation_count",
        "retry_count",
        "tool_call_count",
        "exit_code",
        "cache_read_tokens",
        "cache_write_tokens",
        "visible_observation_bytes",
        "tool_count",
    }
)
_BOOLEAN_PAYLOAD_KEYS = frozenset(
    {"evidence_sufficient", "goal_complete", "has_conflict", "output_truncated"}
)


@dataclass(frozen=True)
class AgentRunStreamEvent:
    """One durable v3 event. Payloads are intentionally flat, short, and redacted."""

    run_id: UUID
    sequence: int
    event_type: AgentRunEventType
    payload: Mapping[str, Any]
    event_key: str
    event_id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version not in _SCHEMA_VERSIONS:
            raise AgentRunEventVersionError("unsupported Agent Run event schema version")
        if self.sequence < 1:
            raise AgentRunEventContractError("Agent Run event sequence must be positive")
        if not self.event_key.strip() or len(self.event_key) > 200:
            raise AgentRunEventContractError("Agent Run event key is invalid")
        if self.occurred_at.tzinfo is None:
            raise AgentRunEventContractError("Agent Run event timestamp must be timezone-aware")
        _validate_payload(self.event_type, self.payload, self.schema_version)

    @property
    def terminal(self) -> bool:
        return self.event_type in AGENT_RUN_TERMINAL_EVENT_TYPES

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": str(self.event_id),
            "run_id": str(self.run_id),
            "sequence": self.sequence,
            "occurred_at": self.occurred_at.isoformat(),
            "event_type": self.event_type.value,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True)
class AgentRunEventPage:
    events: tuple[AgentRunStreamEvent, ...]
    next_sequence: int | None
    has_more: bool


class AgentRunEventStore(Protocol):
    async def append(
        self,
        run_id: UUID,
        event_type: AgentRunEventType,
        payload: Mapping[str, Any],
        *,
        event_key: str,
        schema_version: str = _SCHEMA_VERSION,
    ) -> AgentRunStreamEvent: ...

    async def page(
        self, run_id: UUID, *, after_sequence: int = 0, limit: int = 100
    ) -> AgentRunEventPage: ...


class AgentRunEventLog:
    """In-memory v3 event authority for unit tests and local injected dependencies."""

    def __init__(self) -> None:
        self._events: dict[UUID, list[AgentRunStreamEvent]] = {}
        self._keys: dict[tuple[UUID, str], AgentRunStreamEvent] = {}

    async def append(
        self,
        run_id: UUID,
        event_type: AgentRunEventType,
        payload: Mapping[str, Any],
        *,
        event_key: str,
        schema_version: str = _SCHEMA_VERSION,
    ) -> AgentRunStreamEvent:
        event = AgentRunStreamEvent(
            run_id=run_id,
            sequence=1,
            event_type=event_type,
            payload=dict(payload),
            event_key=event_key,
            schema_version=schema_version,
        )
        key = (run_id, event_key)
        existing = self._keys.get(key)
        if existing is not None:
            if existing.event_type is event_type and existing.payload == event.payload:
                return existing
            raise AgentRunEventConflictError("Agent Run event key conflicts with history")
        events = self._events.setdefault(run_id, [])
        if events and events[-1].terminal:
            raise AgentRunEventConflictError("Agent Run already has a terminal event")
        event = AgentRunStreamEvent(
            run_id=run_id,
            sequence=len(events) + 1,
            event_type=event_type,
            payload=dict(payload),
            event_key=event_key,
            schema_version=schema_version,
        )
        events.append(event)
        self._keys[key] = event
        return event

    async def page(
        self, run_id: UUID, *, after_sequence: int = 0, limit: int = 100
    ) -> AgentRunEventPage:
        _validate_page(after_sequence, limit)
        values = [
            event for event in self._events.get(run_id, ()) if event.sequence > after_sequence
        ]
        selected = tuple(values[:limit])
        return AgentRunEventPage(
            events=selected,
            next_sequence=selected[-1].sequence if selected else None,
            has_more=len(values) > len(selected),
        )


def _validate_payload(
    event_type: AgentRunEventType,
    payload: Mapping[str, Any],
    schema_version: str,
) -> None:
    if not payload or len(payload) > 16:
        raise AgentRunEventContractError("Agent Run event payload must contain 1-16 safe fields")
    keys = set(payload)
    if keys & _FORBIDDEN_PAYLOAD_KEYS or keys - _PAYLOAD_KEYS:
        raise AgentRunEventContractError("Agent Run event payload contains unsupported fields")
    if "query_preview" in keys and (
        event_type
        not in {
            AgentRunEventType.TOOL_REQUESTED,
            AgentRunEventType.TOOL_STARTED,
            AgentRunEventType.TOOL_OUTPUT,
            AgentRunEventType.APPROVAL_REQUIRED,
        }
        or payload.get("tool_name") != "knowledge_search"
    ):
        raise AgentRunEventContractError(
            "Agent Run query preview is restricted to knowledge_search Tool events"
        )
    if "resource_reference" in keys and (
        event_type
        not in {
            AgentRunEventType.TOOL_REQUESTED,
            AgentRunEventType.TOOL_STARTED,
            AgentRunEventType.TOOL_OUTPUT,
            AgentRunEventType.APPROVAL_REQUIRED,
        }
        or payload.get("tool_name") != "summarize_document"
    ):
        raise AgentRunEventContractError(
            "Agent Run resource reference is restricted to summarize_document Tool events"
        )
    if event_type is AgentRunEventType.SKILL_ACTIVATED and (
        payload.get("status") != "activated"
        or not isinstance(payload.get("iteration"), int)
        or not isinstance(payload.get("skill_name"), str)
        or not isinstance(payload.get("skill_version"), str)
    ):
        raise AgentRunEventContractError(
            "Skill activation events require a status, iteration, name, and version"
        )
    if event_type in AGENT_RUN_TERMINAL_EVENT_TYPES and not isinstance(payload.get("status"), str):
        raise AgentRunEventContractError("terminal Agent Run events require a safe status")
    if schema_version == AGENT_RUN_SSE_V4:
        if event_type is AgentRunEventType.ACCEPTED and payload.get("harness_version") != (
            "native-tool-use-v2"
        ):
            raise AgentRunEventContractError(
                "v4 accepted events require the native Tool-use harness version"
            )
        if event_type in {
            AgentRunEventType.TOOL_STARTED,
            AgentRunEventType.TOOL_OUTPUT,
        } and payload.get("tool_family") not in {
            "bootstrap",
            "knowledge",
            "workspace",
            "command",
            "skill_creator",
            "read",
        }:
            raise AgentRunEventContractError("v4 Tool events require a safe Tool family")
        if event_type in AGENT_RUN_TERMINAL_EVENT_TYPES and payload.get("terminal_kind") not in {
            "direct",
            "grounded",
        }:
            raise AgentRunEventContractError(
                "v4 terminal events require a direct or grounded terminal kind"
            )
        if event_type is AgentRunEventType.CACHE_USED and any(
            payload.get(key) is None
            for key in (
                "iteration",
                "cache_mode",
                "cache_read_tokens",
                "cache_write_tokens",
                "context_digest",
                "visible_observation_bytes",
                "tool_count",
            )
        ):
            raise AgentRunEventContractError(
                "v4 cache events require safe cache and context counters"
            )
    for key, value in payload.items():
        if key == "exit_code":
            if isinstance(value, bool) or not isinstance(value, int):
                raise AgentRunEventContractError("Agent Run exit code is invalid")
        elif key in _INTEGER_PAYLOAD_KEYS:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise AgentRunEventContractError("Agent Run event counter is invalid")
        elif key in _BOOLEAN_PAYLOAD_KEYS:
            if not isinstance(value, bool):
                raise AgentRunEventContractError("Agent Run event completion flag is invalid")
        elif key in {"query_preview", "resource_reference"}:
            if (
                not isinstance(value, str)
                or not value
                or len(value) > (512 if key == "query_preview" else 280)
                or any(not character.isprintable() for character in value)
            ):
                raise AgentRunEventContractError("Agent Run event preview is invalid")
        elif key == "output_preview":
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 4_000
                or any(
                    not (character.isprintable() or character in {"\n", "\r", "\t"})
                    for character in value
                )
            ):
                raise AgentRunEventContractError("Agent Run event output preview is invalid")
        elif not isinstance(value, str) or not value or len(value) > 512 or "\n" in value:
            raise AgentRunEventContractError("Agent Run event summary is invalid")


def _validate_page(after_sequence: int, limit: int) -> None:
    if after_sequence < 0 or not 1 <= limit <= 200:
        raise AgentRunEventContractError("Agent Run event page is invalid")


__all__ = [
    "AGENT_RUN_TERMINAL_EVENT_TYPES",
    "AGENT_RUN_SSE_V4",
    "AgentRunEventConflictError",
    "AgentRunEventContractError",
    "AgentRunEventLog",
    "AgentRunEventPage",
    "AgentRunEventStore",
    "AgentRunEventType",
    "AgentRunEventVersionError",
    "AgentRunStreamEvent",
]
