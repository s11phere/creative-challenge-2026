"""In-memory provisional Grounded QA repository for ownership and atomicity tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from domain.grounded_qa import (
    Citation,
    CitationStatus,
    QAContractError,
    QAErrorCode,
    QAEvent,
    QAOutcome,
    QAResult,
    QAStatus,
    transition_qa_status,
    validate_answer_citations,
)
from domain.qa_persistence import (
    CitationRecord,
    ConversationRecord,
    EvidenceRecord,
    FeedbackRecord,
    MessageRecord,
    MessageRole,
    QARunRecord,
    QARunUsage,
    terminal_status_for_result,
)

_RUNTIME_TERMINAL = frozenset({QAStatus.FAILED, QAStatus.CANCELLED, QAStatus.TIMED_OUT})
_BUSINESS_TERMINAL = frozenset({QAStatus.COMPLETED, QAStatus.REFUSED})


class InMemoryGroundedQARepository:
    """Serialize writes to model the future PostgreSQL transaction boundary."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._conversations: dict[UUID, ConversationRecord] = {}
        self._messages: dict[UUID, MessageRecord] = {}
        self._message_keys: dict[tuple[UUID, str], UUID] = {}
        self._attempts: dict[UUID, QARunRecord] = {}
        self._latest_attempts: dict[UUID, UUID] = {}
        self._run_keys: dict[tuple[UUID, str, str], UUID] = {}
        self._evidence: dict[tuple[UUID, UUID], EvidenceRecord] = {}
        self._citations: dict[tuple[UUID, UUID], CitationRecord] = {}
        self._feedback: dict[UUID, FeedbackRecord] = {}
        self._feedback_keys: dict[tuple[UUID, str, str], UUID] = {}

    async def create_conversation(self, conversation: ConversationRecord) -> ConversationRecord:
        async with self._lock:
            existing = self._conversations.get(conversation.conversation_id)
            if existing is not None:
                if (
                    existing.space_id == conversation.space_id
                    and existing.owner_id == conversation.owner_id
                ):
                    return existing
                raise QAContractError("Conversation identity already exists with another owner")
            self._conversations[conversation.conversation_id] = conversation
            return conversation

    async def get_conversation(self, conversation_id: UUID) -> ConversationRecord | None:
        async with self._lock:
            return self._conversations.get(conversation_id)

    async def list_conversations(
        self, space_id: UUID, owner_id: str
    ) -> tuple[ConversationRecord, ...]:
        async with self._lock:
            return tuple(
                sorted(
                    (
                        conversation
                        for conversation in self._conversations.values()
                        if conversation.space_id == space_id
                        and conversation.owner_id == owner_id
                        and conversation.archived_at is None
                    ),
                    key=lambda conversation: (
                        conversation.updated_at,
                        str(conversation.conversation_id),
                    ),
                    reverse=True,
                )
            )

    async def archive_conversation(self, conversation_id: UUID) -> ConversationRecord | None:
        async with self._lock:
            conversation = self._conversations.get(conversation_id)
            if conversation is None:
                return None
            if conversation.archived_at is not None:
                return conversation
            archived = replace(conversation, archived_at=datetime.now(UTC))
            self._conversations[conversation_id] = archived
            return archived

    async def append_message(self, message: MessageRecord) -> MessageRecord:
        async with self._lock:
            if message.role is not MessageRole.USER:
                raise QAContractError("Assistant messages require atomic terminal publication")
            self._validate_message_owner(message)
            if message.idempotency_key is not None:
                key = (message.conversation_id, message.idempotency_key)
                existing_id = self._message_keys.get(key)
                if existing_id is not None:
                    existing = self._messages[existing_id]
                    if _same_message_command(existing, message):
                        return existing
                    raise QAContractError("Message idempotency key has conflicting content")
            if message.message_id in self._messages:
                raise QAContractError("Message identity already exists")
            self._messages[message.message_id] = message
            if message.idempotency_key is not None:
                self._message_keys[(message.conversation_id, message.idempotency_key)] = (
                    message.message_id
                )
            conversation = self._conversations[message.conversation_id]
            if message.created_at > conversation.updated_at:
                self._conversations[message.conversation_id] = replace(
                    conversation, updated_at=message.created_at
                )
            return message

    async def get_message(self, message_id: UUID) -> MessageRecord | None:
        async with self._lock:
            return self._messages.get(message_id)

    async def list_messages(self, conversation_id: UUID) -> tuple[MessageRecord, ...]:
        async with self._lock:
            self._require_conversation(conversation_id)
            return tuple(
                sorted(
                    (
                        message
                        for message in self._messages.values()
                        if message.conversation_id == conversation_id
                    ),
                    key=lambda message: (message.created_at, str(message.message_id)),
                )
            )

    async def create_run(self, run: QARunRecord) -> QARunRecord:
        async with self._lock:
            conversation = self._require_conversation(run.conversation_id)
            if conversation.space_id != run.space_id or conversation.owner_id != run.caller_id:
                raise QAContractError("QA run does not belong to the conversation owner and Space")
            question = self._messages.get(run.question_message_id)
            if question is None or question.role is not MessageRole.USER:
                raise QAContractError("QA run must reference an existing user question")
            if question.conversation_id != run.conversation_id or question.space_id != run.space_id:
                raise QAContractError("QA run question does not belong to its conversation")
            if run.status is not QAStatus.CREATED or run.result is not None:
                raise QAContractError("New QA attempts must start without a result")

            key = (run.space_id, run.caller_id, run.idempotency_key)
            existing_attempt_id = self._run_keys.get(key)
            if existing_attempt_id is not None:
                existing = self._attempts[existing_attempt_id]
                if _same_run_command(existing, run):
                    return existing
                raise QAContractError("QA run idempotency key has conflicting ownership")

            self._validate_new_attempt(run)
            if run.attempt.attempt_id in self._attempts:
                raise QAContractError("QA attempt identity already exists")
            self._attempts[run.attempt.attempt_id] = run
            self._latest_attempts[run.run_id] = run.attempt.attempt_id
            self._run_keys[key] = run.attempt.attempt_id
            return run

    async def get_run(self, run_id: UUID) -> QARunRecord | None:
        async with self._lock:
            attempt_id = self._latest_attempts.get(run_id)
            return self._attempts.get(attempt_id) if attempt_id is not None else None

    async def list_runs(self, conversation_id: UUID) -> tuple[QARunRecord, ...]:
        async with self._lock:
            self._require_conversation(conversation_id)
            runs = (
                self._attempts[attempt_id]
                for attempt_id in self._latest_attempts.values()
                if self._attempts[attempt_id].conversation_id == conversation_id
            )
            return tuple(sorted(runs, key=lambda run: (run.created_at, str(run.run_id))))

    async def transition_run(
        self,
        run_id: UUID,
        event: QAEvent,
        *,
        error_code: str | None = None,
    ) -> QARunRecord:
        if event in {QAEvent.COMPLETE, QAEvent.REFUSE, QAEvent.CONFLICT}:
            raise QAContractError("Business terminal transitions require atomic publication")
        async with self._lock:
            run = self._require_latest_run(run_id)
            status = transition_qa_status(run.status, event)
            expected_error = {
                QAStatus.FAILED: error_code,
                QAStatus.TIMED_OUT: QAErrorCode.TIMED_OUT.value,
                QAStatus.CANCELLED: QAErrorCode.CANCELLED.value,
            }.get(status)
            if status is QAStatus.FAILED and not expected_error:
                raise QAContractError("Failed QA runs require a safe error code")
            updated = replace(
                run,
                status=status,
                cancellation_requested=(
                    run.cancellation_requested or status is QAStatus.CANCEL_REQUESTED
                ),
                error_code=expected_error,
                updated_at=datetime.now(UTC),
            )
            self._attempts[run.attempt.attempt_id] = updated
            return updated

    async def request_cancel(self, run_id: UUID) -> QARunRecord:
        async with self._lock:
            run = self._require_latest_run(run_id)
            if run.status is QAStatus.CANCEL_REQUESTED or run.status in (
                _RUNTIME_TERMINAL | _BUSINESS_TERMINAL
            ):
                return run
            updated = replace(
                run,
                status=transition_qa_status(run.status, QAEvent.REQUEST_CANCEL),
                cancellation_requested=True,
                updated_at=datetime.now(UTC),
            )
            self._attempts[run.attempt.attempt_id] = updated
            return updated

    async def save_usage(self, run_id: UUID, usage: QARunUsage) -> QARunRecord:
        async with self._lock:
            run = self._require_latest_run(run_id)
            if run.usage == usage:
                return run
            if run.status in _RUNTIME_TERMINAL | _BUSINESS_TERMINAL:
                raise QAContractError("QA usage cannot change after terminal publication")
            if not _usage_is_monotonic(run.usage, usage):
                raise QAContractError("QA usage updates must be monotonic")
            updated = replace(run, usage=usage, updated_at=datetime.now(UTC))
            self._attempts[run.attempt.attempt_id] = updated
            return updated

    async def save_evidence(self, evidence: EvidenceRecord) -> EvidenceRecord:
        async with self._lock:
            run = self._require_latest_run(evidence.run_id)
            if run.attempt.attempt_id != evidence.attempt_id:
                raise QAContractError("Evidence does not belong to the current QA attempt")
            if evidence.candidate.space_id != run.space_id:
                raise QAContractError("Evidence does not belong to the QA run Space")
            if run.status in _RUNTIME_TERMINAL | _BUSINESS_TERMINAL:
                raise QAContractError("Evidence cannot be added to a terminal QA run")
            key = (evidence.attempt_id, evidence.candidate.evidence_id)
            existing = self._evidence.get(key)
            if existing is not None:
                if existing == evidence:
                    return existing
                raise QAContractError("Evidence identity has conflicting immutable data")
            self._evidence[key] = evidence
            return evidence

    async def list_evidence(self, attempt_id: UUID) -> tuple[EvidenceRecord, ...]:
        async with self._lock:
            return tuple(
                value
                for (stored_attempt_id, _evidence_id), value in self._evidence.items()
                if stored_attempt_id == attempt_id
            )

    async def publish_terminal(
        self,
        *,
        run_id: UUID,
        result: QAResult,
        answer_message: MessageRecord,
        citations: tuple[CitationRecord, ...] = (),
    ) -> QARunRecord:
        async with self._lock:
            run = self._require_latest_run(run_id)
            if run.status in _BUSINESS_TERMINAL:
                if _same_publication(
                    run,
                    result,
                    answer_message,
                    citations,
                    self._citations,
                    self._messages,
                ):
                    return run
                raise QAContractError("QA run already has a different terminal result")
            if run.status is not QAStatus.VERIFYING or run.cancellation_requested:
                raise QAContractError("QA run is not eligible for terminal publication")
            if answer_message.role is not MessageRole.ASSISTANT:
                raise QAContractError("Terminal publication requires an assistant message")
            if (
                answer_message.run_id != run.run_id
                or answer_message.conversation_id != run.conversation_id
                or answer_message.space_id != run.space_id
            ):
                raise QAContractError("Terminal message does not belong to the QA run")
            if answer_message.content != _result_content(result):
                raise QAContractError("Terminal message content does not match the QA result")
            if answer_message.message_id in self._messages:
                raise QAContractError("Terminal message identity already exists")

            evidence = tuple(
                item
                for (attempt_id, _evidence_id), item in self._evidence.items()
                if attempt_id == run.attempt.attempt_id
            )
            self._validate_result_evidence(
                run=run,
                result=result,
                message=answer_message,
                evidence=evidence,
                citations=citations,
            )

            status = terminal_status_for_result(result)
            updated = replace(
                run,
                status=status,
                result=result,
                answer_message_id=answer_message.message_id,
                updated_at=datetime.now(UTC),
            )
            self._messages[answer_message.message_id] = answer_message
            for citation in citations:
                self._citations[(citation.attempt_id, citation.citation.evidence_id)] = citation
            self._attempts[run.attempt.attempt_id] = updated
            return updated

    async def list_citations(self, attempt_id: UUID) -> tuple[CitationRecord, ...]:
        async with self._lock:
            return tuple(
                value
                for (stored_attempt_id, _evidence_id), value in self._citations.items()
                if stored_attempt_id == attempt_id
            )

    async def submit_feedback(self, feedback: FeedbackRecord) -> FeedbackRecord:
        async with self._lock:
            conversation = self._require_conversation(feedback.conversation_id)
            if (
                conversation.space_id != feedback.space_id
                or conversation.owner_id != feedback.caller_id
            ):
                raise QAContractError(
                    "Feedback does not belong to the conversation owner and Space"
                )
            run = self._attempts.get(feedback.attempt_id)
            message = self._messages.get(feedback.message_id)
            if (
                run is None
                or run.run_id != feedback.run_id
                or run.conversation_id != feedback.conversation_id
                or run.space_id != feedback.space_id
                or run.answer_message_id != feedback.message_id
                or message is None
                or message.role is not MessageRole.ASSISTANT
            ):
                raise QAContractError("Feedback target does not belong to the published QA result")
            key = (feedback.run_id, feedback.caller_id, feedback.idempotency_key)
            existing_id = self._feedback_keys.get(key)
            if existing_id is not None:
                existing = self._feedback[existing_id]
                if _same_feedback_command(existing, feedback):
                    return existing
                raise QAContractError("Feedback idempotency key has conflicting content")
            if feedback.feedback_id in self._feedback:
                raise QAContractError("Feedback identity already exists")
            self._feedback[feedback.feedback_id] = feedback
            self._feedback_keys[key] = feedback.feedback_id
            return feedback

    def _validate_message_owner(self, message: MessageRecord) -> None:
        conversation = self._require_conversation(message.conversation_id)
        if conversation.space_id != message.space_id:
            raise QAContractError("Message does not belong to the conversation Space")

    def _validate_new_attempt(self, run: QARunRecord) -> None:
        latest_id = self._latest_attempts.get(run.run_id)
        if run.attempt.number == 1:
            if latest_id is not None:
                raise QAContractError("Initial QA run identity already exists")
            return
        previous_attempt_id = run.attempt.previous_attempt_id
        assert previous_attempt_id is not None
        if latest_id != previous_attempt_id:
            raise QAContractError("Retry does not follow the latest QA attempt")
        previous = self._attempts.get(previous_attempt_id)
        if previous is None or previous.status not in {QAStatus.FAILED, QAStatus.TIMED_OUT}:
            raise QAContractError("Retry predecessor is not an eligible terminal attempt")
        if (
            previous.conversation_id != run.conversation_id
            or previous.question_message_id != run.question_message_id
            or previous.space_id != run.space_id
            or previous.caller_id != run.caller_id
        ):
            raise QAContractError("Retry changes immutable QA run ownership")

    def _validate_result_evidence(
        self,
        *,
        run: QARunRecord,
        result: QAResult,
        message: MessageRecord,
        evidence: tuple[EvidenceRecord, ...],
        citations: tuple[CitationRecord, ...],
    ) -> None:
        if any(item.resolution_status is not CitationStatus.VALID for item in evidence):
            raise QAContractError("Terminal publication contains unavailable Evidence")
        evidence_by_id = {item.candidate.evidence_id: item for item in evidence}
        citation_ids = tuple(item.citation.evidence_id for item in citations)
        if len(citation_ids) != len(set(citation_ids)):
            raise QAContractError("Terminal publication contains duplicate Citation Evidence IDs")
        if any(item.citation.status is not CitationStatus.VALID for item in citations):
            raise QAContractError("Terminal publication contains a non-publishable Citation")
        if result.outcome is QAOutcome.ANSWER:
            assert result.answer is not None
            if len(citations) != len(result.answer.citations):
                raise QAContractError("Published answer citations are incomplete")
            if {item.citation for item in citations} != set(result.answer.citations):
                raise QAContractError("Citation records do not match the published answer")
            validate_answer_citations(
                result.answer,
                tuple(item.candidate for item in evidence),
                space_id=run.space_id,
            )
        elif citations:
            raise QAContractError("Only grounded answers may publish Citation records")
        if result.outcome is QAOutcome.CONFLICT:
            assert result.conflict is not None
            if any(
                evidence_id not in evidence_by_id for evidence_id in result.conflict.evidence_ids
            ):
                raise QAContractError("Conflict references Evidence outside the QA attempt")
            if (
                len(
                    {
                        evidence_by_id[evidence_id].candidate.source_id
                        for evidence_id in result.conflict.evidence_ids
                    }
                )
                < 2
            ):
                raise QAContractError("Conflict must preserve Evidence from at least two sources")
        for citation in citations:
            if (
                citation.run_id != run.run_id
                or citation.attempt_id != run.attempt.attempt_id
                or citation.message_id != message.message_id
                or citation.citation.evidence_id not in evidence_by_id
            ):
                raise QAContractError("Citation record does not belong to the terminal publication")

    def _require_conversation(self, conversation_id: UUID) -> ConversationRecord:
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            raise QAContractError("Conversation does not exist")
        return conversation

    def _require_latest_run(self, run_id: UUID) -> QARunRecord:
        attempt_id = self._latest_attempts.get(run_id)
        if attempt_id is None:
            raise QAContractError("QA run does not exist")
        return self._attempts[attempt_id]


def _result_content(result: QAResult) -> str:
    if result.answer is not None:
        return result.answer.text
    if result.refusal is not None:
        return result.refusal.message
    if result.conflict is not None:
        return result.conflict.message
    raise QAContractError("Infrastructure failures cannot create assistant messages")


def _same_message_command(existing: MessageRecord, requested: MessageRecord) -> bool:
    return (
        existing.conversation_id,
        existing.space_id,
        existing.role,
        existing.content,
        existing.run_id,
    ) == (
        requested.conversation_id,
        requested.space_id,
        requested.role,
        requested.content,
        requested.run_id,
    )


def _same_run_command(existing: QARunRecord, requested: QARunRecord) -> bool:
    return (
        existing.conversation_id,
        existing.question_message_id,
        existing.space_id,
        existing.caller_id,
        existing.versions,
        existing.retrieval_scope,
    ) == (
        requested.conversation_id,
        requested.question_message_id,
        requested.space_id,
        requested.caller_id,
        requested.versions,
        requested.retrieval_scope,
    )


def _same_feedback_command(existing: FeedbackRecord, requested: FeedbackRecord) -> bool:
    return (
        existing.conversation_id,
        existing.message_id,
        existing.run_id,
        existing.attempt_id,
        existing.space_id,
        existing.caller_id,
        existing.decision,
        existing.note,
    ) == (
        requested.conversation_id,
        requested.message_id,
        requested.run_id,
        requested.attempt_id,
        requested.space_id,
        requested.caller_id,
        requested.decision,
        requested.note,
    )


def _same_publication(
    run: QARunRecord,
    result: QAResult,
    message: MessageRecord,
    citations: tuple[CitationRecord, ...],
    stored_citations: dict[tuple[UUID, UUID], CitationRecord],
    stored_messages: dict[UUID, MessageRecord],
) -> bool:
    stored_message = stored_messages.get(message.message_id)
    if (
        run.result != result
        or run.answer_message_id != message.message_id
        or stored_message is None
        or not _same_message_command(stored_message, message)
    ):
        return False
    stored = tuple(
        value
        for (attempt_id, _evidence_id), value in stored_citations.items()
        if attempt_id == run.attempt.attempt_id
    )
    return {_citation_identity(value) for value in stored} == {
        _citation_identity(value) for value in citations
    }


def _citation_identity(record: CitationRecord) -> tuple[UUID, UUID, Citation]:
    return (
        record.run_id,
        record.attempt_id,
        record.citation,
    )


def _usage_is_monotonic(previous: QARunUsage, current: QARunUsage) -> bool:
    if (
        current.input_tokens < previous.input_tokens
        or current.output_tokens < previous.output_tokens
        or current.model_calls < previous.model_calls
        or current.repair_attempts < previous.repair_attempts
        or current.model_latency_ms < previous.model_latency_ms
    ):
        return False
    current_phases = {timing.phase: timing.duration_ms for timing in current.phase_timings}
    return all(
        current_phases.get(timing.phase, -1) >= timing.duration_ms
        for timing in previous.phase_timings
    )


__all__ = ["InMemoryGroundedQARepository"]
