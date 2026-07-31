"""Provisional Grounded QA HTTP/SSE surface backed by PostgreSQL and Worker execution."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Literal
from uuid import UUID, uuid4

from application.skills import OrganizationScopeError
from domain.grounded_qa import CitationStatus, QAAttempt, QAEvent, QAStatus, normalize_question
from domain.qa_persistence import (
    ConversationRecord,
    FeedbackDecision,
    FeedbackRecord,
    GroundedQARepository,
    MessageRecord,
    MessageRole,
    QARetrievalScope,
    QARunRecord,
)
from domain.qa_sse import QAEventStore, QAEventType
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from ..errors import AppError

router = APIRouter(prefix="/api/v1")


class ConversationCreate(BaseModel):
    owner_id: str = Field(default="local", min_length=1, max_length=128)


class ConversationResponse(BaseModel):
    conversation_id: UUID
    space_id: UUID
    owner_id: str


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=12000)
    idempotency_key: str = Field(min_length=1, max_length=200)


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
    status: Literal["blocked"]
    code: Literal["SKILL_WRITE_PORT_UNAVAILABLE"]
    side_effects: Literal[0]


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


class FeedbackRequest(BaseModel):
    decision: FeedbackDecision
    idempotency_key: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, min_length=1, max_length=2000)


class FeedbackResponse(BaseModel):
    feedback_id: UUID
    run_id: UUID
    message_id: UUID
    review_status: str


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
                code="SKILL_WRITE_PORT_UNAVAILABLE",
                side_effects=0,
            )
            if run.versions.skill_name == "create_review_cards"
            else None
        ),
        result=_result_payload(run),
        citations=_citation_payloads(run),
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
        conversation_id=record.conversation_id, space_id=record.space_id, owner_id=record.owner_id
    )


@router.post(
    "/conversations/{conversation_id}/questions", response_model=RunResponse, status_code=202
)
async def submit_question(
    conversation_id: UUID, body: QuestionRequest, request: Request
) -> RunResponse:
    repo, events = _state(request)
    conversation = await repo.get_conversation(conversation_id)
    if conversation is None:
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
    if conversation is None:
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
    return _run_response(run)


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


@router.post("/qa/runs/{run_id}/cancel", response_model=RunResponse)
async def cancel_run(run_id: UUID, request: Request) -> RunResponse:
    repo, events = _state(request)
    try:
        run = await repo.request_cancel(run_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc
    await events.append(run_id, QAEventType.CANCEL_REQUESTED, {"status": run.status.value})
    return _run_response(run)


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
    return FeedbackResponse(
        feedback_id=feedback.feedback_id,
        run_id=feedback.run_id,
        message_id=feedback.message_id,
        review_status=feedback.review_status.value,
    )


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
            yield (f"id: {event.event_id}\nevent: {event.event_type.value}\ndata: {data}\n\n")
        if not replayed:
            await asyncio.sleep(0)
            yield ": heartbeat\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
