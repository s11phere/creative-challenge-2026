"""Provisional Grounded QA HTTP and SSE surface (no database/worker claim)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

from application.qa.persistence import InMemoryGroundedQARepository
from domain.grounded_qa import QAAttempt, QAEvent, QAStatus, normalize_question
from domain.qa_persistence import (
    ConversationRecord,
    FeedbackDecision,
    FeedbackRecord,
    MessageRecord,
    MessageRole,
    QARunRecord,
    QARunVersions,
)
from domain.qa_sse import QAEventLog, QAEventType
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

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


class RunResponse(BaseModel):
    run_id: UUID
    attempt_id: UUID
    status: str
    conversation_id: UUID
    question_message_id: UUID
    cancellation_requested: bool
    error_code: str | None = None


class FeedbackRequest(BaseModel):
    decision: FeedbackDecision
    idempotency_key: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, min_length=1, max_length=2000)


class FeedbackResponse(BaseModel):
    feedback_id: UUID
    run_id: UUID
    message_id: UUID
    review_status: str


def _state(request: Request) -> tuple[InMemoryGroundedQARepository, QAEventLog]:
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
    )


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
            versions=QARunVersions(
                "provisional",
                "qa-profile-v1",
                "retrieval-v1",
                "fake-fast-chat-v1",
                "prompt-v1",
                "grounded-answer-v1",
                "provisional",
                "provisional",
            ),
        )
        run = await repo.create_run(run)
        if run.status is QAStatus.CREATED:
            run = await repo.transition_run(run.run_id, QAEvent.QUEUE)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    events.append(run.run_id, QAEventType.ACCEPTED, {"status": QAStatus.QUEUED.value})
    return _run_response(run)


@router.get("/qa/runs/{run_id}", response_model=RunResponse)
async def get_run(run_id: UUID, request: Request) -> RunResponse:
    repo, _ = _state(request)
    run = await repo.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return _run_response(run)


@router.post("/qa/runs/{run_id}/cancel", response_model=RunResponse)
async def cancel_run(run_id: UUID, request: Request) -> RunResponse:
    repo, events = _state(request)
    try:
        run = await repo.request_cancel(run_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc
    events.append(run_id, QAEventType.CANCEL_REQUESTED, {"status": run.status.value})
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
        for event in events.replay(run_id, cursor):
            data = json.dumps(event.as_dict(), separators=(",", ":"))
            yield (f"id: {event.event_id}\nevent: {event.event_type.value}\ndata: {data}\n\n")
        if not events.replay(run_id, cursor):
            await asyncio.sleep(0)
            yield ": heartbeat\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
