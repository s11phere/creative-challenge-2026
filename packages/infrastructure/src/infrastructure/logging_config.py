"""Structured application logging with centralized redaction."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from .telemetry_context import current_request_id, current_task_id, current_trace_id

REDACTED = "[REDACTED]"
_SENSITIVE_KEY = re.compile(
    r"(?i)(authorization|cookie|api[_-]?key|password|secret|prompt|"
    r"document[_-]?(body|content)|request[_-]?body)"
)
_KEY_VALUE_SECRET = re.compile(
    r"(?i)\b(authorization|cookie|api[_ -]?key|password|secret|prompt)"
    r"\s*[:=]\s*([^\r\n]+)"
)
_DATABASE_URL_SECRET = re.compile(r"(?i)(postgres(?:ql)?(?:\+asyncpg)?://[^:\s/]+:)[^@\s]+(@)")

_LOG_RECORD_FIELDS = set(logging.makeLogRecord({}).__dict__)
_SAFE_EXTRA_FIELDS = {
    "capability",
    "count",
    "dependency",
    "duration_ms",
    "error_type",
    "event_version",
    "exporter_enabled",
    "input_tokens",
    "max_retries",
    "message_id",
    "method",
    "operation",
    "output_tokens",
    "path",
    "requested_at",
    "provider",
    "retry_count",
    "retries",
    "status_code",
}


def redact_text(value: str) -> str:
    """Remove common credential forms without logging their values."""
    value = _DATABASE_URL_SECRET.sub(r"\1" + REDACTED + r"\2", value)
    return _KEY_VALUE_SECRET.sub(lambda match: f"{match.group(1)}={REDACTED}", value)


class RedactionFilter(logging.Filter):
    """Redact sensitive messages and structured fields before formatting."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage())
        record.args = ()
        for key, value in tuple(record.__dict__.items()):
            if key in _LOG_RECORD_FIELDS:
                continue
            if _SENSITIVE_KEY.search(key):
                setattr(record, key, REDACTED)
            elif isinstance(value, str):
                setattr(record, key, redact_text(value))
        return True


class JsonLogFormatter(logging.Formatter):
    """Emit a bounded JSON log schema without request or document bodies."""

    def __init__(self, *, service: str, environment: str) -> None:
        super().__init__()
        self.service = service
        self.environment = environment

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "service": self.service,
            "environment": self.environment,
            "logger": record.name,
            "event": record.getMessage(),
        }
        trace_id = getattr(record, "trace_id", None) or current_trace_id()
        request_id = getattr(record, "request_id", None) or current_request_id()
        task_id = getattr(record, "task_id", None) or current_task_id()
        if trace_id:
            payload["trace_id"] = trace_id
        if request_id:
            payload["request_id"] = request_id
        if task_id:
            payload["task_id"] = task_id
        for key in _SAFE_EXTRA_FIELDS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            payload["error_type"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def configure_logging(*, service: str, environment: str, level: str, log_format: str) -> None:
    """Install one process-wide redacting handler."""
    handler = logging.StreamHandler()
    handler.addFilter(RedactionFilter())
    if log_format.lower() == "json":
        handler.setFormatter(JsonLogFormatter(service=service, environment=environment))
    else:
        handler.setFormatter(
            logging.Formatter(
                f"%(asctime)s %(levelname)s service={service} environment={environment} "
                "%(name)s %(message)s"
            )
        )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
