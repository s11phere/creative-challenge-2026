"""Pure contracts for the shared ConversationRun identity."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

from .qa_persistence import MessageRecord


class ConversationRunKind(StrEnum):
    """Durable work kinds dispatched by the shared Worker boundary."""

    ASSISTANT_TURN = "assistant_turn"
    GROUNDED_QA = "grounded_qa"
    SKILL = "skill"
    CONTEXT_COMPACTION = "context_compaction"


class ConversationRunSelectionSource(StrEnum):
    """How the Application selected the current behavior."""

    AUTO = "auto"
    COMMAND = "command"
    NONE = "none"


class ConversationRunStatus(StrEnum):
    """Shared parent lifecycle; detailed QA/runtime states remain projections."""

    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_CLARIFICATION = "waiting_clarification"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    REFUSED = "refused"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class ClarificationKind(StrEnum):
    """Reasons a turn cannot safely continue without user input."""

    INPUT_REQUIRED = "input_required"
    RESOURCE_AMBIGUOUS = "resource_ambiguous"
    RESOURCE_MISSING = "resource_missing"


class AssistantResultKind(StrEnum):
    """Terminal or waiting result projections owned by the Assistant parent."""

    DIRECT_MESSAGE = "direct_message"
    CLARIFICATION = "clarification"
    SKILL_RESULT = "skill_result"


@dataclass(frozen=True)
class FixedSkillIdentity:
    """Immutable server-pinned Skill identity captured by a Run."""

    name: str
    version: str
    content_sha256: str

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.version.strip():
            raise ValueError("fixed Skill identity requires a name and version")
        if re.fullmatch(r"[0-9a-f]{64}", self.content_sha256) is None:
            raise ValueError("fixed Skill identity requires a lowercase SHA-256")


@dataclass(frozen=True)
class ConversationRunUsage:
    """Actual usage; product budgets are deliberately not part of this value object."""

    input_tokens: int = 0
    output_tokens: int = 0
    model_latency_ms: float = 0.0

    def __post_init__(self) -> None:
        if self.input_tokens < 0 or self.output_tokens < 0 or self.model_latency_ms < 0:
            raise ValueError("ConversationRun usage cannot be negative")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class ResourceCandidate:
    """Safe display metadata; it intentionally contains no content or storage UUID."""

    candidate_id: str
    resource_type: str
    label: str
    source_label: str | None = None
    version_label: str | None = None

    def __post_init__(self) -> None:
        if not self.candidate_id.strip() or not self.label.strip():
            raise ValueError("resource candidate identity and label are required")
        if self.resource_type not in {"source", "document", "document_version"}:
            raise ValueError("unsupported resource candidate type")


@dataclass(frozen=True)
class Clarification:
    """Server-authored clarification state waiting for a bounded user choice."""

    clarification_id: str
    kind: ClarificationKind
    message: str
    resource_candidates: tuple[ResourceCandidate, ...] = ()

    def __post_init__(self) -> None:
        if not self.clarification_id.strip() or not self.message.strip():
            raise ValueError("clarification identity and message are required")
        candidate_ids = tuple(candidate.candidate_id for candidate in self.resource_candidates)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("clarification candidate IDs must be unique")
        if len(self.resource_candidates) > 20:
            raise ValueError("clarification cannot expose more than twenty candidates")


@dataclass(frozen=True)
class AssistantResult:
    """Safe result reference; message bodies stay in append-only Message records."""

    kind: AssistantResultKind
    message_id: UUID | None = None
    clarification: Clarification | None = None

    def __post_init__(self) -> None:
        if self.kind is AssistantResultKind.CLARIFICATION:
            if self.clarification is None or self.message_id is not None:
                raise ValueError("clarification results require only clarification state")
        elif self.clarification is not None:
            raise ValueError("non-clarification results cannot contain clarification state")
        if self.kind is AssistantResultKind.DIRECT_MESSAGE and self.message_id is None:
            raise ValueError("direct message results require an assistant message ID")


@dataclass(frozen=True)
class ConversationRun:
    """The shared durable parent identity for one conversation turn."""

    run_id: UUID = field(default_factory=uuid4)
    conversation_id: UUID = field(default_factory=uuid4)
    space_id: UUID = field(default_factory=uuid4)
    caller_id: str = ""
    user_message_id: UUID = field(default_factory=uuid4)
    idempotency_key: str = ""
    run_kind: ConversationRunKind = ConversationRunKind.ASSISTANT_TURN
    selection_source: ConversationRunSelectionSource = ConversationRunSelectionSource.NONE
    status: ConversationRunStatus = ConversationRunStatus.CREATED
    cancellation_requested: bool = False
    error_code: str | None = None
    router_version: str = "assistant-router-decision-v1"
    core_prompt_version: str = "assistant-base-prompt-v1"
    model_identity: str = "unselected"
    skill: FixedSkillIdentity | None = None
    usage: ConversationRunUsage = field(default_factory=ConversationRunUsage)
    result: AssistantResult | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.caller_id.strip() or not self.idempotency_key.strip():
            raise ValueError("ConversationRun caller and idempotency key are required")
        if not self.router_version.strip() or not self.core_prompt_version.strip():
            raise ValueError("ConversationRun prompt and router versions are required")
        if not self.model_identity.strip():
            raise ValueError("ConversationRun model identity is required")
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("ConversationRun timestamps must be timezone-aware")
        if (
            self.status is ConversationRunStatus.CANCEL_REQUESTED
            and not self.cancellation_requested
        ):
            raise ValueError("cancel_requested status requires a persisted cancellation request")
        if self.status is ConversationRunStatus.WAITING_CLARIFICATION and (
            self.result is None or self.result.kind is not AssistantResultKind.CLARIFICATION
        ):
            raise ValueError("clarifying Runs require a clarification result")
        if (
            self.run_kind is not ConversationRunKind.CONTEXT_COMPACTION
            and self.status
            in {
                ConversationRunStatus.COMPLETED,
                ConversationRunStatus.REFUSED,
            }
            and self.result is None
        ):
            raise ValueError("business terminal ConversationRuns require a result")
        if self.run_kind is ConversationRunKind.CONTEXT_COMPACTION and self.result is not None:
            raise ValueError("context compaction Runs cannot publish an assistant result")
        if self.status in {
            ConversationRunStatus.FAILED,
            ConversationRunStatus.CANCELLED,
            ConversationRunStatus.TIMED_OUT,
        }:
            if self.result is not None:
                raise ValueError("runtime terminal ConversationRuns cannot contain a result")
            if self.status is ConversationRunStatus.FAILED and not self.error_code:
                raise ValueError("failed ConversationRuns require an error code")


class ConversationRunRepository(Protocol):
    """Persistence port for parent identity and idempotent turn creation."""

    async def create_turn(
        self, run: ConversationRun, user_message: MessageRecord
    ) -> ConversationRun: ...

    async def create_context_compaction_run(self, run: ConversationRun) -> ConversationRun: ...

    async def get_conversation_run(self, run_id: UUID) -> ConversationRun | None: ...

    async def list_conversation_runs(
        self, conversation_id: UUID
    ) -> tuple[ConversationRun, ...]: ...

    async def request_conversation_cancel(self, run_id: UUID) -> ConversationRun: ...

    async def prepare_conversation_recovery(self) -> tuple[UUID, ...]: ...

    async def prepare_assistant_recovery(self) -> tuple[UUID, ...]: ...

    async def prepare_context_compaction_recovery(self) -> tuple[UUID, ...]: ...

    async def claim_conversation_run(
        self, run_id: UUID, *, lease_owner: str, lease_seconds: int
    ) -> ConversationRun | None: ...

    async def renew_conversation_run_lease(
        self, run_id: UUID, *, lease_owner: str, lease_seconds: int
    ) -> bool: ...

    async def release_conversation_run_lease(self, run_id: UUID, *, lease_owner: str) -> None: ...

    async def promote_to_skill(
        self,
        run_id: UUID,
        *,
        run_kind: ConversationRunKind,
        selection_source: ConversationRunSelectionSource,
        skill: FixedSkillIdentity,
        core_prompt_version: str,
    ) -> ConversationRun: ...

    async def publish_direct_message(
        self,
        *,
        run_id: UUID,
        message: MessageRecord,
        usage: ConversationRunUsage,
        model_identity: str,
    ) -> ConversationRun: ...

    async def publish_clarification(
        self,
        *,
        run_id: UUID,
        clarification: Clarification,
        usage: ConversationRunUsage,
        model_identity: str,
    ) -> ConversationRun: ...

    async def fail_conversation_run(self, run_id: UUID, *, error_code: str) -> ConversationRun: ...

    async def cancel_conversation_run(self, run_id: UUID) -> ConversationRun: ...

    async def complete_context_compaction(
        self,
        run_id: UUID,
        *,
        usage: ConversationRunUsage,
        model_identity: str,
    ) -> ConversationRun: ...


__all__ = [
    "AssistantResult",
    "AssistantResultKind",
    "Clarification",
    "ClarificationKind",
    "ConversationRun",
    "ConversationRunKind",
    "ConversationRunRepository",
    "ConversationRunSelectionSource",
    "ConversationRunStatus",
    "ConversationRunUsage",
    "FixedSkillIdentity",
    "ResourceCandidate",
]
