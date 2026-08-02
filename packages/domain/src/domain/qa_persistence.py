"""Pure provisional contracts for Grounded QA persistence ownership and publication."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

from .grounded_qa import (
    Citation,
    CitationStatus,
    EvidenceCandidate,
    QAAttempt,
    QAEvent,
    QAOutcome,
    QAResult,
    QAStatus,
)


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class FeedbackDecision(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"


class FeedbackReviewStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class QAPhase(StrEnum):
    PLANNING = "planning"
    RETRIEVAL = "retrieval"
    GENERATION = "generation"
    VERIFICATION = "verification"
    PERSISTENCE = "persistence"


@dataclass(frozen=True)
class QAPhaseTiming:
    phase: QAPhase
    duration_ms: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.duration_ms) or self.duration_ms < 0:
            raise ValueError("QA phase duration must be finite and non-negative")


@dataclass(frozen=True)
class QARunUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    repair_attempts: int = 0
    model_latency_ms: float = 0.0
    phase_timings: tuple[QAPhaseTiming, ...] = ()

    def __post_init__(self) -> None:
        counts = (
            self.input_tokens,
            self.output_tokens,
            self.model_calls,
            self.repair_attempts,
        )
        if any(value < 0 for value in counts):
            raise ValueError("QA run usage counts must be non-negative")
        if self.repair_attempts > self.model_calls:
            raise ValueError("QA repair attempts cannot exceed model calls")
        if not math.isfinite(self.model_latency_ms) or self.model_latency_ms < 0:
            raise ValueError("QA model latency must be finite and non-negative")
        phases = tuple(timing.phase for timing in self.phase_timings)
        if len(phases) != len(set(phases)):
            raise ValueError("QA phase timings must have unique phases")


@dataclass(frozen=True)
class QARunVersions:
    skill_version: str
    profile_version: str
    retrieval_profile_version: str
    model_identity: str
    prompt_version: str
    output_schema_version: str
    corpus_version: str
    dataset_version: str
    skill_name: str = "knowledge_qa"
    skill_content_sha256: str | None = None

    def __post_init__(self) -> None:
        required = (
            self.skill_name,
            self.skill_version,
            self.profile_version,
            self.retrieval_profile_version,
            self.model_identity,
            self.prompt_version,
            self.output_schema_version,
            self.corpus_version,
            self.dataset_version,
        )
        if any(not value for value in required):
            raise ValueError("QA run version values must not be blank")
        if (
            self.skill_content_sha256 is not None
            and re.fullmatch(r"[0-9a-f]{64}", self.skill_content_sha256) is None
        ):
            raise ValueError("QA Skill content digest must be a lowercase SHA-256")


@dataclass(frozen=True)
class QARetrievalScope:
    source_ids: frozenset[UUID] = frozenset()
    document_ids: frozenset[UUID] = frozenset()
    version_ids: frozenset[UUID] = frozenset()

    def __post_init__(self) -> None:
        if self.version_ids and not self.document_ids:
            raise ValueError("QA version scope must also fix its documents")


@dataclass(frozen=True)
class ConversationRecord:
    space_id: UUID
    owner_id: str
    conversation_id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    archived_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.owner_id:
            raise ValueError("Conversation owner_id must not be blank")


@dataclass(frozen=True)
class MessageRecord:
    conversation_id: UUID
    space_id: UUID
    role: MessageRole
    content: str
    message_id: UUID = field(default_factory=uuid4)
    run_id: UUID | None = None
    idempotency_key: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError("Message content must not be blank")
        if self.idempotency_key is not None and not self.idempotency_key:
            raise ValueError("Message idempotency_key must not be blank")
        if self.role is MessageRole.USER and self.run_id is not None:
            raise ValueError("User messages cannot reference a QA run")
        if self.role is MessageRole.ASSISTANT and self.run_id is None:
            raise ValueError("Assistant messages must reference a QA run")


@dataclass(frozen=True)
class QARunRecord:
    run_id: UUID
    attempt: QAAttempt
    conversation_id: UUID
    question_message_id: UUID
    space_id: UUID
    caller_id: str
    idempotency_key: str
    versions: QARunVersions
    retrieval_scope: QARetrievalScope = QARetrievalScope()
    status: QAStatus = QAStatus.CREATED
    cancellation_requested: bool = False
    error_code: str | None = None
    result: QAResult | None = None
    answer_message_id: UUID | None = None
    usage: QARunUsage = field(default_factory=QARunUsage)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.attempt.run_id != self.run_id:
            raise ValueError("QA attempt must reference the same run identity")
        if not self.caller_id or not self.idempotency_key:
            raise ValueError("QA run caller and idempotency key are required")
        if self.status is QAStatus.CANCEL_REQUESTED and not self.cancellation_requested:
            raise ValueError("cancel_requested status requires a cancellation request")
        if self.status in {QAStatus.COMPLETED, QAStatus.REFUSED}:
            if self.result is None:
                raise ValueError("Business terminal QA runs require an immutable result")
            if (
                terminal_status_for_result(self.result) is not self.status
                or self.answer_message_id is None
            ):
                raise ValueError("Business terminal status must match its result and message")
        if self.status in {QAStatus.FAILED, QAStatus.CANCELLED, QAStatus.TIMED_OUT} and (
            self.result is not None or self.answer_message_id is not None
        ):
            raise ValueError("Runtime terminal QA runs cannot contain a business result")
        if self.status is QAStatus.FAILED and not self.error_code:
            raise ValueError("Failed QA runs require an error code")
        expected_error = {
            QAStatus.CANCELLED: "QA_CANCELLED",
            QAStatus.TIMED_OUT: "QA_TIMED_OUT",
        }.get(self.status)
        if expected_error is not None and self.error_code != expected_error:
            raise ValueError("Cancelled and timed-out QA runs require their terminal error code")
        if self.status not in {
            QAStatus.COMPLETED,
            QAStatus.REFUSED,
            QAStatus.FAILED,
            QAStatus.CANCELLED,
            QAStatus.TIMED_OUT,
        } and (self.result is not None or self.answer_message_id is not None):
            raise ValueError("Non-terminal QA runs cannot contain a business result")


@dataclass(frozen=True)
class EvidenceRecord:
    run_id: UUID
    attempt_id: UUID
    candidate: EvidenceCandidate
    resolution_status: CitationStatus = CitationStatus.VALID
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class CitationRecord:
    run_id: UUID
    attempt_id: UUID
    message_id: UUID
    citation: Citation
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class FeedbackRecord:
    conversation_id: UUID
    message_id: UUID
    run_id: UUID
    attempt_id: UUID
    space_id: UUID
    caller_id: str
    idempotency_key: str
    decision: FeedbackDecision
    feedback_id: UUID = field(default_factory=uuid4)
    note: str | None = None
    review_status: FeedbackReviewStatus = FeedbackReviewStatus.PENDING_REVIEW
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.caller_id or not self.idempotency_key:
            raise ValueError("Feedback caller and idempotency key are required")
        if self.note is not None and not self.note.strip():
            raise ValueError("Feedback note must not be blank when provided")


class GroundedQARepository(Protocol):
    async def create_conversation(self, conversation: ConversationRecord) -> ConversationRecord: ...

    async def get_conversation(self, conversation_id: UUID) -> ConversationRecord | None: ...

    async def list_conversations(
        self, space_id: UUID, owner_id: str
    ) -> tuple[ConversationRecord, ...]: ...

    async def archive_conversation(self, conversation_id: UUID) -> ConversationRecord | None: ...

    async def append_message(self, message: MessageRecord) -> MessageRecord: ...

    async def get_message(self, message_id: UUID) -> MessageRecord | None: ...

    async def list_messages(self, conversation_id: UUID) -> tuple[MessageRecord, ...]: ...

    async def create_run(self, run: QARunRecord) -> QARunRecord: ...

    async def get_run(self, run_id: UUID) -> QARunRecord | None: ...

    async def list_runs(self, conversation_id: UUID) -> tuple[QARunRecord, ...]: ...

    async def transition_run(
        self, run_id: UUID, event: QAEvent, *, error_code: str | None = None
    ) -> QARunRecord: ...

    async def request_cancel(self, run_id: UUID) -> QARunRecord: ...

    async def save_usage(self, run_id: UUID, usage: QARunUsage) -> QARunRecord: ...

    async def save_evidence(self, evidence: EvidenceRecord) -> EvidenceRecord: ...

    async def list_evidence(self, attempt_id: UUID) -> tuple[EvidenceRecord, ...]: ...

    async def publish_terminal(
        self,
        *,
        run_id: UUID,
        result: QAResult,
        answer_message: MessageRecord,
        citations: tuple[CitationRecord, ...] = (),
    ) -> QARunRecord: ...

    async def list_citations(self, attempt_id: UUID) -> tuple[CitationRecord, ...]: ...

    async def submit_feedback(self, feedback: FeedbackRecord) -> FeedbackRecord: ...


class GroundedQAExecutionRepository(GroundedQARepository, Protocol):
    """Durable execution ownership used by at-least-once Worker delivery."""

    async def claim_run(
        self, run_id: UUID, *, lease_owner: str, lease_seconds: int
    ) -> QARunRecord | None: ...

    async def renew_run_lease(
        self, run_id: UUID, *, lease_owner: str, lease_seconds: int
    ) -> bool: ...

    async def release_run_lease(self, run_id: UUID, *, lease_owner: str) -> None: ...

    async def prepare_recovery(self) -> tuple[UUID, ...]: ...


def terminal_status_for_result(result: QAResult) -> QAStatus:
    if result.outcome is QAOutcome.ANSWER or result.outcome is QAOutcome.CONFLICT:
        return QAStatus.COMPLETED
    if result.outcome is QAOutcome.REFUSE:
        return QAStatus.REFUSED
    raise ValueError("Infrastructure failures cannot be published as business results")


__all__ = [
    "CitationRecord",
    "ConversationRecord",
    "EvidenceRecord",
    "FeedbackDecision",
    "FeedbackRecord",
    "FeedbackReviewStatus",
    "GroundedQARepository",
    "GroundedQAExecutionRepository",
    "MessageRecord",
    "MessageRole",
    "QAPhase",
    "QAPhaseTiming",
    "QARetrievalScope",
    "QARunRecord",
    "QARunUsage",
    "QARunVersions",
    "terminal_status_for_result",
]
