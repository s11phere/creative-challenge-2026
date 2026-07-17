"""ASGI request correlation and server spans."""

from __future__ import annotations

import logging
from time import perf_counter

from infrastructure.telemetry_context import (
    bind_observability_context,
    new_trace_id,
    normalize_request_id,
    normalize_trace_id,
    trace_parent_context,
)
from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

TRACE_HEADER = "X-Trace-ID"
REQUEST_HEADER = "X-Request-ID"


class TraceMiddleware:
    """Create a bounded request context and return correlation headers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.tracer = trace.get_tracer("api.http")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        carrier = {key: value for key, value in headers.items()}
        parent_context = propagate.extract(carrier)
        parent_span_context = trace.get_current_span(parent_context).get_span_context()
        requested_trace_id = normalize_trace_id(headers.get(TRACE_HEADER))
        if not parent_span_context.is_valid:
            requested_trace_id = requested_trace_id or new_trace_id()
            parent_context = trace_parent_context(requested_trace_id)

        request_id = normalize_request_id(headers.get(REQUEST_HEADER))
        method = str(scope.get("method", "UNKNOWN"))
        path = str(scope.get("path", "/"))
        status_code = 500
        started_at = perf_counter()

        async def send_with_context(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                response_headers = MutableHeaders(scope=message)
                response_headers[TRACE_HEADER] = scope["trace_id"]
                response_headers[REQUEST_HEADER] = request_id
            await send(message)

        with self.tracer.start_as_current_span(
            f"{method} {path}",
            context=parent_context,
            kind=SpanKind.SERVER,
            attributes={"http.request.method": method, "url.path": path},
        ) as span:
            span_context = span.get_span_context()
            trace_id = (
                format(span_context.trace_id, "032x")
                if span_context.is_valid
                else requested_trace_id or new_trace_id()
            )
            scope["trace_id"] = trace_id
            scope["request_id"] = request_id
            with bind_observability_context(trace_id=trace_id, request_id=request_id):
                logger.info(
                    "http_request_started",
                    extra={"method": method, "path": path},
                )
                try:
                    await self.app(scope, receive, send_with_context)
                except Exception as exc:
                    span.record_exception(exc)
                    span.set_status(Status(StatusCode.ERROR))
                    logger.exception(
                        "http_request_failed",
                        extra={"method": method, "path": path, "status_code": 500},
                    )
                    raise
                finally:
                    duration_ms = round((perf_counter() - started_at) * 1000, 3)
                    span.set_attribute("http.response.status_code", status_code)
                    if status_code >= 500:
                        span.set_status(Status(StatusCode.ERROR))
                    logger.info(
                        "http_request_completed",
                        extra={
                            "method": method,
                            "path": path,
                            "status_code": status_code,
                            "duration_ms": duration_ms,
                        },
                    )
