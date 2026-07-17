"""Tests for trace context, JSON logging, and redaction."""

from __future__ import annotations

import json
import logging
from uuid import uuid4

from infrastructure.logging_config import REDACTED, JsonLogFormatter, RedactionFilter
from infrastructure.telemetry_context import (
    bind_observability_context,
    normalize_request_id,
    normalize_trace_id,
    trace_parent_context,
)
from opentelemetry.sdk.trace import TracerProvider


def test_trace_id_normalization_rejects_untrusted_values() -> None:
    trace_id = uuid4()

    assert normalize_trace_id(str(trace_id)) == trace_id.hex
    assert normalize_trace_id(trace_id.hex) == trace_id.hex
    assert normalize_trace_id("0" * 32) is None
    assert normalize_trace_id("x" * 32) is None
    assert normalize_trace_id("a" * 10_000) is None


def test_request_id_accepts_only_bounded_safe_characters() -> None:
    assert normalize_request_id("request-123") == "request-123"
    assert normalize_request_id("unsafe value") != "unsafe value"
    assert normalize_request_id("x" * 65) != "x" * 65


def test_remote_parent_continues_the_requested_trace() -> None:
    trace_id = uuid4().hex
    tracer = TracerProvider().get_tracer("test")

    with tracer.start_as_current_span("consumer", context=trace_parent_context(trace_id)) as span:
        actual_trace_id = format(span.get_span_context().trace_id, "032x")

    assert actual_trace_id == trace_id


def test_json_log_contains_context_and_redacts_sensitive_values() -> None:
    trace_id = uuid4().hex
    task_id = str(uuid4())
    record = logging.LogRecord(
        name="test.observability",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=(
            "Authorization: Bearer private-token\n"
            "postgresql+asyncpg://app:private-password@localhost/db"
        ),
        args=(),
        exc_info=None,
    )
    record.prompt = "private prompt body"
    record.document_body = "private document body"
    record.status_code = 200

    with bind_observability_context(
        trace_id=trace_id,
        request_id="request-123",
        task_id=task_id,
    ):
        assert RedactionFilter().filter(record) is True
        payload = json.loads(JsonLogFormatter(service="api", environment="test").format(record))

    serialized = json.dumps(payload)
    assert payload["service"] == "api"
    assert payload["environment"] == "test"
    assert payload["trace_id"] == trace_id
    assert payload["request_id"] == "request-123"
    assert payload["task_id"] == task_id
    assert payload["status_code"] == 200
    assert REDACTED in payload["event"]
    assert "private-token" not in serialized
    assert "private-password" not in serialized
    assert "private prompt body" not in serialized
    assert "private document body" not in serialized
