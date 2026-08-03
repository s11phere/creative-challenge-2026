"""Pure contracts and lifecycle rules for the provisional grounded QA workflow."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

from .agent_runtime import AgentRun, RunStatus, RunStep
from .parsing import ParseMetadata
from .retrieval import KeywordLanguageSlice, KeywordQueryKind, SearchLocator

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


class QuestionType(StrEnum):
    FACTUAL = "factual"
    COMPARISON = "comparison"
    PROCEDURAL = "procedural"
    SYNTHESIS = "synthesis"


class QueryFallbackReason(StrEnum):
    REWRITER_UNAVAILABLE = "rewriter_unavailable"
    TIMEOUT = "timeout"
    INVALID_RESPONSE = "invalid_response"
    EMPTY_RESPONSE = "empty_response"


class CitationStatus(StrEnum):
    VALID = "valid"
    SOURCE_UPDATED = "source_updated"
    WITHDRAWN = "withdrawn"
    DELETED = "deleted"
    RETENTION_EXPIRED = "retention_expired"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


class CitationContentKind(StrEnum):
    TEXT = "text"
    PDF = "pdf"


class RefusalReason(StrEnum):
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class RefusalCode(StrEnum):
    INSUFFICIENT_EVIDENCE = "REFUSED_INSUFFICIENT_EVIDENCE"


class QAErrorCode(StrEnum):
    INVALID_INPUT = "QA_INVALID_INPUT"
    SPACE_DENIED = "QA_SPACE_DENIED"
    RETRIEVAL_FAILED = "QA_RETRIEVAL_FAILED"
    MODEL_FAILED = "QA_MODEL_FAILED"
    MODEL_RATE_LIMITED = "QA_MODEL_RATE_LIMITED"
    MODEL_AUTHENTICATION_FAILED = "QA_MODEL_AUTHENTICATION_FAILED"
    STRUCTURED_RESPONSE_INVALID = "QA_STRUCTURED_RESPONSE_INVALID"
    CITATION_INVALID = "QA_CITATION_INVALID"
    STORAGE_FAILED = "QA_STORAGE_FAILED"
    DATABASE_FAILED = "QA_DATABASE_FAILED"
    TIMEOUT = "QA_TIMEOUT"
    TIMED_OUT = "QA_TIMED_OUT"
    CANCELLED = "QA_CANCELLED"
    POLICY_DENIED = "QA_POLICY_DENIED"
    SKILL_INVALID = "QA_SKILL_INVALID"


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


class QACancellationProbe(Protocol):
    """Read the explicit cancellation request without coupling Domain to persistence."""

    async def is_cancel_requested(self) -> bool: ...


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
    source_ids: frozenset[UUID] = frozenset()
    document_ids: frozenset[UUID] = frozenset()
    version_ids: frozenset[UUID] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "question", normalize_question(self.question))
        if not self.caller_id:
            raise QAContractError("Question caller_id must not be blank")
        if self.idempotency_key is not None and not self.idempotency_key:
            raise QAContractError("Question idempotency_key must not be blank when provided")
        if self.version_ids and not self.document_ids:
            raise QAContractError("Version-scoped questions must also fix their documents")


@dataclass(frozen=True)
class QueryPlan:
    original_question: str
    queries: tuple[str, ...]
    language_slice: KeywordLanguageSlice = KeywordLanguageSlice.OTHER
    query_kind: KeywordQueryKind = KeywordQueryKind.NATURAL_LANGUAGE
    question_type: QuestionType = QuestionType.FACTUAL
    rewrite_applied: bool = False
    fallback_reason: QueryFallbackReason | None = None

    def __post_init__(self) -> None:
        original = normalize_question(self.original_question)
        queries = tuple(normalize_question(query) for query in self.queries)
        if not queries or queries[0] != original:
            raise QAContractError("Query plan must retain the original question as its first query")
        if len(queries) != len(set(queries)):
            raise QAContractError("Query plan queries must be unique")
        if self.rewrite_applied != (len(queries) > 1):
            raise QAContractError("Query plan rewrite flag must match its query count")
        if self.rewrite_applied and self.fallback_reason is not None:
            raise QAContractError("A successful rewrite cannot have a fallback reason")
        object.__setattr__(self, "original_question", original)
        object.__setattr__(self, "queries", queries)


class QueryRewriter(Protocol):
    async def rewrite(self, question: QuestionInput, *, max_queries: int) -> tuple[str, ...]: ...


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
class CitationTargetQuery:
    space_id: UUID
    source_id: UUID
    document_id: UUID
    version_id: UUID
    chunk_id: UUID


@dataclass(frozen=True)
class CitationTargetSnapshot:
    query: CitationTargetQuery
    current_version_id: UUID | None
    locators: tuple[SearchLocator, ...]
    blob_hash: str
    storage_key: str
    content_kind: CitationContentKind
    metadata: ParseMetadata
    chunk_text: str | None = None
    source_withdrawn: bool = False
    document_deleted: bool = False
    retention_expired: bool = False
    chunk_available: bool = True

    def __post_init__(self) -> None:
        if not self.locators:
            raise QAContractError("Citation target must preserve at least one locator")
        if not _SHA256.fullmatch(self.blob_hash):
            raise QAContractError("Citation target blob_hash must be lowercase SHA-256")
        if not self.storage_key:
            raise QAContractError("Citation target storage_key must not be blank")
        if self.chunk_text is not None and not self.chunk_text.strip():
            raise QAContractError("Citation target chunk_text must not be blank when provided")


class CitationTargetPort(Protocol):
    async def get_target(self, query: CitationTargetQuery) -> CitationTargetSnapshot | None: ...


@dataclass(frozen=True)
class CitationResolution:
    citation: Citation
    status: CitationStatus
    excerpt: str | None = None

    def __post_init__(self) -> None:
        if (
            self.status
            in {
                CitationStatus.WITHDRAWN,
                CitationStatus.DELETED,
                CitationStatus.RETENTION_EXPIRED,
                CitationStatus.UNAVAILABLE,
                CitationStatus.INVALID,
            }
            and self.excerpt is not None
        ):
            raise QAContractError("Unavailable citation states must not expose an excerpt")


@dataclass(frozen=True)
class GroundedAnswer:
    text: str
    claims: tuple[Claim, ...]
    citations: tuple[Citation, ...]
    answer_id: UUID = field(default_factory=uuid4)
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.text.strip() or not self.claims or not self.citations:
            raise QAContractError("Grounded answers require text, claims, and citations")
        if any(not limitation.strip() for limitation in self.limitations):
            raise QAContractError("Grounded answer limitations must not be blank")
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
    code: RefusalCode = RefusalCode.INSUFFICIENT_EVIDENCE

    def __post_init__(self) -> None:
        if not self.message.strip():
            raise QAContractError("Refusal message must not be blank")
        if (
            self.reason is RefusalReason.INSUFFICIENT_EVIDENCE
            and self.code is not RefusalCode.INSUFFICIENT_EVIDENCE
        ):
            raise QAContractError(
                "Insufficient evidence refusals require their stable refusal code"
            )


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


@dataclass(frozen=True)
class QAAttempt:
    """A new immutable execution identity for a retry; persistence is deferred to Step 6."""

    run_id: UUID
    number: int = 1
    attempt_id: UUID = field(default_factory=uuid4)
    previous_attempt_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.number < 1:
            raise QAContractError("QA attempt number must be positive")
        if self.number == 1 and self.previous_attempt_id is not None:
            raise QAContractError("The first QA attempt cannot have a predecessor")
        if self.number > 1 and self.previous_attempt_id is None:
            raise QAContractError("A retry QA attempt must identify its predecessor")
        if self.attempt_id == self.previous_attempt_id:
            raise QAContractError("A retry must create a distinct QA attempt identity")


_RETRYABLE_QA_ERROR_CODES = frozenset(
    {
        QAErrorCode.RETRIEVAL_FAILED,
        QAErrorCode.MODEL_FAILED,
        QAErrorCode.MODEL_RATE_LIMITED,
        QAErrorCode.STORAGE_FAILED,
        QAErrorCode.DATABASE_FAILED,
        QAErrorCode.TIMEOUT,
        QAErrorCode.TIMED_OUT,
    }
)


def is_retryable_qa_error(error: QAError) -> bool:
    """Retry only explicit transient dependencies, never semantic or terminal outcomes."""
    return error.retryable and error.code in _RETRYABLE_QA_ERROR_CODES


def next_qa_attempt(
    attempt: QAAttempt,
    error: QAError,
    *,
    attempt_id: UUID | None = None,
    max_attempts: int = 2,
) -> QAAttempt:
    """Create, rather than reopen, a retry attempt for one approved transient failure."""
    if max_attempts < 1:
        raise QAContractError("QA retry limit must be positive")
    if not is_retryable_qa_error(error):
        raise QAContractError("QA error is not eligible for retry")
    if attempt.number >= max_attempts:
        raise QAContractError("QA retry limit has been reached")
    return QAAttempt(
        run_id=attempt.run_id,
        number=attempt.number + 1,
        attempt_id=attempt_id or uuid4(),
        previous_attempt_id=attempt.attempt_id,
    )


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
    "CitationContentKind",
    "CitationResolution",
    "CitationStatus",
    "CitationTargetPort",
    "CitationTargetQuery",
    "CitationTargetSnapshot",
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
    "QueryFallbackReason",
    "QueryRewriter",
    "QuestionInput",
    "QuestionType",
    "QAAttempt",
    "QACancellationProbe",
    "Refusal",
    "RefusalCode",
    "RefusalReason",
    "is_retryable_qa_error",
    "next_qa_attempt",
    "normalize_question",
    "project_qa_status",
    "transition_qa_status",
    "validate_answer_citations",
]
