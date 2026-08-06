"""Versioned HTTP skeleton for durable Assistant conversation turns."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from application.assistant import (
    AssistantTurnSubmission,
    ConversationRunApplicationError,
)
from domain.assistant_sse import AssistantEventStore, AssistantEventType
from domain.conversation_run import ConversationRun
from domain.grounded_qa import QAContractError
from domain.qa_persistence import MessageRole
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..errors import AppError, ErrorResponse

router = APIRouter(prefix="/api/v2")


class AssistantTurnRequest(BaseModel):
    content: str = Field(min_length=1, max_length=12000)
    idempotency_key: str = Field(min_length=1, max_length=200)


class SkillIdentityResponse(BaseModel):
    name: str
    version: str
    content_sha256: str


class SelectionResponse(BaseModel):
    source: str
    skill: SkillIdentityResponse | None = None


class UsageResponse(BaseModel):
    input_tokens: int
    output_tokens: int
    total_tokens: int
    model_latency_ms: float


class AssistantMessageResponse(BaseModel):
    message_id: UUID
    content: str


class ResourceCandidateResponse(BaseModel):
    candidate_id: str
    resource_type: str
    label: str
    source_label: str | None = None
    version_label: str | None = None


class ClarificationResponse(BaseModel):
    clarification_id: str
    kind: str
    message: str
    resource_candidates: list[ResourceCandidateResponse]


class ConversationRunResponse(BaseModel):
    run_id: UUID
    status: str
    run_kind: str
    selection: SelectionResponse
    assistant_message: AssistantMessageResponse | None = None
    clarification: ClarificationResponse | None = None
    usage: UsageResponse


_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "Conversation or Run was not found"},
    409: {"model": ErrorResponse, "description": "Idempotency or Run state conflict"},
}


@router.post(
    "/conversations/{conversation_id}/turns",
    response_model=ConversationRunResponse,
    status_code=202,
    responses=_ERROR_RESPONSES,
)
async def submit_turn(
    conversation_id: UUID, body: AssistantTurnRequest, request: Request
) -> ConversationRunResponse:
    """Durably accept a user turn without synchronously selecting or calling a model."""
    try:
        run = await request.app.state.assistant_turn_service.submit(
            AssistantTurnSubmission(
                conversation_id=conversation_id,
                content=body.content,
                idempotency_key=body.idempotency_key,
            )
        )
    except ConversationRunApplicationError as exc:
        raise AppError("CONVERSATION_NOT_FOUND", "Conversation not found", 404) from exc
    except QAContractError as exc:
        raise AppError(
            "CONVERSATION_RUN_CONFLICT", "Turn conflicts with persisted state", 409
        ) from exc
    await request.app.state.assistant_event_log.append(
        run.run_id,
        AssistantEventType.ACCEPTED,
        {"status": run.status.value},
    )
    if request.app.state.qa_execution_enabled:
        request.app.state.assistant_runtime.start(run.run_id)
    return await _response(run, request)


@router.get("/runs/{run_id}", response_model=ConversationRunResponse, responses=_ERROR_RESPONSES)
async def get_run(run_id: UUID, request: Request) -> ConversationRunResponse:
    run = await request.app.state.assistant_turn_service.get(run_id)
    if run is None:
        raise AppError("RUN_NOT_FOUND", "Run not found", 404)
    return await _response(run, request)


@router.post(
    "/runs/{run_id}/cancel", response_model=ConversationRunResponse, responses=_ERROR_RESPONSES
)
async def cancel_run(run_id: UUID, request: Request) -> ConversationRunResponse:
    try:
        run = await request.app.state.assistant_turn_service.cancel(run_id)
    except QAContractError as exc:
        raise AppError("RUN_NOT_FOUND", "Run not found", 404) from exc
    if request.app.state.qa_execution_enabled and run.status.value == "cancel_requested":
        request.app.state.assistant_runtime.start(run.run_id)
    return await _response(run, request)


@router.get("/runs/{run_id}/events")
async def stream_events(
    run_id: UUID,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    try:
        cursor = int(last_event_id or 0)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Last-Event-ID must be a sequence") from exc
    events: AssistantEventStore = request.app.state.assistant_event_log

    async def generate() -> AsyncIterator[str]:
        replayed = await events.replay(run_id, cursor)
        for event in replayed:
            data = json.dumps(event.as_dict(), separators=(",", ":"))
            yield f"id: {event.sequence}\nevent: {event.event_type.value}\ndata: {data}\n\n"
        if not replayed:
            await asyncio.sleep(0)
            yield ": heartbeat\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _response(run: ConversationRun, request: Request) -> ConversationRunResponse:
    assistant_message = None
    if run.result is not None and run.result.message_id is not None:
        message = await request.app.state.qa_repository.get_message(run.result.message_id)
        if message is not None and message.role is MessageRole.ASSISTANT:
            assistant_message = AssistantMessageResponse(
                message_id=message.message_id,
                content=message.content,
            )
    clarification = None
    if run.result is not None and run.result.clarification is not None:
        value = run.result.clarification
        clarification = ClarificationResponse(
            clarification_id=value.clarification_id,
            kind=value.kind.value,
            message=value.message,
            resource_candidates=[
                ResourceCandidateResponse(
                    candidate_id=candidate.candidate_id,
                    resource_type=candidate.resource_type,
                    label=candidate.label,
                    source_label=candidate.source_label,
                    version_label=candidate.version_label,
                )
                for candidate in value.resource_candidates
            ],
        )
    return ConversationRunResponse(
        run_id=run.run_id,
        status=run.status.value,
        run_kind=run.run_kind.value,
        selection=SelectionResponse(
            source=run.selection_source.value,
            skill=(
                SkillIdentityResponse(
                    name=run.skill.name,
                    version=run.skill.version,
                    content_sha256=run.skill.content_sha256,
                )
                if run.skill is not None
                else None
            ),
        ),
        assistant_message=assistant_message,
        clarification=clarification,
        usage=UsageResponse(
            input_tokens=run.usage.input_tokens,
            output_tokens=run.usage.output_tokens,
            total_tokens=run.usage.total_tokens,
            model_latency_ms=run.usage.model_latency_ms,
        ),
    )
