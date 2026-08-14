"""Versioned interactive Exam Preparation API."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from application.exam_preparation import ExamPreparationError
from domain.exam_preparation import ExamSession
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..errors import AppError

router = APIRouter(prefix="/api/v3")
v4_router = APIRouter(prefix="/api/v4")


class ExamSessionResponse(BaseModel):
    schema_version: str = "exam-session-v1"
    session_id: UUID
    conversation_id: UUID
    phase: str
    revision: int
    interaction: dict[str, Any]


class ExamAnswerRequest(BaseModel):
    question_id: str = Field(min_length=1, max_length=100)
    selected_options: list[str] | None = None
    response_text: str | None = Field(default=None, max_length=12000)


class ExamActionRequest(BaseModel):
    interaction_id: str = Field(min_length=1, max_length=100)
    action: str = Field(min_length=1, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=200)
    paper_id: UUID | None = None
    paper_version: int | None = Field(default=None, ge=1)
    submission_id: str | None = Field(default=None, min_length=1, max_length=200)
    answers: list[ExamAnswerRequest] = Field(default_factory=list, max_length=200)


class ExamMessageInteractionResponse(BaseModel):
    schema_version: str = "exam-message-interaction-v1"
    session_id: UUID
    anchor_run_id: UUID
    anchor_message_id: UUID
    interaction: dict[str, Any]
    submitted: bool


def _response(value: ExamSession) -> ExamSessionResponse:
    return ExamSessionResponse(
        session_id=value.session_id,
        conversation_id=value.conversation_id,
        phase=value.phase.value,
        revision=value.revision,
        interaction=value.current_interaction,
    )


@router.get(
    "/conversations/{conversation_id}/exam-sessions", response_model=list[ExamSessionResponse]
)
async def list_sessions(conversation_id: UUID, request: Request) -> list[ExamSessionResponse]:
    values = await request.app.state.exam_repository.list_for_conversation(conversation_id)
    return [_response(v) for v in values if v.owner_id == "local"]


@router.get("/exam-sessions/{session_id}", response_model=ExamSessionResponse)
async def get_session(
    session_id: UUID, request: Request, owner_id: str = "local"
) -> ExamSessionResponse:
    value = await request.app.state.exam_repository.get(session_id)
    if value is None or value.owner_id != owner_id:
        raise AppError("EXAM_SESSION_NOT_FOUND", "Exam Session not found", 404)
    return _response(value)


@router.post("/exam-sessions/{session_id}/actions", response_model=ExamSessionResponse)
async def apply_action(
    session_id: UUID, body: ExamActionRequest, request: Request
) -> ExamSessionResponse:
    try:
        value = await request.app.state.exam_service.apply_action(
            session_id,
            owner_id="local",
            action=body.action,
            idempotency_key=body.idempotency_key,
            interaction_id=body.interaction_id,
            paper_id=body.paper_id,
            paper_version=body.paper_version,
            submission_id=body.submission_id,
            answers=[a.model_dump(exclude_none=True) for a in body.answers],
        )
    except ExamPreparationError as exc:
        status = 404 if exc.code == "EXAM_SESSION_NOT_FOUND" else 409
        raise AppError(exc.code, str(exc), status) from exc
    return _response(value)


@router.get("/exam-sessions/{session_id}/history", response_model=list[ExamSessionResponse])
async def history(
    session_id: UUID, request: Request, owner_id: str = "local"
) -> list[ExamSessionResponse]:
    session = await request.app.state.exam_repository.get(session_id)
    if session is None or session.owner_id != owner_id:
        raise AppError("EXAM_SESSION_NOT_FOUND", "Exam Session not found", 404)
    actions = await request.app.state.exam_repository.list_actions(session_id)
    return [
        ExamSessionResponse(
            session_id=session.session_id,
            conversation_id=session.conversation_id,
            phase=str(action.safe_result.get("phase", session.phase.value)),
            revision=index,
            interaction={
                "interaction_id": action.safe_result.get("interaction_id"),
                "action": action.action,
                "paper_id": action.safe_result.get("paper_id"),
            },
        )
        for index, action in enumerate(actions, 1)
    ]


@v4_router.get(
    "/conversations/{conversation_id}/exam-interactions",
    response_model=list[ExamMessageInteractionResponse],
)
async def list_message_interactions(
    conversation_id: UUID, request: Request
) -> list[ExamMessageInteractionResponse]:
    sessions = await request.app.state.exam_repository.list_for_conversation(conversation_id)
    values: list[ExamMessageInteractionResponse] = []
    for session in sessions:
        if session.owner_id != "local":
            continue
        actions = await request.app.state.exam_repository.list_actions(session.session_id)
        submitted_paper_ids = {
            str(value)
            for action in actions
            if (value := action.safe_result.get("submitted_paper_id")) is not None
        }
        for action in actions:
            raw_interaction = action.safe_result.get("interaction")
            raw_run_id = action.safe_result.get("anchor_run_id")
            if not isinstance(raw_interaction, dict) or not isinstance(raw_run_id, str):
                continue
            try:
                run_id = UUID(raw_run_id)
            except ValueError:
                continue
            run = await request.app.state.conversation_run_repository.get_conversation_run(run_id)
            if (
                run is None
                or run.conversation_id != conversation_id
                or run.result is None
                or run.result.message_id is None
            ):
                continue
            values.append(
                ExamMessageInteractionResponse(
                    session_id=session.session_id,
                    anchor_run_id=run_id,
                    anchor_message_id=run.result.message_id,
                    interaction=raw_interaction,
                    submitted=(
                        isinstance((paper := raw_interaction.get("paper")), dict)
                        and str(paper.get("paper_id")) in submitted_paper_ids
                    ),
                )
            )
    return values
