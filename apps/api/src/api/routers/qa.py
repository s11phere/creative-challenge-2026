"""Provisional Grounded QA HTTP/SSE surface backed by PostgreSQL and Worker execution."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from application.skills import OrganizationScopeError
from domain.agent_runtime import AgentRunContext, ToolCallRecord, ToolPermission
from domain.grounded_qa import CitationStatus, QAAttempt, QAEvent, QAStatus, normalize_question
from domain.qa_persistence import (
    ConversationRecord,
    FeedbackDecision,
    FeedbackRecord,
    FeedbackReviewRecord,
    FeedbackReviewStatus,
    GroundedQARepository,
    MessageRecord,
    MessageRole,
    QARetrievalScope,
    QARunRecord,
)
from domain.qa_sse import QAEventStore, QAEventType
from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from ..errors import AppError

router = APIRouter(prefix="/api/v1")


class ConversationCreate(BaseModel):
    owner_id: str = Field(default="local", min_length=1, max_length=128)


class ConversationResponse(BaseModel):
    conversation_id: UUID
    space_id: UUID
    owner_id: str
    workspace_path: str | None = None


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=12000)
    idempotency_key: str = Field(min_length=1, max_length=200)


class RunCreateRequest(BaseModel):
    """Generic Run facade for every server-registered knowledge Skill."""

    skill_name: Literal[
        "knowledge_agent",
        "summarize_document",
        "compare_sources",
        "create_review_cards",
    ] = "knowledge_agent"
    conversation_id: UUID
    question: str | None = Field(default=None, max_length=12000)
    document_id: UUID | None = None
    version_id: UUID | None = None
    source_ids: list[UUID] | None = Field(default=None, min_length=2, max_length=8)
    focus: str | None = Field(default=None, min_length=1, max_length=1000)
    idempotency_key: str = Field(min_length=1, max_length=200)

    @field_validator("source_ids")
    @classmethod
    def unique_sources(cls, value: list[UUID] | None) -> list[UUID] | None:
        if value is not None and len(value) != len(set(value)):
            raise ValueError("source_ids must be unique")
        return value

    @model_validator(mode="after")
    def validate_skill_input(self) -> RunCreateRequest:
        if self.skill_name == "knowledge_agent":
            if not self.question or not self.question.strip():
                raise ValueError("question is required for this Skill")
        elif self.skill_name in {"summarize_document", "create_review_cards"}:
            if self.document_id is None or self.version_id is None:
                raise ValueError("document_id and version_id are required for this Skill")
        elif self.source_ids is None:
            raise ValueError("source_ids are required for compare_sources")
        return self


class AnswerResultResponse(BaseModel):
    type: Literal["answer"] = "answer"
    text: str
    limitations: list[str] = Field(default_factory=list)


class RefusalResultResponse(BaseModel):
    type: Literal["refusal"] = "refusal"
    code: str
    message: str


class ConflictResultResponse(BaseModel):
    type: Literal["conflict"] = "conflict"
    message: str
    evidence_ids: list[UUID]


class CitationLocatorResponse(BaseModel):
    kind: str
    start: int
    end: int


class CitationResponse(BaseModel):
    evidence_id: UUID
    source_id: UUID
    document_id: UUID
    version_id: UUID
    chunk_id: UUID
    locator: CitationLocatorResponse


class CitationExcerptResponse(CitationResponse):
    status: CitationStatus
    excerpt: str | None = None


class RunSkillResponse(BaseModel):
    name: str
    version: str
    content_sha256: str | None


class RunScopeResponse(BaseModel):
    source_ids: list[UUID]
    document_ids: list[UUID]
    version_ids: list[UUID]


class RunWriteResponse(BaseModel):
    status: Literal["blocked", "persisted"]
    code: str | None = None
    side_effects: Literal[0, 1]


class RunResponse(BaseModel):
    run_id: UUID
    attempt_id: UUID
    status: str
    conversation_id: UUID
    question_message_id: UUID
    cancellation_requested: bool
    error_code: str | None = None
    skill: RunSkillResponse
    fixed_scope: RunScopeResponse
    write: RunWriteResponse | None = None
    result: AnswerResultResponse | RefusalResultResponse | ConflictResultResponse | None = None
    citations: list[CitationResponse] = Field(default_factory=list)


class ConversationMessageResponse(BaseModel):
    message_id: UUID
    role: MessageRole
    content: str
    run_id: UUID | None
    created_at: datetime


class ConversationHistoryItem(ConversationResponse):
    created_at: datetime
    updated_at: datetime
    messages: list[ConversationMessageResponse]
    runs: list[RunResponse]


class ConversationHistoryResponse(BaseModel):
    conversations: list[ConversationHistoryItem]


class ConversationDeleteResponse(BaseModel):
    conversation_id: UUID
    status: Literal["deleted", "already_deleted"]


class FeedbackRequest(BaseModel):
    decision: FeedbackDecision
    idempotency_key: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, min_length=1, max_length=2000)


class FeedbackResponse(BaseModel):
    feedback_id: UUID
    run_id: UUID
    message_id: UUID
    review_status: str
    space_id: UUID
    decision: FeedbackDecision
    created_at: datetime
    reviewer_id: str | None = None
    reviewed_at: datetime | None = None
    authorization_confirmed: bool = False
    redaction_complete: bool = False
    expected_behavior: str | None = None
    approved_evidence_ids: list[UUID] = Field(default_factory=list)
    gold_answer_sha256: str | None = None
    rejection_reason: str | None = None


class FeedbackReviewRequest(BaseModel):
    review_status: Literal["accepted", "rejected"]
    reviewer_id: str = Field(min_length=1, max_length=255)
    authorization_confirmed: bool = False
    redaction_complete: bool = False
    expected_behavior: Literal["answer", "refuse"] | None = None
    approved_evidence_ids: list[UUID] = Field(default_factory=list, max_length=100)
    gold_answer_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    rejection_reason: str | None = Field(default=None, min_length=1, max_length=2000)

    @field_validator("gold_answer_sha256")
    @classmethod
    def validate_digest(cls, value: str | None) -> str | None:
        if value is not None and any(char not in "0123456789abcdef" for char in value):
            raise ValueError("gold_answer_sha256 must be lowercase hexadecimal")
        return value


class ApprovalRequest(BaseModel):
    tool_name: Literal["write_review_cards"]
    tool_version: Literal["1.0.0"]
    idempotency_key: str = Field(min_length=1, max_length=200)


class ApprovalDecisionRequest(BaseModel):
    approved: bool
    decided_by: str = Field(min_length=1, max_length=255)
    expires_at: datetime | None = None


class ApprovalResponse(BaseModel):
    approval_id: UUID
    run_id: UUID
    status: Literal["pending", "approved", "rejected", "revoked", "expired"]
    side_effects: Literal[0, 1] = 0
    derived_knowledge_id: UUID | None = None


class DerivedKnowledgeResponse(BaseModel):
    item_id: UUID
    run_id: UUID
    space_id: UUID
    kind: str
    status: Literal["active", "revoked"]
    citation_ids: list[str]
    created_by: str
    created_at: datetime
    revoked_at: datetime | None = None
    revoked_by: str | None = None


class RevokeDerivedKnowledgeRequest(BaseModel):
    revoked_by: str = Field(min_length=1, max_length=255)


class ApprovalRecordResponse(BaseModel):
    approval_id: UUID
    run_id: UUID
    status: Literal["pending", "approved", "rejected", "revoked", "expired"]
    tool_name: str
    tool_version: str
    requested_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_by: str | None = None


class DocumentSkillRequest(BaseModel):
    document_id: UUID
    version_id: UUID
    focus: str | None = Field(default=None, min_length=1, max_length=1000)
    idempotency_key: str = Field(min_length=1, max_length=200)


class CompareSourcesRequest(BaseModel):
    source_ids: list[UUID] = Field(min_length=2, max_length=8)
    focus: str | None = Field(default=None, min_length=1, max_length=1000)
    idempotency_key: str = Field(min_length=1, max_length=200)

    @field_validator("source_ids")
    @classmethod
    def unique_sources(cls, value: list[UUID]) -> list[UUID]:
        if len(value) != len(set(value)):
            raise ValueError("source_ids must be unique")
        return value


def _state(request: Request) -> tuple[GroundedQARepository, QAEventStore]:
    return request.app.state.qa_repository, request.app.state.qa_event_log


def _run_response(run: QARunRecord) -> RunResponse:
    return RunResponse(
        run_id=run.run_id,
        attempt_id=run.attempt.attempt_id,
        status=run.status.value,
        conversation_id=run.conversation_id,
        question_message_id=run.question_message_id,
        cancellation_requested=run.cancellation_requested,
        error_code=run.error_code,
        skill=RunSkillResponse(
            name=run.versions.skill_name,
            version=run.versions.skill_version,
            content_sha256=run.versions.skill_content_sha256,
        ),
        fixed_scope=RunScopeResponse(
            source_ids=sorted(run.retrieval_scope.source_ids, key=str),
            document_ids=sorted(run.retrieval_scope.document_ids, key=str),
            version_ids=sorted(run.retrieval_scope.version_ids, key=str),
        ),
        write=(
            RunWriteResponse(
                status="blocked",
                code="SKILL_WRITE_REQUIRES_APPROVAL",
                side_effects=0,
            )
            if run.versions.skill_name == "create_review_cards"
            else None
        ),
        result=_result_payload(run),
        citations=_citation_payloads(run),
    )


async def _run_response_with_write(run: QARunRecord, request: Request) -> RunResponse:
    response = _run_response(run)
    if (
        run.versions.skill_name == "create_review_cards"
        and getattr(request.app.state.qa_repository, "_database", None) is not None
    ):
        item = await request.app.state.derived_knowledge_store.get(run.run_id)
        if item is not None:
            response.write = RunWriteResponse(
                status="persisted" if item.status == "active" else "blocked",
                code=None if item.status == "active" else "DERIVED_KNOWLEDGE_REVOKED",
                side_effects=1 if item.status == "active" else 0,
            )
    return response


def _approval_response(record: object, *, run_id: UUID) -> ApprovalRecordResponse:
    status = record.status  # type: ignore[attr-defined]
    expires_at = record.expires_at  # type: ignore[attr-defined]
    if status == "approved" and expires_at is not None and expires_at <= datetime.now(UTC):
        status = "expired"
    return ApprovalRecordResponse(
        approval_id=record.approval_id,  # type: ignore[attr-defined]
        run_id=run_id,
        status=status,
        tool_name=record.tool_name,  # type: ignore[attr-defined]
        tool_version=record.tool_version,  # type: ignore[attr-defined]
        requested_at=record.requested_at,  # type: ignore[attr-defined]
        decided_at=record.decided_at,  # type: ignore[attr-defined]
        decided_by=record.decided_by,  # type: ignore[attr-defined]
        expires_at=expires_at,
        revoked_at=record.revoked_at,  # type: ignore[attr-defined]
        revoked_by=record.revoked_by,  # type: ignore[attr-defined]
    )


def _derived_response(record: object) -> DerivedKnowledgeResponse:
    return DerivedKnowledgeResponse(
        item_id=record.id,  # type: ignore[attr-defined]
        run_id=record.run_id,  # type: ignore[attr-defined]
        space_id=record.space_id,  # type: ignore[attr-defined]
        kind=record.kind,  # type: ignore[attr-defined]
        status=record.status,  # type: ignore[attr-defined]
        citation_ids=list(record.citation_ids),  # type: ignore[attr-defined]
        created_by=record.created_by,  # type: ignore[attr-defined]
        created_at=record.created_at,  # type: ignore[attr-defined]
        revoked_at=record.revoked_at,  # type: ignore[attr-defined]
        revoked_by=record.revoked_by,  # type: ignore[attr-defined]
    )


def _feedback_response(feedback: FeedbackRecord) -> FeedbackResponse:
    """Expose feedback review metadata without question, answer, note, or excerpts."""
    return FeedbackResponse(
        feedback_id=feedback.feedback_id,
        run_id=feedback.run_id,
        message_id=feedback.message_id,
        review_status=feedback.review_status.value,
        space_id=feedback.space_id,
        decision=feedback.decision,
        created_at=feedback.created_at,
        reviewer_id=feedback.reviewer_id,
        reviewed_at=feedback.reviewed_at,
        authorization_confirmed=feedback.authorization_confirmed,
        redaction_complete=feedback.redaction_complete,
        expected_behavior=feedback.expected_behavior,
        approved_evidence_ids=list(feedback.approved_evidence_ids),
        gold_answer_sha256=feedback.gold_answer_sha256,
        rejection_reason=feedback.rejection_reason,
    )


def _result_payload(
    run: QARunRecord,
) -> AnswerResultResponse | RefusalResultResponse | ConflictResultResponse | None:
    result = run.result
    if result is None:
        return None
    if result.answer is not None:
        return AnswerResultResponse(
            text=result.answer.text,
            limitations=list(result.answer.limitations),
        )
    if result.refusal is not None:
        return RefusalResultResponse(
            code=result.refusal.code.value,
            message=result.refusal.message,
        )
    if result.conflict is not None:
        return ConflictResultResponse(
            message=result.conflict.message,
            evidence_ids=list(result.conflict.evidence_ids),
        )
    return None


def _citation_payloads(run: QARunRecord) -> list[CitationResponse]:
    if run.result is None or run.result.answer is None:
        return []
    return [
        CitationResponse(
            evidence_id=citation.evidence_id,
            source_id=citation.source_id,
            document_id=citation.document_id,
            version_id=citation.version_id,
            chunk_id=citation.chunk_id,
            locator=CitationLocatorResponse(
                kind=citation.locator.kind.value,
                start=citation.locator.start,
                end=citation.locator.end,
            ),
        )
        for citation in run.result.answer.citations
    ]


@router.post(
    "/spaces/{space_id}/conversations", response_model=ConversationResponse, status_code=201
)
async def create_conversation(
    space_id: UUID, body: ConversationCreate, request: Request
) -> ConversationResponse:
    repo, _ = _state(request)
    record = await repo.create_conversation(
        ConversationRecord(space_id=space_id, owner_id=body.owner_id)
    )
    return ConversationResponse(
        conversation_id=record.conversation_id,
        space_id=record.space_id,
        owner_id=record.owner_id,
        workspace_path=record.workspace_path,
    )


@router.get(
    "/spaces/{space_id}/conversations",
    response_model=ConversationHistoryResponse,
)
async def list_conversations(
    space_id: UUID,
    request: Request,
    owner_id: str = Query(default="local", min_length=1, max_length=128),
) -> ConversationHistoryResponse:
    repo, _ = _state(request)
    conversations = await repo.list_conversations(space_id, owner_id)
    history: list[ConversationHistoryItem] = []
    for conversation in conversations:
        messages = await repo.list_messages(conversation.conversation_id)
        runs = await repo.list_runs(conversation.conversation_id)
        last_activity = max(
            [
                conversation.updated_at,
                *(message.created_at for message in messages),
                *(run.updated_at for run in runs),
            ]
        )
        history.append(
            ConversationHistoryItem(
                conversation_id=conversation.conversation_id,
                space_id=conversation.space_id,
                owner_id=conversation.owner_id,
                workspace_path=conversation.workspace_path,
                created_at=conversation.created_at,
                updated_at=last_activity,
                messages=[
                    ConversationMessageResponse(
                        message_id=message.message_id,
                        role=message.role,
                        content=message.content,
                        run_id=message.run_id,
                        created_at=message.created_at,
                    )
                    for message in messages
                ],
                runs=[_run_response(run) for run in runs],
            )
        )
    history.sort(key=lambda item: (item.updated_at, str(item.conversation_id)), reverse=True)
    return ConversationHistoryResponse(conversations=history)


@router.delete(
    "/spaces/{space_id}/conversations/{conversation_id}",
    response_model=ConversationDeleteResponse,
)
async def delete_conversation(
    space_id: UUID,
    conversation_id: UUID,
    request: Request,
    owner_id: str = Query(default="local", min_length=1, max_length=128),
) -> ConversationDeleteResponse:
    repo, _ = _state(request)
    conversation = await repo.get_conversation(conversation_id)
    if (
        conversation is None
        or conversation.space_id != space_id
        or conversation.owner_id != owner_id
    ):
        raise HTTPException(status_code=404, detail="Conversation not found")
    already_deleted = conversation.archived_at is not None
    await repo.archive_conversation(conversation_id)
    return ConversationDeleteResponse(
        conversation_id=conversation_id,
        status="already_deleted" if already_deleted else "deleted",
    )


@router.post(
    "/conversations/{conversation_id}/questions", response_model=RunResponse, status_code=202
)
async def submit_question(
    conversation_id: UUID, body: QuestionRequest, request: Request
) -> RunResponse:
    repo, events = _state(request)
    conversation = await repo.get_conversation(conversation_id)
    if conversation is None or conversation.archived_at is not None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    try:
        question = normalize_question(body.question)
        message = await repo.append_message(
            MessageRecord(
                conversation_id=conversation_id,
                space_id=conversation.space_id,
                role=MessageRole.USER,
                content=question,
                idempotency_key=body.idempotency_key,
            )
        )
        run_id = uuid4()
        run = QARunRecord(
            run_id=run_id,
            attempt=QAAttempt(run_id=run_id),
            conversation_id=conversation_id,
            question_message_id=message.message_id,
            space_id=conversation.space_id,
            caller_id=conversation.owner_id,
            idempotency_key=body.idempotency_key,
            versions=await request.app.state.qa_runtime.current_versions(),
        )
        run = await repo.create_run(run)
        if run.status is QAStatus.CREATED:
            run = await repo.transition_run(run.run_id, QAEvent.QUEUE)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await events.append(run.run_id, QAEventType.ACCEPTED, {"status": QAStatus.QUEUED.value})
    if request.app.state.qa_execution_enabled:
        request.app.state.qa_runtime.start(run.run_id)
    return _run_response(run)


@router.post("/runs", response_model=RunResponse, status_code=202)
async def create_run(body: RunCreateRequest, request: Request) -> RunResponse:
    """Create a Run through the same QA Application used by every Skill entry point."""
    conversation = await _conversation(request, body.conversation_id)
    if body.skill_name in {"summarize_document", "create_review_cards"}:
        assert body.document_id is not None and body.version_id is not None
        try:
            scope = await request.app.state.organization_scope.document_scope(
                space_id=conversation.space_id,
                document_id=body.document_id,
                version_id=body.version_id,
            )
        except OrganizationScopeError as exc:
            raise AppError(exc.code.value, str(exc), 409) from exc
        prefix = (
            "Summarize the selected fixed document version."
            if body.skill_name == "summarize_document"
            else "Create a citation-backed review-card preview from the selected version."
        )
        focus = f" Focus on: {body.focus}" if body.focus else ""
        return await _submit_scoped_skill(
            request,
            conversation,
            skill_name=body.skill_name,
            question=f"{prefix}{focus}",
            idempotency_key=body.idempotency_key,
            scope=scope,
        )
    if body.skill_name == "compare_sources":
        assert body.source_ids is not None
        try:
            scope = await request.app.state.organization_scope.sources_scope(
                space_id=conversation.space_id,
                source_ids=frozenset(body.source_ids),
            )
        except OrganizationScopeError as exc:
            raise AppError(exc.code.value, str(exc), 409) from exc
        focus = f" Focus on: {body.focus}" if body.focus else ""
        return await _submit_scoped_skill(
            request,
            conversation,
            skill_name=body.skill_name,
            question=(
                "Compare the selected fixed sources and distinguish agreement, conflict, and gaps."
                f"{focus}"
            ),
            idempotency_key=body.idempotency_key,
            scope=scope,
        )
    return await _submit_scoped_skill(
        request,
        conversation,
        skill_name=body.skill_name,
        question=normalize_question(body.question or ""),
        idempotency_key=body.idempotency_key,
        scope=QARetrievalScope(),
    )


@router.post(
    "/conversations/{conversation_id}/skills/knowledge_agent/runs",
    response_model=RunResponse,
    status_code=202,
)
async def run_knowledge_agent(
    conversation_id: UUID, body: QuestionRequest, request: Request
) -> RunResponse:
    conversation = await _conversation(request, conversation_id)
    return await _submit_scoped_skill(
        request,
        conversation,
        skill_name="knowledge_agent",
        question=normalize_question(body.question),
        idempotency_key=body.idempotency_key,
        scope=QARetrievalScope(),
    )


@router.post(
    "/conversations/{conversation_id}/skills/summarize_document/runs",
    response_model=RunResponse,
    status_code=202,
)
async def summarize_document(
    conversation_id: UUID, body: DocumentSkillRequest, request: Request
) -> RunResponse:
    conversation = await _conversation(request, conversation_id)
    try:
        scope = await request.app.state.organization_scope.document_scope(
            space_id=conversation.space_id,
            document_id=body.document_id,
            version_id=body.version_id,
        )
    except OrganizationScopeError as exc:
        raise AppError(exc.code.value, str(exc), 409) from exc
    focus = f" Focus on: {body.focus}" if body.focus else ""
    return await _submit_scoped_skill(
        request,
        conversation,
        skill_name="summarize_document",
        question=f"Summarize the selected fixed document version.{focus}",
        idempotency_key=body.idempotency_key,
        scope=scope,
    )


@router.post(
    "/conversations/{conversation_id}/skills/compare_sources/runs",
    response_model=RunResponse,
    status_code=202,
)
async def compare_sources(
    conversation_id: UUID, body: CompareSourcesRequest, request: Request
) -> RunResponse:
    conversation = await _conversation(request, conversation_id)
    try:
        scope = await request.app.state.organization_scope.sources_scope(
            space_id=conversation.space_id,
            source_ids=frozenset(body.source_ids),
        )
    except OrganizationScopeError as exc:
        raise AppError(exc.code.value, str(exc), 409) from exc
    focus = f" Focus on: {body.focus}" if body.focus else ""
    return await _submit_scoped_skill(
        request,
        conversation,
        skill_name="compare_sources",
        question=(
            "Compare the selected fixed sources and distinguish agreement, conflict, and gaps."
            f"{focus}"
        ),
        idempotency_key=body.idempotency_key,
        scope=scope,
    )


@router.post(
    "/conversations/{conversation_id}/skills/create_review_cards/runs",
    response_model=RunResponse,
    status_code=202,
)
async def create_review_cards(
    conversation_id: UUID, body: DocumentSkillRequest, request: Request
) -> RunResponse:
    conversation = await _conversation(request, conversation_id)
    try:
        scope = await request.app.state.organization_scope.document_scope(
            space_id=conversation.space_id,
            document_id=body.document_id,
            version_id=body.version_id,
        )
    except OrganizationScopeError as exc:
        raise AppError(exc.code.value, str(exc), 409) from exc
    focus = f" Focus on: {body.focus}" if body.focus else ""
    return await _submit_scoped_skill(
        request,
        conversation,
        skill_name="create_review_cards",
        question=f"Create a citation-backed review-card preview from the selected version.{focus}",
        idempotency_key=body.idempotency_key,
        scope=scope,
    )


async def _conversation(request: Request, conversation_id: UUID) -> ConversationRecord:
    repo, _events = _state(request)
    conversation = await repo.get_conversation(conversation_id)
    if conversation is None or conversation.archived_at is not None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


async def _submit_scoped_skill(
    request: Request,
    conversation: ConversationRecord,
    *,
    skill_name: str,
    question: str,
    idempotency_key: str,
    scope: QARetrievalScope,
) -> RunResponse:
    repo, events = _state(request)
    try:
        message = await repo.append_message(
            MessageRecord(
                conversation_id=conversation.conversation_id,
                space_id=conversation.space_id,
                role=MessageRole.USER,
                content=question,
                idempotency_key=idempotency_key,
            )
        )
        run_id = uuid4()
        run = QARunRecord(
            run_id=run_id,
            attempt=QAAttempt(run_id=run_id),
            conversation_id=conversation.conversation_id,
            question_message_id=message.message_id,
            space_id=conversation.space_id,
            caller_id=conversation.owner_id,
            idempotency_key=idempotency_key,
            versions=await request.app.state.qa_runtime.current_versions(skill_name),
            retrieval_scope=scope,
        )
        run = await repo.create_run(run)
        if run.status is QAStatus.CREATED:
            run = await repo.transition_run(run.run_id, QAEvent.QUEUE)
    except AppError:
        raise
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await events.append(run.run_id, QAEventType.ACCEPTED, {"status": QAStatus.QUEUED.value})
    if request.app.state.qa_execution_enabled:
        request.app.state.qa_runtime.start(run.run_id)
    return _run_response(run)


@router.get("/qa/runs/{run_id}", response_model=RunResponse)
async def get_run(run_id: UUID, request: Request) -> RunResponse:
    repo, _ = _state(request)
    run = await repo.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return await _run_response_with_write(run, request)


@router.get("/runs/{run_id}", response_model=RunResponse)
async def get_generic_run(run_id: UUID, request: Request) -> RunResponse:
    return await get_run(run_id, request)


@router.get(
    "/qa/runs/{run_id}/citations/{evidence_id}",
    response_model=CitationExcerptResponse,
)
async def resolve_citation(
    run_id: UUID,
    evidence_id: UUID,
    request: Request,
) -> CitationExcerptResponse:
    resolution = await request.app.state.qa_citation_service.resolve(run_id, evidence_id)
    if resolution is None:
        raise HTTPException(status_code=404, detail="Citation not found")
    citation = resolution.citation
    return CitationExcerptResponse(
        evidence_id=citation.evidence_id,
        source_id=citation.source_id,
        document_id=citation.document_id,
        version_id=citation.version_id,
        chunk_id=citation.chunk_id,
        locator=CitationLocatorResponse(
            kind=citation.locator.kind.value,
            start=citation.locator.start,
            end=citation.locator.end,
        ),
        status=resolution.status,
        excerpt=resolution.excerpt,
    )


@router.get(
    "/runs/{run_id}/citations/{evidence_id}",
    response_model=CitationExcerptResponse,
)
async def resolve_generic_citation(
    run_id: UUID,
    evidence_id: UUID,
    request: Request,
) -> CitationExcerptResponse:
    return await resolve_citation(run_id, evidence_id, request)


@router.post("/qa/runs/{run_id}/cancel", response_model=RunResponse)
async def cancel_run(run_id: UUID, request: Request) -> RunResponse:
    repo, events = _state(request)
    try:
        run = await repo.request_cancel(run_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc
    await events.append(run_id, QAEventType.CANCEL_REQUESTED, {"status": run.status.value})
    return await _run_response_with_write(run, request)


@router.post("/runs/{run_id}/cancel", response_model=RunResponse)
async def cancel_generic_run(run_id: UUID, request: Request) -> RunResponse:
    return await cancel_run(run_id, request)


@router.post("/qa/runs/{run_id}/approvals", response_model=ApprovalResponse, status_code=201)
async def request_approval(
    run_id: UUID, body: ApprovalRequest, request: Request
) -> ApprovalResponse:
    repo, _events = _state(request)
    run = await repo.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.versions.skill_name != "create_review_cards":
        raise HTTPException(status_code=409, detail="This Run has no writable Skill action")
    context = AgentRunContext(
        run_id=run.run_id,
        space_id=run.space_id,
        skill_name=run.versions.skill_name,
        skill_version=run.versions.skill_version,
        skill_content_sha256=run.versions.skill_content_sha256 or "",
        trace_id="api-approval",
        caller_id=run.caller_id,
        granted_permissions=frozenset({ToolPermission.WRITE_KNOWLEDGE}),
    )
    approval_id = await request.app.state.approval_port.request(
        context,
        ToolCallRecord(
            tool_name=body.tool_name,
            tool_version=body.tool_version,
            permissions=frozenset({ToolPermission.WRITE_KNOWLEDGE}),
            idempotency_key=body.idempotency_key,
        ),
    )
    record = await request.app.state.approval_port.get(approval_id, run_id=run_id)
    if record is None:
        raise HTTPException(status_code=409, detail="Approval was not persisted")
    return ApprovalResponse(approval_id=record.approval_id, run_id=run_id, status=record.status)


@router.post("/runs/{run_id}/approvals", response_model=ApprovalResponse, status_code=201)
async def request_generic_approval(
    run_id: UUID, body: ApprovalRequest, request: Request
) -> ApprovalResponse:
    return await request_approval(run_id, body, request)


@router.get(
    "/qa/runs/{run_id}/approvals",
    response_model=list[ApprovalRecordResponse],
)
async def list_approvals(run_id: UUID, request: Request) -> list[ApprovalRecordResponse]:
    run = await request.app.state.qa_repository.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    records = await request.app.state.approval_port.list_for_run(run_id)
    return [_approval_response(record, run_id=run_id) for record in records]


@router.get(
    "/qa/runs/{run_id}/approvals/{approval_id}",
    response_model=ApprovalRecordResponse,
)
async def get_approval(run_id: UUID, approval_id: UUID, request: Request) -> ApprovalRecordResponse:
    run = await request.app.state.qa_repository.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    record = await request.app.state.approval_port.get(str(approval_id), run_id=run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    return _approval_response(record, run_id=run_id)


@router.get(
    "/runs/{run_id}/approvals/{approval_id}",
    response_model=ApprovalRecordResponse,
)
async def get_generic_approval(
    run_id: UUID, approval_id: UUID, request: Request
) -> ApprovalRecordResponse:
    return await get_approval(run_id, approval_id, request)


@router.post(
    "/qa/runs/{run_id}/approvals/{approval_id}/decision",
    response_model=ApprovalResponse,
)
async def decide_approval(
    run_id: UUID,
    approval_id: UUID,
    body: ApprovalDecisionRequest,
    request: Request,
) -> ApprovalResponse:
    repo, _events = _state(request)
    run = await repo.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    decided = await request.app.state.approval_port.decide(
        str(approval_id),
        approved=body.approved,
        decided_by=body.decided_by,
        expires_at=body.expires_at,
    )
    if not decided:
        raise HTTPException(status_code=409, detail="Approval is missing or already decided")
    approval = await request.app.state.approval_port.get(str(approval_id), run_id=run_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    derived_id = None
    side_effects: Literal[0, 1] = 0
    if (
        body.approved
        and run.versions.skill_name == "create_review_cards"
        and run.result is not None
    ):
        content = {"text": run.result.answer.text} if run.result.answer is not None else {}
        citations = (
            tuple(str(citation.evidence_id) for citation in run.result.answer.citations)
            if run.result.answer is not None
            else ()
        )
        if citations:
            derived = await request.app.state.derived_knowledge_store.write_review_cards(
                run_id=run.run_id,
                space_id=run.space_id,
                created_by=body.decided_by,
                idempotency_key=f"{approval_id}:derived",
                content=content,
                citation_ids=citations,
            )
            derived_id = derived.id
            side_effects = 1
    return ApprovalResponse(
        approval_id=approval_id,
        run_id=run_id,
        status=approval.status,
        side_effects=side_effects,
        derived_knowledge_id=derived_id,
    )


@router.post(
    "/qa/runs/{run_id}/approvals/{approval_id}/revoke",
    response_model=ApprovalResponse,
)
async def revoke_approval(
    run_id: UUID,
    approval_id: UUID,
    body: ApprovalDecisionRequest,
    request: Request,
) -> ApprovalResponse:
    run = await request.app.state.qa_repository.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    record = await request.app.state.approval_port.get(str(approval_id), run_id=run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    if not await request.app.state.approval_port.revoke(
        str(approval_id), revoked_by=body.decided_by
    ):
        raise HTTPException(status_code=409, detail="Approval cannot be revoked")
    updated = await request.app.state.approval_port.get(str(approval_id), run_id=run_id)
    assert updated is not None
    return ApprovalResponse(approval_id=approval_id, run_id=run_id, status=updated.status)


@router.get(
    "/runs/{run_id}/approvals",
    response_model=list[ApprovalRecordResponse],
)
async def list_generic_approvals(run_id: UUID, request: Request) -> list[ApprovalRecordResponse]:
    return await list_approvals(run_id, request)


@router.post(
    "/runs/{run_id}/approvals/{approval_id}/decision",
    response_model=ApprovalResponse,
)
async def decide_generic_approval(
    run_id: UUID,
    approval_id: UUID,
    body: ApprovalDecisionRequest,
    request: Request,
) -> ApprovalResponse:
    return await decide_approval(run_id, approval_id, body, request)


@router.post(
    "/runs/{run_id}/approvals/{approval_id}/revoke",
    response_model=ApprovalResponse,
)
async def revoke_generic_approval(
    run_id: UUID,
    approval_id: UUID,
    body: ApprovalDecisionRequest,
    request: Request,
) -> ApprovalResponse:
    return await revoke_approval(run_id, approval_id, body, request)


@router.post("/qa/runs/{run_id}/resume", response_model=RunResponse, status_code=202)
async def resume_run(run_id: UUID, request: Request) -> RunResponse:
    """Requeue one non-terminal QA Run through the existing Worker/SSE path."""
    repo, events = _state(request)
    run = await repo.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status in {
        QAStatus.COMPLETED,
        QAStatus.REFUSED,
        QAStatus.FAILED,
        QAStatus.CANCELLED,
        QAStatus.TIMED_OUT,
    }:
        raise HTTPException(status_code=409, detail="Terminal Run cannot be resumed")
    recovered = await request.app.state.qa_runtime.recover_one(run_id)
    if not recovered:
        raise HTTPException(
            status_code=409, detail="Run is leased by another Worker or not recoverable"
        )
    await events.append(
        run_id, QAEventType.ACCEPTED, {"status": QAStatus.QUEUED.value, "recovered": True}
    )
    return await _run_response_with_write((await repo.get_run(run_id)) or run, request)


@router.post("/runs/{run_id}/resume", response_model=RunResponse, status_code=202)
async def resume_generic_run(run_id: UUID, request: Request) -> RunResponse:
    return await resume_run(run_id, request)


@router.post("/qa/runs/{run_id}/retry", response_model=RunResponse, status_code=202)
async def retry_run(run_id: UUID, request: Request) -> RunResponse:
    """Create a new immutable attempt while retaining the failed predecessor."""
    repo, events = _state(request)
    current = await repo.get_run(run_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if current.status not in {QAStatus.FAILED, QAStatus.TIMED_OUT}:
        raise HTTPException(status_code=409, detail="Only failed or timed-out Runs can be retried")
    retry = QARunRecord(
        run_id=current.run_id,
        attempt=QAAttempt(
            run_id=current.run_id,
            number=current.attempt.number + 1,
            previous_attempt_id=current.attempt.attempt_id,
        ),
        conversation_id=current.conversation_id,
        question_message_id=current.question_message_id,
        space_id=current.space_id,
        caller_id=current.caller_id,
        idempotency_key=f"{current.idempotency_key}:retry:{current.attempt.number + 1}",
        versions=current.versions,
        retrieval_scope=current.retrieval_scope,
    )
    try:
        created = await repo.create_run(retry)
        queued = await repo.transition_run(created.run_id, QAEvent.QUEUE)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await events.append(
        run_id,
        QAEventType.ACCEPTED,
        {"status": QAStatus.QUEUED.value, "retry": True, "attempt": queued.attempt.number},
    )
    if request.app.state.qa_execution_enabled:
        request.app.state.qa_runtime.start(run_id)
    return await _run_response_with_write(queued, request)


@router.post("/runs/{run_id}/retry", response_model=RunResponse, status_code=202)
async def retry_generic_run(run_id: UUID, request: Request) -> RunResponse:
    return await retry_run(run_id, request)


@router.get("/qa/runs/{run_id}/derived-knowledge", response_model=list[DerivedKnowledgeResponse])
async def list_derived_knowledge(run_id: UUID, request: Request) -> list[DerivedKnowledgeResponse]:
    run = await request.app.state.qa_repository.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    records = await request.app.state.derived_knowledge_store.list_for_run(run_id)
    return [_derived_response(record) for record in records]


@router.delete(
    "/qa/runs/{run_id}/derived-knowledge/{item_id}",
    response_model=DerivedKnowledgeResponse,
)
async def revoke_derived_knowledge(
    run_id: UUID,
    item_id: UUID,
    body: RevokeDerivedKnowledgeRequest,
    request: Request,
) -> DerivedKnowledgeResponse:
    run = await request.app.state.qa_repository.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    record = await request.app.state.derived_knowledge_store.revoke(
        item_id,
        run_id=run_id,
        space_id=run.space_id,
        revoked_by=body.revoked_by,
    )
    if record is None:
        raise HTTPException(status_code=404, detail="Derived knowledge not found")
    return _derived_response(record)


@router.get("/runs/{run_id}/derived-knowledge", response_model=list[DerivedKnowledgeResponse])
async def list_generic_derived_knowledge(
    run_id: UUID, request: Request
) -> list[DerivedKnowledgeResponse]:
    return await list_derived_knowledge(run_id, request)


@router.delete(
    "/runs/{run_id}/derived-knowledge/{item_id}",
    response_model=DerivedKnowledgeResponse,
)
async def revoke_generic_derived_knowledge(
    run_id: UUID,
    item_id: UUID,
    body: RevokeDerivedKnowledgeRequest,
    request: Request,
) -> DerivedKnowledgeResponse:
    return await revoke_derived_knowledge(run_id, item_id, body, request)


@router.post("/qa/runs/{run_id}/feedback", response_model=FeedbackResponse, status_code=201)
async def submit_feedback(
    run_id: UUID, body: FeedbackRequest, request: Request
) -> FeedbackResponse:
    repo, _ = _state(request)
    run = await repo.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.answer_message_id is None:
        raise HTTPException(status_code=409, detail="Run has no published answer to review")
    try:
        feedback = await repo.submit_feedback(
            FeedbackRecord(
                conversation_id=run.conversation_id,
                message_id=run.answer_message_id,
                run_id=run.run_id,
                attempt_id=run.attempt.attempt_id,
                space_id=run.space_id,
                caller_id=run.caller_id,
                idempotency_key=body.idempotency_key,
                decision=body.decision,
                note=body.note,
            )
        )
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _feedback_response(feedback)


@router.post("/runs/{run_id}/feedback", response_model=FeedbackResponse, status_code=201)
async def submit_generic_feedback(
    run_id: UUID, body: FeedbackRequest, request: Request
) -> FeedbackResponse:
    return await submit_feedback(run_id, body, request)


@router.get("/spaces/{space_id}/feedback", response_model=list[FeedbackResponse])
async def list_feedback(
    space_id: UUID,
    request: Request,
    review_status: Literal["pending_review", "accepted", "rejected"] | None = Query(default=None),
) -> list[FeedbackResponse]:
    repo, _ = _state(request)
    try:
        status = FeedbackReviewStatus(review_status) if review_status else None
        records = await repo.list_feedback(space_id, status)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return [_feedback_response(record) for record in records]


@router.get("/spaces/{space_id}/feedback/{feedback_id}", response_model=FeedbackResponse)
async def get_feedback(space_id: UUID, feedback_id: UUID, request: Request) -> FeedbackResponse:
    repo, _ = _state(request)
    feedback = await repo.get_feedback(feedback_id)
    if feedback is None or feedback.space_id != space_id:
        raise HTTPException(status_code=404, detail="Feedback not found")
    return _feedback_response(feedback)


@router.post(
    "/spaces/{space_id}/feedback/{feedback_id}/review",
    response_model=FeedbackResponse,
)
async def review_feedback(
    space_id: UUID,
    feedback_id: UUID,
    body: FeedbackReviewRequest,
    request: Request,
) -> FeedbackResponse:
    repo, _ = _state(request)
    try:
        reviewed = await repo.review_feedback(
            FeedbackReviewRecord(
                feedback_id=feedback_id,
                space_id=space_id,
                review_status=FeedbackReviewStatus(body.review_status),
                reviewer_id=body.reviewer_id,
                reviewed_at=datetime.now(UTC),
                authorization_confirmed=body.authorization_confirmed,
                redaction_complete=body.redaction_complete,
                expected_behavior=body.expected_behavior,
                approved_evidence_ids=tuple(body.approved_evidence_ids),
                gold_answer_sha256=body.gold_answer_sha256,
                rejection_reason=body.rejection_reason,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _feedback_response(reviewed)


@router.get("/qa/runs/{run_id}/events")
async def stream_events(
    run_id: UUID,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    _, events = _state(request)
    try:
        cursor = int(last_event_id or 0)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Last-Event-ID must be a sequence") from exc

    async def generate() -> AsyncIterator[str]:
        replayed = await events.replay(run_id, cursor)
        for event in replayed:
            data = json.dumps(event.as_dict(), separators=(",", ":"))
            # SSE Last-Event-ID is the replay cursor, so expose the monotonic
            # sequence rather than the opaque UUID event identity.
            yield (f"id: {event.sequence}\nevent: {event.event_type.value}\ndata: {data}\n\n")
        if not replayed:
            await asyncio.sleep(0)
            yield ": heartbeat\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/runs/{run_id}/events")
async def stream_generic_events(
    run_id: UUID,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    return await stream_events(run_id, request, last_event_id)
