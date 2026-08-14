"""Validated Tool definitions, registration, and bounded invocation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from time import monotonic
from typing import Protocol
from uuid import UUID

from domain.agent_runtime import (
    AgentRun,
    AgentRunContext,
    ApprovalPort,
    BudgetExceededError,
    ToolCallRecord,
    ToolPermission,
)
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

type JSONValue = None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]
type ToolHandler = Callable[[dict[str, JSONValue], ToolExecutionContext], Awaitable[JSONValue]]
QA_ANSWER_MARKER = "{{current_grounded_qa_answer}}"

_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_VERSION_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class ToolRegistryErrorCode(StrEnum):
    INVALID_DEFINITION = "TOOL_INVALID_DEFINITION"
    INVALID_SCHEMA = "TOOL_INVALID_SCHEMA"
    HANDLER_UNREGISTERED = "TOOL_HANDLER_UNREGISTERED"
    CAPABILITY_UNAVAILABLE = "TOOL_CAPABILITY_UNAVAILABLE"
    VERSION_CONFLICT = "TOOL_VERSION_CONFLICT"
    NOT_FOUND = "TOOL_NOT_FOUND"
    NOT_ALLOWED = "TOOL_NOT_ALLOWED"
    PERMISSION_DENIED = "TOOL_PERMISSION_DENIED"
    SPACE_MISMATCH = "TOOL_SPACE_MISMATCH"
    INPUT_INVALID = "TOOL_INPUT_INVALID"
    OUTPUT_INVALID = "TOOL_OUTPUT_INVALID"
    BUDGET_EXCEEDED = "TOOL_BUDGET_EXCEEDED"
    APPROVAL_REQUIRED = "TOOL_APPROVAL_REQUIRED"
    MODEL_OUTPUT_DENIED = "TOOL_MODEL_OUTPUT_DENIED"
    IDEMPOTENCY_CONFLICT = "TOOL_IDEMPOTENCY_CONFLICT"
    PATH_DENIED = "TOOL_PATH_DENIED"
    FILE_TOO_LARGE = "TOOL_FILE_TOO_LARGE"
    ENCODING_INVALID = "TOOL_ENCODING_INVALID"
    SOURCE_CHANGED = "TOOL_SOURCE_CHANGED"
    CANCELLED = "TOOL_CANCELLED"
    TIMEOUT = "TOOL_TIMEOUT"
    EXECUTION_FAILED = "TOOL_EXECUTION_FAILED"
    EXAM_GENERATION_TIMEOUT = "EXAM_GENERATION_TIMEOUT"
    EXAM_MODEL_STRUCTURE_INVALID = "EXAM_MODEL_STRUCTURE_INVALID"
    EXAM_SOURCE_SCOPE_REQUIRED = "EXAM_SOURCE_SCOPE_REQUIRED"
    EXAM_SESSION_CONFLICT = "EXAM_SESSION_CONFLICT"
    EXAM_PAPER_STALE = "EXAM_PAPER_STALE"


class ToolRegistryError(Exception):
    """Safe, stable Tool error suitable for Application-layer mapping."""

    def __init__(
        self,
        code: ToolRegistryErrorCode,
        message: str,
        *,
        retryable: bool = False,
        record: ToolCallRecord | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.record = record


@dataclass(frozen=True, order=True)
class ToolRef:
    name: str
    version: str


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    version: str
    description: str
    input_schema: Mapping[str, JSONValue]
    output_schema: Mapping[str, JSONValue]
    permissions: frozenset[ToolPermission]
    handler_name: str
    timeout_seconds: float = 30.0
    max_retries: int = 0
    idempotent: bool = True
    required_capabilities: frozenset[str] = field(default_factory=frozenset)
    audit_event: str = "tool_invoked"
    model_visible: bool = False
    model_observation_schema: Mapping[str, JSONValue] | None = None

    def __post_init__(self) -> None:
        if not _NAME_PATTERN.fullmatch(self.name):
            raise ToolRegistryError(
                ToolRegistryErrorCode.INVALID_DEFINITION,
                "Tool name must use lowercase snake_case.",
            )
        if not _VERSION_PATTERN.fullmatch(self.version):
            raise ToolRegistryError(
                ToolRegistryErrorCode.INVALID_DEFINITION,
                "Tool version must be a three-part semantic version.",
            )
        if not self.description or not self.handler_name or not self.audit_event:
            raise ToolRegistryError(
                ToolRegistryErrorCode.INVALID_DEFINITION,
                "Tool description, handler, and audit event are required.",
            )
        if self.timeout_seconds <= 0 or self.max_retries < 0:
            raise ToolRegistryError(
                ToolRegistryErrorCode.INVALID_DEFINITION,
                "Tool timeout must be positive and retries cannot be negative.",
            )
        if not self.permissions or any(
            not isinstance(permission, ToolPermission) for permission in self.permissions
        ):
            raise ToolRegistryError(
                ToolRegistryErrorCode.INVALID_DEFINITION,
                "Tool permissions must contain registered permission values.",
            )
        if any(not capability for capability in self.required_capabilities):
            raise ToolRegistryError(
                ToolRegistryErrorCode.INVALID_DEFINITION,
                "Tool capability aliases cannot be empty.",
            )
        if tool_requires_durable_approval(self.permissions) and not self.idempotent:
            raise ToolRegistryError(
                ToolRegistryErrorCode.INVALID_DEFINITION,
                "Side-effect Tools must declare idempotent execution.",
            )

    @property
    def ref(self) -> ToolRef:
        return ToolRef(self.name, self.version)


@dataclass(frozen=True)
class ToolExecutionContext:
    run: AgentRunContext
    idempotency_key: str


@dataclass(frozen=True)
class ToolInvocation:
    ref: ToolRef
    arguments: Mapping[str, JSONValue]
    allowed_tools: frozenset[ToolRef]
    granted_permissions: frozenset[ToolPermission]
    resource_space_id: UUID
    idempotency_key: str
    approval_id: str | None = None
    retry_count: int = 0


@dataclass(frozen=True)
class ToolInvocationResult:
    output: JSONValue
    run: AgentRun
    record: ToolCallRecord


class AgentToolRegistry(Protocol):
    """Minimal Tool registry contract shared by native Tool-use execution."""

    def is_available(self, name: str, version: str) -> bool: ...

    def get(self, ref: ToolRef) -> ToolDefinition: ...

    async def invoke(self, run: AgentRun, invocation: ToolInvocation) -> ToolInvocationResult: ...


class InMemoryToolRegistry:
    """Process-local immutable-definition registry with explicit handler allowlisting."""

    def __init__(
        self,
        *,
        handlers: Mapping[str, ToolHandler],
        available_capabilities: frozenset[str] = frozenset(),
        approval_port: ApprovalPort | None = None,
    ) -> None:
        self._handlers = dict(handlers)
        self._available_capabilities = available_capabilities
        self._approval_port = approval_port
        self._definitions: dict[ToolRef, ToolDefinition] = {}
        self._idempotent_results: dict[
            tuple[UUID, str], tuple[ToolRef, str, ToolInvocationResult]
        ] = {}
        self._idempotency_locks: dict[tuple[UUID, str], asyncio.Lock] = {}

    def register(self, definition: ToolDefinition) -> ToolDefinition:
        self._validate_schema(definition.input_schema)
        self._validate_schema(definition.output_schema)
        if definition.model_observation_schema is not None:
            self._validate_schema(definition.model_observation_schema)
            if definition.model_observation_schema.get("type") != "object":
                raise ToolRegistryError(
                    ToolRegistryErrorCode.INVALID_DEFINITION,
                    "Model observation projection must be an object schema.",
                )
        if definition.handler_name not in self._handlers:
            raise ToolRegistryError(
                ToolRegistryErrorCode.HANDLER_UNREGISTERED,
                "Tool handler is not registered by application startup.",
            )
        missing = definition.required_capabilities - self._available_capabilities
        if missing:
            raise ToolRegistryError(
                ToolRegistryErrorCode.CAPABILITY_UNAVAILABLE,
                "Tool requires an unavailable capability.",
            )
        existing = self._definitions.get(definition.ref)
        if existing is not None and existing != definition:
            raise ToolRegistryError(
                ToolRegistryErrorCode.VERSION_CONFLICT,
                "Tool name and version are already registered with another definition.",
            )
        if existing is None:
            self._definitions[definition.ref] = definition
        return self._definitions[definition.ref]

    def is_available(self, name: str, version: str) -> bool:
        return ToolRef(name, version) in self._definitions

    def get(self, ref: ToolRef) -> ToolDefinition:
        try:
            return self._definitions[ref]
        except KeyError as exc:
            raise ToolRegistryError(
                ToolRegistryErrorCode.NOT_FOUND,
                "Requested Tool version is not registered.",
            ) from exc

    async def invoke(self, run: AgentRun, invocation: ToolInvocation) -> ToolInvocationResult:
        definition = self.get(invocation.ref)
        self._validate_preconditions(run, definition, invocation)
        arguments = dict(invocation.arguments)
        self._validate_instance(
            definition.input_schema, arguments, ToolRegistryErrorCode.INPUT_INVALID
        )
        cache_key = (run.context.run_id, invocation.idempotency_key)
        fingerprint = self._digest(arguments)
        if tool_requires_durable_approval(definition.permissions):
            await self._validate_approval(
                run.context,
                invocation,
                definition,
                input_summary=fingerprint,
            )
        lock = self._idempotency_locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            cached = self._idempotent_results.get(cache_key)
            if cached is not None:
                cached_ref, cached_fingerprint, cached_result = cached
                if cached_ref != definition.ref or cached_fingerprint != fingerprint:
                    raise ToolRegistryError(
                        ToolRegistryErrorCode.IDEMPOTENCY_CONFLICT,
                        "Tool idempotency key conflicts with an earlier invocation.",
                    )
                return cached_result
            try:
                updated_run = run.consume(tool_calls=1)
            except BudgetExceededError as exc:
                raise ToolRegistryError(
                    ToolRegistryErrorCode.BUDGET_EXCEEDED,
                    f"Tool call budget is exhausted: {exc}",
                ) from exc

            started = monotonic()
            context = ToolExecutionContext(
                run=run.context, idempotency_key=invocation.idempotency_key
            )
            handler = self._handlers[definition.handler_name]
            try:
                output = await asyncio.wait_for(
                    handler(arguments, context), timeout=definition.timeout_seconds
                )
            except ToolRegistryError as exc:
                record = exc.record or self._record(
                    definition, invocation, started, error_code=exc.code
                )
                raise ToolRegistryError(
                    exc.code,
                    str(exc),
                    retryable=exc.retryable,
                    record=record,
                ) from exc
            except TimeoutError as exc:
                record = self._record(
                    definition, invocation, started, error_code=ToolRegistryErrorCode.TIMEOUT
                )
                raise ToolRegistryError(
                    ToolRegistryErrorCode.TIMEOUT,
                    "Tool execution timed out.",
                    retryable=invocation.retry_count < definition.max_retries,
                    record=record,
                ) from exc
            except Exception as exc:
                record = self._record(
                    definition,
                    invocation,
                    started,
                    error_code=ToolRegistryErrorCode.EXECUTION_FAILED,
                )
                raise ToolRegistryError(
                    ToolRegistryErrorCode.EXECUTION_FAILED,
                    "Tool execution failed.",
                    retryable=invocation.retry_count < definition.max_retries,
                    record=record,
                ) from exc

            try:
                self._validate_instance(
                    definition.output_schema, output, ToolRegistryErrorCode.OUTPUT_INVALID
                )
            except ToolRegistryError as exc:
                record = self._record(
                    definition,
                    invocation,
                    started,
                    output=output,
                    error_code=ToolRegistryErrorCode.OUTPUT_INVALID,
                )
                raise ToolRegistryError(
                    ToolRegistryErrorCode.OUTPUT_INVALID,
                    str(exc),
                    record=record,
                ) from exc
            record = self._record(definition, invocation, started, output=output)
            result = ToolInvocationResult(output=output, run=updated_run, record=record)
            if definition.idempotent:
                self._idempotent_results[cache_key] = (definition.ref, fingerprint, result)
            return result

    def _validate_preconditions(
        self, run: AgentRun, definition: ToolDefinition, invocation: ToolInvocation
    ) -> None:
        if invocation.ref not in invocation.allowed_tools:
            raise ToolRegistryError(
                ToolRegistryErrorCode.NOT_ALLOWED,
                "Tool is not declared by the fixed Skill.",
            )
        if not definition.permissions.issubset(invocation.granted_permissions):
            raise ToolRegistryError(
                ToolRegistryErrorCode.PERMISSION_DENIED,
                "Run does not have all Tool permissions.",
            )
        if invocation.resource_space_id != run.context.space_id:
            raise ToolRegistryError(
                ToolRegistryErrorCode.SPACE_MISMATCH,
                "Tool resource does not belong to the run Space.",
            )
        if not invocation.idempotency_key:
            raise ToolRegistryError(
                ToolRegistryErrorCode.INVALID_DEFINITION,
                "Tool invocation requires an idempotency key.",
            )
        if invocation.retry_count < 0 or invocation.retry_count > definition.max_retries:
            raise ToolRegistryError(
                ToolRegistryErrorCode.INVALID_DEFINITION,
                "Tool retry count is outside its declared policy.",
            )

    async def _validate_approval(
        self,
        context: AgentRunContext,
        invocation: ToolInvocation,
        definition: ToolDefinition,
        *,
        input_summary: str,
    ) -> None:
        approval_id = invocation.approval_id
        if self._approval_port is None:
            raise ToolRegistryError(
                ToolRegistryErrorCode.APPROVAL_REQUIRED,
                "Side-effect Tool requires a durable approval.",
            )
        if approval_id is None:
            checker = getattr(self._approval_port, "is_always_allowed", None)
            if checker is not None and await checker(
                context, tool_name=definition.name, tool_version=definition.version
            ):
                return
            raise ToolRegistryError(
                ToolRegistryErrorCode.APPROVAL_REQUIRED,
                "Side-effect Tool requires a durable approval.",
            )
        validator = getattr(self._approval_port, "is_approved_for_invocation", None)
        if validator is not None:
            approved = await validator(
                approval_id,
                context,
                tool_name=definition.name,
                tool_version=definition.version,
                idempotency_key=invocation.idempotency_key,
                input_summary=input_summary,
            )
        else:
            validator = getattr(self._approval_port, "is_approved_for_tool", None)
            if validator is not None:
                approved = await validator(
                    approval_id,
                    context,
                    tool_name=definition.name,
                    tool_version=definition.version,
                )
            else:
                approved = await self._approval_port.is_approved(approval_id, context)
        if not approved:
            raise ToolRegistryError(
                ToolRegistryErrorCode.APPROVAL_REQUIRED,
                "Side-effect Tool approval is invalid or expired.",
            )

    @classmethod
    def _validate_schema(cls, schema: Mapping[str, JSONValue]) -> None:
        if cls._contains_ref(schema):
            raise ToolRegistryError(
                ToolRegistryErrorCode.INVALID_SCHEMA,
                "Tool schemas must be self-contained and cannot use references.",
            )
        try:
            Draft202012Validator.check_schema(dict(schema))
        except SchemaError as exc:
            raise ToolRegistryError(
                ToolRegistryErrorCode.INVALID_SCHEMA,
                "Tool JSON Schema is invalid.",
            ) from exc

    @classmethod
    def _contains_ref(cls, value: JSONValue | Mapping[str, JSONValue]) -> bool:
        if isinstance(value, Mapping):
            return "$ref" in value or any(cls._contains_ref(item) for item in value.values())
        if isinstance(value, list):
            return any(cls._contains_ref(item) for item in value)
        return False

    @staticmethod
    def _validate_instance(
        schema: Mapping[str, JSONValue],
        instance: JSONValue | Mapping[str, JSONValue],
        code: ToolRegistryErrorCode,
    ) -> None:
        error = next(Draft202012Validator(dict(schema)).iter_errors(instance), None)
        if error is not None:
            location = "/".join(str(part) for part in error.absolute_path) or "root"
            raise ToolRegistryError(code, f"Tool schema validation failed at {location}.")

    @classmethod
    def _record(
        cls,
        definition: ToolDefinition,
        invocation: ToolInvocation,
        started: float,
        *,
        output: JSONValue | None = None,
        error_code: ToolRegistryErrorCode | None = None,
    ) -> ToolCallRecord:
        return ToolCallRecord(
            tool_name=definition.name,
            tool_version=definition.version,
            permissions=definition.permissions,
            idempotency_key=invocation.idempotency_key,
            input_summary=cls._digest(invocation.arguments),
            output_summary=cls._digest(output) if output is not None else "",
            error_code=error_code.value if error_code is not None else None,
            retry_count=invocation.retry_count,
            duration_ms=max(0, int((monotonic() - started) * 1000)),
            display_summary="",
        )

    @staticmethod
    def _digest(value: JSONValue | Mapping[str, JSONValue]) -> str:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def tool_requires_durable_approval(permissions: frozenset[ToolPermission]) -> bool:
    """Return whether a Tool can mutate state or start an external process."""
    return bool(
        permissions & frozenset({ToolPermission.WRITE_KNOWLEDGE, ToolPermission.EXECUTE_PROCESS})
    )


def tool_input_summary(value: JSONValue | Mapping[str, JSONValue]) -> str:
    """Return the exact digest bound into a durable Tool approval."""
    return InMemoryToolRegistry._digest(value)


__all__ = [
    "AgentToolRegistry",
    "InMemoryToolRegistry",
    "JSONValue",
    "QA_ANSWER_MARKER",
    "ToolDefinition",
    "ToolExecutionContext",
    "ToolHandler",
    "ToolInvocation",
    "ToolInvocationResult",
    "ToolRef",
    "ToolRegistryError",
    "ToolRegistryErrorCode",
    "tool_requires_durable_approval",
    "tool_input_summary",
]
