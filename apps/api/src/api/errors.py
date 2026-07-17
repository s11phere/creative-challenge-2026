"""Unified error schema and exception handlers for the API."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

if TYPE_CHECKING:
    from fastapi import FastAPI, Request


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


def _get_trace_id() -> str:
    return str(uuid.uuid4())


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:  # noqa: ARG001
    trace_id = _get_trace_id()
    body = ErrorResponse(
        code=exc.code,
        message=exc.message,
        trace_id=trace_id,
        details=exc.details,
    ).model_dump(exclude_none=True)
    return JSONResponse(status_code=exc.status_code, content=body)


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:  # noqa: ARG001
    trace_id = _get_trace_id()
    body = ErrorResponse(
        code=f"HTTP_{exc.status_code}",
        message=str(exc.detail),
        trace_id=trace_id,
    ).model_dump(exclude_none=True)
    return JSONResponse(status_code=exc.status_code, content=body)


async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,  # noqa: ARG001
) -> JSONResponse:
    trace_id = _get_trace_id()
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
    return JSONResponse(status_code=422, content=body)


async def generic_error_handler(request: Request, _exc: Exception) -> JSONResponse:  # noqa: ARG001
    trace_id = _get_trace_id()
    body = ErrorResponse(
        code="INTERNAL_ERROR",
        message="An unexpected error occurred.",
        trace_id=trace_id,
    ).model_dump(exclude_none=True)
    return JSONResponse(status_code=500, content=body)


def register_error_handlers(app: FastAPI) -> None:
    """Register global exception handlers on a FastAPI app."""
    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, generic_error_handler)
