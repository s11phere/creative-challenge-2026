"""Opt-in, local-only full-content diagnostics for development QA runs.

The normal application logs intentionally do not contain prompts, answers, or
document excerpts. This facility is separate, disabled by default, and only
activates in a development environment when explicitly enabled.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from agent_runtime import (
    AgentToolRegistry,
    ToolDefinition,
    ToolInvocation,
    ToolInvocationResult,
    ToolRef,
)
from domain.agent_runtime import AgentRun
from model_gateway import (
    CapabilityAlias,
    ChatRequest,
    ChatResponse,
    EmbeddingRequest,
    EmbeddingResponse,
    GatewayStatus,
    ModelGateway,
    ModelGatewayError,
    RerankRequest,
    RerankResponse,
)

logger = logging.getLogger(__name__)


def _json_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, UUID):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_value(item) for item in value]
    if hasattr(value, "value"):
        return _json_value(value.value)
    return str(value)


def _error_payload(error: BaseException) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "error_type": type(error).__name__,
        "message": str(error)[:2000],
    }
    if isinstance(error, ModelGatewayError):
        payload.update(
            {
                "error_code": error.code.value,
                "retryable": error.retryable,
                "capability": error.capability.value,
            }
        )
        if error.debug_details is not None:
            payload["provider_response"] = _json_value(error.debug_details)
    cause = error.__cause__
    if cause is not None:
        payload["cause"] = {
            "error_type": type(cause).__name__,
            "message": str(cause)[:1000],
        }
    return payload


class QADebugTrace:
    """Append bounded per-run JSONL events without blocking the event loop."""

    def __init__(
        self,
        *,
        run_id: UUID,
        trace_id: str,
        enabled: bool,
        environment: str,
        path: str,
        max_bytes: int = 10_000_000,
    ) -> None:
        self.run_id = run_id
        self.trace_id = trace_id
        self.enabled = enabled and environment.lower() == "development"
        self._directory = Path(path)
        self._max_bytes = max(100_000, max_bytes)
        self._file = self._directory / f"{run_id}.jsonl"
        self._lock = threading.Lock()
        self._sequence = 0
        if self.enabled:
            try:
                self._directory.mkdir(parents=True, exist_ok=True)
                with suppress(OSError):
                    os.chmod(self._directory, 0o700)
            except OSError:
                self.enabled = False
                logger.warning("qa_debug_trace_initialization_failed", exc_info=True)

    async def record(self, event_type: str, **payload: Any) -> None:
        if not self.enabled:
            return
        event = {
            "timestamp": datetime.now(UTC).isoformat(),
            "sequence": self._sequence,
            "event": event_type,
            "run_id": str(self.run_id),
            "trace_id": self.trace_id,
            **{key: _json_value(value) for key, value in payload.items()},
        }
        self._sequence += 1
        line = json.dumps(event, ensure_ascii=False, indent=2) + "\n\n"
        await asyncio.to_thread(self._append, line)

    def _append(self, line: str) -> None:
        encoded = line.encode("utf-8")
        with self._lock:
            try:
                current_size = self._file.stat().st_size if self._file.exists() else 0
            except OSError:
                current_size = 0
            if current_size + len(encoded) > self._max_bytes:
                backup = self._file.with_suffix(".jsonl.1")
                try:
                    if backup.exists():
                        backup.unlink()
                    if self._file.exists():
                        self._file.replace(backup)
                except OSError:
                    logger.warning("qa_debug_trace_rotation_failed", exc_info=True)
            try:
                with self._file.open("ab") as handle:
                    handle.write(encoded)
                with suppress(OSError):
                    os.chmod(self._file, 0o600)
            except OSError:
                logger.warning("qa_debug_trace_write_failed", exc_info=True)

    @classmethod
    def from_settings(cls, *, run_id: UUID, trace_id: str, settings: Any) -> QADebugTrace:
        return cls(
            run_id=run_id,
            trace_id=trace_id,
            enabled=settings.qa_debug_trace_enabled,
            environment=settings.app_env,
            path=settings.qa_debug_trace_path,
            max_bytes=settings.qa_debug_trace_max_bytes,
        )


class TracingModelGateway:
    """Trace Chat traffic while delegating all model capabilities unchanged."""

    def __init__(self, delegate: ModelGateway, trace: QADebugTrace, *, phase: str) -> None:
        self._delegate = delegate
        self._trace = trace
        self._phase = phase

    @property
    def status(self) -> GatewayStatus:
        return self._delegate.status

    async def chat(
        self,
        request: ChatRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.FAST_CHAT,
    ) -> ChatResponse:
        call_id = str(uuid4())
        await self._trace.record(
            "llm_request",
            phase=self._phase,
            call_id=call_id,
            capability=capability.value,
            provider=self.status.provider.value,
            messages=[
                {"role": message.role.value, "content": message.content}
                for message in request.messages
            ],
            temperature=request.temperature,
            max_tokens=request.max_tokens,
        )
        try:
            response = await self._delegate.chat(request, capability=capability)
        except BaseException as error:
            await self._trace.record(
                "llm_error",
                phase=self._phase,
                call_id=call_id,
                capability=capability.value,
                provider=self.status.provider.value,
                error=_error_payload(error),
            )
            raise
        await self._trace.record(
            "llm_response",
            phase=self._phase,
            call_id=call_id,
            capability=capability.value,
            provider=self.status.provider.value,
            text=response.text,
            finish_reason=response.finish_reason,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            latency_ms=response.latency_ms,
        )
        return response

    async def embed(
        self,
        request: EmbeddingRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.EMBEDDING_ZH,
    ) -> EmbeddingResponse:
        return await self._delegate.embed(request, capability=capability)

    async def rerank(
        self,
        request: RerankRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.RERANKER_MULTILINGUAL,
    ) -> RerankResponse:
        return await self._delegate.rerank(request, capability=capability)

    async def aclose(self) -> None:
        await self._delegate.aclose()


class TracingToolRegistry:
    """Trace Agent Tool inputs/results while preserving the runtime registry contract."""

    def __init__(self, delegate: AgentToolRegistry, trace: QADebugTrace) -> None:
        self._delegate = delegate
        self._trace = trace

    def get(self, ref: ToolRef) -> ToolDefinition:
        return self._delegate.get(ref)

    def is_available(self, name: str, version: str) -> bool:
        return bool(self._delegate.is_available(name, version))

    async def invoke(self, run: AgentRun, invocation: ToolInvocation) -> ToolInvocationResult:
        await self._trace.record(
            "tool_call",
            tool_name=invocation.ref.name,
            tool_version=invocation.ref.version,
            arguments=invocation.arguments,
            idempotency_key=invocation.idempotency_key,
        )
        try:
            result = await self._delegate.invoke(run, invocation)
        except BaseException as error:
            await self._trace.record(
                "tool_error",
                tool_name=invocation.ref.name,
                tool_version=invocation.ref.version,
                error=_error_payload(error),
            )
            raise
        await self._trace.record(
            "tool_result",
            tool_name=invocation.ref.name,
            tool_version=invocation.ref.version,
            output=result.output,
            input_summary=result.record.input_summary,
            output_summary=result.record.output_summary,
        )
        return result


__all__ = ["QADebugTrace", "TracingModelGateway", "TracingToolRegistry"]
