"""Tests for the body-free diagnostic Worker task."""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

import pytest
from worker.tasks import (
    diagnostic_task,
    diagnostic_task_permanently_failed,
    process_diagnostic_task,
)


def diagnostic_payload() -> dict[str, Any]:
    return {
        "task_id": str(uuid4()),
        "trace_id": str(uuid4()),
        "event_version": 1,
        "count": 2,
        "requested_at": "2026-07-17T12:00:00+08:00",
    }


def test_diagnostic_task_is_deterministic_and_idempotent() -> None:
    payload = diagnostic_payload()

    first = process_diagnostic_task(**payload)
    second = process_diagnostic_task(**payload)

    assert first == second == payload


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("task_id", "not-a-uuid", "badly formed"),
        ("event_version", 2, "Unsupported diagnostic event version"),
        ("count", -1, "must be non-negative"),
        ("requested_at", "2026-07-17T12:00:00", "must include a timezone"),
    ],
)
def test_diagnostic_task_rejects_invalid_metadata(field: str, value: object, message: str) -> None:
    payload = diagnostic_payload()
    payload[field] = value

    with pytest.raises(ValueError, match=message):
        process_diagnostic_task(**payload)


def test_diagnostic_message_contains_metadata_only() -> None:
    payload = diagnostic_payload()

    message = diagnostic_task.message(**payload)

    assert message.queue_name == "diagnostics"
    assert message.kwargs == payload
    assert set(message.kwargs) == {
        "task_id",
        "trace_id",
        "event_version",
        "count",
        "requested_at",
    }
    assert diagnostic_task.options["max_retries"] == 3
    assert diagnostic_task.options["time_limit"] == 10_000
    assert diagnostic_task.options["notify_shutdown"] is True
    assert diagnostic_task.options["on_retry_exhausted"] == "diagnostic_task_permanently_failed"


def test_permanent_failure_log_contains_only_control_metadata(
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = diagnostic_payload()
    message_data = {"kwargs": payload, "args": []}

    with caplog.at_level(logging.ERROR):
        diagnostic_task_permanently_failed.fn(
            message_data,
            {"retries": 3, "max_retries": 3},
        )

    record = caplog.records[-1]
    assert record.message == "diagnostic_task_permanently_failed"
    assert record.task_id == payload["task_id"]
    assert record.trace_id == payload["trace_id"]
    assert record.retries == 3
