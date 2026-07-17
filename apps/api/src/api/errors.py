"""Unified error schema and exception handlers for the API."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from infrastructure.telemetry_context import (
    bind_observability_context,
    current_trace_id,
    new_trace_id,
)
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from .observability import REQUEST_HEADER, TRACE_HEADER

if TYPE_CHECKING:
    from fastapi import FastAPI, Request

logger = logging.getLogger(__name__)


class ErrorResponse(BaseModel):
    """Standard error response schema published in OpenAPI."""

    code: str
    message: str
    trace_id: str
    details: dict[str, object] | None = None


class AppError(Exception):
    """Application exception that translates to a stable error response."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 500,
        details: dict[str, object] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


def _get_trace_id(request: Request) -> str:
    return str(request.scope.get("trace_id") or current_trace_id() or new_trace_id())


def _response_headers(request: Request, trace_id: str) -> dict[str, str]:
    headers = {TRACE_HEADER: trace_id}
    request_id = request.scope.get("request_id")
    if isinstance(request_id, str):
        headers[REQUEST_HEADER] = request_id
    return headers


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    trace_id = _get_trace_id(request)
    body = ErrorResponse(
        code=exc.code,
        message=exc.message,
        trace_id=trace_id,
        details=exc.details,
    ).model_dump(exclude_none=True)
    return JSONResponse(
        status_code=exc.status_code,
        content=body,
        headers=_response_headers(request, trace_id),
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    trace_id = _get_trace_id(request)
    body = ErrorResponse(
        code=f"HTTP_{exc.status_code}",
        message=str(exc.detail),
        trace_id=trace_id,
    ).model_dump(exclude_none=True)
    return JSONResponse(
        status_code=exc.status_code,
        content=body,
        headers=_response_headers(request, trace_id),
    )


async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,  # noqa: ARG001
) -> JSONResponse:
    trace_id = _get_trace_id(request)
    errors = [
        {
            "location": [str(part) for part in error["loc"]],
            "message": error["msg"],
            "type": error["type"],
        }
        for error in exc.errors()
    ]
    body = ErrorResponse(
        code="VALIDATION_ERROR",
        message="Request validation failed.",
        trace_id=trace_id,
        details={"errors": errors},
    ).model_dump(exclude_none=True)
    return JSONResponse(
        status_code=422,
        content=body,
        headers=_response_headers(request, trace_id),
    )


async def generic_error_handler(request: Request, exc: Exception) -> JSONResponse:
    trace_id = _get_trace_id(request)
    request_id = request.scope.get("request_id")
    with bind_observability_context(
        trace_id=trace_id,
        request_id=request_id if isinstance(request_id, str) else None,
    ):
        logger.error(
            "unhandled_api_exception",
            extra={"error_type": type(exc).__name__},
        )
    body = ErrorResponse(
        code="INTERNAL_ERROR",
        message="An unexpected error occurred.",
        trace_id=trace_id,
    ).model_dump(exclude_none=True)
    return JSONResponse(
        status_code=500,
        content=body,
        headers=_response_headers(request, trace_id),
    )


def register_error_handlers(app: FastAPI) -> None:
    """Register global exception handlers on a FastAPI app."""
    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, generic_error_handler)
