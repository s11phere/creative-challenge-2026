"""Pure domain contracts for bounded Agent Runtime executions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class RunStep(StrEnum):
    PLANNING = "planning"
    RETRIEVING = "retrieving"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    WAITING_APPROVAL = "waiting_approval"


class RunEvent(StrEnum):
    START = "start"
    RETRIEVE = "retrieve"
    EXECUTE = "execute"
    VERIFY = "verify"
    WAIT_APPROVAL = "wait_approval"
    APPROVE = "approve"
    REQUEST_CANCEL = "request_cancel"
    CANCEL = "cancel"
    FAIL = "fail"
    TIMEOUT = "timeout"
    COMPLETE = "complete"


class ToolPermission(StrEnum):
    READ_KNOWLEDGE = "read_knowledge"
    WRITE_KNOWLEDGE = "write_knowledge"
    MODEL = "model"
    EXTERNAL_NETWORK = "external_network"


class RunErrorCategory(StrEnum):
    INPUT = "input"
    MANIFEST = "manifest"
    SCHEMA = "schema"
    PERMISSION = "permission"
    DEPENDENCY = "dependency"
    BUDGET = "budget"
    CANCELLATION = "cancellation"
    INTERNAL = "internal"
    REFUSAL = "refusal"


class RuntimeContractError(ValueError):
    """Base error for invalid runtime state or contract values."""


class InvalidRunTransitionError(RuntimeContractError):
    """Raised when an event is not legal for the current run status."""


class BudgetExceededError(RuntimeContractError):
    """Raised before work starts when a run budget would be exceeded."""


class RecoveryRejectedError(RuntimeContractError):
    """Raised when a checkpoint cannot safely be used for recovery."""


@dataclass(frozen=True)
class RunBudget:
    max_steps: int = 20
    max_tool_calls: int = 20
    max_input_tokens: int = 32_000
    max_output_tokens: int = 8_000
    timeout_seconds: int = 300

    def __post_init__(self) -> None:
        if any(
            value < 1
            for value in (
                self.max_steps,
                self.max_tool_calls,
                self.max_input_tokens,
                self.max_output_tokens,
                self.timeout_seconds,
            )
        ):
            raise ValueError("run budget limits must be positive")

    def allows(self, usage: BudgetUsage) -> bool:
        return (
            usage.steps <= self.max_steps
            and usage.tool_calls <= self.max_tool_calls
            and usage.input_tokens <= self.max_input_tokens
            and usage.output_tokens <= self.max_output_tokens
            and usage.elapsed_ms <= self.timeout_seconds * 1000
        )


@dataclass(frozen=True)
class BudgetUsage:
    steps: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    elapsed_ms: int = 0

    def __post_init__(self) -> None:
        if any(
            value < 0
            for value in (
                self.steps,
                self.tool_calls,
                self.input_tokens,
                self.output_tokens,
                self.elapsed_ms,
            )
        ):
            raise ValueError("budget usage cannot be negative")

    def add(
        self,
        *,
        steps: int = 0,
        tool_calls: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        elapsed_ms: int = 0,
        budget: RunBudget | None = None,
    ) -> BudgetUsage:
        delta = (steps, tool_calls, input_tokens, output_tokens, elapsed_ms)
        if any(value < 0 for value in delta):
            raise ValueError("budget increments cannot be negative")
        updated = replace(
            self,
            steps=self.steps + steps,
            tool_calls=self.tool_calls + tool_calls,
            input_tokens=self.input_tokens + input_tokens,
            output_tokens=self.output_tokens + output_tokens,
            elapsed_ms=self.elapsed_ms + elapsed_ms,
        )
        if budget is not None and not budget.allows(updated):
            raise BudgetExceededError("run budget exceeded")
        return updated


@dataclass(frozen=True)
class ToolCallRecord:
    tool_name: str
    tool_version: str
    permission: ToolPermission
    idempotency_key: str
    input_summary: str = ""
    output_summary: str = ""
    error_code: str | None = None
    retry_count: int = 0
    duration_ms: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.tool_name or not self.tool_version or not self.idempotency_key:
            raise ValueError("tool name, version, and idempotency key are required")
        if self.retry_count < 0 or self.duration_ms < 0:
            raise ValueError("tool retry count and duration cannot be negative")


@dataclass(frozen=True)
class RunError:
    code: str
    category: RunErrorCategory
    message: str
    retryable: bool = False
    safe_summary: str = ""

    def __post_init__(self) -> None:
        if not self.code or not self.message:
            raise ValueError("run error code and message are required")
        if not self.code.startswith(("SKILL_", "TOOL_", "RUN_", "AUTH_", "DEPENDENCY_")):
            raise ValueError("run error code does not use an ADR-006 prefix")


@dataclass(frozen=True)
class RunCheckpoint:
    run_id: UUID
    sequence: int
    schema_version: int
    skill_name: str
    skill_version: str
    skill_content_sha256: str
    state: Mapping[str, str] = field(default_factory=dict)
    usage: BudgetUsage = field(default_factory=BudgetUsage)
    next_step: RunStep | None = None
    verified: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.sequence < 0 or self.schema_version < 1:
            raise ValueError("checkpoint sequence and schema version are invalid")
        if not self.skill_name or not self.skill_version or not self.skill_content_sha256:
            raise ValueError("checkpoint must identify its fixed Skill")


@dataclass(frozen=True)
class AgentRunContext:
    run_id: UUID
    space_id: UUID
    skill_name: str
    skill_version: str
    skill_content_sha256: str
    trace_id: str
    caller_id: str

    def __post_init__(self) -> None:
        if not all(
            (
                self.skill_name,
                self.skill_version,
                self.skill_content_sha256,
                self.trace_id,
                self.caller_id,
            )
        ):
            raise ValueError("run context requires caller, trace, and fixed Skill identity")


@dataclass(frozen=True)
class AgentRun:
    context: AgentRunContext
    budget: RunBudget = field(default_factory=RunBudget)
    usage: BudgetUsage = field(default_factory=BudgetUsage)
    status: RunStatus = RunStatus.CREATED
    current_step: RunStep | None = None
    checkpoint_sequence: int = 0
    last_error: RunError | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        valid_state = (
            (self.status == RunStatus.CREATED and self.current_step is None)
            or (self.status == RunStatus.RUNNING and self.current_step is not None)
            or (
                self.status == RunStatus.WAITING_APPROVAL and self.current_step == RunStep.EXECUTING
            )
            or self.status == RunStatus.CANCEL_REQUESTED
            or (
                self.status
                in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.TIMED_OUT}
                and self.current_step is None
            )
        )
        if not valid_state:
            raise ValueError("run status and current step are inconsistent")
        if self.checkpoint_sequence < 0:
            raise ValueError("checkpoint sequence cannot be negative")
        if not self.budget.allows(self.usage):
            raise BudgetExceededError("initial usage exceeds run budget")

    def transition(self, event: RunEvent, *, now: datetime | None = None) -> AgentRun:
        next_status, next_step = transition_run(self.status, self.current_step, event)
        return replace(
            self, status=next_status, current_step=next_step, updated_at=now or datetime.now(UTC)
        )

    def consume(self, **increments: int) -> AgentRun:
        return replace(self, usage=self.usage.add(budget=self.budget, **increments))


_State = tuple[RunStatus, RunStep | None]
_TRANSITIONS: dict[tuple[RunStatus, RunStep | None, RunEvent], _State] = {
    (RunStatus.CREATED, None, RunEvent.START): (RunStatus.RUNNING, RunStep.PLANNING),
    (RunStatus.RUNNING, RunStep.PLANNING, RunEvent.RETRIEVE): (
        RunStatus.RUNNING,
        RunStep.RETRIEVING,
    ),
    (RunStatus.RUNNING, RunStep.RETRIEVING, RunEvent.EXECUTE): (
        RunStatus.RUNNING,
        RunStep.EXECUTING,
    ),
    (RunStatus.RUNNING, RunStep.EXECUTING, RunEvent.VERIFY): (
        RunStatus.RUNNING,
        RunStep.VERIFYING,
    ),
    (RunStatus.RUNNING, RunStep.EXECUTING, RunEvent.WAIT_APPROVAL): (
        RunStatus.WAITING_APPROVAL,
        RunStep.EXECUTING,
    ),
    (RunStatus.WAITING_APPROVAL, RunStep.EXECUTING, RunEvent.APPROVE): (
        RunStatus.RUNNING,
        RunStep.EXECUTING,
    ),
    (RunStatus.RUNNING, RunStep.VERIFYING, RunEvent.COMPLETE): (
        RunStatus.COMPLETED,
        None,
    ),
}

for _status, _step in (
    (RunStatus.CREATED, None),
    (RunStatus.RUNNING, RunStep.PLANNING),
    (RunStatus.RUNNING, RunStep.RETRIEVING),
    (RunStatus.RUNNING, RunStep.EXECUTING),
    (RunStatus.RUNNING, RunStep.VERIFYING),
    (RunStatus.WAITING_APPROVAL, RunStep.EXECUTING),
):
    _TRANSITIONS[(_status, _step, RunEvent.REQUEST_CANCEL)] = (
        RunStatus.CANCEL_REQUESTED,
        _step,
    )
    _TRANSITIONS[(_status, _step, RunEvent.FAIL)] = (RunStatus.FAILED, None)
    _TRANSITIONS[(_status, _step, RunEvent.TIMEOUT)] = (RunStatus.TIMED_OUT, None)
_TRANSITIONS[(RunStatus.CANCEL_REQUESTED, RunStep.PLANNING, RunEvent.CANCEL)] = (
    RunStatus.CANCELLED,
    None,
)
_TRANSITIONS[(RunStatus.CANCEL_REQUESTED, RunStep.RETRIEVING, RunEvent.CANCEL)] = (
    RunStatus.CANCELLED,
    None,
)
_TRANSITIONS[(RunStatus.CANCEL_REQUESTED, RunStep.EXECUTING, RunEvent.CANCEL)] = (
    RunStatus.CANCELLED,
    None,
)
_TRANSITIONS[(RunStatus.CANCEL_REQUESTED, RunStep.VERIFYING, RunEvent.CANCEL)] = (
    RunStatus.CANCELLED,
    None,
)
_TRANSITIONS[(RunStatus.CANCEL_REQUESTED, None, RunEvent.CANCEL)] = (
    RunStatus.CANCELLED,
    None,
)


def transition_run(status: RunStatus, current_step: RunStep | None, event: RunEvent) -> _State:
    """Apply one explicit lifecycle/step event; terminal states cannot be reopened."""
    try:
        return _TRANSITIONS[(status, current_step, event)]
    except KeyError as exc:
        raise InvalidRunTransitionError(
            f"event {event.value!r} is invalid for {status.value!r}/{current_step!s}"
        ) from exc


class AgentRuntime(Protocol):
    async def start(self, run: AgentRun) -> AgentRun: ...

    async def cancel(self, context: AgentRunContext) -> AgentRun: ...

    async def resume(self, run: AgentRun, checkpoint: RunCheckpoint) -> AgentRun: ...


class CheckpointStore(Protocol):
    async def save(self, checkpoint: RunCheckpoint) -> RunCheckpoint: ...

    async def get_latest(self, run_id: UUID) -> RunCheckpoint | None: ...


class ToolRegistry(Protocol):
    def is_available(self, name: str, version: str) -> bool: ...


class SkillRegistry(Protocol):
    def is_available(self, name: str, version: str, content_sha256: str) -> bool: ...


class ApprovalPort(Protocol):
    async def request(self, context: AgentRunContext, tool: ToolCallRecord) -> str: ...

    async def is_approved(self, approval_id: str, context: AgentRunContext) -> bool: ...


def validate_recovery(
    run: AgentRun, checkpoint: RunCheckpoint, *, caller_id: str, space_id: UUID
) -> None:
    """Reject recovery unless ownership, fixed Skill, schema, and terminal state are safe."""
    if run.context.caller_id != caller_id or run.context.space_id != space_id:
        raise RecoveryRejectedError("run ownership does not match recovery caller")
    if run.status in {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.TIMED_OUT,
    }:
        raise RecoveryRejectedError("terminal runs cannot be recovered")
    if not checkpoint.verified:
        raise RecoveryRejectedError("checkpoint is not verified")
    if checkpoint.run_id != run.context.run_id:
        raise RecoveryRejectedError("checkpoint belongs to another run")
    if (checkpoint.skill_name, checkpoint.skill_version, checkpoint.skill_content_sha256) != (
        run.context.skill_name,
        run.context.skill_version,
        run.context.skill_content_sha256,
    ):
        raise RecoveryRejectedError("checkpoint Skill identity does not match run")
    checkpoint_counters = (
        checkpoint.usage.steps,
        checkpoint.usage.tool_calls,
        checkpoint.usage.input_tokens,
        checkpoint.usage.output_tokens,
        checkpoint.usage.elapsed_ms,
    )
    run_counters = (
        run.usage.steps,
        run.usage.tool_calls,
        run.usage.input_tokens,
        run.usage.output_tokens,
        run.usage.elapsed_ms,
    )
    if any(
        checkpoint_value < run_value
        for checkpoint_value, run_value in zip(checkpoint_counters, run_counters, strict=True)
    ):
        raise RecoveryRejectedError("checkpoint usage is inconsistent")


__all__ = [
    "AgentRun",
    "AgentRunContext",
    "AgentRuntime",
    "ApprovalPort",
    "BudgetExceededError",
    "BudgetUsage",
    "CheckpointStore",
    "InvalidRunTransitionError",
    "RecoveryRejectedError",
    "RunBudget",
    "RunCheckpoint",
    "RunError",
    "RunErrorCategory",
    "RunEvent",
    "RunStatus",
    "RunStep",
    "SkillRegistry",
    "ToolCallRecord",
    "ToolPermission",
    "ToolRegistry",
    "transition_run",
    "validate_recovery",
]
