"""Pure contracts and lifecycle rules for the provisional grounded QA workflow."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID, uuid4

from .agent_runtime import AgentRun, RunStatus, RunStep
from .retrieval import SearchLocator

MAX_QUESTION_CHARS = 4_000
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class QAOutcome(StrEnum):
    ANSWER = "answer"
    REFUSE = "refuse"
    CONFLICT = "conflict"
    FAILED = "failed"


class QAStatus(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    REFUSED = "refused"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class QAEvent(StrEnum):
    QUEUE = "queue"
    START = "start"
    VERIFY = "verify"
    COMPLETE = "complete"
    REFUSE = "refuse"
    CONFLICT = "conflict"
    FAIL = "fail"
    REQUEST_CANCEL = "request_cancel"
    CANCEL = "cancel"
    TIMEOUT = "timeout"


class CitationStatus(StrEnum):
    VALID = "valid"
    SOURCE_UPDATED = "source_updated"
    WITHDRAWN = "withdrawn"
    DELETED = "deleted"
    RETENTION_EXPIRED = "retention_expired"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


class RefusalReason(StrEnum):
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class QAErrorCode(StrEnum):
    INVALID_INPUT = "QA_INVALID_INPUT"
    SPACE_DENIED = "QA_SPACE_DENIED"
    RETRIEVAL_FAILED = "QA_RETRIEVAL_FAILED"
    MODEL_FAILED = "QA_MODEL_FAILED"
    STRUCTURED_RESPONSE_INVALID = "QA_STRUCTURED_RESPONSE_INVALID"
    CITATION_INVALID = "QA_CITATION_INVALID"
    STORAGE_FAILED = "QA_STORAGE_FAILED"
    TIMEOUT = "QA_TIMEOUT"
    CANCELLED = "QA_CANCELLED"
    POLICY_DENIED = "QA_POLICY_DENIED"


class QAContractError(ValueError):
    """Raised when an input, result, or lifecycle contract is invalid."""


class InvalidQATransitionError(QAContractError):
    """Raised when an event would reopen or otherwise invalidate a QA run."""


class QAError(Exception):
    """Stable, transport-safe QA failure that is never a grounded refusal."""

    def __init__(self, code: QAErrorCode, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def normalize_question(question: str) -> str:
    """Normalize whitespace without retaining unbounded or blank user input."""
    normalized = " ".join(unicodedata.normalize("NFKC", question).split())
    if not normalized:
        raise QAContractError("Question must not be blank")
    if len(normalized) > MAX_QUESTION_CHARS:
        raise QAContractError(f"Question must not exceed {MAX_QUESTION_CHARS} characters")
    return normalized


@dataclass(frozen=True)
class QuestionInput:
    question: str
    space_id: UUID
    caller_id: str
    conversation_id: UUID | None = None
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "question", normalize_question(self.question))
        if not self.caller_id:
            raise QAContractError("Question caller_id must not be blank")
        if self.idempotency_key is not None and not self.idempotency_key:
            raise QAContractError("Question idempotency_key must not be blank when provided")


@dataclass(frozen=True)
class QueryPlan:
    original_question: str
    queries: tuple[str, ...]

    def __post_init__(self) -> None:
        original = normalize_question(self.original_question)
        queries = tuple(normalize_question(query) for query in self.queries)
        if not queries or queries[0] != original:
            raise QAContractError("Query plan must retain the original question as its first query")
        if len(queries) != len(set(queries)):
            raise QAContractError("Query plan queries must be unique")
        object.__setattr__(self, "original_question", original)
        object.__setattr__(self, "queries", queries)


@dataclass(frozen=True)
class EvidenceCandidate:
    evidence_id: UUID
    space_id: UUID
    source_id: UUID
    document_id: UUID
    version_id: UUID
    chunk_id: UUID
    source_key: str
    locators: tuple[SearchLocator, ...]
    excerpt_sha256: str
    matched: bool
    context_only: bool

    def __post_init__(self) -> None:
        if not self.source_key:
            raise QAContractError("Evidence source_key must not be blank")
        if not self.locators:
            raise QAContractError("Evidence must have at least one locator")
        if not _SHA256.fullmatch(self.excerpt_sha256):
            raise QAContractError("Evidence excerpt_sha256 must be lowercase SHA-256")
        if self.matched == self.context_only:
            raise QAContractError("Evidence must be exactly one of matched or context_only")


@dataclass(frozen=True)
class Claim:
    claim_id: str
    text: str
    evidence_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        if not self.claim_id or not self.text.strip():
            raise QAContractError("Claim ID and text must not be blank")
        if not self.evidence_ids:
            raise QAContractError("Every verifiable claim must reference evidence")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise QAContractError("Claim evidence IDs must be unique")


@dataclass(frozen=True)
class Citation:
    evidence_id: UUID
    space_id: UUID
    source_id: UUID
    document_id: UUID
    version_id: UUID
    chunk_id: UUID
    locator: SearchLocator
    excerpt_sha256: str
    status: CitationStatus = CitationStatus.VALID

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.excerpt_sha256):
            raise QAContractError("Citation excerpt_sha256 must be lowercase SHA-256")


@dataclass(frozen=True)
class GroundedAnswer:
    claims: tuple[Claim, ...]
    citations: tuple[Citation, ...]
    answer_id: UUID = field(default_factory=uuid4)
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.claims or not self.citations:
            raise QAContractError("Grounded answers require claims and citations")
        claim_ids = tuple(claim.claim_id for claim in self.claims)
        citation_evidence_ids = tuple(citation.evidence_id for citation in self.citations)
        if len(claim_ids) != len(set(claim_ids)):
            raise QAContractError("Grounded answer claim IDs must be unique")
        if len(citation_evidence_ids) != len(set(citation_evidence_ids)):
            raise QAContractError("Grounded answer citations must have unique evidence IDs")
        citation_evidence = set(citation_evidence_ids)
        if any(not set(claim.evidence_ids) <= citation_evidence for claim in self.claims):
            raise QAContractError("Claims must only cite evidence published with the answer")


@dataclass(frozen=True)
class Refusal:
    reason: RefusalReason
    message: str

    def __post_init__(self) -> None:
        if not self.message.strip():
            raise QAContractError("Refusal message must not be blank")


@dataclass(frozen=True)
class ConflictNotice:
    evidence_ids: tuple[UUID, ...]
    message: str

    def __post_init__(self) -> None:
        if len(self.evidence_ids) < 2 or len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise QAContractError("A conflict requires at least two unique evidence IDs")
        if not self.message.strip():
            raise QAContractError("Conflict message must not be blank")


@dataclass(frozen=True)
class QAResult:
    outcome: QAOutcome
    answer: GroundedAnswer | None = None
    refusal: Refusal | None = None
    conflict: ConflictNotice | None = None
    error_code: QAErrorCode | None = None

    def __post_init__(self) -> None:
        payload_count = sum(
            value is not None
            for value in (self.answer, self.refusal, self.conflict, self.error_code)
        )
        if self.outcome is QAOutcome.ANSWER and self.answer is not None and payload_count == 1:
            return
        if self.outcome is QAOutcome.REFUSE and self.refusal is not None and payload_count == 1:
            return
        if self.outcome is QAOutcome.CONFLICT and self.conflict is not None and payload_count == 1:
            return
        if self.outcome is QAOutcome.FAILED and self.error_code is not None and payload_count == 1:
            return
        raise QAContractError("QA result must contain exactly one payload matching its outcome")


type QAResultPayload = GroundedAnswer | Refusal | ConflictNotice


def validate_answer_citations(
    answer: GroundedAnswer, evidence: tuple[EvidenceCandidate, ...], *, space_id: UUID
) -> None:
    """Verify that published citations exactly preserve this run's Evidence identity."""
    evidence_ids = tuple(candidate.evidence_id for candidate in evidence)
    if len(evidence_ids) != len(set(evidence_ids)):
        raise QAContractError("Run Evidence IDs must be unique")
    evidence_by_id = {candidate.evidence_id: candidate for candidate in evidence}
    for citation in answer.citations:
        candidate = evidence_by_id.get(citation.evidence_id)
        if candidate is None:
            raise QAContractError("Citation references an Evidence ID outside the current run")
        if candidate.space_id != space_id or citation.space_id != space_id:
            raise QAContractError("Citation does not belong to the requested Space")
        if (
            citation.source_id,
            citation.document_id,
            citation.version_id,
            citation.chunk_id,
            citation.excerpt_sha256,
        ) != (
            candidate.source_id,
            candidate.document_id,
            candidate.version_id,
            candidate.chunk_id,
            candidate.excerpt_sha256,
        ):
            raise QAContractError("Citation identity does not match its Evidence")
        if citation.locator not in candidate.locators:
            raise QAContractError("Citation locator does not belong to its Evidence")


_State = QAStatus
_TRANSITIONS: dict[tuple[QAStatus, QAEvent], _State] = {
    (QAStatus.CREATED, QAEvent.QUEUE): QAStatus.QUEUED,
    (QAStatus.QUEUED, QAEvent.START): QAStatus.RUNNING,
    (QAStatus.RUNNING, QAEvent.VERIFY): QAStatus.VERIFYING,
    (QAStatus.VERIFYING, QAEvent.COMPLETE): QAStatus.COMPLETED,
    (QAStatus.VERIFYING, QAEvent.CONFLICT): QAStatus.COMPLETED,
    (QAStatus.VERIFYING, QAEvent.REFUSE): QAStatus.REFUSED,
}

for _status in (QAStatus.CREATED, QAStatus.QUEUED, QAStatus.RUNNING, QAStatus.VERIFYING):
    _TRANSITIONS[(_status, QAEvent.REQUEST_CANCEL)] = QAStatus.CANCEL_REQUESTED
    _TRANSITIONS[(_status, QAEvent.FAIL)] = QAStatus.FAILED
    _TRANSITIONS[(_status, QAEvent.TIMEOUT)] = QAStatus.TIMED_OUT
_TRANSITIONS[(QAStatus.CANCEL_REQUESTED, QAEvent.CANCEL)] = QAStatus.CANCELLED


def transition_qa_status(status: QAStatus, event: QAEvent) -> QAStatus:
    """Apply a terminal-safe QA lifecycle transition."""
    try:
        return _TRANSITIONS[(status, event)]
    except KeyError as exc:
        raise InvalidQATransitionError(
            f"event {event.value!r} is invalid for status {status.value!r}"
        ) from exc


def project_qa_status(
    run: AgentRun, *, queued: bool = False, result: QAResult | None = None
) -> QAStatus:
    """Project the shared AgentRun lifecycle without creating a second run entity or budget."""
    if run.status is RunStatus.CREATED:
        return QAStatus.QUEUED if queued else QAStatus.CREATED
    if run.status in {RunStatus.RUNNING, RunStatus.WAITING_APPROVAL}:
        return QAStatus.VERIFYING if run.current_step is RunStep.VERIFYING else QAStatus.RUNNING
    if run.status is RunStatus.COMPLETED:
        if result is None:
            raise QAContractError("Completed AgentRun requires a QA result projection")
        if result.outcome is QAOutcome.REFUSE:
            return QAStatus.REFUSED
        if result.outcome is QAOutcome.FAILED:
            raise QAContractError("A failed QA result cannot project from a completed AgentRun")
        return QAStatus.COMPLETED
    return {
        RunStatus.FAILED: QAStatus.FAILED,
        RunStatus.CANCEL_REQUESTED: QAStatus.CANCEL_REQUESTED,
        RunStatus.CANCELLED: QAStatus.CANCELLED,
        RunStatus.TIMED_OUT: QAStatus.TIMED_OUT,
    }[run.status]


__all__ = [
    "Citation",
    "CitationStatus",
    "Claim",
    "ConflictNotice",
    "EvidenceCandidate",
    "GroundedAnswer",
    "InvalidQATransitionError",
    "MAX_QUESTION_CHARS",
    "QAContractError",
    "QAError",
    "QAErrorCode",
    "QAEvent",
    "QAOutcome",
    "QAResult",
    "QAResultPayload",
    "QAStatus",
    "QueryPlan",
    "QuestionInput",
    "Refusal",
    "RefusalReason",
    "normalize_question",
    "project_qa_status",
    "transition_qa_status",
    "validate_answer_citations",
]
