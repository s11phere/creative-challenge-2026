"""Read-only HTTP and SSE projections for generic Agent Loop v3 history."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from uuid import UUID

from domain.agent_sse import (
    AgentRunEventContractError,
    AgentRunEventPage,
    AgentRunEventStore,
    AgentRunEventVersionError,
)
from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..errors import AppError, ErrorResponse

router = APIRouter(prefix="/api/v3")


class AgentRunEventResponse(BaseModel):
    schema_version: str
    event_id: UUID
    run_id: UUID
    sequence: int
    occurred_at: datetime
    event_type: str
    payload: dict[str, Any]


class AgentRunEventHistoryResponse(BaseModel):
    schema_version: str = "agent-run-event-page-v1"
    events: list[AgentRunEventResponse]
    next_sequence: int | None
    has_more: bool


_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "Run was not found"},
    409: {"model": ErrorResponse, "description": "Event history cannot be safely interpreted"},
}


@router.get(
    "/runs/{run_id}/events",
    response_model=AgentRunEventHistoryResponse,
    responses=_ERROR_RESPONSES,
)
async def list_agent_events(
    run_id: UUID,
    request: Request,
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
) -> AgentRunEventHistoryResponse:
    page = await _page(request, run_id, after_sequence=after_sequence, limit=limit)
    return _response(page)


@router.get("/runs/{run_id}/events/stream", responses=_ERROR_RESPONSES)
async def stream_agent_events(
    run_id: UUID,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    limit: int = Query(default=200, ge=1, le=200),
) -> StreamingResponse:
    try:
        cursor = int(last_event_id or 0)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Last-Event-ID must be a sequence") from exc
    if cursor < 0:
        raise HTTPException(status_code=400, detail="Last-Event-ID must be a sequence")
    page = await _page(request, run_id, after_sequence=cursor, limit=limit)

    async def generate() -> AsyncIterator[str]:
        for event in page.events:
            data = json.dumps(event.as_dict(), separators=(",", ":"))
            yield f"id: {event.sequence}\nevent: {event.event_type.value}\ndata: {data}\n\n"
        if not page.events:
            await asyncio.sleep(0)
            yield ": heartbeat\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Agent-Event-Has-More": str(page.has_more).lower(),
        },
    )


async def _page(
    request: Request, run_id: UUID, *, after_sequence: int, limit: int
) -> AgentRunEventPage:
    run = await request.app.state.assistant_turn_service.get(run_id)
    if run is None:
        raise AppError("RUN_NOT_FOUND", "Run not found", 404)
    events: AgentRunEventStore = request.app.state.agent_event_log
    try:
        return await events.page(run_id, after_sequence=after_sequence, limit=limit)
    except AgentRunEventVersionError as exc:
        raise AppError(
            "AGENT_EVENT_VERSION_UNSUPPORTED",
            "Agent event history uses an unsupported version.",
            409,
        ) from exc
    except AgentRunEventContractError as exc:
        raise AppError(
            "AGENT_EVENT_HISTORY_INVALID",
            "Agent event history cannot be safely interpreted.",
            409,
        ) from exc


def _response(page: AgentRunEventPage) -> AgentRunEventHistoryResponse:
    return AgentRunEventHistoryResponse(
        events=[AgentRunEventResponse(**event.as_dict()) for event in page.events],
        next_sequence=page.next_sequence,
        has_more=page.has_more,
    )
