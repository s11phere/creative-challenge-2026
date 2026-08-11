"""Durable PostgreSQL Grounded QA repository and event store."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from domain.conversation_context import ConversationSensitivity, ConversationSummary
from domain.conversation_run import (
    ConversationRunKind,
    ConversationRunSelectionSource,
    ConversationRunStatus,
)
from domain.grounded_qa import (
    Citation,
    CitationStatus,
    EvidenceCandidate,
    QAAttempt,
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
    FeedbackDecision,
    FeedbackRecord,
    FeedbackReviewRecord,
    FeedbackReviewStatus,
    MessageRecord,
    MessageRole,
    QARetrievalScope,
    QARunRecord,
    QARunUsage,
    QARunVersions,
    terminal_status_for_result,
)
from domain.qa_sse import QAEventType, QAStreamEvent
from domain.reasoning import ReasoningEffort
from pydantic import TypeAdapter
from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from .database import Database
from .orm import (
    ConversationModel,
    ConversationRunModel,
    ConversationSummaryModel,
    QACitationModel,
    QAEventModel,
    QAEvidenceModel,
    QAFeedbackModel,
    QAMessageModel,
    QARunAttemptModel,
    QARunModel,
)

_VERSIONS = TypeAdapter(QARunVersions)
_RETRIEVAL_SCOPE = TypeAdapter(QARetrievalScope)
_USAGE = TypeAdapter(QARunUsage)
_RESULT = TypeAdapter(QAResult)
_EVIDENCE = TypeAdapter(EvidenceCandidate)
_CITATION = TypeAdapter(Citation)
_RUNTIME_TERMINAL = frozenset({QAStatus.FAILED, QAStatus.CANCELLED, QAStatus.TIMED_OUT})
_BUSINESS_TERMINAL = frozenset({QAStatus.COMPLETED, QAStatus.REFUSED})
_TERMINAL = _RUNTIME_TERMINAL | _BUSINESS_TERMINAL


def _dump[T](adapter: TypeAdapter[T], value: T) -> dict[str, object]:
    payload = adapter.dump_python(value, mode="json")
    assert isinstance(payload, dict)
    return payload


class PostgresGroundedQARepository:
    """Persist QA ownership, attempts, evidence, results, and feedback transactionally."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def create_conversation(self, conversation: ConversationRecord) -> ConversationRecord:
        async with self._database.transaction() as session:
            existing = await session.get(ConversationModel, conversation.conversation_id)
            if existing is not None:
                record = _conversation(existing)
                if (
                    record.space_id == conversation.space_id
                    and record.owner_id == conversation.owner_id
                ):
                    return record
                raise QAContractError("Conversation identity already exists with another owner")
            session.add(
                ConversationModel(
                    id=conversation.conversation_id,
                    space_id=conversation.space_id,
                    owner_id=conversation.owner_id,
                    reasoning_effort=conversation.reasoning_effort.value,
                    workspace_path=conversation.workspace_path,
                    always_allowed_tool_names=list(conversation.always_allowed_tool_names),
                    created_at=conversation.created_at,
                    updated_at=conversation.updated_at,
                    archived_at=conversation.archived_at,
                )
            )
        return conversation

    async def get_conversation(self, conversation_id: UUID) -> ConversationRecord | None:
        async with self._database.session() as session:
            model = await session.get(ConversationModel, conversation_id)
            return _conversation(model) if model is not None else None

    async def set_reasoning_effort(
        self, conversation_id: UUID, effort: ReasoningEffort
    ) -> ConversationRecord:
        async with self._database.transaction() as session:
            model = await session.get(ConversationModel, conversation_id, with_for_update=True)
            if model is None or model.archived_at is not None:
                raise QAContractError("Conversation does not exist")
            model.reasoning_effort = effort.value
            model.updated_at = datetime.now(UTC)
            await session.flush()
            return _conversation(model)

    async def set_workspace_path(
        self, conversation_id: UUID, workspace_path: str | None
    ) -> ConversationRecord:
        async with self._database.transaction() as session:
            model = await session.get(ConversationModel, conversation_id, with_for_update=True)
            if model is None or model.archived_at is not None:
                raise QAContractError("Conversation does not exist")
            model.workspace_path = workspace_path
            model.updated_at = datetime.now(UTC)
            await session.flush()
            return _conversation(model)

    async def list_conversations(
        self, space_id: UUID, owner_id: str
    ) -> tuple[ConversationRecord, ...]:
        async with self._database.session() as session:
            models = (
                await session.execute(
                    select(ConversationModel)
                    .where(
                        ConversationModel.space_id == space_id,
                        ConversationModel.owner_id == owner_id,
                        ConversationModel.archived_at.is_(None),
                    )
                    .order_by(ConversationModel.updated_at.desc(), ConversationModel.id.desc())
                )
            ).scalars()
            return tuple(_conversation(model) for model in models)

    async def archive_conversation(self, conversation_id: UUID) -> ConversationRecord | None:
        async with self._database.transaction() as session:
            model = await session.get(ConversationModel, conversation_id)
            if model is None:
                return None
            if model.archived_at is None:
                model.archived_at = datetime.now(UTC)
                await session.flush()
            return _conversation(model)

    async def append_message(self, message: MessageRecord) -> MessageRecord:
        if message.role is not MessageRole.USER:
            raise QAContractError("Assistant messages require atomic terminal publication")
        async with self._database.transaction() as session:
            conversation = await session.get(ConversationModel, message.conversation_id)
            if conversation is None or conversation.space_id != message.space_id:
                raise QAContractError("Message does not belong to the conversation Space")
            if message.idempotency_key is not None:
                existing = (
                    await session.execute(
                        select(QAMessageModel).where(
                            QAMessageModel.conversation_id == message.conversation_id,
                            QAMessageModel.idempotency_key == message.idempotency_key,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    record = _message(existing)
                    if _same_message(record, message):
                        return record
                    raise QAContractError("Message idempotency key has conflicting content")
            session.add(_message_model(message))
            if message.created_at > conversation.updated_at:
                conversation.updated_at = message.created_at
        return message

    async def get_message(self, message_id: UUID) -> MessageRecord | None:
        async with self._database.session() as session:
            model = await session.get(QAMessageModel, message_id)
            return _message(model) if model is not None else None

    async def list_messages(self, conversation_id: UUID) -> tuple[MessageRecord, ...]:
        async with self._database.session() as session:
            if await session.get(ConversationModel, conversation_id) is None:
                raise QAContractError("Conversation does not exist")
            models = (
                await session.execute(
                    select(QAMessageModel)
                    .where(QAMessageModel.conversation_id == conversation_id)
                    .order_by(QAMessageModel.created_at, QAMessageModel.id)
                )
            ).scalars()
            return tuple(_message(model) for model in models)

    async def list_conversation_summaries(
        self, conversation_id: UUID
    ) -> tuple[ConversationSummary, ...]:
        async with self._database.session() as session:
            if await session.get(ConversationModel, conversation_id) is None:
                raise QAContractError("Conversation does not exist")
            models = (
                await session.execute(
                    select(ConversationSummaryModel)
                    .where(ConversationSummaryModel.conversation_id == conversation_id)
                    .order_by(ConversationSummaryModel.created_at, ConversationSummaryModel.id)
                )
            ).scalars()
            return tuple(_conversation_summary(model) for model in models)

    async def create_conversation_summary(
        self, summary: ConversationSummary
    ) -> ConversationSummary:
        async with self._database.transaction() as session:
            conversation = await session.get(ConversationModel, summary.conversation_id)
            if conversation is None or conversation.space_id != summary.space_id:
                raise QAContractError("Conversation summary crosses Space boundary")
            existing = await session.get(ConversationSummaryModel, summary.run_id)
            if existing is not None:
                stored = _conversation_summary(existing)
                if stored.content_sha256 == summary.content_sha256:
                    return stored
                raise QAContractError("Conversation summary Run conflicts with persisted content")
            coverage = (
                await session.execute(
                    select(ConversationSummaryModel).where(
                        ConversationSummaryModel.conversation_id == summary.conversation_id,
                        ConversationSummaryModel.covered_end_message_id
                        == summary.covered_end_message_id,
                        ConversationSummaryModel.prompt_version == summary.prompt_version,
                    )
                )
            ).scalar_one_or_none()
            if coverage is not None:
                return _conversation_summary(coverage)
            session.add(
                ConversationSummaryModel(
                    id=summary.summary_id,
                    conversation_id=summary.conversation_id,
                    space_id=summary.space_id,
                    run_id=summary.run_id,
                    covered_start_message_id=summary.covered_start_message_id,
                    covered_end_message_id=summary.covered_end_message_id,
                    covered_message_count=summary.covered_message_count,
                    content=summary.content,
                    content_sha256=summary.content_sha256,
                    prompt_version=summary.prompt_version,
                    model_identity=summary.model_identity,
                    sensitivity=summary.sensitivity.value,
                    created_at=summary.created_at,
                )
            )
        return summary

    async def create_run(self, run: QARunRecord) -> QARunRecord:
        if run.status is not QAStatus.CREATED or run.result is not None:
            raise QAContractError("New QA attempts must start without a result")
        async with self._database.transaction() as session:
            conversation = await session.get(ConversationModel, run.conversation_id)
            question = await session.get(QAMessageModel, run.question_message_id)
            if (
                conversation is None
                or conversation.space_id != run.space_id
                or conversation.owner_id != run.caller_id
                or question is None
                or question.role != MessageRole.USER.value
                or question.conversation_id != run.conversation_id
                or question.space_id != run.space_id
            ):
                raise QAContractError("QA run ownership or question is invalid")

            parent = await session.get(ConversationRunModel, run.run_id, with_for_update=True)
            base = await session.get(QARunModel, run.run_id, with_for_update=True)
            if base is not None:
                if parent is None:
                    raise QAContractError("QA run is missing its ConversationRun parent")
                _validate_parent(parent, run, check_idempotency=False)
                latest = await self._latest_attempt(session, run.run_id, for_update=True)
                assert latest is not None
                existing = _run(base, latest)
                if run.attempt.attempt_id == existing.attempt.attempt_id:
                    if _same_run(existing, run):
                        return existing
                    raise QAContractError("QA attempt identity has conflicting immutable data")
                self._validate_retry(existing, run)
                attempt = _attempt_model(run)
                session.add(attempt)
                _project_run(base, run)
                _project_conversation_run(parent, run)
                return run

            idempotent = (
                await session.execute(
                    select(QARunModel).where(
                        QARunModel.space_id == run.space_id,
                        QARunModel.caller_id == run.caller_id,
                        QARunModel.idempotency_key == run.idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            if idempotent is not None:
                latest = await self._latest_attempt(session, idempotent.id)
                assert latest is not None
                existing = _run(idempotent, latest)
                if _same_run(existing, run):
                    return existing
                raise QAContractError("QA run idempotency key has conflicting ownership")
            if run.attempt.number != 1:
                raise QAContractError("First persisted QA attempt must be number one")
            if parent is None:
                conflicting_parent = (
                    await session.execute(
                        select(ConversationRunModel)
                        .where(
                            ConversationRunModel.space_id == run.space_id,
                            ConversationRunModel.caller_id == run.caller_id,
                            ConversationRunModel.idempotency_key == run.idempotency_key,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if conflicting_parent is not None:
                    raise QAContractError("QA run idempotency key conflicts with a ConversationRun")
                parent = _conversation_run_model(run)
                session.add(parent)
                await session.flush()
            else:
                _validate_parent(parent, run)
            base = QARunModel(
                id=run.run_id,
                conversation_id=run.conversation_id,
                space_id=run.space_id,
                caller_id=run.caller_id,
                idempotency_key=run.idempotency_key,
                question_message_id=run.question_message_id,
                answer_message_id=None,
                status=run.status.value,
                cancellation_requested=False,
                error_code=None,
                versions=_dump(_VERSIONS, run.versions),
                retrieval_scope=_dump(_RETRIEVAL_SCOPE, run.retrieval_scope),
                standalone_request=run.standalone_request,
                context_sensitivity=run.context_sensitivity,
                usage=_dump(_USAGE, run.usage),
                result=None,
                created_at=run.created_at,
                updated_at=run.updated_at,
            )
            session.add(base)
            await session.flush()
            session.add(_attempt_model(run))
        return run

    async def get_run(self, run_id: UUID) -> QARunRecord | None:
        async with self._database.session() as session:
            base = await session.get(QARunModel, run_id)
            if base is None:
                return None
            attempt = await self._latest_attempt(session, run_id)
            return _run(base, attempt) if attempt is not None else None

    async def list_runs(self, conversation_id: UUID) -> tuple[QARunRecord, ...]:
        async with self._database.session() as session:
            if await session.get(ConversationModel, conversation_id) is None:
                raise QAContractError("Conversation does not exist")
            models = (
                await session.execute(
                    select(QARunModel)
                    .where(QARunModel.conversation_id == conversation_id)
                    .order_by(QARunModel.created_at, QARunModel.id)
                )
            ).scalars()
            runs: list[QARunRecord] = []
            for model in models:
                attempt = await self._latest_attempt(session, model.id)
                if attempt is not None:
                    runs.append(_run(model, attempt))
            return tuple(runs)

    async def transition_run(
        self, run_id: UUID, event: QAEvent, *, error_code: str | None = None
    ) -> QARunRecord:
        if event in {QAEvent.COMPLETE, QAEvent.REFUSE, QAEvent.CONFLICT}:
            raise QAContractError("Business terminal transitions require atomic publication")
        async with self._database.transaction() as session:
            base, attempt, current = await self._locked_run(session, run_id)
            status = transition_qa_status(current.status, event)
            expected_error = {
                QAStatus.FAILED: error_code,
                QAStatus.TIMED_OUT: QAErrorCode.TIMED_OUT.value,
                QAStatus.CANCELLED: QAErrorCode.CANCELLED.value,
            }.get(status)
            if status is QAStatus.FAILED and not expected_error:
                raise QAContractError("Failed QA runs require a safe error code")
            updated = replace(
                current,
                status=status,
                cancellation_requested=current.cancellation_requested
                or status is QAStatus.CANCEL_REQUESTED,
                error_code=expected_error,
                updated_at=datetime.now(UTC),
            )
            _project_attempt(attempt, updated)
            _project_run(base, updated)
            await self._project_parent(session, updated)
            if updated.status in _TERMINAL:
                _clear_lease(attempt)
            return updated

    async def request_cancel(self, run_id: UUID) -> QARunRecord:
        async with self._database.transaction() as session:
            base, attempt, current = await self._locked_run(session, run_id)
            if current.status is QAStatus.CANCEL_REQUESTED or current.status in _TERMINAL:
                return current
            updated = replace(
                current,
                status=transition_qa_status(current.status, QAEvent.REQUEST_CANCEL),
                cancellation_requested=True,
                updated_at=datetime.now(UTC),
            )
            _project_attempt(attempt, updated)
            _project_run(base, updated)
            await self._project_parent(session, updated)
            return updated

    async def save_usage(self, run_id: UUID, usage: QARunUsage) -> QARunRecord:
        async with self._database.transaction() as session:
            base, attempt, current = await self._locked_run(session, run_id)
            if current.usage == usage:
                return current
            if current.status in _TERMINAL or not _usage_is_monotonic(current.usage, usage):
                raise QAContractError("QA usage is terminal or non-monotonic")
            updated = replace(current, usage=usage, updated_at=datetime.now(UTC))
            _project_attempt(attempt, updated)
            _project_run(base, updated)
            await self._project_parent(session, updated)
            return updated

    async def save_evidence(self, evidence: EvidenceRecord) -> EvidenceRecord:
        async with self._database.transaction() as session:
            _base, _attempt, run = await self._locked_run(session, evidence.run_id)
            if (
                run.attempt.attempt_id != evidence.attempt_id
                or evidence.candidate.space_id != run.space_id
                or run.status in _TERMINAL
            ):
                raise QAContractError("Evidence does not belong to an active QA attempt")
            existing = await session.get(QAEvidenceModel, evidence.candidate.evidence_id)
            if existing is not None:
                record = _evidence(existing)
                if record == evidence:
                    return record
                raise QAContractError("Evidence identity has conflicting immutable data")
            session.add(
                QAEvidenceModel(
                    id=evidence.candidate.evidence_id,
                    run_id=evidence.run_id,
                    attempt_id=evidence.attempt_id,
                    space_id=evidence.candidate.space_id,
                    payload=_dump(_EVIDENCE, evidence.candidate),
                    resolution_status=evidence.resolution_status.value,
                    created_at=evidence.created_at,
                )
            )
        return evidence

    async def list_evidence(self, attempt_id: UUID) -> tuple[EvidenceRecord, ...]:
        async with self._database.session() as session:
            models = (
                await session.execute(
                    select(QAEvidenceModel).where(QAEvidenceModel.attempt_id == attempt_id)
                )
            ).scalars()
            return tuple(_evidence(model) for model in models)

    async def publish_terminal(
        self,
        *,
        run_id: UUID,
        result: QAResult,
        answer_message: MessageRecord,
        citations: tuple[CitationRecord, ...] = (),
    ) -> QARunRecord:
        async with self._database.transaction() as session:
            base, attempt, current = await self._locked_run(session, run_id)
            if current.status in _BUSINESS_TERMINAL:
                if (
                    current.result == result
                    and current.answer_message_id == answer_message.message_id
                ):
                    return current
                raise QAContractError("QA run already has a different terminal result")
            if current.status is not QAStatus.VERIFYING or current.cancellation_requested:
                raise QAContractError("QA run is not eligible for terminal publication")
            evidence = await self._evidence_for_attempt(session, current.attempt.attempt_id)
            _validate_publication(current, result, answer_message, citations, evidence)
            session.add(_message_model(answer_message))
            await session.flush()
            for record in citations:
                session.add(
                    QACitationModel(
                        run_id=record.run_id,
                        attempt_id=record.attempt_id,
                        message_id=record.message_id,
                        evidence_id=record.citation.evidence_id,
                        payload=_dump(_CITATION, record.citation),
                        created_at=record.created_at,
                    )
                )
            updated = replace(
                current,
                status=terminal_status_for_result(result),
                result=result,
                answer_message_id=answer_message.message_id,
                updated_at=datetime.now(UTC),
            )
            _project_attempt(attempt, updated)
            _project_run(base, updated)
            await self._project_parent(session, updated)
            _clear_lease(attempt)
            return updated

    async def list_citations(self, attempt_id: UUID) -> tuple[CitationRecord, ...]:
        async with self._database.session() as session:
            models = (
                await session.execute(
                    select(QACitationModel).where(QACitationModel.attempt_id == attempt_id)
                )
            ).scalars()
            return tuple(_citation(model) for model in models)

    async def submit_feedback(self, feedback: FeedbackRecord) -> FeedbackRecord:
        async with self._database.transaction() as session:
            existing = (
                await session.execute(
                    select(QAFeedbackModel).where(
                        QAFeedbackModel.run_id == feedback.run_id,
                        QAFeedbackModel.caller_id == feedback.caller_id,
                        QAFeedbackModel.idempotency_key == feedback.idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                record = _feedback(existing)
                if _same_feedback(record, feedback):
                    return record
                raise QAContractError("Feedback idempotency key has conflicting content")
            base, attempt, run = await self._locked_run(session, feedback.run_id)
            conversation = await session.get(ConversationModel, feedback.conversation_id)
            message = await session.get(QAMessageModel, feedback.message_id)
            if (
                conversation is None
                or conversation.space_id != feedback.space_id
                or conversation.owner_id != feedback.caller_id
                or run.answer_message_id != feedback.message_id
                or attempt.id != feedback.attempt_id
                or base.conversation_id != feedback.conversation_id
                or message is None
                or message.role != MessageRole.ASSISTANT.value
            ):
                raise QAContractError("Feedback target does not belong to the published QA result")
            session.add(
                QAFeedbackModel(
                    id=feedback.feedback_id,
                    conversation_id=feedback.conversation_id,
                    message_id=feedback.message_id,
                    run_id=feedback.run_id,
                    attempt_id=feedback.attempt_id,
                    space_id=feedback.space_id,
                    caller_id=feedback.caller_id,
                    idempotency_key=feedback.idempotency_key,
                    decision=feedback.decision.value,
                    note=feedback.note,
                    review_status=feedback.review_status.value,
                    created_at=feedback.created_at,
                )
            )
        return feedback

    async def get_feedback(self, feedback_id: UUID) -> FeedbackRecord | None:
        async with self._database.session() as session:
            model = await session.get(QAFeedbackModel, feedback_id)
            return _feedback(model) if model is not None else None

    async def list_feedback(
        self, space_id: UUID, review_status: FeedbackReviewStatus | None = None
    ) -> tuple[FeedbackRecord, ...]:
        async with self._database.session() as session:
            query = select(QAFeedbackModel).where(QAFeedbackModel.space_id == space_id)
            if review_status is not None:
                query = query.where(QAFeedbackModel.review_status == review_status.value)
            query = query.order_by(QAFeedbackModel.created_at, QAFeedbackModel.id)
            models = (await session.execute(query)).scalars()
            return tuple(_feedback(model) for model in models)

    async def review_feedback(self, review: FeedbackReviewRecord) -> FeedbackRecord:
        async with self._database.transaction() as session:
            model = await session.get(QAFeedbackModel, review.feedback_id, with_for_update=True)
            if model is None:
                raise QAContractError("Feedback not found")
            if model.space_id != review.space_id:
                raise QAContractError("Feedback does not belong to the requested Space")
            current = _feedback(model)
            evidence_rows = (
                await session.execute(
                    select(QAEvidenceModel.id).where(
                        QAEvidenceModel.attempt_id == current.attempt_id,
                        QAEvidenceModel.space_id == current.space_id,
                    )
                )
            ).scalars()
            evidence_ids = set(evidence_rows)
            if not set(review.approved_evidence_ids).issubset(evidence_ids):
                raise QAContractError("Feedback review Evidence does not belong to the QA attempt")
            if current.review_status is not FeedbackReviewStatus.PENDING_REVIEW:
                if (
                    current.review_status is review.review_status
                    and current.reviewer_id == review.reviewer_id
                    and current.authorization_confirmed == review.authorization_confirmed
                    and current.redaction_complete == review.redaction_complete
                    and current.expected_behavior == review.expected_behavior
                    and current.approved_evidence_ids == review.approved_evidence_ids
                    and current.gold_answer_sha256 == review.gold_answer_sha256
                    and current.rejection_reason == review.rejection_reason
                ):
                    return current
                raise QAContractError("Feedback has already been reviewed")
            model.review_status = review.review_status.value
            model.reviewer_id = review.reviewer_id
            model.reviewed_at = review.reviewed_at
            model.authorization_confirmed = review.authorization_confirmed
            model.redaction_complete = review.redaction_complete
            model.expected_behavior = review.expected_behavior
            model.approved_evidence_ids = [str(value) for value in review.approved_evidence_ids]
            model.gold_answer_sha256 = review.gold_answer_sha256
            model.rejection_reason = review.rejection_reason
            await session.flush()
            return _feedback(model)

    async def claim_run(
        self, run_id: UUID, *, lease_owner: str, lease_seconds: int
    ) -> QARunRecord | None:
        if not lease_owner or lease_seconds < 1:
            raise QAContractError("QA Worker lease requires an owner and positive duration")
        async with self._database.transaction() as session:
            base, attempt, current = await self._locked_run(session, run_id)
            if current.status in _TERMINAL:
                return current

            now = datetime.now(UTC)
            if _lease_is_active(attempt, now) and attempt.lease_owner != lease_owner:
                return None
            if current.status in {QAStatus.RUNNING, QAStatus.VERIFYING}:
                current = await self._reset_interrupted(session, base, attempt, current)
            if current.status not in {QAStatus.QUEUED, QAStatus.CANCEL_REQUESTED}:
                return None

            attempt.lease_owner = lease_owner
            attempt.heartbeat_at = now
            attempt.lease_expires_at = now + timedelta(seconds=lease_seconds)
            return current

    async def renew_run_lease(self, run_id: UUID, *, lease_owner: str, lease_seconds: int) -> bool:
        if lease_seconds < 1:
            return False
        async with self._database.transaction() as session:
            _base, attempt, current = await self._locked_run(session, run_id)
            if current.status in _TERMINAL or attempt.lease_owner != lease_owner:
                return False
            now = datetime.now(UTC)
            attempt.heartbeat_at = now
            attempt.lease_expires_at = now + timedelta(seconds=lease_seconds)
            return True

    async def release_run_lease(self, run_id: UUID, *, lease_owner: str) -> None:
        async with self._database.transaction() as session:
            _base, attempt, _current = await self._locked_run(session, run_id)
            if attempt.lease_owner == lease_owner:
                _clear_lease(attempt)

    async def prepare_recovery(self) -> tuple[UUID, ...]:
        """Return unleased queued work and reset expired interrupted attempts."""
        async with self._database.transaction() as session:
            bases = (
                await session.execute(
                    select(QARunModel)
                    .where(QARunModel.status.not_in(tuple(status.value for status in _TERMINAL)))
                    .with_for_update()
                )
            ).scalars()
            run_ids: list[UUID] = []
            now = datetime.now(UTC)
            for base in bases:
                attempt = await self._latest_attempt(session, base.id, for_update=True)
                if attempt is None:
                    continue
                status = QAStatus(attempt.status)
                if _lease_is_active(attempt, now):
                    continue
                current = _run(base, attempt)
                if status in {QAStatus.RUNNING, QAStatus.VERIFYING}:
                    current = await self._reset_interrupted(session, base, attempt, current)
                if current.status in {QAStatus.QUEUED, QAStatus.CANCEL_REQUESTED}:
                    _clear_lease(attempt)
                    run_ids.append(base.id)
            return tuple(run_ids)

    @staticmethod
    async def _reset_interrupted(
        session: AsyncSession,
        base: QARunModel,
        attempt: QARunAttemptModel,
        current: QARunRecord,
    ) -> QARunRecord:
        await session.execute(
            delete(QAEvidenceModel).where(QAEvidenceModel.attempt_id == attempt.id)
        )
        recovered = replace(
            current,
            status=QAStatus.QUEUED,
            cancellation_requested=False,
            error_code=None,
            usage=QARunUsage(),
            result=None,
            answer_message_id=None,
            updated_at=datetime.now(UTC),
        )
        _project_attempt(attempt, recovered)
        _project_run(base, recovered)
        parent = await session.get(ConversationRunModel, recovered.run_id, with_for_update=True)
        if parent is None:
            raise QAContractError("QA run is missing its ConversationRun parent")
        _project_conversation_run(parent, recovered)
        _clear_lease(attempt)
        return recovered

    @staticmethod
    async def _project_parent(session: AsyncSession, run: QARunRecord) -> None:
        parent = await session.get(ConversationRunModel, run.run_id, with_for_update=True)
        if parent is None:
            raise QAContractError("QA run is missing its ConversationRun parent")
        _project_conversation_run(parent, run)

    async def _latest_attempt(
        self, session: AsyncSession, run_id: UUID, *, for_update: bool = False
    ) -> QARunAttemptModel | None:
        statement = (
            select(QARunAttemptModel)
            .where(QARunAttemptModel.run_id == run_id)
            .order_by(desc(QARunAttemptModel.number))
            .limit(1)
        )
        if for_update:
            statement = statement.with_for_update()
        return (await session.execute(statement)).scalar_one_or_none()

    async def _locked_run(
        self, session: AsyncSession, run_id: UUID
    ) -> tuple[QARunModel, QARunAttemptModel, QARunRecord]:
        base = await session.get(QARunModel, run_id, with_for_update=True)
        if base is None:
            raise QAContractError("QA run does not exist")
        attempt = await self._latest_attempt(session, run_id, for_update=True)
        if attempt is None:
            raise QAContractError("QA run has no attempt")
        return base, attempt, _run(base, attempt)

    async def _evidence_for_attempt(
        self, session: AsyncSession, attempt_id: UUID
    ) -> tuple[EvidenceRecord, ...]:
        models = (
            await session.execute(
                select(QAEvidenceModel).where(QAEvidenceModel.attempt_id == attempt_id)
            )
        ).scalars()
        return tuple(_evidence(model) for model in models)

    @staticmethod
    def _validate_retry(existing: QARunRecord, requested: QARunRecord) -> None:
        if (
            requested.attempt.number != existing.attempt.number + 1
            or requested.attempt.previous_attempt_id != existing.attempt.attempt_id
            or existing.status not in {QAStatus.FAILED, QAStatus.TIMED_OUT}
            or (
                existing.conversation_id,
                existing.question_message_id,
                existing.space_id,
                existing.caller_id,
                existing.versions,
                existing.retrieval_scope,
            )
            != (
                requested.conversation_id,
                requested.question_message_id,
                requested.space_id,
                requested.caller_id,
                requested.versions,
                requested.retrieval_scope,
            )
        ):
            raise QAContractError("Retry does not follow the latest eligible QA attempt")


class PostgresQAEventStore:
    """Persist privacy-safe, monotonic SSE events under a run row lock."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def append(
        self, run_id: UUID, event_type: QAEventType, payload: Mapping[str, Any]
    ) -> QAStreamEvent:
        async with self._database.transaction() as session:
            run = await session.get(QARunModel, run_id, with_for_update=True)
            if run is None:
                raise QAContractError("QA event run does not exist")
            latest = (
                await session.execute(
                    select(QAEventModel)
                    .where(QAEventModel.run_id == run_id)
                    .order_by(desc(QAEventModel.sequence))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if latest is not None:
                latest_event = _stream_event(latest)
                if latest_event.terminal:
                    return latest_event
                if latest_event.event_type is event_type and latest_event.payload == payload:
                    return latest_event
            event = QAStreamEvent(
                run_id=run_id,
                sequence=1 if latest is None else latest.sequence + 1,
                event_type=event_type,
                payload=dict(payload),
            )
            session.add(
                QAEventModel(
                    id=event.event_id,
                    run_id=run_id,
                    sequence=event.sequence,
                    event_type=event.event_type.value,
                    payload=dict(event.payload),
                    created_at=event.occurred_at,
                )
            )
            return event

    async def replay(self, run_id: UUID, after_sequence: int = 0) -> tuple[QAStreamEvent, ...]:
        async with self._database.session() as session:
            models = (
                await session.execute(
                    select(QAEventModel)
                    .where(
                        QAEventModel.run_id == run_id,
                        QAEventModel.sequence > after_sequence,
                    )
                    .order_by(QAEventModel.sequence)
                )
            ).scalars()
            return tuple(_stream_event(model) for model in models)


def _conversation(model: ConversationModel) -> ConversationRecord:
    return ConversationRecord(
        conversation_id=model.id,
        space_id=model.space_id,
        owner_id=model.owner_id,
        workspace_path=model.workspace_path,
        always_allowed_tool_names=tuple(model.always_allowed_tool_names or ()),
        created_at=model.created_at,
        updated_at=model.updated_at,
        archived_at=model.archived_at,
        reasoning_effort=ReasoningEffort(model.reasoning_effort),
    )


def _message(model: QAMessageModel) -> MessageRecord:
    return MessageRecord(
        message_id=model.id,
        conversation_id=model.conversation_id,
        space_id=model.space_id,
        role=MessageRole(model.role),
        content=model.content,
        run_id=model.run_id,
        idempotency_key=model.idempotency_key,
        created_at=model.created_at,
    )


def _conversation_summary(model: ConversationSummaryModel) -> ConversationSummary:
    return ConversationSummary(
        summary_id=model.id,
        conversation_id=model.conversation_id,
        space_id=model.space_id,
        run_id=model.run_id,
        covered_start_message_id=model.covered_start_message_id,
        covered_end_message_id=model.covered_end_message_id,
        covered_message_count=model.covered_message_count,
        content=model.content,
        content_sha256=model.content_sha256,
        prompt_version=model.prompt_version,
        model_identity=model.model_identity,
        sensitivity=ConversationSensitivity(model.sensitivity),
        created_at=model.created_at,
    )


def _message_model(message: MessageRecord) -> QAMessageModel:
    return QAMessageModel(
        id=message.message_id,
        conversation_id=message.conversation_id,
        run_id=message.run_id,
        space_id=message.space_id,
        role=message.role.value,
        content=message.content,
        idempotency_key=message.idempotency_key,
        created_at=message.created_at,
    )


def _attempt_model(run: QARunRecord) -> QARunAttemptModel:
    return QARunAttemptModel(
        id=run.attempt.attempt_id,
        run_id=run.run_id,
        number=run.attempt.number,
        previous_attempt_id=run.attempt.previous_attempt_id,
        status=run.status.value,
        cancellation_requested=run.cancellation_requested,
        error_code=run.error_code,
        usage=_dump(_USAGE, run.usage),
        result=_dump(_RESULT, run.result) if run.result is not None else None,
        answer_message_id=run.answer_message_id,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _run(base: QARunModel, attempt: QARunAttemptModel) -> QARunRecord:
    return QARunRecord(
        run_id=base.id,
        attempt=QAAttempt(
            run_id=base.id,
            number=attempt.number,
            attempt_id=attempt.id,
            previous_attempt_id=attempt.previous_attempt_id,
        ),
        conversation_id=base.conversation_id,
        question_message_id=base.question_message_id,
        space_id=base.space_id,
        caller_id=base.caller_id,
        idempotency_key=base.idempotency_key,
        versions=_VERSIONS.validate_python(base.versions),
        retrieval_scope=_RETRIEVAL_SCOPE.validate_python(base.retrieval_scope),
        standalone_request=base.standalone_request,
        context_sensitivity=base.context_sensitivity,
        status=QAStatus(attempt.status),
        cancellation_requested=attempt.cancellation_requested,
        error_code=attempt.error_code,
        result=_RESULT.validate_python(attempt.result) if attempt.result is not None else None,
        answer_message_id=attempt.answer_message_id,
        usage=_USAGE.validate_python(attempt.usage),
        created_at=attempt.created_at,
        updated_at=attempt.updated_at,
    )


def _project_attempt(model: QARunAttemptModel, run: QARunRecord) -> None:
    model.status = run.status.value
    model.cancellation_requested = run.cancellation_requested
    model.error_code = run.error_code
    model.usage = _dump(_USAGE, run.usage)
    model.result = _dump(_RESULT, run.result) if run.result is not None else None
    model.answer_message_id = run.answer_message_id
    model.updated_at = run.updated_at


def _project_run(model: QARunModel, run: QARunRecord) -> None:
    model.status = run.status.value
    model.cancellation_requested = run.cancellation_requested
    model.error_code = run.error_code
    model.usage = _dump(_USAGE, run.usage)
    model.result = _dump(_RESULT, run.result) if run.result is not None else None
    model.answer_message_id = run.answer_message_id
    model.updated_at = run.updated_at


def _conversation_run_model(run: QARunRecord) -> ConversationRunModel:
    model = ConversationRunModel(
        id=run.run_id,
        conversation_id=run.conversation_id,
        space_id=run.space_id,
        caller_id=run.caller_id,
        user_message_id=run.question_message_id,
        idempotency_key=run.idempotency_key,
        run_kind=ConversationRunKind.SKILL.value,
        selection_source=ConversationRunSelectionSource.NONE.value,
        status=_conversation_run_status(run.status).value,
        cancellation_requested=run.cancellation_requested,
        error_code=run.error_code,
        router_version="assistant-agent-loop-v1",
        core_prompt_version="assistant-base-prompt-v7",
        model_identity=run.versions.model_identity,
        skill_name=(
            run.versions.skill_name if run.versions.skill_content_sha256 is not None else None
        ),
        skill_version=(
            run.versions.skill_version if run.versions.skill_content_sha256 is not None else None
        ),
        skill_content_sha256=run.versions.skill_content_sha256,
        usage=_conversation_run_usage(run),
        result=_conversation_run_result(run),
        created_at=run.created_at,
        updated_at=run.updated_at,
    )
    return model


def _validate_parent(
    model: ConversationRunModel, run: QARunRecord, *, check_idempotency: bool = False
) -> None:
    if (
        model.conversation_id != run.conversation_id
        or model.space_id != run.space_id
        or model.caller_id != run.caller_id
        or model.user_message_id != run.question_message_id
        or (check_idempotency and model.idempotency_key != run.idempotency_key)
        or model.router_version != "assistant-agent-loop-v1"
        or model.core_prompt_version != "assistant-base-prompt-v7"
    ):
        raise QAContractError("QA Run conflicts with its current ConversationRun parent")
    if model.run_kind == ConversationRunKind.ASSISTANT_TURN.value:
        valid = (
            model.skill_name is None
            and model.skill_version is None
            and model.skill_content_sha256 is None
            and run.versions.skill_name == "knowledge_agent"
        )
    else:
        valid = (
            model.run_kind == ConversationRunKind.SKILL.value
            and model.selection_source
            in {
                ConversationRunSelectionSource.COMMAND.value,
                ConversationRunSelectionSource.AUTO.value,
                ConversationRunSelectionSource.NONE.value,
            }
            and (
                (
                    run.versions.skill_content_sha256 is None
                    and model.skill_name is None
                    and model.skill_version is None
                    and model.skill_content_sha256 is None
                )
                or (
                    run.versions.skill_content_sha256 is not None
                    and model.skill_name == run.versions.skill_name
                    and model.skill_version == run.versions.skill_version
                    and model.skill_content_sha256 == run.versions.skill_content_sha256
                )
            )
        )
    if not valid:
        raise QAContractError("QA Run conflicts with its current ConversationRun parent")


def _project_conversation_run(model: ConversationRunModel, run: QARunRecord) -> None:
    _validate_parent(model, run, check_idempotency=False)
    hold_for_finalizer = (
        model.run_kind == ConversationRunKind.ASSISTANT_TURN.value
        and model.router_version == "assistant-agent-loop-v1"
        and model.core_prompt_version == "assistant-base-prompt-v7"
        and model.result is None
    )
    hold_for_finalizer = hold_for_finalizer and run.status in _BUSINESS_TERMINAL
    model.status = (
        ConversationRunStatus.RUNNING.value
        if hold_for_finalizer
        else _conversation_run_status(run.status).value
    )
    model.cancellation_requested = run.cancellation_requested
    model.error_code = run.error_code
    model.usage = _conversation_run_usage(run)
    model.result = None if hold_for_finalizer else _conversation_run_result(run)
    model.updated_at = run.updated_at


def _conversation_run_status(status: QAStatus) -> ConversationRunStatus:
    return {
        QAStatus.CREATED: ConversationRunStatus.CREATED,
        QAStatus.QUEUED: ConversationRunStatus.QUEUED,
        QAStatus.RUNNING: ConversationRunStatus.RUNNING,
        QAStatus.VERIFYING: ConversationRunStatus.RUNNING,
        QAStatus.COMPLETED: ConversationRunStatus.COMPLETED,
        QAStatus.REFUSED: ConversationRunStatus.REFUSED,
        QAStatus.FAILED: ConversationRunStatus.FAILED,
        QAStatus.CANCEL_REQUESTED: ConversationRunStatus.CANCEL_REQUESTED,
        QAStatus.CANCELLED: ConversationRunStatus.CANCELLED,
        QAStatus.TIMED_OUT: ConversationRunStatus.TIMED_OUT,
    }[status]


def _conversation_run_usage(run: QARunRecord) -> dict[str, object]:
    return {
        "input_tokens": run.usage.input_tokens,
        "output_tokens": run.usage.output_tokens,
        "model_latency_ms": run.usage.model_latency_ms,
    }


def _conversation_run_result(run: QARunRecord) -> dict[str, object] | None:
    if run.status not in _BUSINESS_TERMINAL:
        return None
    return {
        "kind": "skill_result",
        "message_id": str(run.answer_message_id) if run.answer_message_id is not None else None,
        "clarification": None,
    }


def _lease_is_active(model: QARunAttemptModel, now: datetime) -> bool:
    return (
        model.lease_owner is not None
        and model.lease_expires_at is not None
        and model.lease_expires_at > now
    )


def _clear_lease(model: QARunAttemptModel) -> None:
    model.lease_owner = None
    model.lease_expires_at = None
    model.heartbeat_at = None


def _evidence(model: QAEvidenceModel) -> EvidenceRecord:
    return EvidenceRecord(
        run_id=model.run_id,
        attempt_id=model.attempt_id,
        candidate=_EVIDENCE.validate_python(model.payload),
        resolution_status=CitationStatus(model.resolution_status),
        created_at=model.created_at,
    )


def _citation(model: QACitationModel) -> CitationRecord:
    return CitationRecord(
        run_id=model.run_id,
        attempt_id=model.attempt_id,
        message_id=model.message_id,
        citation=_CITATION.validate_python(model.payload),
        created_at=model.created_at,
    )


def _feedback(model: QAFeedbackModel) -> FeedbackRecord:
    return FeedbackRecord(
        feedback_id=model.id,
        conversation_id=model.conversation_id,
        message_id=model.message_id,
        run_id=model.run_id,
        attempt_id=model.attempt_id,
        space_id=model.space_id,
        caller_id=model.caller_id,
        idempotency_key=model.idempotency_key,
        decision=FeedbackDecision(model.decision),
        note=model.note,
        review_status=FeedbackReviewStatus(model.review_status),
        reviewer_id=model.reviewer_id,
        reviewed_at=model.reviewed_at,
        authorization_confirmed=model.authorization_confirmed,
        redaction_complete=model.redaction_complete,
        expected_behavior=model.expected_behavior,
        approved_evidence_ids=tuple(
            UUID(str(value)) for value in (model.approved_evidence_ids or [])
        ),
        gold_answer_sha256=model.gold_answer_sha256,
        rejection_reason=model.rejection_reason,
        created_at=model.created_at,
    )


def _stream_event(model: QAEventModel) -> QAStreamEvent:
    return QAStreamEvent(
        event_id=model.id,
        run_id=model.run_id,
        sequence=model.sequence,
        event_type=QAEventType(model.event_type),
        payload=model.payload,
        occurred_at=model.created_at,
    )


def _same_message(existing: MessageRecord, requested: MessageRecord) -> bool:
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


def _same_run(existing: QARunRecord, requested: QARunRecord) -> bool:
    return (
        existing.conversation_id,
        existing.question_message_id,
        existing.space_id,
        existing.caller_id,
        existing.idempotency_key,
        existing.versions,
        existing.retrieval_scope,
    ) == (
        requested.conversation_id,
        requested.question_message_id,
        requested.space_id,
        requested.caller_id,
        requested.idempotency_key,
        requested.versions,
        requested.retrieval_scope,
    )


def _same_feedback(existing: FeedbackRecord, requested: FeedbackRecord) -> bool:
    return _same_feedback_command(existing, requested)


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


def _validate_publication(
    run: QARunRecord,
    result: QAResult,
    message: MessageRecord,
    citations: tuple[CitationRecord, ...],
    evidence: tuple[EvidenceRecord, ...],
) -> None:
    if (
        message.role is not MessageRole.ASSISTANT
        or message.run_id != run.run_id
        or message.conversation_id != run.conversation_id
        or message.space_id != run.space_id
        or message.content != _result_content(result)
    ):
        raise QAContractError("Terminal message does not belong to the QA result")
    if any(item.resolution_status is not CitationStatus.VALID for item in evidence):
        raise QAContractError("Terminal publication contains unavailable Evidence")
    evidence_by_id = {item.candidate.evidence_id: item for item in evidence}
    if result.outcome is QAOutcome.ANSWER:
        assert result.answer is not None
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
        sources = {
            evidence_by_id[evidence_id].candidate.source_id
            for evidence_id in result.conflict.evidence_ids
            if evidence_id in evidence_by_id
        }
        if len(sources) < 2:
            raise QAContractError("Conflict must preserve Evidence from at least two sources")
    for citation in citations:
        if (
            citation.run_id != run.run_id
            or citation.attempt_id != run.attempt.attempt_id
            or citation.message_id != message.message_id
            or citation.citation.status is not CitationStatus.VALID
            or citation.citation.evidence_id not in evidence_by_id
        ):
            raise QAContractError("Citation record does not belong to the terminal publication")


def _result_content(result: QAResult) -> str:
    if result.answer is not None:
        return result.answer.text
    if result.refusal is not None:
        return result.refusal.message
    if result.conflict is not None:
        return result.conflict.message
    raise QAContractError("Infrastructure failures cannot create assistant messages")


__all__ = ["PostgresGroundedQARepository", "PostgresQAEventStore"]
