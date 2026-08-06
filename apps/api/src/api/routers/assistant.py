"""Versioned HTTP skeleton for durable Assistant conversation turns."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from application.assistant import (
    AssistantCommandKind,
    AssistantTurnSubmission,
    CommandCatalogError,
    CommandDescriptor,
    CommandExecutionResult,
    CommandParseError,
    ConversationRunApplicationError,
)
from domain.assistant_sse import AssistantEventStore, AssistantEventType
from domain.conversation_run import ConversationRun, ConversationRunKind, ConversationRunStatus
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
    command: str | None = Field(default=None, min_length=1, max_length=32)


class AssistantCommandResponse(BaseModel):
    name: str
    aliases: list[str]
    kind: str
    description: str
    argument_hint: str
    input_mode: str


class CommandCatalogResponse(BaseModel):
    schema_version: str = "assistant-command-catalog-v1"
    commands: list[AssistantCommandResponse]


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


class ClarificationSelectionRequest(BaseModel):
    candidate_id: str = Field(min_length=10, max_length=200)


class ConversationRunResponse(BaseModel):
    run_id: UUID
    user_message_id: UUID
    status: str
    run_kind: str
    error_code: str | None = None
    selection: SelectionResponse
    model_identity: str
    assistant_message: AssistantMessageResponse | None = None
    clarification: ClarificationResponse | None = None
    usage: UsageResponse


class ConversationRunsResponse(BaseModel):
    runs: list[ConversationRunResponse]


class CommandExecutionResponse(BaseModel):
    command: str
    status: str
    content: str | None = None
    conversation_id: UUID | None = None
    run: ConversationRunResponse | None = None
    commands: list[AssistantCommandResponse] = Field(default_factory=list)


_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "Command could not be parsed"},
    404: {"model": ErrorResponse, "description": "Conversation or Run was not found"},
    409: {"model": ErrorResponse, "description": "Idempotency or Run state conflict"},
}


@router.post(
    "/conversations/{conversation_id}/turns",
    response_model=ConversationRunResponse | CommandExecutionResponse,
    status_code=202,
    responses=_ERROR_RESPONSES,
)
async def submit_turn(
    conversation_id: UUID, body: AssistantTurnRequest, request: Request
) -> ConversationRunResponse | CommandExecutionResponse:
    """Durably accept a user turn without synchronously selecting or calling a model."""
    commands = request.app.state.assistant_command_service
    try:
        parsed = commands.parser.parse(body.content, declared_command=body.command)
        if parsed.descriptor is not None:
            if parsed.descriptor.kind is AssistantCommandKind.SKILL:
                executed = await commands.invoke_skill(
                    conversation_id, parsed, idempotency_key=body.idempotency_key
                )
                assert executed.run is not None
                await _publish_command_events(executed.run, request)
                await _schedule_context_compaction(executed.run, request)
                return await _command_response(executed, request)
            if parsed.descriptor.name == "help":
                return await _command_response(await commands.help(), request)
            if parsed.descriptor.name == "skills":
                return await _command_response(await commands.skills(), request)
            if parsed.descriptor.name == "new":
                return await _command_response(
                    await commands.new_conversation(conversation_id), request
                )
            if parsed.descriptor.name == "compact":
                executed = await commands.compact(
                    conversation_id, content=body.content, idempotency_key=body.idempotency_key
                )
                assert executed.run is not None
                await _publish_command_events(executed.run, request)
                if request.app.state.qa_execution_enabled:
                    request.app.state.assistant_runtime.start(executed.run.run_id)
                return await _command_response(executed, request)
            if parsed.descriptor.name == "stop":
                return await _command_response(await commands.stop(conversation_id), request)
            raise CommandParseError("RUN_COMMAND_UNKNOWN", "Unknown Assistant command.")
        run = await request.app.state.assistant_turn_service.submit(
            AssistantTurnSubmission(
                conversation_id=conversation_id,
                content=parsed.content,
                idempotency_key=body.idempotency_key,
            )
        )
    except CommandParseError as exc:
        raise AppError(
            exc.code,
            "Command could not be completed.",
            400,
            details={"candidates": [candidate.public_dict() for candidate in exc.candidates]},
        ) from exc
    except CommandCatalogError as exc:
        raise AppError(
            "RUN_AGENT_DECISION_INVALID", "Command catalog is unavailable.", 409
        ) from exc
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
    await _schedule_context_compaction(run, request)
    return await _response(run, request)


@router.get("/commands", response_model=CommandCatalogResponse)
async def list_commands(request: Request) -> CommandCatalogResponse:
    commands = request.app.state.assistant_command_service.catalog.list()
    return CommandCatalogResponse(
        commands=[_command_descriptor_response(item) for item in commands]
    )


@router.get(
    "/conversations/{conversation_id}/runs",
    response_model=ConversationRunsResponse,
    responses=_ERROR_RESPONSES,
)
async def list_conversation_runs(
    conversation_id: UUID, request: Request
) -> ConversationRunsResponse:
    conversation = await request.app.state.qa_repository.get_conversation(conversation_id)
    if conversation is None or conversation.archived_at is not None:
        raise AppError("CONVERSATION_NOT_FOUND", "Conversation not found", 404)
    runs = await request.app.state.conversation_run_repository.list_conversation_runs(
        conversation_id
    )
    return ConversationRunsResponse(runs=[await _response(run, request) for run in runs])


@router.get("/runs/{run_id}", response_model=ConversationRunResponse, responses=_ERROR_RESPONSES)
async def get_run(run_id: UUID, request: Request) -> ConversationRunResponse:
    run = await request.app.state.assistant_turn_service.get(run_id)
    if run is None:
        raise AppError("RUN_NOT_FOUND", "Run not found", 404)
    return await _response(run, request)


@router.post(
    "/runs/{run_id}/clarifications/{clarification_id}",
    response_model=ConversationRunResponse,
    status_code=202,
    responses=_ERROR_RESPONSES,
)
async def select_clarification_resource(
    run_id: UUID,
    clarification_id: str,
    body: ClarificationSelectionRequest,
    request: Request,
) -> ConversationRunResponse:
    run = await request.app.state.assistant_turn_service.get(run_id)
    if run is None:
        raise AppError("RUN_NOT_FOUND", "Run not found", 404)
    try:
        resumed = await request.app.state.assistant_skill_invoker.resume_resource_clarification(
            run,
            clarification_id=clarification_id,
            candidate_id=body.candidate_id,
            context_service=request.app.state.conversation_context_service,
        )
    except ValueError as exc:
        code = str(exc)
        if code not in {
            "RUN_CLARIFICATION_INVALID",
            "SKILL_NOT_ACTIVE",
            "RESOURCE_NOT_FOUND",
            "RESOURCE_CONFLICT",
        }:
            code = "RUN_CLARIFICATION_INVALID"
        raise AppError(code, "Clarification choice could not be completed.", 409) from exc
    await request.app.state.assistant_event_log.append(
        resumed.run_id,
        AssistantEventType.SKILL_STARTED,
        {
            "status": resumed.status.value,
            "skill": resumed.skill.name if resumed.skill else "unknown",
        },
    )
    return await _response(resumed, request)


@router.post(
    "/runs/{run_id}/cancel", response_model=ConversationRunResponse, responses=_ERROR_RESPONSES
)
async def cancel_run(run_id: UUID, request: Request) -> ConversationRunResponse:
    try:
        existing = await request.app.state.assistant_turn_service.get(run_id)
        if existing is not None and existing.run_kind.value in {"skill", "grounded_qa"}:
            await request.app.state.qa_repository.request_cancel(run_id)
            run = await request.app.state.assistant_turn_service.get(run_id)
            if run is None:
                raise QAContractError("ConversationRun does not exist")
        else:
            run = await request.app.state.assistant_turn_service.cancel(run_id)
    except QAContractError as exc:
        raise AppError("RUN_NOT_FOUND", "Run not found", 404) from exc
    if request.app.state.qa_execution_enabled and run.status.value == "cancel_requested":
        if run.run_kind.value in {"skill", "grounded_qa"}:
            request.app.state.qa_runtime.start(run.run_id)
        else:
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
        user_message_id=run.user_message_id,
        status=run.status.value,
        run_kind=run.run_kind.value,
        error_code=run.error_code,
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
        model_identity=run.model_identity,
        assistant_message=assistant_message,
        clarification=clarification,
        usage=UsageResponse(
            input_tokens=run.usage.input_tokens,
            output_tokens=run.usage.output_tokens,
            total_tokens=run.usage.total_tokens,
            model_latency_ms=run.usage.model_latency_ms,
        ),
    )


async def _command_response(
    result: CommandExecutionResult, request: Request
) -> CommandExecutionResponse:
    return CommandExecutionResponse(
        command=result.command,
        status=result.status,
        content=result.content,
        conversation_id=result.conversation_id,
        run=await _response(result.run, request) if result.run is not None else None,
        commands=[_command_descriptor_response(item) for item in result.commands],
    )


def _command_descriptor_response(item: CommandDescriptor) -> AssistantCommandResponse:
    return AssistantCommandResponse(
        name=item.name,
        aliases=list(item.aliases),
        kind=item.kind.value,
        description=item.description,
        argument_hint=item.argument_hint,
        input_mode=item.input_mode,
    )


async def _publish_command_events(run: ConversationRun, request: Request) -> None:
    events: AssistantEventStore = request.app.state.assistant_event_log
    published = {event.event_type for event in await events.replay(run.run_id)}
    if AssistantEventType.ACCEPTED not in published:
        await events.append(
            run.run_id,
            AssistantEventType.ACCEPTED,
            {"status": run.status.value, "selection_source": "command"},
        )
    if (
        run.run_kind in {ConversationRunKind.GROUNDED_QA, ConversationRunKind.SKILL}
        and run.status is not ConversationRunStatus.WAITING_CLARIFICATION
        and AssistantEventType.SKILL_STARTED not in published
    ):
        await events.append(
            run.run_id,
            AssistantEventType.SKILL_STARTED,
            {"status": run.status.value, "skill": run.skill.name if run.skill else "unknown"},
        )
    if (
        run.status is ConversationRunStatus.WAITING_CLARIFICATION
        and AssistantEventType.CLARIFICATION not in published
    ):
        await events.append(
            run.run_id,
            AssistantEventType.CLARIFICATION,
            {"status": run.status.value, "action": "clarify"},
        )


async def _schedule_context_compaction(run: ConversationRun, request: Request) -> None:
    scheduled = await request.app.state.conversation_context_service.schedule_automatic(run)
    if scheduled is None:
        return
    await request.app.state.assistant_event_log.append(
        scheduled.run_id,
        AssistantEventType.ACCEPTED,
        {"status": scheduled.status.value, "selection_source": "none"},
    )
    if request.app.state.qa_execution_enabled:
        request.app.state.assistant_runtime.start(scheduled.run_id)
