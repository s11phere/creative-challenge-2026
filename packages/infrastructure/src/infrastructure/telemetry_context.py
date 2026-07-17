"""Request and task correlation context shared by logs and traces."""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, TraceState

_TRACE_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$")
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)
_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_task_id: ContextVar[str | None] = ContextVar("task_id", default=None)


def normalize_trace_id(value: str | None) -> str | None:
    """Return a canonical 32-character trace ID or reject untrusted input."""
    if value is None or len(value) > 36:
        return None
    candidate = value.replace("-", "")
    if not _TRACE_ID_PATTERN.fullmatch(candidate) or int(candidate, 16) == 0:
        return None
    return candidate.lower()


def new_trace_id() -> str:
    """Generate a non-zero W3C-compatible trace ID."""
    while (trace_id := secrets.token_hex(16)) == "0" * 32:
        pass
    return trace_id


def normalize_request_id(value: str | None) -> str:
    """Keep a bounded safe request ID or generate a replacement."""
    if value is not None and _REQUEST_ID_PATTERN.fullmatch(value):
        return value
    return str(uuid4())


def trace_parent_context(trace_id: str) -> Context:
    """Create a remote parent context so a consumer can continue a trace."""
    normalized = normalize_trace_id(trace_id)
    if normalized is None:
        raise ValueError("Invalid trace ID")
    span_id = secrets.randbits(64) or 1
    span_context = SpanContext(
        trace_id=int(normalized, 16),
        span_id=span_id,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
        trace_state=TraceState(),
    )
    return trace.set_span_in_context(NonRecordingSpan(span_context))


def current_trace_id() -> str | None:
    return _trace_id.get()


def current_request_id() -> str | None:
    return _request_id.get()


def current_task_id() -> str | None:
    return _task_id.get()


@contextmanager
def bind_observability_context(
    *,
    trace_id: str,
    request_id: str | None = None,
    task_id: str | None = None,
) -> Iterator[None]:
    """Bind correlation fields for the current async/thread execution context."""
    trace_token = _trace_id.set(trace_id)
    request_token = _request_id.set(request_id)
    task_token = _task_id.set(task_id)
    try:
        yield
    finally:
        _task_id.reset(task_token)
        _request_id.reset(request_token)
        _trace_id.reset(trace_token)
